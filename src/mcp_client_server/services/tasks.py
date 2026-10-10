import base64
import json
from collections.abc import Collection
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from mcp_client_server import limits
from mcp_client_server.credentials import Principal
from mcp_client_server.errors import Conflict, Forbidden, LimitReached, NotFound, RateLimited, ValidationFailed
from mcp_client_server.events import frames
from mcp_client_server.ids import new_id, now
from mcp_client_server.models import (
    OPEN_STATUSES,
    LiveActivityInput,
    LiveActivityRemoval,
    NotifyInput,
    NotifyResult,
    Priority,
    Status,
    Task,
    TaskCreate,
    TaskPatch,
)
from mcp_client_server.push import PushDispatcher, TaskChange
from mcp_client_server.store import Cursor, Store, StoredTask

_WRITE_ATTEMPTS = 8
# A filter on priority reads tasks that it does not return. One request reads this many at most.
_SCAN_BUDGET = 2000
_PATCH_FIELDS = ("title", "description", "status", "priority", "notify", "progress", "status_note")
_HOUR = 3600
_OPEN_LIMIT_MESSAGE = (
    f"A key can have {limits.MAX_OPEN_TASKS_PER_KEY:,} open tasks at most. "
    "Set finished tasks to done or cancelled, or delete tasks."
)


@dataclass(frozen=True)
class TaskPage:
    """One page of tasks, as the stored JSON. The REST list sends `docs` on and does not parse them."""

    docs: list[str]
    next_cursor: str | None = None

    @cached_property
    def tasks(self) -> list[Task]:
        return [Task.model_validate_json(doc) for doc in self.docs]

    def to_json(self) -> bytes:
        return f'{{"tasks":[{",".join(self.docs)}],"next_cursor":{json.dumps(self.next_cursor)}}}'.encode()


def encode_cursor(cursor: Cursor) -> str:
    return base64.urlsafe_b64encode(f"{cursor.score!r}:{cursor.task_id}".encode()).decode().rstrip("=")


def decode_cursor(value: str | None) -> Cursor | None:
    if not value:
        return None
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        score, _, task_id = text.partition(":")
        if not task_id:
            raise ValueError
        return Cursor(float(score), task_id)
    except ValueError as exc:
        raise ValidationFailed({"cursor": "Not a valid cursor. Use the next_cursor of the previous page."}) from exc


def apply_patch(task: Task, changes: dict[str, Any], at: float) -> tuple[Task, frozenset[str]]:
    """Return the task after the patch and the names of the fields that changed."""
    data = task.model_dump()
    for field, value in changes.items():
        data[field] = (value or None) if field == "status_note" else value
    if data["status"] == "done":
        data["progress"] = 100
        data["completed_at"] = task.completed_at if task.status == "done" else at
    else:
        data["completed_at"] = None
    changed = frozenset(field for field in _PATCH_FIELDS if data[field] != getattr(task, field))
    if not changed:
        return task, changed
    # Two writes in one microsecond must still sort in write order.
    data["updated_at"] = max(at, round(task.updated_at + 1e-6, 6))
    return Task(**data), changed


class TaskService:
    def __init__(self, store: Store, push: PushDispatcher) -> None:
        self._store = store
        self._push = push

    async def create(self, principal: Principal, data: TaskCreate) -> Task:
        key_id = _target_key(principal, data.key_id)
        at = now()
        done = data.status == "done"
        task = Task(
            id=new_id("tsk"),
            key_id=key_id,
            title=data.title,
            description=data.description,
            status=data.status,
            priority=data.priority,
            notify=data.notify,
            progress=100 if done else None,
            status_note=None,
            source="agent" if principal.is_agent else "app",
            created_at=at,
            updated_at=at,
            completed_at=at if done else None,
        )
        status = await self._store.create_task(principal.account_id, task, frames.task_event("created", task))
        if status == "limit":
            raise LimitReached(_OPEN_LIMIT_MESSAGE)
        if status != "ok":
            raise NotFound("Key not found.")
        self._push.task_changed(principal.account_id, TaskChange("created", task, frozenset(), principal.is_agent))
        return task

    async def page(
        self,
        principal: Principal,
        *,
        key_id: str | None = None,
        statuses: Collection[Status] = (),
        priorities: Collection[Priority] = (),
        limit: int = limits.LIST_LIMIT_DEFAULT,
        cursor: str | None = None,
    ) -> TaskPage:
        if not 1 <= limit <= limits.LIST_LIMIT_MAX:
            raise ValidationFailed({"limit": f"Must be from 1 to {limits.LIST_LIMIT_MAX}."})
        position = decode_cursor(cursor)
        index = await self._index(principal, key_id)
        if priorities:
            return await self._filtered_page(index, list(statuses), priorities, limit, position)
        page = await self._store.page_tasks(index, list(statuses), limit, position)
        return TaskPage(page.docs, encode_cursor(page.last) if page.has_more and page.last else None)

    async def _filtered_page(
        self,
        index: str,
        statuses: list[Status],
        priorities: Collection[Priority],
        limit: int,
        position: Cursor | None,
    ) -> TaskPage:
        docs: list[str] = []
        scanned = 0
        while True:
            page = await self._store.page_tasks(index, statuses, limit, position)
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
            if scanned >= _SCAN_BUDGET:
                return TaskPage(docs, encode_cursor(position))

    async def open_tasks(self, principal: Principal) -> list[Task]:
        tasks: list[Task] = []
        cursor: str | None = None
        while True:
            page = await self.page(
                principal, statuses=sorted(OPEN_STATUSES), limit=limits.LIST_LIMIT_MAX, cursor=cursor
            )
            tasks += page.tasks
            cursor = page.next_cursor
            if cursor is None:
                return tasks

    async def get(self, principal: Principal, task_id: str) -> Task:
        return (await self._load(principal, task_id)).task

    async def update(self, principal: Principal, task_id: str, patch: TaskPatch) -> Task:
        changes = patch.model_dump(exclude_unset=True)
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._load(principal, task_id)
            task, changed = apply_patch(stored.task, changes, now())
            if not changed:
                return task
            status = await self._store.update_task(
                principal.account_id, stored, task, frames.task_event("updated", task)
            )
            if status == "limit":
                raise LimitReached(_OPEN_LIMIT_MESSAGE)
            if status == "ok":
                self._push.task_changed(principal.account_id, TaskChange("updated", task, changed, principal.is_agent))
                return task
        raise Conflict("The task changed at the same time. Try again.")

    async def delete(self, principal: Principal, task_id: str) -> Task:
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._load(principal, task_id)
            task = stored.task
            activities = await self._store.delete_task(principal.account_id, stored, frames.task_deleted(task))
            if activities is not None:
                self._push.task_changed(
                    principal.account_id,
                    TaskChange("deleted", task, frozenset(), principal.is_agent, activities),
                )
                return task
        raise Conflict("The task changed at the same time. Try again.")

    async def notify_user(self, principal: Principal, data: NotifyInput) -> NotifyResult:
        key_id = principal.key_id
        if key_id is None:
            raise Forbidden("Only an API key can send a notification.")
        task = await self.get(principal, data.task_id) if data.task_id else None
        at = now()
        count = await self._store.hit(f"notify:{key_id}:{int(at // _HOUR)}", 2 * _HOUR)
        if count > limits.NOTIFY_USER_PER_HOUR:
            raise RateLimited(
                f"A key can send {limits.NOTIFY_USER_PER_HOUR} notifications in one hour.",
                _HOUR - int(at % _HOUR),
            )
        devices = await self._push.message(principal.account_id, key_id, data.title, data.body, task)
        return NotifyResult(sent=devices > 0, devices=devices)

    async def set_live_activity(self, principal: Principal, task_id: str, data: LiveActivityInput) -> None:
        status = await self._store.put_activity(principal.account_id, task_id, data.device_id, data.push_token)
        if status == "no_task":
            raise NotFound("Task not found.")
        if status == "no_device":
            raise NotFound("Device not found.")

    async def clear_live_activity(self, principal: Principal, task_id: str, data: LiveActivityRemoval) -> None:
        if not await self._store.delete_activity(principal.account_id, task_id, data.device_id):
            raise NotFound("Task not found.")

    async def _load(self, principal: Principal, task_id: str) -> StoredTask:
        keys = self._store.keys
        index = keys.key_tasks(principal.key_id) if principal.key_id else keys.account_tasks(principal.account_id)
        stored = await self._store.get_task(index, task_id)
        if stored is None:
            raise NotFound("Task not found.")
        return stored

    async def _index(self, principal: Principal, key_id: str | None) -> str:
        keys = self._store.keys
        if principal.key_id:
            if key_id is not None:
                raise ValidationFailed({"key_id": "Not permitted with an API key."})
            return keys.key_tasks(principal.key_id)
        if key_id is None:
            return keys.account_tasks(principal.account_id)
        if await self._store.key_account(key_id) != principal.account_id:
            raise NotFound("Key not found.")
        return keys.key_tasks(key_id)


def _target_key(principal: Principal, key_id: str | None) -> str:
    if principal.key_id:
        if key_id is not None:
            raise ValidationFailed({"key_id": "Not permitted with an API key."})
        return principal.key_id
    if key_id is None:
        raise ValidationFailed({"key_id": "Required with an account credential."})
    return key_id
