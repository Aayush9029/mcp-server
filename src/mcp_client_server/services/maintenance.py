import asyncio
import logging
import random

from mcp_client_server import limits
from mcp_client_server.events import frames
from mcp_client_server.ids import now
from mcp_client_server.store import Store

logger = logging.getLogger(__name__)

_INTERVAL_SECONDS = 15 * 60
_BATCH = 200


class Maintenance:
    """Deletes closed tasks 90 days after their last update."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def expire_closed_tasks(self, batch: int = _BATCH) -> int:
        cutoff = now() - limits.CLOSED_TASK_TTL_SECONDS
        expired = 0
        for task_id in await self._store.closed_before(cutoff, batch):
            stored = await self._store.get_task_unscoped(task_id)
            account_id = await self._store.key_account(stored.task.key_id) if stored else None
            if stored is None or account_id is None or stored.task.is_open:
                await self._store.forget_closed(task_id)
                continue
            if await self._store.delete_task(account_id, stored, frames.task_deleted(stored.task)) is not None:
                expired += 1
        return expired

    async def run(self) -> None:
        while True:
            # The jitter and the lock keep several workers from doing the same work.
            await asyncio.sleep(_INTERVAL_SECONDS * random.uniform(0.5, 1.0))
            try:
                if not await self._store.acquire_lock("expire", _INTERVAL_SECONDS // 2):
                    continue
                while await self.expire_closed_tasks() == _BATCH:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task expiry failed")
