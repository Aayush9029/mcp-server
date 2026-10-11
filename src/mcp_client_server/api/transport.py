"""Headers about the transport. HSTS goes on each response of a request that came over HTTPS."""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HSTS = "max-age=31536000; includeSubDomains"


class StrictTransport:
    """Adds `Strict-Transport-Security` to a response over HTTPS.

    Behind Render, the proxy ends the TLS connection and says `X-Forwarded-Proto: https`. That header is trusted
    only there, under the same rule as the client address in api/clientip.py.
    """

    def __init__(self, app: ASGIApp, *, behind_render: bool) -> None:
        self._app = app
        self._behind_render = behind_render

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._secure(scope):
            await self._app(scope, receive, send)
            return

        async def with_hsts(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Strict-Transport-Security"] = HSTS
            await send(message)

        await self._app(scope, receive, with_hsts)

    def _secure(self, scope: Scope) -> bool:
        if scope.get("scheme") == "https":
            return True
        if not self._behind_render:
            return False
        return Headers(scope=scope).get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
