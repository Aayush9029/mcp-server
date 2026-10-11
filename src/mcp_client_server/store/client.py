"""The typed edge of redis-py. Replies come out as `object` and are narrowed here; an outage is `StoreUnavailable`."""

from collections.abc import Iterable, Sequence
from typing import TypeGuard, TypeIs

from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.client import Pipeline
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff, NoBackoff
from redis.commands.core import AsyncScript
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from mcp_client_server.errors import StoreUnavailable

type Arg = str | int | float

OUTAGE_ERRORS = (RedisConnectionError, RedisTimeoutError, OSError)


def command_client(url: str, max_connections: int) -> Redis:
    # A pooled connection can be dead after the store starts again. The retry opens a new one.
    # A timeout is not tried again, so one request waits for one timeout at most.
    retry = Retry(ExponentialWithJitterBackoff(base=0.05, cap=0.5), retries=2, supported_errors=(RedisConnectionError,))
    pool = BlockingConnectionPool.from_url(  # pyright: ignore[reportUnknownMemberType]  redis-py has no types for from_url
        url,
        decode_responses=True,
        max_connections=max_connections,
        timeout=5,
        socket_connect_timeout=2,
        socket_timeout=10,
        health_check_interval=30,
        retry=retry,
    )
    return Redis.from_pool(pool)


def event_client(url: str) -> Redis:
    # No retry: the event hub must see each lost connection, because events are lost with it.
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType]  redis-py has no types for from_url
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        retry=Retry(NoBackoff(), 0),
    )


class ReplyError(TypeError):
    def __init__(self, expected: str, reply: object) -> None:
        super().__init__(f"The store gave {type(reply).__name__}, not {expected}.")


def _is_list(value: object) -> TypeIs[list[object]]:
    return isinstance(value, list)


def _is_dict(value: object) -> TypeIs[dict[object, object]]:
    return isinstance(value, dict)


def _is_set(value: object) -> TypeIs[set[object]]:
    return isinstance(value, set)


def text(reply: object) -> str:
    if isinstance(reply, str):
        return reply
    raise ReplyError("a string", reply)


def opt_text(reply: object) -> str | None:
    return None if reply is None else text(reply)


def integer(reply: object) -> int:
    if isinstance(reply, int):
        return reply
    raise ReplyError("an integer", reply)


def items(reply: object) -> list[object]:
    if _is_list(reply):
        return reply
    raise ReplyError("a list", reply)


def _all_text(values: list[object]) -> TypeGuard[list[str]]:
    # One pass in C, and no copy: a page of tasks has 500 documents.
    return set(map(type, values)) <= {str}


def texts(reply: list[object]) -> list[str]:
    if _all_text(reply):
        return reply
    raise ReplyError("a list of strings", reply)


def text_set(reply: object) -> set[str]:
    if _is_set(reply):
        return set(texts(list(reply)))
    return set(texts(items(reply)))


def text_map(reply: object) -> dict[str, str]:
    """A hash. redis-py gives a dict for HGETALL; a Lua script gives a flat list of pairs."""
    if _is_dict(reply):
        return {text(name): text(value) for name, value in reply.items()}
    flat = texts(items(reply))
    return dict(zip(flat[::2], flat[1::2], strict=True))


class Script:
    def __init__(self, script: AsyncScript) -> None:
        self._script = script

    async def __call__(self, *, keys: Sequence[str] = (), args: Sequence[Arg] = ()) -> object:
        try:
            return await self._script(keys=keys, args=args)
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc


class KeyValue:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def script(self, source: str) -> Script:
        return Script(self._redis.register_script(source))

    async def call(self, *args: Arg) -> object:
        try:
            return await self._redis.execute_command(*args)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]  redis-py does not annotate execute_command
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc

    async def batch(self, commands: Iterable[Sequence[Arg]], *, atomic: bool = False) -> list[object]:
        """Send the commands in one round trip. `atomic` wraps them in MULTI/EXEC."""
        pipe: Pipeline = self._redis.pipeline(transaction=atomic)
        for command in commands:
            pipe.execute_command(*command)  # pyright: ignore[reportUnknownMemberType]  redis-py does not annotate it
        try:
            return await pipe.execute()
        except OUTAGE_ERRORS as exc:
            raise StoreUnavailable from exc
