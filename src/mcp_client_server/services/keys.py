from typing import assert_never

from mcp_client_server.clock import Clock
from mcp_client_server.credentials import API_KEY_PREFIX, AccountPrincipal, AgentPrincipal, hash_secret, new_secret
from mcp_client_server.errors import LimitReached, NotFound
from mcp_client_server.events import frames
from mcp_client_server.ids import IdSource, KeyId
from mcp_client_server.limits import LAST_USED_WRITE_INTERVAL_SECONDS, Limits
from mcp_client_server.models import Agent, ClientInfo, Key, KeyCreate, KeyCreated, KeyPatch
from mcp_client_server.push import PushDispatcher
from mcp_client_server.services.purge import KeyPurger
from mcp_client_server.store import Store

_CLIENT_CACHE_MAX = 10_000
_CLIENT_FIELD_MAX = 100


class KeyService:
    def __init__(
        self, store: Store, push: PushDispatcher, purger: KeyPurger, clock: Clock, ids: IdSource, limits: Limits
    ) -> None:
        self._store = store
        self._push = push
        self._purger = purger
        self._clock = clock
        self._ids = ids
        self._limits = limits
        # Only a write throttle. The store decides if the client is a new one, so each worker gives the same result.
        self._recent_clients: dict[KeyId, tuple[ClientInfo, float]] = {}

    async def all(self, principal: AccountPrincipal) -> list[Key]:
        return await self._store.list_keys(principal.account_id)

    async def agents(self, principal: AgentPrincipal) -> list[Agent]:
        """The keys of the account as an agent sees them: who it can send a message to."""
        return [
            Agent(
                key_id=key.id,
                label=key.label,
                is_self=key.id == principal.key_id,
                last_client=key.last_client,
                last_used_at=key.last_used_at,
            )
            for key in await self._store.list_keys(principal.account_id)
        ]

    async def create(self, principal: AccountPrincipal, data: KeyCreate) -> KeyCreated:
        secret = new_secret(API_KEY_PREFIX)
        key = Key(
            id=self._ids.key_id(), label=data.label, prefix=secret[:8], last4=secret[-4:], created_at=self._clock.now()
        )
        status = await self._store.create_key(principal.account_id, key, hash_secret(secret), frames.key_created(key))
        match status:
            case "ok":
                return KeyCreated(key=key, secret=secret)
            case "limit":
                raise LimitReached(
                    f"An account can have {self._limits.max_keys_per_account} keys at most. Delete a key first."
                )
            case "no_account":
                raise NotFound("Account not found.")
            case _:
                assert_never(status)

    async def rename(self, principal: AccountPrincipal, key_id: KeyId, patch: KeyPatch) -> Key:
        key = await self._store.set_key_label(principal.account_id, key_id, patch.label)
        if key is None:
            raise NotFound("Key not found.")
        await self._store.publish(principal.account_id, frames.key_updated(key))
        return key

    async def delete(self, principal: AccountPrincipal, key_id: KeyId) -> None:
        """The key is unusable after the first step. Its data goes in bounded steps; a retry finishes them."""
        at = self._clock.now()
        if not await self._store.detach_key(principal.account_id, key_id, at, frames.key_deleted(key_id)):
            raise NotFound("Key not found.")
        self._recent_clients.pop(key_id, None)
        await self._purger.purge(key_id, principal.account_id)

    async def record_client(self, principal: AgentPrincipal, name: str, version: str) -> None:
        """Keep `last_client` of the key. The store gets one write each minute at most for an unchanged client."""
        if not name:
            return
        key_id = principal.key_id
        client = ClientInfo(name=name[:_CLIENT_FIELD_MAX], version=version[:_CLIENT_FIELD_MAX])
        recent = self._recent_clients.get(key_id)
        at = self._clock.monotonic()
        if recent and recent[0] == client and at - recent[1] < LAST_USED_WRITE_INTERVAL_SECONDS:
            return
        if len(self._recent_clients) >= _CLIENT_CACHE_MAX:
            self._recent_clients.clear()
        self._recent_clients[key_id] = (client, at)
        changed = await self._store.set_key_client(principal.account_id, key_id, client)
        if changed:
            await self._store.publish(principal.account_id, frames.key_updated(changed))
