"""The request side of the activity log: one entry for each request of an API key, when the request is complete."""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_client_server import activity
from mcp_client_server.api.errors import code_of
from mcp_client_server.services.activity import ActivityLog

MCP_PATH = "/mcp"
_FIRST_ERROR_STATUS = 400
_SERVER_ERROR = 500


class ActivityRecorder:
    """Times each request and gives it a trace. The auth step, the route, and the error handlers fill in the trace.

    The entry goes to the log after the response is sent. A request with no API key leaves no entry: the health
    check, the website, and the requests of the app.
    """

    def __init__(self, app: ASGIApp, log: ActivityLog) -> None:
        self._app = app
        self._log = log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        on_mcp = scope["path"] == MCP_PATH
        trace = self._log.open("mcp" if on_mcp else "rest", Headers(scope=scope).get(activity.HOST_HEADER))
        # None until the response starts. A client that went away before that leaves no entry.
        status: int | None = None

        async def observed(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        token = activity.bind(trace)
        try:
            await self._app(scope, receive, observed)
        except Exception:
            status = _SERVER_ERROR
            raise
        finally:
            activity.unbind(token)
            if trace.principal is not None and status is not None:
                _complete(trace, scope, status)
                self._log.close(trace)


def _complete(trace: activity.Trace, scope: Scope, status: int) -> None:
    failed = status >= _FIRST_ERROR_STATUS
    if status >= _SERVER_ERROR and trace.code is None:
        trace.fail("internal", "Internal error.")
    if trace.surface == "rest":
        # The path of the route, with the names of its parameters and not their values.
        path: object = getattr(scope.get("route"), "path", None)
        if isinstance(path, str):
            trace.action = f"{scope['method']} {path}"
        if failed and trace.code is None:
            trace.fail(code_of(status), None)
    elif failed and trace.action is None:
        # The MCP layer names the action of a request that it handled. A request that it refused has no method.
        trace.action = f"{scope['method']} {MCP_PATH}"
        if trace.code is None:
            trace.fail(code_of(status), "The MCP request is not valid.")
