import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from mcp_client_server.events import frames

logger = logging.getLogger(__name__)

PING_SECONDS = 20.0
_CONFIRM_SECONDS = 5.0
_RETRY_SECONDS = 1.0
# A quiet connection gets a PING after this time. Three with no answer mean that the connection is dead.
_LIVENESS_SECONDS = 20.0

Queue = asyncio.Queue[str | None]


class EventHub:
    """One pub/sub connection for this process. It fans each account channel out to the local streams.

    A stream gets `ready` only after the store confirms the subscription, so no event
    can fall between `ready` and the lists that the client gets next. If the
    connection breaks, events are lost, so the hub closes every local stream and the
    clients connect again.
    """

    def __init__(self, redis: Redis, channel_prefix: str, queue_size: int = 256) -> None:
        self._redis = redis
        self._prefix = channel_prefix
        self._idle_channel = f"{channel_prefix}_"
        self._queue_size = queue_size
        self.ping_seconds = PING_SECONDS
        self._pubsub: PubSub = self._new_pubsub()
        self._listeners: dict[str, set[Queue]] = {}
        self._active: set[str] = set()
        self._confirmed: dict[str, asyncio.Future[None]] = {}
        self._acks: dict[str, deque[asyncio.Future[None]]] = {}
        self._lock = asyncio.Lock()
        self._reader: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()

    def _new_pubsub(self) -> PubSub:
        return self._redis.pubsub()  # pyright: ignore[reportUnknownMemberType]

    async def _next_message(self) -> dict[str, Any] | None:
        return await self._pubsub.get_message(timeout=_LIVENESS_SECONDS)  # pyright: ignore[reportUnknownVariableType]

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._run(), name="event-hub")

    async def stop(self) -> None:
        for task in (self._reader, *self._background):
            if task:
                task.cancel()
        await asyncio.gather(*filter(None, (self._reader, *self._background)), return_exceptions=True)
        self._close_all()
        try:
            await self._pubsub.aclose()
        except Exception:
            logger.debug("pub/sub close failed", exc_info=True)

    @property
    def stream_count(self) -> int:
        return sum(len(queues) for queues in self._listeners.values())

    async def open(self, account_id: str) -> "Subscription":
        channel = self._prefix + account_id
        queue: Queue = asyncio.Queue(self._queue_size)
        self._listeners.setdefault(channel, set()).add(queue)
        try:
            await self._reconcile()
            confirmed = self._confirmed.get(channel)
            if confirmed is None:
                raise ConnectionError("The event subscriber lost its connection.")
            await asyncio.wait_for(asyncio.shield(confirmed), _CONFIRM_SECONDS)
        except BaseException:
            self._detach(channel, queue)
            raise
        return Subscription(self, channel, account_id, queue)

    def _detach(self, channel: str, queue: Queue) -> None:
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
        except Exception:
            logger.debug("unsubscribe failed; the reader resets the connection", exc_info=True)

    async def _reconcile(self) -> None:
        async with self._lock:
            wanted = set(self._listeners)
            for channel in wanted - self._active:
                confirmed: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._acks.setdefault(channel, deque()).append(confirmed)
                self._confirmed[channel] = confirmed
                self._active.add(channel)
                await self._pubsub.subscribe(channel)
            for channel in self._active - wanted:
                self._active.discard(channel)
                self._confirmed.pop(channel, None)
                await self._pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]

    async def _run(self) -> None:
        while True:
            try:
                async with self._lock:
                    await self._pubsub.subscribe(self._idle_channel)
                last_seen = time.monotonic()
                while True:
                    message = await self._next_message()
                    if message:
                        last_seen = time.monotonic()
                        self._dispatch(message)
                    elif time.monotonic() - last_seen > 3 * _LIVENESS_SECONDS:
                        raise ConnectionError("the store did not answer")
                    else:
                        async with self._lock:
                            await self._pubsub.ping()  # pyright: ignore[reportUnknownMemberType]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("event subscriber lost its connection (%s); closing %d streams", exc, self.stream_count)
            await self._reset()
            await asyncio.sleep(_RETRY_SECONDS)

    def _dispatch(self, message: dict[str, Any]) -> None:
        channel = message["channel"]
        if channel is None:
            return
        if message["type"] == "subscribe":
            pending = self._acks.get(channel)
            if pending:
                confirmed = pending.popleft()
                if not confirmed.done():
                    confirmed.set_result(None)
            return
        if message["type"] != "message":
            return
        data: str = message["data"]
        final = frames.event_name(data) == frames.ACCOUNT_DELETED
        for queue in tuple(self._listeners.get(channel, ())):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # A client this far behind gets the full lists again after it connects again.
                self._drop(channel, queue)
                continue
            if final:
                self._drop(channel, queue)

    def _drop(self, channel: str, queue: Queue) -> None:
        self._close(queue)
        self._detach(channel, queue)

    @staticmethod
    def _close(queue: Queue) -> None:
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
        queue.put_nowait(None)

    def _close_all(self) -> None:
        for queues in self._listeners.values():
            for queue in queues:
                self._close(queue)
        self._listeners.clear()

    async def _reset(self) -> None:
        async with self._lock:
            self._close_all()
            self._active.clear()
            self._confirmed.clear()
            for pending in self._acks.values():
                for confirmed in pending:
                    if not confirmed.done():
                        confirmed.set_exception(ConnectionError("The event subscriber lost its connection."))
            self._acks.clear()
            try:
                await self._pubsub.aclose()
            except Exception:
                logger.debug("pub/sub close failed", exc_info=True)
            self._pubsub = self._new_pubsub()


class Subscription:
    def __init__(self, hub: EventHub, channel: str, account_id: str, queue: Queue) -> None:
        self._hub = hub
        self._channel = channel
        self._account_id = account_id
        self._queue = queue

    def close(self) -> None:
        self._hub._detach(self._channel, self._queue)  # pyright: ignore[reportPrivateUsage]

    async def stream(self) -> AsyncIterator[str]:
        try:
            yield frames.ready(self._account_id)
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), self._hub.ping_seconds)
                except TimeoutError:
                    yield frames.PING
                    continue
                if item is None:
                    return
                yield item
        finally:
            self.close()
