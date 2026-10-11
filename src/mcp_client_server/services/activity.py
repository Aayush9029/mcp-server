import asyncio
import logging
from collections import deque
from dataclasses import dataclass

from mcp_client_server import limits
from mcp_client_server.activity import Trace, host_name
from mcp_client_server.clock import Clock
from mcp_client_server.credentials import AccountPrincipal
from mcp_client_server.errors import NotFound, ValidationFailed
from mcp_client_server.ids import AccountId, IdSource, KeyId
from mcp_client_server.models import ActivityEntry, Surface
from mcp_client_server.services import cursors
from mcp_client_server.store import Store

logger = logging.getLogger(__name__)

# Entries that wait for the store. One more is dropped, so a store that is slow cannot fill the memory.
_MAX_PENDING = 2000
_BATCH = 100
_CLOSE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ActivityPage:
    items: list[ActivityEntry]
    next_cursor: str | None


class ActivityLog:
    """The last requests of each API key, for the person who looks for what went wrong.

    A request does not wait for the store: `close` puts the entry in a queue of this process, and one writer
    sends the queue to the store in batches. Thus a request has no store call for its entry.
    """

    def __init__(self, store: Store, clock: Clock, ids: IdSource) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._pending: deque[tuple[AccountId, KeyId, ActivityEntry]] = deque()
        self._writer: asyncio.Task[None] | None = None
        self._dropped = 0

    def open(self, surface: Surface, host_header: str | None) -> Trace:
        return Trace(surface=surface, host=host_name(host_header), started=self._clock.monotonic())

    def close(self, trace: Trace) -> None:
        """Queue the entry of a request that is complete. Only a request of an API key has an entry."""
        principal = trace.principal
        if principal is None or trace.at is None or trace.action is None or trace.quiet:
            return
        if len(self._pending) >= _MAX_PENDING:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.warning("the activity log is behind: %d entries dropped so far", self._dropped)
            return
        entry = ActivityEntry(
            id=self._ids.activity_id(),
            at=trace.at,
            surface=trace.surface,
            action=trace.action,
            status="error" if trace.code else "ok",
            code=trace.code,
            message=trace.message,
            duration_ms=max(0, round((self._clock.monotonic() - trace.started) * 1000)),
            client=trace.client,
            host=trace.host,
        )
        self._pending.append((principal.account_id, principal.key_id, entry))
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._write(), name="activity-log")

    async def page(
        self,
        principal: AccountPrincipal,
        key_id: KeyId,
        *,
        limit: int = limits.ACTIVITY_LIMIT_DEFAULT,
        cursor: str | None = None,
    ) -> ActivityPage:
        """The entries of one key of the account, newest first."""
        if not 1 <= limit <= limits.ACTIVITY_LIMIT_MAX:
            raise ValidationFailed({"limit": f"Must be from 1 to {limits.ACTIVITY_LIMIT_MAX}."})
        after = cursors.decode(cursor)
        # The entries that this process still holds go to the store first, so the person sees them.
        await self.drain()
        docs = await self._store.activity_log(principal.account_id, key_id, self._clock.now())
        if docs is None:
            raise NotFound("Key not found.")
        # The log has a few hundred small entries at most, so one read gets all of them.
        entries = [ActivityEntry.model_validate_json(doc) for doc in docs]
        if after is not None:
            entries = [entry for entry in entries if (entry.at, entry.id) < after]
        page = entries[:limit]
        more = len(entries) > limit
        return ActivityPage(page, cursors.encode(page[-1].at, page[-1].id) if more else None)

    async def drain(self) -> None:
        """Wait until the queue of this process is in the store."""
        while self._writer is not None and not self._writer.done():
            await asyncio.wait({self._writer})

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                await self.drain()
        except TimeoutError:
            logger.warning("shutdown: %d activity entries were not written", len(self._pending))
            if self._writer is not None:
                self._writer.cancel()
                await asyncio.wait({self._writer})

    async def _write(self) -> None:
        while self._pending:
            batch = [self._pending.popleft() for _ in range(min(_BATCH, len(self._pending)))]
            try:
                await self._store.add_activity(batch)
            except Exception:
                # The log is a help for the person. An entry that the store did not take is not sent again.
                logger.warning("could not write %d activity entries", len(batch), exc_info=True)
