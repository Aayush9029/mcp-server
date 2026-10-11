from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from mcp_client_server.api.deps import AccountAuth, ServicesDep
from mcp_client_server.models import Health

router = APIRouter()

_SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}
_UP = Health(status="ok", store="ok")
_DOWN = Health(status="unavailable", store="unavailable")


@router.get("/health", response_model=None)
async def health(services: ServicesDep) -> JSONResponse:
    if await services.system.store_answers():
        return JSONResponse(_UP.model_dump())
    return JSONResponse(_DOWN.model_dump(), status_code=503)


@router.get("/v1/events", response_model=None)
async def events(principal: AccountAuth, services: ServicesDep) -> StreamingResponse:
    subscription = await services.events.open(principal.account_id)
    return StreamingResponse(
        subscription.stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
        background=BackgroundTask(subscription.close),
    )
