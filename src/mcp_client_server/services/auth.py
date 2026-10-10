from mcp_client_server.config import Settings
from mcp_client_server.credentials import Principal, hash_secret, looks_like_credential
from mcp_client_server.errors import RateLimited, Unauthorized
from mcp_client_server.ids import now
from mcp_client_server.store import Store

_MINUTE = 60
_HOUR = 3600


class AuthService:
    def __init__(self, store: Store, settings: Settings) -> None:
        self._store = store
        self._settings = settings

    async def authenticate(self, secret: str | None) -> Principal:
        """Resolve a credential, count it against its rate limit, and note that the key was used."""
        if not secret or not looks_like_credential(secret):
            raise Unauthorized("Missing or invalid credential.")
        cred_hash = hash_secret(secret)
        at = now()
        result = await self._store.authenticate(
            cred_hash,
            now=at,
            rate_limit=self._settings.rate_limit_per_minute,
            rate_bucket=f"c:{cred_hash[:24]}:{int(at // _MINUTE)}",
            rate_ttl=2 * _MINUTE,
        )
        if result.status == "unknown":
            raise Unauthorized("Missing or invalid credential.")
        if result.status == "limited":
            raise RateLimited("Too many requests. Slow down.", _MINUTE - int(at % _MINUTE))
        return Principal(result.account_id, result.key_id)

    async def limit_account_creation(self, client_ip: str) -> None:
        limit = self._settings.rate_limit_accounts_per_hour
        if limit <= 0:
            return
        at = now()
        count = await self._store.hit(f"ip:{client_ip}:{int(at // _HOUR)}", 2 * _HOUR)
        if count > limit:
            raise RateLimited("Too many new accounts from this address.", _HOUR - int(at % _HOUR))
