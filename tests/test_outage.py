"""The store is down, then up again. A request gets a clean 503, and the server recovers with no restart.

With a real store, the application makes its own connections (the production pool and subscriber) through a
TCP gate that the test closes and opens. With fakeredis, the fake server refuses commands.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol

import fakeredis
import httpx2
import pytest
from redis.asyncio import Redis

from mcp_client_server.config import Settings
from mcp_client_server.main import create_app
from mcp_client_server.services import Services
from tests.conftest import Connect, free_port
from tests.support import Api, FakeClock, Headers, RecordingPushSender, Running, SequentialIds, error


class Outage(Protocol):
    async def begin(self) -> None: ...

    async def end(self) -> None: ...


class FakeServerOutage:
    def __init__(self, server: fakeredis.FakeServer) -> None:
        self._server = server

    async def begin(self) -> None:
        self._server.connected = False

    async def end(self) -> None:
        self._server.connected = True


class Gate:
    """A TCP proxy in front of the test store. Closed, it refuses new connections and drops the open ones."""

    def __init__(self, upstream_port: int) -> None:
        self.port = free_port()
        self._upstream_port = upstream_port
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()

    async def end(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._accept, "127.0.0.1", self.port)

    async def begin(self) -> None:
        if self._server:
            self._server.close()
            for writer in tuple(self._writers):
                writer.close()
            await self._server.wait_closed()
            self._server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._upstream_port)
        self._writers |= {writer, up_writer}
        try:
            await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))
        finally:
            self._writers -= {writer, up_writer}

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()


type Start = Callable[[], Awaitable[Running]]


@pytest.fixture
async def outage_and_start(
    settings: Settings,
    redis: Redis,
    connect: Connect,
    store_url: str | None,
    fake_server: fakeredis.FakeServer,
    clock: FakeClock,
    push: RecordingPushSender,
) -> AsyncIterator[tuple[Outage, Start]]:
    """The switch for the store, and a function that starts the application (at a moment the test chooses)."""
    gate: Gate | None = None
    if store_url:
        gate = Gate(int(store_url.rsplit(":", 1)[1].split("/")[0]))
        await gate.end()
        outage: Outage = gate
        through_gate = settings.model_copy(update={"redis_url": f"redis://127.0.0.1:{gate.port}/0"})
        app = create_app(through_gate, push_sender=push, clock=clock, ids=SequentialIds())
    else:
        outage = FakeServerOutage(fake_server)
        app = create_app(
            settings, redis=connect(), event_redis=connect(), push_sender=push, clock=clock, ids=SequentialIds()
        )
    services = app.state.services
    assert isinstance(services, Services)

    @asynccontextmanager
    async def lifespan() -> AsyncGenerator[Running]:
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=transport, base_url="http://test") as http,
        ):
            yield Running(app, http, Api(http), services, push, clock)

    context = lifespan()

    async def start() -> Running:
        return await context.__aenter__()

    try:
        yield outage, start
    finally:
        await outage.end()
        await context.__aexit__(None, None, None)
        if gate:
            await gate.begin()


def unavailable(response: httpx2.Response) -> None:
    body = error(response, 503, "unavailable")
    assert body == {"code": "unavailable", "message": "The service is not available now. Try again in a few seconds."}
    assert int(response.headers["retry-after"]) > 0


async def stream_opens(running: Running, account: Headers) -> bool:
    """Open the event stream of the hub and close it again. False while the subscriber has no connection."""
    account_id = await running.api.account_id(account)
    running.clock.advance(1.0)
    try:
        subscription = await running.services.events.open(account_id)
    except Exception:  # noqa: BLE001  any failure means "not yet"; the test has a time limit
        await asyncio.sleep(0.01)
        return False
    subscription.close()
    return True


async def test_each_surface_gives_503_while_the_store_is_down_and_works_again_after(
    outage_and_start: tuple[Outage, Start], caplog: pytest.LogCaptureFixture
) -> None:
    outage, start = outage_and_start
    running = await start()
    api, http = running.api, running.http
    account, key, _ = await api.agent()
    task = await api.task(key)
    assert await stream_opens(running, account)
    caplog.set_level(logging.WARNING)

    await outage.begin()
    health = await http.get("/health")
    assert (health.status_code, health.json()) == (503, {"status": "unavailable", "store": "unavailable"})
    unavailable(await http.post("/v1/accounts", json={}))
    unavailable(await http.get("/v1/account", headers=account))
    unavailable(await http.get("/v1/tasks", headers=key))
    unavailable(await http.patch(f"/v1/tasks/{task['id']}", json={"progress": 5}, headers=key))
    unavailable(await http.get("/v1/events", headers=account))
    unavailable(await api.rpc(key, "tools/list"))
    call = {"name": "create_task", "arguments": {"title": "During the outage"}}
    unavailable(await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "create_task"}))
    assert (await http.get("/")).status_code == 200
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]

    await outage.end()
    assert (await http.get("/health")).json() == {"status": "ok", "store": "ok"}
    assert (await api.patch(key, task["id"], progress=5))["progress"] == 5
    assert (await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "create_task"})).status_code == 200
    titles = {item["title"] for item in (await http.get("/v1/tasks", headers=account)).json()["tasks"]}
    assert titles == {"Write tests", "During the outage"}
    async with asyncio.timeout(5):
        while not await stream_opens(running, account):
            pass


async def test_the_server_starts_while_the_store_is_down_and_serves_when_the_store_comes_up(
    outage_and_start: tuple[Outage, Start],
) -> None:
    outage, start = outage_and_start
    await outage.begin()
    running = await start()
    http = running.http

    assert (await http.get("/health")).status_code == 503
    unavailable(await http.post("/v1/accounts", json={}))
    unavailable(await running.api.rpc({"Authorization": "Bearer mck_" + "a" * 43}, "tools/list"))
    # The subscriber tries again each second and does not fill the log.
    for _ in range(5):
        running.clock.advance(1.0)
        await asyncio.sleep(0.01)

    await outage.end()
    account, key, _ = await running.api.agent()
    assert (await running.api.rpc(key, "tools/list")).status_code == 200
    async with asyncio.timeout(5):
        while not await stream_opens(running, account):
            pass
