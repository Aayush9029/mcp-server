import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import BlockingConnectionPool, Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from mcp_client_server import __version__
from mcp_client_server.api.errors import install_error_handlers
from mcp_client_server.api.routes import router as api_router
from mcp_client_server.config import Settings, get_settings
from mcp_client_server.events import EventHub
from mcp_client_server.logsetup import configure_logging
from mcp_client_server.mcp.server import mcp_route
from mcp_client_server.push import ApnsClient, PushDispatcher, PushSender, Topics
from mcp_client_server.services import build_services
from mcp_client_server.store import Store
from mcp_client_server.web.pages import pages_router

logger = logging.getLogger(__name__)


def _command_client(settings: Settings) -> Redis:
    pool = BlockingConnectionPool.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        timeout=5,
        socket_connect_timeout=5,
        socket_timeout=10,
        health_check_interval=30,
    )
    return Redis.from_pool(pool)


def event_client(settings: Settings) -> Redis:
    # No retry: the hub must see each lost connection, because events are lost with it.
    return Redis.from_url(  # pyright: ignore[reportUnknownMemberType]
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=5,
        retry=Retry(NoBackoff(), 0),
    )


def _push_sender(settings: Settings) -> PushSender | None:
    key = settings.apns_private_key() if settings.push_configured else None
    if not key or not settings.apns_key_id or not settings.apns_team_id:
        logger.warning(
            "push is off: set APNS_KEY_P8 or APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID, and APNS_BUNDLE_ID to turn it on"
        )
        return None
    return ApnsClient(private_key=key, key_id=settings.apns_key_id, team_id=settings.apns_team_id)


def _topics(settings: Settings) -> Topics | None:
    if not settings.apns_bundle_id:
        return None
    return Topics(app=settings.apns_bundle_id, watch=settings.apns_watch_bundle_id or settings.apns_bundle_id)


def create_app(
    settings: Settings | None = None,
    *,
    redis: Redis | None = None,
    event_redis: Redis | None = None,
    push_sender: PushSender | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    commands = redis or _command_client(settings)
    subscriber = event_redis or event_client(settings)
    store = Store(commands, settings.redis_key_prefix)
    events = EventHub(subscriber, store.keys.channel_prefix)
    push = PushDispatcher(store, push_sender or _push_sender(settings), _topics(settings))
    services = build_services(settings, store, events, push)
    route, mcp_sessions = mcp_route(services)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        await events.start()
        maintenance = asyncio.create_task(services.maintenance.run(), name="maintenance")
        try:
            async with mcp_sessions.run():
                yield
        finally:
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            await events.stop()
            await push.aclose()
            await subscriber.aclose()
            await commands.aclose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services
    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(pages_router(settings))
    app.router.routes.append(route)
    return app
