import os
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import fakeredis
import httpx2
import pytest
from cryptography import x509
from fastapi import FastAPI
from pydantic_settings import SettingsConfigDict
from redis.asyncio import Redis

from mcp_client_server.config import Settings
from mcp_client_server.ids import AccountId
from mcp_client_server.limits import Limits
from mcp_client_server.main import create_app
from mcp_client_server.services import Services
from mcp_client_server.store import Store
from tests.support import Api, EventListener, FakeClock, RawStore, RecordingPushSender, Running, SequentialIds

type Connect = Callable[[], Redis]
type Launch = Callable[..., AbstractAsyncContextManager[Running]]


class TestSettings(Settings):
    """Settings that read no `.env` file, so the tests do not depend on the developer's machine."""

    __test__ = False
    model_config = SettingsConfigDict(env_file=None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _wait_for_port(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.02)
    raise RuntimeError("the test store did not start")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """TEST_REPEAT=50 runs each selected test 50 times in one process, to find a test that fails only sometimes."""
    repeat = int(os.environ.get("TEST_REPEAT", "1"))
    if repeat > 1:
        metafunc.fixturenames.append("repeat_number")
        metafunc.parametrize("repeat_number", range(repeat))


@pytest.fixture(scope="session")
def anyio_backend() -> tuple[str, dict[str, bool]]:
    """asyncio. TEST_LOOP=uvloop runs the tests on uvloop, the event loop of the server in production."""
    return "asyncio", {"use_uvloop": os.environ.get("TEST_LOOP") == "uvloop"}


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
    port = free_port()
    process = subprocess.Popen(  # noqa: S603  the binary comes from PATH on the developer's machine
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
def fake_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def connect(store_url: str | None, fake_server: fakeredis.FakeServer) -> Connect:
    if store_url:
        return lambda: Redis.from_url(store_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]  redis-py has no types for from_url
    return lambda: fakeredis.FakeAsyncRedis(server=fake_server, decode_responses=True)


@pytest.fixture
async def raw(connect: Connect) -> AsyncIterator[RawStore]:
    """The test store, empty before and after the test."""
    client = connect()
    store = RawStore(client)
    await store.flush()
    yield store
    await store.flush()
    await client.aclose()


@pytest.fixture
def redis(raw: RawStore) -> Redis:
    return raw.redis


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def push() -> RecordingPushSender:
    return RecordingPushSender()


@pytest.fixture
def redis_listener(redis: Redis) -> EventListener:
    """Collects the events of the first account that a test makes."""
    return EventListener(redis, AccountId("acc_000001"))


@pytest.fixture
def store(redis: Redis) -> Store:
    return Store(redis, "t")


@pytest.fixture
def settings(store_url: str | None) -> Settings:
    return TestSettings(
        redis_url=store_url or "redis://unused:6379/0",
        redis_key_prefix="t",
        public_base_url="https://tasks.example.com",
        support_email="help@example.com",
        apns_bundle_id="com.example.app",
        rate_limit_per_minute=0,
        rate_limit_accounts_per_hour=0,
    )


@pytest.fixture
def launch(settings: Settings, redis: Redis, connect: Connect, clock: FakeClock, push: RecordingPushSender) -> Launch:
    """Start an application on the test store. Keyword arguments replace fields of the settings or the limits."""

    @asynccontextmanager
    async def start(
        *,
        limits: Limits | None = None,
        own_connections: bool = False,
        attest_roots: Sequence[x509.Certificate] | None = None,
        **changes: object,
    ) -> AsyncGenerator[Running]:
        app = create_app(
            settings.model_copy(update=changes),
            # With `own_connections`, the application makes its production pool and subscriber for the real store.
            redis=None if own_connections else connect(),
            event_redis=None if own_connections else connect(),
            push_sender=push,
            clock=clock,
            ids=SequentialIds(),
            limits=limits,
            attest_roots=attest_roots,
        )
        services = app.state.services
        assert isinstance(services, Services)
        transport = httpx2.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(transport=transport, base_url="http://test") as http,
        ):
            yield Running(app, http, Api(http), services, push, clock)

    return start


@pytest.fixture
async def running(launch: Launch) -> AsyncGenerator[Running]:
    async with launch() as started:
        yield started


@pytest.fixture
def app(running: Running) -> FastAPI:
    return running.app


@pytest.fixture
def client(running: Running) -> httpx2.AsyncClient:
    return running.http


@pytest.fixture
def api(running: Running) -> Api:
    return running.api


@pytest.fixture
def services(running: Running) -> Services:
    return running.services
