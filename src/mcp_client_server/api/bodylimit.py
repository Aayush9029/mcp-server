"""A cap on the size of a request body, so that one request cannot fill the memory of a worker."""

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_client_server.api.errors import error_response
from mcp_client_server.limits import MAX_BODY_BYTES

_TOO_LARGE = 413


class BodyLimit:
    """Refuses a body above `max_bytes` with 413. A body with no Content-Length is counted as it comes."""

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    @property
    def _message(self) -> str:
        return f"The body is larger than {self._max_bytes // 1024} KB."

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        length = Headers(scope=scope).get("content-length", "")
        if length.isdigit() and int(length) > self._max_bytes:
            # The body is not read. The server closes the connection after the response.
            await error_response(_TOO_LARGE, "bad_request", self._message)(scope, receive, send)
            return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    # The route reads the body, so the error comes out of that read and gets the usual handlers.
                    raise HTTPException(_TOO_LARGE, self._message)
            return message

        await self._app(scope, limited, send)
