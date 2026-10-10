import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
import jwt

from mcp_client_server.push.payloads import PushRequest, encode

logger = logging.getLogger(__name__)

HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
# APNs rejects a provider token that is older than one hour.
_JWT_LIFETIME_SECONDS = 50 * 60


@dataclass(frozen=True, slots=True)
class PushResult:
    status: int
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def token_is_dead(self) -> bool:
        return self.status == 410 or self.reason == "BadDeviceToken"


class PushSender(Protocol):
    async def send(self, request: PushRequest) -> PushResult: ...

    async def aclose(self) -> None: ...


class ApnsClient:
    """APNs over HTTP/2 with token (JWT) authentication."""

    def __init__(
        self,
        *,
        private_key: str,
        key_id: str,
        team_id: str,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._private_key = private_key
        self._key_id = key_id
        self._team_id = team_id
        self._http = http or httpx.AsyncClient(http2=True, timeout=httpx.Timeout(10.0))
        self._clock = clock
        self._jwt: tuple[str, float] | None = None
        # A key that cannot sign must stop the startup, not each push.
        self._provider_token()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _provider_token(self) -> str:
        now = self._clock()
        if self._jwt and now - self._jwt[1] < _JWT_LIFETIME_SECONDS:
            return self._jwt[0]
        token = jwt.encode(
            {"iss": self._team_id, "iat": int(now)},
            self._private_key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )
        self._jwt = (token, now)
        return token

    def _headers(self, request: PushRequest) -> dict[str, str]:
        headers = {
            "authorization": f"bearer {self._provider_token()}",
            "apns-topic": request.topic,
            "apns-push-type": request.push_type,
            "apns-priority": str(request.priority),
            "apns-expiration": str(int(self._clock()) + request.ttl_seconds),
        }
        if request.collapse_id:
            headers["apns-collapse-id"] = request.collapse_id
        return headers

    async def send(self, request: PushRequest) -> PushResult:
        url = f"{HOSTS[request.environment]}/3/device/{request.token}"
        body = encode(request.payload)
        result = PushResult(0, "NotSent")
        for _ in range(2):
            try:
                response = await self._http.post(url, content=body, headers=self._headers(request))
            except httpx.TransportError as exc:
                # APNs closes idle connections. One more try opens a new one.
                result = PushResult(0, type(exc).__name__)
                continue
            result = PushResult(response.status_code, _reason(response))
            if result.status == 403 and result.reason == "ExpiredProviderToken":
                self._jwt = None
                continue
            break
        return result


def _reason(response: httpx.Response) -> str | None:
    if response.status_code == 200:
        return None
    try:
        return response.json().get("reason")
    except ValueError:
        return None
