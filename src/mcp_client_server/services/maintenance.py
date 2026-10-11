import asyncio
import logging
import random

from mcp_client_server.clock import Clock
from mcp_client_server.events import frames
from mcp_client_server.limits import Limits
from mcp_client_server.services.questions import QuestionService
from mcp_client_server.store import Store

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 15 * 60
# A pending question becomes `expired` this many seconds after its time at the latest.
_QUESTION_SWEEP_SECONDS = 10
_BATCH = 200
_QUESTION_BATCH = 100


class Maintenance:
    """Deletes closed tasks and closed questions after their retention time, and expires pending questions."""

    def __init__(self, store: Store, questions: QuestionService, clock: Clock, limits: Limits) -> None:
        self._store = store
        self._questions = questions
        self._clock = clock
        self._limits = limits

    async def expire_closed_tasks(self, batch: int = _BATCH) -> int:
        cutoff = self._clock.now() - self._limits.closed_task_ttl_seconds
        expired = 0
        for task_id in await self._store.closed_before(cutoff, batch):
            stored = await self._store.get_task_unscoped(task_id)
            account_id = await self._store.key_account(stored.task.key_id) if stored else None
            if stored is None or account_id is None or stored.task.is_open:
                await self._store.forget_closed(task_id)
                continue
            deleted = await self._store.delete_task(account_id, stored, frames.task_deleted(stored.task))
            if deleted is not None:
                await self._questions.cancel_for_deleted_task(account_id, deleted.pending_questions)
                expired += 1
        return expired

    async def run(self) -> None:
        while True:
            # The jitter and the lock keep several workers from doing the same work.
            await self._clock.sleep(_INTERVAL_SECONDS * random.uniform(0.5, 1.0))  # noqa: S311  jitter, not a secret
            try:
                if not await self._store.acquire_lock("expire", _INTERVAL_SECONDS // 2):
                    continue
                while await self.expire_closed_tasks() == _BATCH:
                    pass
                while await self._questions.purge_closed(_QUESTION_BATCH) == _QUESTION_BATCH:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task expiry failed")

    async def run_question_expiry(self) -> None:
        """Each worker runs this loop. The store lets only one of them close a question."""
        while True:
            await self._clock.sleep(_QUESTION_SWEEP_SECONDS)
            try:
                while await self._questions.expire_due(_QUESTION_BATCH) == _QUESTION_BATCH:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("question expiry failed")
