"""Fakes and helpers for the tests. Nothing here reaches a real network service."""

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx2
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, Implementation, TextContent
from redis.asyncio import Redis

from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.ids import AccountId, DeviceId, InboxItemId, KeyId, QuestionId, TaskId
from mcp_client_server.models import Device, Task
from mcp_client_server.push import PushRequest, PushResult
from mcp_client_server.services import Services
from mcp_client_server.store import PubSubMessage, RedisSubscriber
from mcp_client_server.store.client import KeyValue, integer, items, text, text_map, texts

type Headers = dict[str, str]
# Parsed JSON has no static shape. The tests read it by key, so `Any` stays inside the test code.
type Json = dict[str, Any]

# The time limit for a signal or a result that must come. It only ends a test that has a defect; it is long,
# so that a slow or busy machine does not fail a correct test.
SIGNAL_SECONDS = 30.0

DEVICE_A = DeviceId("dev_11111111-1111-4111-8111-111111111111")
DEVICE_B = DeviceId("dev_22222222-2222-4222-8222-222222222222")
APNS_TOKEN = "a1b2c3d4" * 8
START_TOKEN = "c3d4e5f6" * 8
ACTIVITY_TOKEN = "e5f6a7b8" * 8
WIDGET_TOKEN = "9a8b7c6d" * 8
MCP_VERSION = "2026-07-28"
MCP_META: Json = {
    "io.modelcontextprotocol/protocolVersion": MCP_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "raw-client", "version": "0.1"},
}


@dataclass(frozen=True)
class _Sleep:
    seconds: float
    deadline: float
    woken: asyncio.Future[None]


class FakeClock:
    """A clock that the test moves.

    Each read of `now` moves the time by `tick`, so two writes never share a timestamp.
    A task that sleeps continues only after the test moves the clock past its wake-up time.

    A wake-up time is counted from the moment the task starts to sleep. Thus a test must not move the clock
    for a task before that task sleeps: `await clock.sleeping(seconds)` is the signal that it does.
    """

    def __init__(self, start: float = 1_760_000_000.0, tick: float = 0.001) -> None:
        self.time = start
        self.tick = tick
        self._monotonic = 0.0
        self._sleepers: list[_Sleep] = []
        self._arrivals: list[asyncio.Future[None]] = []
        self.sleeps: list[float] = []

    def now(self) -> float:
        self.time += self.tick
        return round(self.time, 6)

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self.time += seconds
        self._monotonic += seconds
        due = [sleep for sleep in self._sleepers if sleep.deadline <= self._monotonic]
        self._sleepers = [sleep for sleep in self._sleepers if sleep.deadline > self._monotonic]
        for sleep in due:
            if not sleep.woken.done():
                sleep.woken.set_result(None)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        sleep = _Sleep(seconds, self._monotonic + seconds, asyncio.get_running_loop().create_future())
        self._sleepers.append(sleep)
        _wake(self._arrivals)
        await sleep.woken

    def _armed(self, seconds: float, tolerance: float) -> int:
        # A task that was cancelled in its sleep has a future that is done.
        return sum(
            1 for sleep in self._sleepers if not sleep.woken.done() and abs(sleep.seconds - seconds) <= tolerance
        )

    async def sleeping(self, seconds: float, count: int = 1, *, tolerance: float = 0.0) -> None:
        """Wait until `count` tasks sleep for `seconds`. Each of them has then armed its wake-up time.

        This is a signal from `sleep`, not a poll: no time passes, and the speed of the machine has no effect.
        """
        try:
            async with asyncio.timeout(SIGNAL_SECONDS):
                while self._armed(seconds, tolerance) < count:
                    await _arrival(self._arrivals)
        except TimeoutError:
            now = sorted(sleep.seconds for sleep in self._sleepers if not sleep.woken.done())
            message = f"{self._armed(seconds, tolerance)} of {count} tasks sleep for {seconds} s. All sleeps: {now}"
            raise AssertionError(message) from None


def _wake(waiters: list[asyncio.Future[None]]) -> None:
    for waiter in waiters:
        if not waiter.done():
            waiter.set_result(None)
    waiters.clear()


async def _arrival(waiters: list[asyncio.Future[None]]) -> None:
    """Wait for the next `_wake` of this list."""
    waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    waiters.append(waiter)
    await waiter


class SequentialIds:
    def __init__(self) -> None:
        self._count = 0

    def _next(self, prefix: str) -> str:
        self._count += 1
        return f"{prefix}_{self._count:06d}"

    def account_id(self) -> AccountId:
        return AccountId(self._next("acc"))

    def key_id(self) -> KeyId:
        return KeyId(self._next("key"))

    def task_id(self) -> TaskId:
        return TaskId(self._next("tsk"))

    def question_id(self) -> QuestionId:
        return QuestionId(self._next("qst"))

    def inbox_id(self) -> InboxItemId:
        return InboxItemId(self._next("inb"))


class RecordingPushSender:
    """An APNs stand-in. It records each request and answers as the test tells it."""

    def __init__(self) -> None:
        self.sent: list[PushRequest] = []
        self.replies: dict[str, PushResult | Exception] = {}
        self.closed = False
        self._arrivals: list[asyncio.Future[None]] = []

    async def send(self, request: PushRequest) -> PushResult:
        self.sent.append(request)
        _wake(self._arrivals)
        reply = self.replies.get(request.token, PushResult(200))
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def aclose(self) -> None:
        self.closed = True

    def of_type(self, push_type: str) -> list[PushRequest]:
        return [request for request in self.sent if request.push_type == push_type]

    def payloads(self, push_type: str) -> list[Json]:
        return [payload_json(request) for request in self.of_type(push_type)]

    async def sent_of(self, push_type: str, count: int) -> None:
        """Wait until `count` pushes of the type are recorded. A signal from `send`, not a poll."""
        async with asyncio.timeout(SIGNAL_SECONDS):
            while len(self.of_type(push_type)) < count:
                await _arrival(self._arrivals)


def payload_json(request: PushRequest) -> Json:
    """The payload as the JSON that goes to APNs."""
    return json.loads(json.dumps(request.payload))


@dataclass
class FakeSubscriber:
    bus: "FakeBus"
    channels: set[str] = field(default_factory=set[str])
    pings: int = 0
    closed: bool = False
    inbox: asyncio.Queue[PubSubMessage | StoreUnavailable | None] = field(
        default_factory=asyncio.Queue[PubSubMessage | StoreUnavailable | None]
    )

    def _check(self) -> None:
        if self.bus.down or self.closed:
            raise StoreUnavailable

    async def subscribe(self, channel: str) -> None:
        self._check()
        self.channels.add(channel)
        if self.bus.confirms:
            self.inbox.put_nowait(PubSubMessage("subscribed", channel))

    async def unsubscribe(self, channel: str) -> None:
        self._check()
        self.channels.discard(channel)

    async def ping(self) -> None:
        self._check()
        self.pings += 1
        if self.bus.answers_pings:
            self.inbox.put_nowait(PubSubMessage("other"))

    async def next_message(self, timeout: float) -> PubSubMessage | None:  # noqa: ASYNC109  mirrors the real adapter
        item = await self.inbox.get()
        if isinstance(item, StoreUnavailable):
            raise item
        if item is None:
            self.bus.clock.advance(timeout)
        return item

    async def aclose(self) -> None:
        self.closed = True


class FakeBus:
    """Pub/sub in memory. The test publishes, lets time pass with no message, or breaks the connection."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.down = False
        self.confirms = True
        self.answers_pings = True
        self.subscribers: list[FakeSubscriber] = []

    def connect(self) -> FakeSubscriber:
        subscriber = FakeSubscriber(self)
        self.subscribers.append(subscriber)
        return subscriber

    @property
    def current(self) -> FakeSubscriber:
        return self.subscribers[-1]

    def publish(self, channel: str, data: str) -> None:
        if channel in self.current.channels:
            self.current.inbox.put_nowait(PubSubMessage("message", channel, data))

    def stay_quiet(self) -> None:
        """One liveness period passes with no message."""
        self.current.inbox.put_nowait(None)

    def break_connection(self) -> None:
        self.current.inbox.put_nowait(StoreUnavailable())


class RawStore:
    """Typed access to the test store, for the setup of a test and for assertions about what is stored."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._kv = KeyValue(redis)

    async def flush(self) -> None:
        await self._kv.call("FLUSHDB")

    async def keys(self, pattern: str = "t:*") -> list[str]:
        return sorted(texts(items(await self._kv.call("KEYS", pattern))))

    async def exists(self, *keys: str) -> int:
        return integer(await self._kv.call("EXISTS", *keys))

    async def sorted_set(self, key: str) -> list[str]:
        return texts(items(await self._kv.call("ZRANGE", key, 0, -1)))

    async def publish(self, channel: str, data: str) -> None:
        await self._kv.call("PUBLISH", channel, data)

    async def kill_subscribers(self) -> None:
        await self._kv.call("CLIENT", "KILL", "TYPE", "pubsub")

    async def everything(self) -> str:
        """Each key with its string or hash value, as one text."""
        parts: list[str] = []
        for key in await self.keys():
            kind = text(await self._kv.call("TYPE", key))
            if kind == "string":
                parts.append(f"{key}={text(await self._kv.call('GET', key))}")
            elif kind == "hash":
                parts.append(f"{key}={text_map(await self._kv.call('HGETALL', key))}")
            else:
                parts.append(key)
        return "\n".join(parts)


class EventListener:
    """Collects the names of the events that the store publishes for one account."""

    def __init__(self, redis: Redis, account_id: AccountId) -> None:
        self._subscriber = RedisSubscriber(redis)
        self._channel = f"t:ev:{account_id}"

    async def start(self) -> None:
        await self._subscriber.subscribe(self._channel)
        confirmed = await self._subscriber.next_message(5)
        assert confirmed is not None

    async def names(self) -> list[str]:
        names: list[str] = []
        while message := await self._subscriber.next_message(0.05):
            names.append(message.data.partition("\n")[0].removeprefix("event: "))
        await self._subscriber.aclose()
        return names


async def settle(turns: int = 20) -> None:
    """Let the tasks that are ready run. No time passes."""
    for _ in range(turns):
        await asyncio.sleep(0)


def make_task(**fields: object) -> Task:
    base: dict[str, object] = {
        "id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
        "title": "Add dark mode support",
        "description": "Implement a dark theme in all views.",
        "status": "in_progress",
        "priority": "high",
        "notify": True,
        "progress": 40,
        "status_note": "Updating the color tokens",
        "source": "agent",
        "created_at": 1760112000.412,
        "updated_at": 1760112300.9,
        "completed_at": None,
    }
    return Task.model_validate({**base, **fields})


def make_device(device_id: str = "dev_1", **fields: object) -> Device:
    base: dict[str, object] = {
        "id": device_id,
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "27.0",
        "app_version": "1.0 (1)",
        "apns_environment": "production",
        "apns_token": APNS_TOKEN,
        "push_to_start_token": START_TOKEN,
        "updated_at": 1.0,
    }
    return Device.model_validate({**base, **fields})


def bearer(secret: str) -> Headers:
    return {"Authorization": f"Bearer {secret}"}


def secret_of(auth: Headers) -> str:
    return auth["Authorization"].removeprefix("Bearer ")


def error(response: httpx2.Response, status: int, code: str) -> Json:
    """Assert the error shape of docs/overview.md and return the `error` object."""
    assert response.status_code == status, response.text
    body: Json = response.json()["error"]
    assert body["code"] == code
    assert isinstance(body["message"], str)
    assert body["message"]
    return body


class Api:
    """Short helpers for the REST calls that most tests need."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http

    async def account(self) -> Headers:
        response = await self.http.post("/v1/accounts", json={})
        assert response.status_code == 201, response.text
        return bearer(response.json()["token"])

    async def account_id(self, account: Headers) -> AccountId:
        return AccountId((await self.http.get("/v1/account", headers=account)).json()["id"])

    async def key(self, account: Headers, label: str = "Claude Code") -> tuple[Headers, Json]:
        response = await self.http.post("/v1/keys", json={"label": label}, headers=account)
        assert response.status_code == 201, response.text
        body = response.json()
        return bearer(body["secret"]), body["key"]

    async def agent(self, label: str = "Claude Code") -> tuple[Headers, Headers, Json]:
        """A new account with one key: the account credential, the key credential, and the Key object."""
        account = await self.account()
        key, info = await self.key(account, label)
        return account, key, info

    async def task(self, auth: Headers, **fields: object) -> Json:
        response = await self.http.post("/v1/tasks", json={"title": "Write tests", **fields}, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()

    async def patch(self, auth: Headers, task_id: str, **fields: object) -> Json:
        response = await self.http.patch(f"/v1/tasks/{task_id}", json=fields, headers=auth)
        assert response.status_code == 200, response.text
        return response.json()

    async def device(self, account: Headers, device_id: str = DEVICE_A, **fields: object) -> Json:
        body = {
            "platform": "ios",
            "model": "iPhone18,1",
            "os_version": "27.0",
            "app_version": "1.0 (1)",
            "apns_environment": "production",
            "apns_token": APNS_TOKEN,
            "push_to_start_token": START_TOKEN,
            **fields,
        }
        response = await self.http.put(f"/v1/devices/{device_id}", json=body, headers=account)
        assert response.status_code == 200, response.text
        return response.json()

    async def ask(self, key: Headers, **fields: object) -> Json:
        response = await self.http.post("/v1/questions", json={"prompt": "Deploy now?", **fields}, headers=key)
        assert response.status_code == 201, response.text
        return response.json()

    async def answer(self, account: Headers, question_id: str, answer: str = "Yes") -> httpx2.Response:
        return await self.http.post(f"/v1/questions/{question_id}/answer", json={"answer": answer}, headers=account)

    async def inbox(self, key: Headers, **query: object) -> Json:
        """Read the inbox of a key: `GET /v1/inbox`."""
        response = await self.http.get(
            "/v1/inbox", params={name: str(value) for name, value in query.items()}, headers=key
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def message(self, auth: Headers, **body: object) -> Json:
        response = await self.http.post("/v1/messages", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()

    async def tool(self, key: Headers, name: str, arguments: Mapping[str, object]) -> Json:
        """Call an MCP tool over raw HTTP and return the `result` object."""
        call = {"name": name, "arguments": arguments}
        response = await self.rpc(key, "tools/call", call, headers={"Mcp-Name": name})
        assert response.status_code == 200, response.text
        result: Json = response.json()["result"]
        return result

    async def live_activity(self, account: Headers, task_id: str, device_id: str = DEVICE_A) -> None:
        body = {"device_id": device_id, "push_token": ACTIVITY_TOKEN}
        response = await self.http.put(f"/v1/tasks/{task_id}/live-activity", json=body, headers=account)
        assert response.status_code == 204, response.text

    async def rpc(
        self,
        auth: Headers,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        version: str = MCP_VERSION,
        headers: Headers | None = None,
    ) -> httpx2.Response:
        """One raw MCP request on the 2026-07-28 transport."""
        meta = {**MCP_META, "io.modelcontextprotocol/protocolVersion": version}
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": meta}}
        sent = {
            **auth,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": version,
            "Mcp-Method": method,
            **(headers or {}),
        }
        return await self.http.post("/mcp", json=body, headers=sent)


@dataclass(frozen=True)
class Running:
    """An application with its lifespan running, and the fakes that it was built with."""

    app: FastAPI
    http: httpx2.AsyncClient
    api: Api
    services: Services
    push: RecordingPushSender
    clock: FakeClock

    async def pushes_done(self) -> None:
        await self.services.push.drain()


@asynccontextmanager
async def mcp_client(
    app: FastAPI, auth: Headers, mode: str = "auto", name: str = "claude-code"
) -> AsyncGenerator[Client]:
    """The SDK's own client, connected to the ASGI app. `mode` is "auto" (2026-07-28) or "legacy" (initialize)."""
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=auth)
    transport = streamable_http_client("http://test/mcp", http_client=http)
    info = Implementation(name=name, version="2.1.290")
    async with http, Client(transport, mode=mode, client_info=info) as client:
        yield client


def tool_text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def structured(result: CallToolResult) -> Json:
    assert result.is_error is not True, tool_text(result)
    content = result.structured_content
    assert content is not None
    return content
