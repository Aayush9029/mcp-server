from contextvars import ContextVar

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_client_server.api.deps import request_credential
from mcp_client_server.credentials import Principal
from mcp_client_server.errors import Forbidden
from mcp_client_server.services.auth import AuthService

_principal: ContextVar[Principal | None] = ContextVar("mcp_principal", default=None)


def current_principal() -> Principal:
    principal = _principal.get()
    if principal is None:
        raise RuntimeError("No API key is authenticated in this context.")
    return principal


def optional_principal() -> Principal | None:
    return _principal.get()


class McpEndpoint:
    """Authenticates each MCP request with its API key, then hands it to the SDK transport.

    The server is stateless, so POST is the only method. An error raised here goes to
    the application's error handlers, thus a bad key gives the same 401 body as REST.
    """

    def __init__(self, transport: ASGIApp, auth: AuthService, allowed_origins: frozenset[str]) -> None:
        self._transport = transport
        self._auth = auth
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope)
        if request.method != "POST":
            raise HTTPException(405, headers={"Allow": "POST"})
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in self._allowed_origins:
            raise Forbidden("This origin cannot use the MCP endpoint.")
        principal = await self._auth.authenticate(request_credential(request, allow_query=True))
        if not principal.is_agent:
            raise Forbidden("The MCP endpoint needs an API key (mck_). The account token cannot use it.")
        token = _principal.set(principal)
        try:
            await self._transport(scope, receive, send)
        finally:
            _principal.reset(token)
