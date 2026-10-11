import logging
from dataclasses import dataclass
from typing import Protocol

import httpx2
import jwt
from pydantic import BaseModel, ValidationError

from mcp_client_server.clock import Clock
from mcp_client_server.config import ApnsConfig
from mcp_client_server.models import ApnsEnvironment
from mcp_client_server.push.payloads import PushRequest, encode

logger = logging.getLogger(__name__)

HOSTS: dict[ApnsEnvironment, str] = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
# APNs rejects a provider token that is older than one hour.
_JWT_LIFETIME_SECONDS = 50 * 60
_OK = 200
_GONE = 410
_FORBIDDEN = 403


@dataclass(frozen=True, slots=True)
class PushResult:
    status: int
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == _OK

    @property
    def token_is_dead(self) -> bool:
        return self.status == _GONE or self.reason == "BadDeviceToken"


class PushSender(Protocol):
    async def send(self, request: PushRequest) -> PushResult: ...

    async def aclose(self) -> None: ...


class _ErrorBody(BaseModel):
    reason: str | None = None


class _ProviderToken:
    """The ES256 token that authenticates this server to APNs. PyJWT is used only here."""

    def __init__(self, config: ApnsConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._signed: tuple[str, float] | None = None

    def current(self) -> str:
        now = self._clock.now()
        if self._signed and now - self._signed[1] < _JWT_LIFETIME_SECONDS:
            return self._signed[0]
        token = jwt.encode(
            {"iss": self._config.team_id, "iat": int(now)},
            self._config.private_key,
            algorithm="ES256",
            headers={"kid": self._config.key_id},
        )
        self._signed = (token, now)
        return token

    def discard(self) -> None:
        self._signed = None


class ApnsClient:
    """APNs over HTTP/2 with token (JWT) authentication."""

    def __init__(self, config: ApnsConfig, clock: Clock, http: httpx2.AsyncClient | None = None) -> None:
        self._http = http or httpx2.AsyncClient(http2=True, timeout=httpx2.Timeout(10.0))
        self._clock = clock
        self._token = _ProviderToken(config, clock)
        # A key that cannot sign must stop the startup, not each push.
        self._token.current()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, request: PushRequest) -> dict[str, str]:
        headers = {
            "authorization": f"bearer {self._token.current()}",
            "apns-topic": request.topic,
            "apns-push-type": request.push_type,
            "apns-expiration": str(int(self._clock.now()) + request.ttl_seconds),
        }
        if request.priority is not None:
            headers["apns-priority"] = str(request.priority)
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
            except httpx2.TransportError as exc:
                # APNs closes idle connections. One more try opens a new one.
                result = PushResult(0, type(exc).__name__)
                continue
            result = PushResult(response.status_code, _reason(response))
            if result.status == _FORBIDDEN and result.reason == "ExpiredProviderToken":
                self._token.discard()
                continue
            break
        return result


def _reason(response: httpx2.Response) -> str | None:
    if response.status_code == _OK:
        return None
    try:
        return _ErrorBody.model_validate_json(response.content).reason
    except ValidationError:
        return None
