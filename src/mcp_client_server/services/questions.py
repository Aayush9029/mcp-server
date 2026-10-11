import logging
from collections.abc import Collection
from dataclasses import dataclass
from functools import cached_property
from typing import assert_never

from mcp_client_server import limits
from mcp_client_server.clock import Clock
from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Principal
from mcp_client_server.errors import Conflict, LimitReached, NotFound, ValidationFailed
from mcp_client_server.events import EventHub, frames
from mcp_client_server.ids import AccountId, IdSource, QuestionId
from mcp_client_server.limits import Limits
from mcp_client_server.models import (
    CONFIRM_OPTIONS,
    Question,
    QuestionAnswer,
    QuestionCreate,
    QuestionStatus,
)
from mcp_client_server.push import PushDispatcher, QuestionChange
from mcp_client_server.services.inbox import InboxService
from mcp_client_server.services.scope import own_scope
from mcp_client_server.services.waits import WaitSlots
from mcp_client_server.store import Store, StoredQuestion

logger = logging.getLogger(__name__)

_WRITE_ATTEMPTS = 4
_BATCH = 100
_PENDING: tuple[QuestionStatus, ...] = ("pending",)


@dataclass(frozen=True)
class QuestionPage:
    """Questions as the stored JSON. The REST list sends `docs` on and does not parse them."""

    docs: list[str]

    @cached_property
    def questions(self) -> list[Question]:
        return [Question.model_validate_json(doc) for doc in self.docs]

    def to_json(self) -> bytes:
        """The JSON of `QuestionList`."""
        return f'{{"questions":[{",".join(self.docs)}]}}'.encode()


class QuestionService:
    def __init__(
        self,
        store: Store,
        events: EventHub,
        push: PushDispatcher,
        inbox: InboxService,
        waits: WaitSlots,
        clock: Clock,
        ids: IdSource,
        limits: Limits,
    ) -> None:
        self._store = store
        self._events = events
        self._push = push
        self._inbox = inbox
        self._waits = waits
        self._clock = clock
        self._ids = ids
        self._limits = limits

    async def ask(self, principal: AgentPrincipal, data: QuestionCreate) -> Question:
        at = self._clock.now()
        question = Question(
            id=self._ids.question_id(),
            key_id=principal.key_id,
            task_id=data.task_id,
            prompt=data.prompt,
            kind=data.kind,
            options=list(CONFIRM_OPTIONS) if data.kind == "confirm" else (data.options or []),
            status="pending",
            answer=None,
            created_at=at,
            answered_at=None,
            expires_at=round(at + data.expires_in, 6),
        )
        status = await self._store.create_question(principal.account_id, question, frames.question_created(question))
        match status:
            case "ok":
                self._push.question_changed(principal.account_id, QuestionChange("created", question))
                return question
            case "limit":
                raise LimitReached(
                    f"A key can have {self._limits.max_pending_questions_per_key} pending questions at most. "
                    "Wait for an answer, or cancel a question."
                )
            case "no_task":
                raise NotFound("Task not found.")
            case "no_key":
                raise NotFound("Key not found.")
            case _:
                assert_never(status)

    async def page(self, principal: Principal, statuses: Collection[QuestionStatus] = _PENDING) -> QuestionPage:
        docs = await self._store.list_questions(
            own_scope(principal), tuple(statuses) or _PENDING, limits.QUESTION_LIST_MAX
        )
        return QuestionPage(docs)

    async def get(self, principal: Principal, question_id: QuestionId, *, wait: float = 0) -> Question:
        """Read a question. With `wait`, hold the call until the question is not pending or the seconds pass.

        The call waits on the event subscription of this process. It holds no store connection and it does not poll.
        """
        question = await self._read(principal, question_id)
        if question.status != "pending" or wait <= 0:
            return question
        with self._waits.take(principal):
            # The subscription comes first, so an answer between the read and the wait is not lost.
            subscription = await self._events.open(principal.account_id)
            seconds = min(wait, max(0.0, question.expires_at - self._clock.now()))
            waiting = subscription.frames(self._clock, seconds)
            try:
                question = await self._read(principal, question_id)
                if question.status != "pending":
                    return question
                async for frame in waiting:
                    if frames.can_change_question(frame, question_id):
                        question = await self._read(principal, question_id)
                        if question.status != "pending":
                            return question
            finally:
                await waiting.aclose()
                subscription.close()
            return await self._read(principal, question_id)

    async def answer(self, principal: AccountPrincipal, question_id: QuestionId, data: QuestionAnswer) -> Question:
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._expire_if_due(principal.account_id, await self._load(principal, question_id))
            question = stored.question
            if question.status != "pending":
                raise Conflict(f"The question is {question.status}. Only a pending question can get an answer.")
            if question.kind != "text" and data.answer not in question.options:
                raise ValidationFailed({"answer": f"Use one of the options: {', '.join(question.options)}."})
            answered = await self._close(principal.account_id, stored, "answered", data.answer)
            if answered:
                await self._inbox.answered(principal.account_id, answered)
                return answered
        raise Conflict("The question changed at the same time. Try again.")

    async def cancel(self, principal: AgentPrincipal, question_id: QuestionId) -> None:
        for _ in range(_WRITE_ATTEMPTS):
            stored = await self._expire_if_due(principal.account_id, await self._load(principal, question_id))
            status = stored.question.status
            if status == "cancelled":
                return
            if status != "pending":
                raise Conflict(f"The question is {status}. Only a pending question can be cancelled.")
            if await self._close(principal.account_id, stored, "cancelled"):
                return
        raise Conflict("The question changed at the same time. Try again.")

    async def cancel_for_deleted_task(self, account_id: AccountId, question_ids: Collection[QuestionId]) -> None:
        for question_id in question_ids:
            stored = await self._store.get_question_unscoped(question_id)
            if stored and stored.question.status == "pending":
                await self._close(account_id, stored, "cancelled")

    async def expire_due(self, batch: int = _BATCH) -> int:
        """Set the pending questions that are past `expires_at` to `expired`. Returns how many it looked at."""
        due = await self._store.questions_due(self._clock.now(), batch)
        for question_id in due:
            stored = await self._store.get_question_unscoped(question_id)
            account_id = await self._store.key_account(stored.question.key_id) if stored else None
            if stored is None or account_id is None or stored.question.status != "pending":
                await self._store.forget_question(question_id, "due")
            else:
                await self._close(account_id, stored, "expired")
        return len(due)

    async def purge_closed(self, batch: int = _BATCH) -> int:
        """Delete the questions that closed more than the retention time ago. Returns how many it looked at."""
        cutoff = self._clock.now() - self._limits.closed_question_ttl_seconds
        closed = await self._store.questions_closed_before(cutoff, batch)
        for question_id in closed:
            stored = await self._store.get_question_unscoped(question_id)
            account_id = await self._store.key_account(stored.question.key_id) if stored else None
            if stored is None or account_id is None:
                await self._store.forget_question(question_id, "closed")
            else:
                await self._store.purge_question(account_id, stored.question)
        return len(closed)

    async def _close(
        self, account_id: AccountId, stored: StoredQuestion, status: QuestionStatus, answer: str | None = None
    ) -> Question | None:
        """Move a pending question to its final status. None if a different writer was first."""
        at = self._clock.now()
        old = stored.question
        closed = Question(
            id=old.id,
            key_id=old.key_id,
            task_id=old.task_id,
            prompt=old.prompt,
            kind=old.kind,
            options=old.options,
            status=status,
            answer=answer,
            created_at=old.created_at,
            answered_at=at if status == "answered" else None,
            expires_at=old.expires_at,
        )
        result = await self._store.close_question(account_id, stored, closed, at, frames.question_updated(closed))
        if result == "conflict":
            return None
        self._push.question_changed(account_id, QuestionChange("updated", closed))
        return closed

    async def _expire_if_due(self, account_id: AccountId, stored: StoredQuestion) -> StoredQuestion:
        """The sweep runs each few seconds. A read or a write at the exact time must not see a late `pending`."""
        question = stored.question
        if question.status != "pending" or question.expires_at > self._clock.now():
            return stored
        expired = await self._close(account_id, stored, "expired")
        if expired:
            return StoredQuestion(expired.model_dump_json(), expired)
        current = await self._store.get_question_unscoped(question.id)
        if current is None:
            raise NotFound("Question not found.")
        return current

    async def _load(self, principal: Principal, question_id: QuestionId) -> StoredQuestion:
        stored = await self._store.get_question(own_scope(principal), question_id)
        if stored is None:
            raise NotFound("Question not found.")
        return stored

    async def _read(self, principal: Principal, question_id: QuestionId) -> Question:
        stored = await self._load(principal, question_id)
        return (await self._expire_if_due(principal.account_id, stored)).question
