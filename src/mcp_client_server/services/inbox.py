import logging
from dataclasses import dataclass

from mcp_client_server import limits
from mcp_client_server.clock import Clock
from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Principal
from mcp_client_server.errors import NotFound, RateLimited, ValidationFailed
from mcp_client_server.events import EventHub, frames
from mcp_client_server.ids import AccountId, IdSource, KeyId, QuestionId, TaskId
from mcp_client_server.limits import Limits
from mcp_client_server.models import (
    AgentSender,
    InboxItem,
    InboxKind,
    Key,
    MessageCreate,
    PatchField,
    PersonSender,
    Question,
    Task,
)
from mcp_client_server.services.scope import own_scope
from mcp_client_server.services.seen import SeenMarker
from mcp_client_server.services.waits import WaitSlots
from mcp_client_server.store import Store

logger = logging.getLogger(__name__)

_HOUR = 3600
_PERSON = PersonSender()
# The person changes these fields of a task, and the agent must know. The other fields are the agent's own.
_TOLD_FIELDS: tuple[PatchField, ...] = ("status", "priority", "title", "description")


@dataclass(frozen=True, slots=True)
class InboxPage:
    items: list[InboxItem]
    unread: int


def describe_change(old: Task, new: Task, changed: frozenset[PatchField]) -> str | None:
    """One line that says what the person changed, or None if the agent need not know."""
    parts: list[str] = []
    if "status" in changed:
        parts.append(f"Status changed from {old.status} to {new.status}.")
    if "priority" in changed:
        parts.append(f"Priority changed from {old.priority} to {new.priority}.")
    if "title" in changed:
        parts.append(f'Title changed to "{new.title}".')
    if "description" in changed:
        parts.append("Description changed.")
    return " ".join(parts) or None


class InboxService:
    """What the person and other agents send to a key. A change that a key makes itself never comes here."""

    def __init__(
        self,
        store: Store,
        events: EventHub,
        seen: SeenMarker,
        waits: WaitSlots,
        clock: Clock,
        ids: IdSource,
        limits: Limits,
    ) -> None:
        self._store = store
        self._events = events
        self._seen = seen
        self._waits = waits
        self._clock = clock
        self._ids = ids
        self._limits = limits

    async def task_assigned(self, account_id: AccountId, task: Task) -> None:
        await self._add(account_id, task.key_id, "task_assigned", _PERSON, task.title, task_id=task.id)

    async def task_changed(self, account_id: AccountId, old: Task, new: Task, changed: frozenset[PatchField]) -> None:
        text = describe_change(old, new, changed)
        if text:
            await self._add(account_id, new.key_id, "task_changed", _PERSON, text, task_id=new.id)

    async def task_deleted(self, account_id: AccountId, task: Task) -> None:
        await self._add(account_id, task.key_id, "task_deleted", _PERSON, task.title, task_id=task.id)

    async def answered(self, account_id: AccountId, question: Question) -> None:
        await self._add(
            account_id,
            question.key_id,
            "answer",
            _PERSON,
            question.answer or "",
            task_id=question.task_id,
            question_id=question.id,
        )

    async def send(self, principal: Principal, data: MessageCreate) -> InboxItem:
        """A message from the person to a key, or from an agent to a different key of the same account."""
        sender: PersonSender | AgentSender
        match principal:
            case AccountPrincipal():
                if data.to is not None:
                    raise ValidationFailed({"to": "Not permitted with an account credential. Use key_id."})
                if data.key_id is None:
                    raise ValidationFailed({"key_id": "Required with an account credential."})
                if await self._store.key_account(data.key_id) != principal.account_id:
                    raise NotFound("Key not found.")
                receiver, sender, sender_id = data.key_id, _PERSON, principal.account_id
            case AgentPrincipal():
                if data.key_id is not None:
                    raise ValidationFailed({"key_id": "Not permitted with an API key. Use to."})
                if data.to is None:
                    raise ValidationFailed({"to": "Required with an API key."})
                own, other = _find(await self._store.list_keys(principal.account_id), principal.key_id, data.to)
                receiver, sender_id = other.id, principal.key_id
                sender = AgentSender(key_id=principal.key_id, label=own.label)
        if data.task_id and await self._store.get_task(own_scope(principal), data.task_id) is None:
            raise NotFound("Task not found.")
        await self._count_message(sender_id)
        item = await self._add(principal.account_id, receiver, "message", sender, data.text, task_id=data.task_id)
        if item is None:
            raise NotFound("Key not found.")
        return item

    async def take(
        self,
        principal: AgentPrincipal,
        *,
        wait: float = 0,
        limit: int = limits.INBOX_LIMIT_DEFAULT,
        mark_read: bool = True,
    ) -> InboxPage:
        """The unread items of the key, oldest first. With `wait`, hold the call until the inbox has an item.

        The call waits on the event subscription of this process. It holds no store connection and it does not poll.
        """
        page = await self._take(principal, limit, mark_read=mark_read)
        if page.items or wait <= 0:
            return page
        with self._waits.take(principal):
            # The subscription comes first, so an item between the read and the wait is not lost.
            subscription = await self._events.open(principal.account_id)
            waiting = subscription.frames(self._clock, wait)
            try:
                page = await self._take(principal, limit, mark_read=mark_read)
                if page.items:
                    return page
                async for frame in waiting:
                    if frames.can_change_inbox(frame, principal.key_id):
                        page = await self._take(principal, limit, mark_read=mark_read)
                        if page.items or frames.is_key_deleted(frame):
                            return page
            finally:
                await waiting.aclose()
                subscription.close()
            return await self._take(principal, limit, mark_read=mark_read)

    async def view(self, principal: AccountPrincipal, key_id: KeyId) -> InboxPage:
        """The newest items of a key with their read state, for the person. Nothing becomes read."""
        if await self._store.key_account(key_id) != principal.account_id:
            raise NotFound("Key not found.")
        rows, unread = await self._store.view_inbox(key_id, limits.INBOX_VIEW_COUNT, self._clock.now())
        items = [InboxItem.model_validate_json(doc).model_copy(update={"read_at": read_at}) for doc, read_at in rows]
        return InboxPage(items, unread)

    async def _take(self, principal: AgentPrincipal, limit: int, *, mark_read: bool) -> InboxPage:
        at = self._clock.now()
        docs, unread = await self._store.take_inbox(principal.key_id, limit, mark_read=mark_read, now=at)
        items = [InboxItem.model_validate_json(doc) for doc in docs]
        if not items:
            return InboxPage(items, unread)
        if mark_read:
            items = [item.model_copy(update={"read_at": at}) for item in items]
            await self._store.publish(principal.account_id, frames.inbox_updated(principal.key_id, unread))
        await self._seen.ids(
            principal, (item.task_id for item in items if item.task_id and item.kind != "task_deleted")
        )
        return InboxPage(items, unread)

    async def _add(
        self,
        account_id: AccountId,
        key_id: KeyId,
        kind: InboxKind,
        sender: PersonSender | AgentSender,
        text: str,
        *,
        task_id: TaskId | None = None,
        question_id: QuestionId | None = None,
    ) -> InboxItem | None:
        """Put one item in the inbox of a key and tell the streams. None if the account has no such key."""
        at = self._clock.now()
        item = InboxItem(
            id=self._ids.inbox_id(),
            kind=kind,
            sender=sender,
            task_id=task_id,
            question_id=question_id,
            text=text,
            created_at=at,
        )
        unread = await self._store.add_inbox_item(account_id, key_id, item, at)
        if unread is None:
            return None
        await self._store.publish(account_id, frames.inbox_updated(key_id, unread))
        return item

    async def _count_message(self, sender_id: str) -> None:
        at = self._clock.now()
        per_hour = self._limits.messages_per_hour
        count = await self._store.hit(f"msg:{sender_id}:{int(at // _HOUR)}", 2 * _HOUR)
        if count > per_hour:
            raise RateLimited(f"One sender can send {per_hour} messages in one hour.", _HOUR - int(at % _HOUR))


def _find(keys: list[Key], own_id: KeyId, to: str) -> tuple[Key, Key]:
    """The key of the sender and the one other key that `to` names by its ID or by its exact label."""
    own = next((key for key in keys if key.id == own_id), None)
    others = [key for key in keys if key.id != own_id]
    matches = [key for key in others if key.id == to] or [key for key in others if key.label == to]
    if own is None or len(matches) != 1:
        raise NotFound(
            "No other key of this account has this ID, or exactly one key with this label. "
            "Call list_agents to get the key IDs."
        )
    return own, matches[0]
