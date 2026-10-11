from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.store.client import OUTAGE_ERRORS


@dataclass(frozen=True, slots=True)
class PubSubMessage:
    # "other" is any reply that only shows that the connection is alive, for example a pong.
    kind: Literal["subscribed", "message", "other"]
    channel: str = ""
    data: str = ""


class RedisSubscriber:
    """One pub/sub connection."""

    def __init__(self, redis: Redis) -> None:
        self._pubsub: PubSub = redis.pubsub()  # pyright: ignore[reportUnknownMemberType]  redis-py does not annotate pubsub()

    async def subscribe(self, channel: str) -> None:
        try:
            await self._pubsub.subscribe(channel)
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc

    async def unsubscribe(self, channel: str) -> None:
        try:
            await self._pubsub.unsubscribe(channel)  # pyright: ignore[reportUnknownMemberType]  redis-py does not annotate unsubscribe
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc

    async def ping(self) -> None:
        try:
            await self._pubsub.ping()  # pyright: ignore[reportUnknownMemberType]  redis-py does not annotate ping
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc

    async def next_message(self, timeout: float) -> PubSubMessage | None:  # noqa: ASYNC109  redis-py owns the wait
        try:
            return _message(await self._pubsub.get_message(timeout=timeout))  # pyright: ignore[reportUnknownArgumentType]  redis-py leaves the values partly without a type
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc

    async def aclose(self) -> None:
        try:
            await self._pubsub.aclose()
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc


def _message(raw: Mapping[str, object] | None) -> PubSubMessage | None:
    if raw is None:
        return None
    channel, data = raw.get("channel"), raw.get("data")
    if raw.get("type") == "subscribe" and isinstance(channel, str):
        return PubSubMessage("subscribed", channel)
    if raw.get("type") == "message" and isinstance(channel, str) and isinstance(data, str):
        return PubSubMessage("message", channel, data)
    return PubSubMessage("other")
