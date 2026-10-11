import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from cryptography import x509
from fastapi import FastAPI
from redis.asyncio import Redis

from mcp_client_server import __version__
from mcp_client_server.api.bodylimit import BodyLimit
from mcp_client_server.api.errors import install_error_handlers
from mcp_client_server.api.routes import router as api_router
from mcp_client_server.api.transport import StrictTransport
from mcp_client_server.appattest import apple_roots
from mcp_client_server.clock import Clock, SystemClock
from mcp_client_server.config import Settings, get_settings
from mcp_client_server.events import EventHub
from mcp_client_server.ids import IdSource, UlidSource
from mcp_client_server.limits import Limits
from mcp_client_server.logsetup import configure_logging
from mcp_client_server.mcp.server import mcp_route
from mcp_client_server.push import ApnsClient, PushDispatcher, PushSender, Topics
from mcp_client_server.services import Services, build_services
from mcp_client_server.site import site_router
from mcp_client_server.store import RedisSubscriber, Store, command_client, event_client

logger = logging.getLogger(__name__)


def _push_sender(settings: Settings, clock: Clock) -> PushSender | None:
    config = settings.apns()
    if config is None:
        logger.warning(
            "push is off: set APNS_KEY_P8 or APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, and APNS_BUNDLE_ID to turn it on"
        )
        return None
    return ApnsClient(config, clock)


def _topics(settings: Settings) -> Topics | None:
    if not settings.apns_bundle_id:
        return None
    return Topics(app=settings.apns_bundle_id, watch=settings.apns_watch_bundle_id or settings.apns_bundle_id)


@dataclass(frozen=True, slots=True)
class Runtime:
    """The service layer with its connections. The web application and the operator commands both run on it."""

    services: Services
    # In the order of the dependencies: the work that makes pushes stops first, the connections close last.
    closers: tuple[Callable[[], Awaitable[None]], ...]

    async def aclose(self) -> None:
        for close in self.closers:
            await close()


def build_runtime(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    event_redis: Redis | None = None,
    push_sender: PushSender | None = None,
    clock: Clock | None = None,
    ids: IdSource | None = None,
    limits: Limits | None = None,
    attest_roots: Sequence[x509.Certificate] | None = None,
) -> Runtime:
    settings = settings or get_settings()
    clock = clock or SystemClock()
    limits = limits or Limits()
    configure_logging(settings.log_level)

    commands = redis or command_client(settings.redis_url, settings.redis_max_connections)
    subscriber = event_redis or event_client(settings.redis_url)
    store = Store(commands, settings.redis_key_prefix, limits)
    events = EventHub(
        lambda: RedisSubscriber(subscriber), store.keys, clock, max_streams=limits.max_streams_per_account
    )
    push = PushDispatcher(store, push_sender or _push_sender(settings, clock), _topics(settings), clock)
    roots = apple_roots() if attest_roots is None else attest_roots
    services = build_services(settings, store, events, push, clock, ids or UlidSource(), limits, roots)
    return Runtime(services, (events.stop, push.aclose, subscriber.aclose, commands.aclose))


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    event_redis: Redis | None = None,
    push_sender: PushSender | None = None,
    clock: Clock | None = None,
    ids: IdSource | None = None,
    limits: Limits | None = None,
    attest_roots: Sequence[x509.Certificate] | None = None,
) -> FastAPI:
    runtime = build_runtime(
        settings,
        redis=redis,
        event_redis=event_redis,
        push_sender=push_sender,
        clock=clock,
        ids=ids,
        limits=limits,
        attest_roots=attest_roots,
    )
    services = runtime.services
    route, mcp_sessions = mcp_route(services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        # The store can be down at this point. Each part connects when it needs to, and connects again after a loss.
        await services.events.start()
        loops = [
            asyncio.create_task(services.maintenance.run(), name="maintenance"),
            asyncio.create_task(services.maintenance.run_question_expiry(), name="question-expiry"),
        ]
        try:
            async with mcp_sessions.run():
                yield
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            await runtime.aclose()

    app = FastAPI(
        title=services.settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    install_error_handlers(app)
    app.add_middleware(BodyLimit)
    app.add_middleware(StrictTransport, behind_render=services.settings.render)
    app.include_router(api_router)
    app.include_router(site_router(services.settings, services.limits))
    app.router.routes.append(route)
    return app
