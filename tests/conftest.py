import asyncio
import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import fakeredis
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from redis.asyncio import Redis

from mcp_client_server.config import Settings
from mcp_client_server.main import create_app
from mcp_client_server.push import PushRequest, PushResult
from mcp_client_server.services import Services
from mcp_client_server.store import Store

Connect = Callable[[], Redis]
Headers = dict[str, str]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError("the test store did not start")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def store_url() -> Iterator[str | None]:
    """A real Valkey or Redis if one is available, else None (the tests then use fakeredis).

    TEST_STORE=fakeredis forces fakeredis. TEST_REDIS_URL uses a server that already runs.
    """
    if os.environ.get("TEST_STORE") == "fakeredis":
        yield None
        return
    if url := os.environ.get("TEST_REDIS_URL"):
        yield url
        return
    binary = shutil.which("valkey-server") or shutil.which("redis-server")
    if not binary:
        yield None
        return
    port = _free_port()
    process = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(port)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def connect(store_url: str | None) -> Connect:
    if store_url:
        return lambda: Redis.from_url(store_url, decode_responses=True)
    server = fakeredis.FakeServer()
    return lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


@pytest.fixture
async def redis(connect: Connect) -> AsyncIterator[Redis]:
    client = connect()
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def store(redis: Redis) -> Store:
    return Store(redis, "t")


async def key_names(redis: Redis) -> set[str]:
    return {str(name) for name in await redis.keys("t:*")}


class FakePush:
    def __init__(self) -> None:
        self.sent: list[PushRequest] = []
        self.dead: set[str] = set()

    async def send(self, request: PushRequest) -> PushResult:
        self.sent.append(request)
        return PushResult(410, "Unregistered") if request.token in self.dead else PushResult(200)

    async def aclose(self) -> None:
        pass

    def of_type(self, push_type: str) -> list[PushRequest]:
        return [request for request in self.sent if request.push_type == push_type]


@pytest.fixture
def push() -> FakePush:
    return FakePush()


@pytest.fixture
def settings(store_url: str | None) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        redis_url=store_url or "redis://unused:6379/0",
        redis_key_prefix="t",
        public_base_url="https://tasks.example.com",
        support_email="help@example.com",
        apns_bundle_id="com.example.app",
        rate_limit_per_minute=0,
        rate_limit_accounts_per_hour=0,
    )


@asynccontextmanager
async def running(application: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the lifespan of an app and give an HTTP client for it."""
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(application.router.lifespan_context(application))
        transport = httpx.ASGITransport(app=application)
        yield await stack.enter_async_context(httpx.AsyncClient(transport=transport, base_url="http://test"))


@pytest.fixture
async def app(settings: Settings, redis: Redis, connect: Connect, push: FakePush) -> AsyncIterator[FastAPI]:
    application = create_app(settings, redis=connect(), event_redis=connect(), push_sender=push)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
def services(app: FastAPI) -> Services:
    return app.state.services


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def live_url(app: FastAPI) -> AsyncIterator[str]:
    """The app on a real socket. httpx.ASGITransport cannot stream, and the event stream needs that."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    await task


class Api:
    """Short helpers for the REST calls that most tests need."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def account(self) -> Headers:
        response = await self.http.post("/v1/accounts", json={})
        assert response.status_code == 201, response.text
        return {"Authorization": f"Bearer {response.json()['token']}"}

    async def key(self, account: Headers, label: str = "Claude Code") -> tuple[Headers, dict[str, Any]]:
        response = await self.http.post("/v1/keys", json={"label": label}, headers=account)
        assert response.status_code == 201, response.text
        body = response.json()
        return {"Authorization": f"Bearer {body['secret']}"}, body["key"]

    async def task(self, auth: Headers, **fields: Any) -> dict[str, Any]:
        response = await self.http.post("/v1/tasks", json={"title": "Write tests", **fields}, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()

    async def device(self, account: Headers, device_id: str, **fields: Any) -> dict[str, Any]:
        body = {
            "platform": "ios",
            "model": "iPhone18,1",
            "os_version": "27.0",
            "app_version": "1.0 (1)",
            "apns_environment": "production",
            "apns_token": "a1b2c3d4" * 8,
            "push_to_start_token": "c3d4e5f6" * 8,
            **fields,
        }
        response = await self.http.put(f"/v1/devices/{device_id}", json=body, headers=account)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def api(client: httpx.AsyncClient) -> Api:
    return Api(client)


DEVICE_A = "dev_11111111-1111-4111-8111-111111111111"
DEVICE_B = "dev_22222222-2222-4222-8222-222222222222"
