import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Protocol

from mcp_client_server.clock import Clock
from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.events import frames
from mcp_client_server.ids import AccountId
from mcp_client_server.store import KeySpace, PubSubMessage

logger = logging.getLogger(__name__)

type _Queue = asyncio.Queue[str | None]


class Subscriber(Protocol):
    """One pub/sub connection. Each method raises `StoreUnavailable` if the connection is lost."""

    async def subscribe(self, channel: str) -> None: ...

    async def unsubscribe(self, channel: str) -> None: ...

    async def ping(self) -> None: ...

    async def next_message(self, timeout: float) -> PubSubMessage | None:  # noqa: ASYNC109  the connection owns the wait
        """The next message, or None if `timeout` seconds pass with no message."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class HubTiming:
    ping_seconds: float = 20.0
    confirm_seconds: float = 5.0
    retry_seconds: float = 1.0
    # A quiet connection gets a PING after this time. Three with no answer mean that the connection is dead.
    liveness_seconds: float = 20.0


class EventHub:
    """One pub/sub connection for this process. It fans each account channel out to the local streams.

    A stream gets `ready` only after the store confirms the subscription, so no event
    can fall between `ready` and the lists that the client gets next. If the
    connection breaks, events are lost, so the hub closes every local stream and the
    clients connect again.
    """

    def __init__(
        self,
        connect: Callable[[], Subscriber],
        keys: KeySpace,
        clock: Clock,
        *,
        timing: HubTiming | None = None,
        queue_size: int = 256,
    ) -> None:
        self._connect = connect
        self._keys = keys
        self._clock = clock
        self._timing = timing or HubTiming()
        self._queue_size = queue_size
        self._subscriber = connect()
        self._listeners: dict[str, set[_Queue]] = {}
        self._active: set[str] = set()
        self._confirmed: dict[str, asyncio.Future[None]] = {}
        self._acks: dict[str, deque[asyncio.Future[None]]] = {}
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._healthy = True

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._run(), name="event-hub")

    async def stop(self) -> None:
        tasks = [task for task in (self._reader, *self._background) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._close_all()
        await self._close_subscriber()

    @property
    def stream_count(self) -> int:
        return sum(len(queues) for queues in self._listeners.values())

    async def open(self, account_id: AccountId) -> "Subscription":
        channel = self._keys.channel(account_id)
        queue: _Queue = asyncio.Queue(self._queue_size)
        self._listeners.setdefault(channel, set()).add(queue)
        try:
            await self._reconcile()
            async with asyncio.timeout(self._timing.confirm_seconds):
                await asyncio.shield(self._confirmation(channel))
        except TimeoutError as exc:
            self._detach(channel, queue)
            raise StoreUnavailable from exc
        except BaseException:
            self._detach(channel, queue)
            raise
        return Subscription(account_id, queue, self._timing.ping_seconds, lambda: self._detach(channel, queue))

    def _confirmation(self, channel: str) -> asyncio.Future[None]:
        confirmed = self._confirmed.get(channel)
        if confirmed is None:
            # The reader reset the connection between the subscribe and this line.
            raise StoreUnavailable
        return confirmed

    def _detach(self, channel: str, queue: _Queue) -> None:
        # No await here: this runs while the stream is being cancelled.
        queues = self._listeners.get(channel)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._listeners[channel]
            task = asyncio.create_task(self._reconcile_quietly())
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _reconcile_quietly(self) -> None:
        try:
            await self._reconcile()
        except StoreUnavailable:
            logger.debug("unsubscribe failed; the reader resets the connection")

    async def _reconcile(self) -> None:
        async with self._lock:
            wanted = set(self._listeners)
            for channel in wanted - self._active:
                confirmed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                pending = self._acks.setdefault(channel, deque())
                pending.append(confirmed)
                self._confirmed[channel] = confirmed
                self._active.add(channel)
                try:
                    await self._subscriber.subscribe(channel)
                except BaseException:
                    # No confirmation comes for a subscribe that failed. If its entry stayed, the confirmation
                    # of the next subscribe went to it, and no later stream of this account became ready.
                    pending.remove(confirmed)
                    self._confirmed.pop(channel, None)
                    self._active.discard(channel)
                    raise
            for channel in self._active - wanted:
                self._active.discard(channel)
                self._confirmed.pop(channel, None)
                await self._subscriber.unsubscribe(channel)

    async def _run(self) -> None:
        while True:
            try:
                await self._read()
            except StoreUnavailable as exc:
                level = logging.WARNING if self._healthy else logging.DEBUG
                logger.log(
                    level,
                    "event subscriber lost its connection (%s); closing %d streams",
                    exc.__cause__ or exc,
                    self.stream_count,
                )
                self._healthy = False
            except Exception:
                logger.exception("event subscriber failed; closing %d streams", self.stream_count)
                self._healthy = False
            await self._reset()
            await self._clock.sleep(self._timing.retry_seconds)

    async def _read(self) -> None:
        liveness = self._timing.liveness_seconds
        async with self._lock:
            await self._subscriber.subscribe(self._keys.idle_channel)
        last_seen = self._clock.monotonic()
        while True:
            message = await self._subscriber.next_message(liveness)
            if message:
                last_seen = self._clock.monotonic()
                if not self._healthy:
                    self._healthy = True
                    logger.info("event subscriber is connected again")
                self._dispatch(message)
            elif self._clock.monotonic() - last_seen > 3 * liveness:
                raise StoreUnavailable("The store did not answer.")
            else:
                async with self._lock:
                    await self._subscriber.ping()

    def _dispatch(self, message: PubSubMessage) -> None:
        if message.kind == "subscribed":
            pending = self._acks.get(message.channel)
            if pending:
                confirmed = pending.popleft()
                if not confirmed.done():
                    confirmed.set_result(None)
            return
        if message.kind != "message":
            return
        final = frames.is_final(message.data)
        for queue in tuple(self._listeners.get(message.channel, ())):
            try:
                queue.put_nowait(message.data)
            except asyncio.QueueFull:
                # A client this far behind gets the full lists again after it connects again.
                self._drop(message.channel, queue)
                continue
            if final:
                self._drop(message.channel, queue)

    def _drop(self, channel: str, queue: _Queue) -> None:
        _end(queue)
        self._detach(channel, queue)

    def _close_all(self) -> None:
        for queues in self._listeners.values():
            for queue in queues:
                _end(queue)
        self._listeners.clear()

    async def _close_subscriber(self) -> None:
        try:
            await self._subscriber.aclose()
        except StoreUnavailable:
            logger.debug("pub/sub close failed")

    async def _reset(self) -> None:
        async with self._lock:
            self._close_all()
            self._active.clear()
            self._confirmed.clear()
            for pending in self._acks.values():
                for confirmed in pending:
                    if not confirmed.done():
                        confirmed.set_exception(StoreUnavailable())
                        # A stream that left before the reset does not read the result.
                        confirmed.exception()
            self._acks.clear()
            await self._close_subscriber()
            self._subscriber = self._connect()


def _end(queue: _Queue) -> None:
    """Tell the reader of a queue that no more frames come."""
    if queue.full():
        while not queue.empty():
            queue.get_nowait()
    queue.put_nowait(None)


class Subscription:
    def __init__(self, account_id: AccountId, queue: _Queue, ping_seconds: float, close: Callable[[], None]) -> None:
        self._account_id = account_id
        self._queue = queue
        self._ping_seconds = ping_seconds
        self._close = close

    def close(self) -> None:
        self._close()

    async def stream(self) -> AsyncGenerator[str]:
        try:
            yield frames.ready(self._account_id)
            while True:
                try:
                    async with asyncio.timeout(self._ping_seconds):
                        item = await self._queue.get()
                except TimeoutError:
                    yield frames.PING
                    continue
                if item is None:
                    return
                yield item
        finally:
            self.close()

    async def frames(self, clock: Clock, seconds: float) -> AsyncGenerator[str]:
        """The frames of the account until `seconds` pass or the hub closes the stream. For a call that waits."""
        timer = asyncio.create_task(self._end_after(clock, seconds))
        try:
            while (item := await self._queue.get()) is not None:
                yield item
        finally:
            timer.cancel()
            self.close()

    async def _end_after(self, clock: Clock, seconds: float) -> None:
        await clock.sleep(seconds)
        _end(self._queue)
