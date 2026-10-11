from dataclasses import dataclass

from mcp_client_server.clock import Clock
from mcp_client_server.credentials import Principal, Secret, hash_secret, looks_like_credential
from mcp_client_server.errors import RateLimited, Unauthorized
from mcp_client_server.store import Store

_MINUTE = 60
_HOUR = 3600


@dataclass(frozen=True, slots=True)
class Identity:
    principal: Principal
    # Unread inbox items of the key at the time of the request. 0 for an account.
    inbox_unread: int


class AuthService:
    def __init__(self, store: Store, clock: Clock, per_minute: int, accounts_per_hour: int) -> None:
        self._store = store
        self._clock = clock
        self._per_minute = per_minute
        self._accounts_per_hour = accounts_per_hour

    async def authenticate(self, secret: Secret | None) -> Principal:
        return (await self.identify(secret)).principal

    async def identify(self, secret: Secret | None) -> "Identity":
        """Resolve a credential, count it against its rate limit, and note that the key was used."""
        if not secret or not looks_like_credential(secret):
            raise Unauthorized("Missing or invalid credential.")
        cred_hash = hash_secret(secret)
        at = self._clock.now()
        result = await self._store.authenticate(
            cred_hash,
            now=at,
            rate_limit=self._per_minute,
            rate_bucket=f"c:{cred_hash[:24]}:{int(at // _MINUTE)}",
            rate_ttl=2 * _MINUTE,
        )
        if result.principal is None:
            raise Unauthorized("Missing or invalid credential.")
        if result.limited:
            raise RateLimited("Too many requests. Slow down.", _MINUTE - int(at % _MINUTE))
        return Identity(result.principal, result.inbox_unread)

    async def limit_account_creation(self, client_ip: str) -> None:
        await self._limit_by_address("ip", client_ip, "Too many new accounts from this address.")

    async def limit_challenges(self, client_ip: str) -> None:
        """One account needs one challenge, so the two limits have the same number."""
        await self._limit_by_address("chal", client_ip, "Too many challenges from this address.")

    async def _limit_by_address(self, bucket: str, client_ip: str, message: str) -> None:
        if self._accounts_per_hour <= 0:
            return
        at = self._clock.now()
        count = await self._store.hit(f"{bucket}:{client_ip}:{int(at // _HOUR)}", 2 * _HOUR)
        if count > self._accounts_per_hour:
            raise RateLimited(message, _HOUR - int(at % _HOUR))
