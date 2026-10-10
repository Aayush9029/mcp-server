import time

from mcp_client_server.credentials import API_KEY_PREFIX, Principal, hash_secret, new_secret
from mcp_client_server.errors import LimitReached, NotFound
from mcp_client_server.events import frames
from mcp_client_server.ids import new_id, now
from mcp_client_server.limits import LAST_USED_WRITE_INTERVAL_SECONDS, MAX_KEYS_PER_ACCOUNT
from mcp_client_server.models import ClientInfo, Key, KeyCreate, KeyCreated, KeyPatch
from mcp_client_server.push import PushDispatcher
from mcp_client_server.store import Store

_CLIENT_CACHE_MAX = 10_000
_CLIENT_FIELD_MAX = 100


class KeyService:
    def __init__(self, store: Store, push: PushDispatcher) -> None:
        self._store = store
        self._push = push
        self._clients: dict[str, tuple[ClientInfo, float]] = {}

    async def all(self, principal: Principal) -> list[Key]:
        return await self._store.list_keys(principal.account_id)

    async def create(self, principal: Principal, data: KeyCreate) -> KeyCreated:
        secret = new_secret(API_KEY_PREFIX)
        key = Key(id=new_id("key"), label=data.label, prefix=secret[:8], last4=secret[-4:], created_at=now())
        status = await self._store.create_key(
            principal.account_id, key, hash_secret(secret), frames.key_event("created", key)
        )
        if status == "limit":
            raise LimitReached(f"An account can have {MAX_KEYS_PER_ACCOUNT} keys at most. Delete a key first.")
        if status != "ok":
            raise NotFound("Account not found.")
        return KeyCreated(key=key, secret=secret)

    async def rename(self, principal: Principal, key_id: str, patch: KeyPatch) -> Key:
        key = await self._store.set_key_label(principal.account_id, key_id, patch.label)
        if key is None:
            raise NotFound("Key not found.")
        await self._store.publish(principal.account_id, frames.key_event("updated", key))
        return key

    async def delete(self, principal: Principal, key_id: str) -> None:
        ended = await self._store.delete_key(principal.account_id, key_id, frames.key_deleted(key_id))
        if ended is None:
            raise NotFound("Key not found.")
        self._clients.pop(key_id, None)
        self._push.activities_ended(principal.account_id, key_id, ended)

    async def record_client(self, principal: Principal, name: str, version: str) -> None:
        """Keep `last_client` of the key. The store gets one write each minute at most for an unchanged client."""
        key_id = principal.key_id
        if key_id is None or not name:
            return
        client = ClientInfo(name=name[:_CLIENT_FIELD_MAX], version=version[:_CLIENT_FIELD_MAX])
        cached = self._clients.get(key_id)
        at = time.monotonic()
        is_new = cached is None or cached[0] != client
        if cached and not is_new and at - cached[1] < LAST_USED_WRITE_INTERVAL_SECONDS:
            return
        if len(self._clients) >= _CLIENT_CACHE_MAX:
            self._clients.clear()
        self._clients[key_id] = (client, at)
        key = await self._store.set_key_client(principal.account_id, key_id, client)
        if key and is_new:
            await self._store.publish(principal.account_id, frames.key_event("updated", key))
