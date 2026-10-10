import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from mcp_client_server.api.deps import AccountPrincipal, ServicesDep

logger = logging.getLogger(__name__)

router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


@router.get("/health")
async def health(services: ServicesDep) -> JSONResponse:
    try:
        await services.store.ping()
    except Exception as exc:
        logger.warning("health check: the store does not answer (%s)", type(exc).__name__)
        return JSONResponse({"status": "unavailable", "store": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok", "store": "ok"})


@router.get("/v1/events")
async def events(principal: AccountPrincipal, services: ServicesDep) -> StreamingResponse:
    subscription = await services.events.open(principal.account_id)
    return StreamingResponse(
        subscription.stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        background=BackgroundTask(subscription.close),
    )
