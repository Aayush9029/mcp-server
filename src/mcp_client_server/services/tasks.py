import base64
import json
import math
from collections.abc import Collection
from dataclasses import dataclass
from functools import cached_property
from typing import assert_never

from mcp_client_server import limits
from mcp_client_server.clock import Clock
from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Principal
from mcp_client_server.errors import Conflict, LimitReached, NotFound, RateLimited, ValidationFailed
from mcp_client_server.events import frames
from mcp_client_server.ids import IdSource, KeyId, TaskId
from mcp_client_server.limits import Limits
from mcp_client_server.models import (
    OPEN_STATUSES,
    LiveActivityInput,
    LiveActivityRemoval,
    NotifyInput,
    NotifyResult,
    PatchField,
    Priority,
    Status,
    Task,
    TaskCreate,
    TaskPatch,
)
from mcp_client_server.push import PushDispatcher, TaskChange
from mcp_client_server.services.questions import QuestionService
from mcp_client_server.services.scope import own_scope
from mcp_client_server.store import AccountTasks, Cursor, KeyTasks, Store, StoredTask, TaskScope

_WRITE_ATTEMPTS = 8
_HOUR = 3600
_NO_CHANGE: frozenset[PatchField] = frozenset()


@dataclass(frozen=True)
class TaskPage:
    """One page of tasks, as the stored JSON. The REST list sends `docs` on and does not parse them."""

    docs: list[str]
    next_cursor: str | None = None

    @cached_property
    def tasks(self) -> list[Task]:
        return [Task.model_validate_json(doc) for doc in self.docs]

    def to_json(self) -> bytes:
        """The JSON of `TaskList`. The store keeps each task as the JSON of `Task`, so no task is parsed here."""
        return f'{{"tasks":[{",".join(self.docs)}],"next_cursor":{json.dumps(self.next_cursor)}}}'.encode()


def encode_cursor(cursor: Cursor) -> str:
    return base64.urlsafe_b64encode(f"{cursor.score!r}:{cursor.task_id}".encode()).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    if not value:
        return None
    problem = ValidationFailed({"cursor": "Not a valid cursor. Use the next_cursor of the previous page."})
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        score, _, task_id = text.partition(":")
        cursor = Cursor(float(score), TaskId(task_id))
    except ValueError as exc:
        raise problem from exc
    # The store cannot compare with a score that is not a finite number.
    if not task_id or not math.isfinite(cursor.score):
        raise problem
    return cursor


def apply_patch(task: Task, patch: TaskPatch, at: float) -> tuple[Task, frozenset[PatchField]]:
    """Return the task after the patch and the names of the fields that changed."""
    sent = patch.model_fields_set
    title = patch.title or task.title
    description = task.description if patch.description is None else patch.description
    status = patch.status or task.status
    priority = patch.priority or task.priority
    notify = task.notify if patch.notify is None else patch.notify
    progress = patch.progress if "progress" in sent else task.progress
    status_note = (patch.status_note or None) if "status_note" in sent else task.status_note
    if status == "done":
        progress = 100
        completed_at = task.completed_at if task.status == "done" else at
    else:
        completed_at = None

    differs: tuple[tuple[PatchField, bool], ...] = (
        ("title", title != task.title),
        ("description", description != task.description),
        ("status", status != task.status),
        ("priority", priority != task.priority),
        ("notify", notify != task.notify),
        ("progress", progress != task.progress),
        ("status_note", status_note != task.status_note),
    )
    changed: frozenset[PatchField] = frozenset(field for field, differs_now in differs if differs_now)
    if not changed:
        return task, _NO_CHANGE
    patched = Task(
        id=task.id,
        key_id=task.key_id,
        title=title,
        description=description,
        status=status,
        priority=priority,
        notify=notify,
        progress=progress,
        status_note=status_note,
        source=task.source,
        created_at=task.created_at,
        # Two writes in one microsecond must still sort in write order.
        updated_at=max(at, round(task.updated_at + 1e-6, 6)),
        completed_at=completed_at,
    )
    return patched, changed


class TaskService:
    def __init__(
        self,
        store: Store,
        push: PushDispatcher,
        questions: QuestionService,
        clock: Clock,
        ids: IdSource,
        limits: Limits,
    ) -> None:
        self._store = store
        self._push = push
        self._questions = questions
        self._clock = clock
        self._ids = ids
        self._limits = limits

    @property
    def _open_limit_message(self) -> str:
        return (
            f"A key can have {self._limits.max_open_tasks_per_key:,} open tasks at most. "
            "Set finished tasks to done or cancelled, or delete tasks."
        )

    async def create(self, principal: Principal, data: TaskCreate) -> Task:
        key_id = _target_key(principal, data.key_id)
        at = self._clock.now()
        done = data.status == "done"
        by_agent = isinstance(principal, AgentPrincipal)
        task = Task(
            id=self._ids.task_id(),
            key_id=key_id,
            title=data.title,
            description=data.description,
            status=data.status,
            priority=data.priority,
            notify=data.notify,
            progress=100 if done else None,
            status_note=None,
            source="agent" if by_agent else "app",
            created_at=at,
            updated_at=at,
            completed_at=at if done else None,
        )
        status = await self._store.create_task(principal.account_id, task, frames.task_created(task))
        match status:
            case "ok":
                self._push.task_changed(principal.account_id, TaskChange("created", task, _NO_CHANGE, by_agent))
                return task
            case "limit":
                raise LimitReached(self._open_limit_message)
            case "no_key":
                raise NotFound("Key not found.")
            case _:
                assert_never(status)

    async def page(
        self,
        principal: Principal,
        *,
        key_id: KeyId | None = None,
        statuses: Collection[Status] = (),
        priorities: Collection[Priority] = (),
        limit: int = limits.LIST_LIMIT_DEFAULT,
        cursor: str | None = None,
    ) -> TaskPage:
        if not 1 <= limit <= limits.LIST_LIMIT_MAX:
            raise ValidationFailed({"limit": f"Must be from 1 to {limits.LIST_LIMIT_MAX}."})
        position = decode_cursor(cursor)
        scope = await self._list_scope(principal, key_id)
        wanted = tuple(statuses)
        if priorities:
            return await self._filtered_page(scope, wanted, priorities, limit, position)
        page = await self._store.page_tasks(scope, wanted, limit, position)
        return TaskPage(page.docs, encode_cursor(page.last) if page.has_more and page.last else None)

    async def _filtered_page(
        self,
        scope: TaskScope,
        statuses: tuple[Status, ...],
        priorities: Collection[Priority],
        limit: int,
        position: Cursor | None,
    ) -> TaskPage:
        docs: list[str] = []
        scanned = 0
        while True:
            page = await self._store.page_tasks(scope, statuses, limit, position)
            for offset, doc in enumerate(page.docs):
                task = Task.model_validate_json(doc)
                if task.priority not in priorities:
                    continue
                docs.append(doc)
                if len(docs) == limit:
                    more = page.has_more or offset + 1 < len(page.docs)
                    return TaskPage(docs, encode_cursor(Cursor(task.updated_at, task.id)) if more else None)
            scanned += len(page.docs)
            position = page.last
            if not page.has_more or position is None:
                return TaskPage(docs)
            if scanned >= self._limits.filter_scan_budget:
                return TaskPage(docs, encode_cursor(position))

    async def open_tasks(self, principal: Principal) -> list[Task]:
        tasks: list[Task] = []
        cursor: str | None = None
        while True:
            page = await self.page(principal, statuses=OPEN_STATUSES, limit=limits.LIST_LIMIT_MAX, cursor=cursor)
            tasks += page.tasks
            cursor = page.next_cursor
            if cursor is None:
                return tasks

    async def get(self, principal: Principal, task_id: TaskId) -> Task:
        return (await self._load(principal, task_id)).task

    async def update(self, principal: Principal, task_id: TaskId, patch: TaskPatch) -> Task:
        by_agent = isinstance(principal, AgentPrincipal)
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._load(principal, task_id)
            task, changed = apply_patch(stored.task, patch, self._clock.now())
            if not changed:
                return task
            status = await self._store.update_task(principal.account_id, stored, task, frames.task_updated(task))
            match status:
                case "ok":
                    self._push.task_changed(principal.account_id, TaskChange("updated", task, changed, by_agent))
                    return task
                case "limit":
                    raise LimitReached(self._open_limit_message)
                case "conflict":
                    continue
                case _:
                    assert_never(status)
        raise Conflict("The task changed at the same time. Try again.")

    async def delete(self, principal: Principal, task_id: TaskId) -> Task:
        by_agent = isinstance(principal, AgentPrincipal)
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._load(principal, task_id)
            task = stored.task
            deleted = await self._store.delete_task(principal.account_id, stored, frames.task_deleted(task))
            if deleted is not None:
                self._push.task_changed(
                    principal.account_id, TaskChange("deleted", task, _NO_CHANGE, by_agent, deleted.activities)
                )
                await self._questions.cancel_for_deleted_task(principal.account_id, deleted.pending_questions)
                return task
        raise Conflict("The task changed at the same time. Try again.")

    async def notify_user(self, principal: AgentPrincipal, data: NotifyInput) -> NotifyResult:
        task = await self.get(principal, data.task_id) if data.task_id else None
        at = self._clock.now()
        per_hour = self._limits.notify_user_per_hour
        count = await self._store.hit(f"notify:{principal.key_id}:{int(at // _HOUR)}", 2 * _HOUR)
        if count > per_hour:
            raise RateLimited(f"A key can send {per_hour} notifications in one hour.", _HOUR - int(at % _HOUR))
        devices = await self._push.message(principal.account_id, principal.key_id, data.title, data.body, task)
        return NotifyResult(sent=devices > 0, devices=devices)

    async def set_live_activity(self, principal: AccountPrincipal, task_id: TaskId, data: LiveActivityInput) -> None:
        status = await self._store.put_activity(principal.account_id, task_id, data.device_id, data.push_token)
        match status:
            case "ok":
                return
            case "no_task":
                raise NotFound("Task not found.")
            case "no_device":
                raise NotFound("Device not found.")
            case _:
                assert_never(status)

    async def clear_live_activity(
        self, principal: AccountPrincipal, task_id: TaskId, data: LiveActivityRemoval
    ) -> None:
        if not await self._store.delete_activity(principal.account_id, task_id, data.device_id):
            raise NotFound("Task not found.")

    async def _load(self, principal: Principal, task_id: TaskId) -> StoredTask:
        stored = await self._store.get_task(own_scope(principal), task_id)
        if stored is None:
            raise NotFound("Task not found.")
        return stored

    async def _list_scope(self, principal: Principal, key_id: KeyId | None) -> TaskScope:
        match principal:
            case AgentPrincipal():
                if key_id is not None:
                    raise ValidationFailed({"key_id": "Not permitted with an API key."})
                return KeyTasks(principal.key_id)
            case AccountPrincipal():
                if key_id is None:
                    return AccountTasks(principal.account_id)
                if await self._store.key_account(key_id) != principal.account_id:
                    raise NotFound("Key not found.")
                return KeyTasks(key_id)


def _target_key(principal: Principal, key_id: KeyId | None) -> KeyId:
    match principal:
        case AgentPrincipal():
            if key_id is not None:
                raise ValidationFailed({"key_id": "Not permitted with an API key."})
            return principal.key_id
        case AccountPrincipal():
            if key_id is None:
                raise ValidationFailed({"key_id": "Required with an account credential."})
            return key_id
