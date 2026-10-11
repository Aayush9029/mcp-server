from contextvars import ContextVar

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from mcp_client_server.credentials import AgentPrincipal, presented_secret
from mcp_client_server.errors import Forbidden
from mcp_client_server.services.auth import AuthService

_principal: ContextVar[AgentPrincipal | None] = ContextVar("mcp_principal", default=None)
_inbox_unread: ContextVar[int] = ContextVar("mcp_inbox_unread", default=0)


def current_principal() -> AgentPrincipal:
    principal = _principal.get()
    if principal is None:
        raise RuntimeError("No API key is authenticated in this context.")
    return principal


def optional_principal() -> AgentPrincipal | None:
    return _principal.get()


def inbox_unread() -> int:
    """The unread inbox items of the key of this request."""
    return _inbox_unread.get()


def set_inbox_unread(count: int) -> None:
    """For a tool that reads the inbox: the number at the start of the request is not current after it."""
    _inbox_unread.set(count)


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
        identity = await self._auth.identify(presented_secret(request.headers, request.query_params))
        principal = identity.principal
        if not isinstance(principal, AgentPrincipal):
            raise Forbidden("The MCP endpoint needs an API key (mck_). The account token cannot use it.")
        token = _principal.set(principal)
        unread = _inbox_unread.set(identity.inbox_unread)
        try:
            await self._transport(scope, receive, send)
        finally:
            _principal.reset(token)
            _inbox_unread.reset(unread)
