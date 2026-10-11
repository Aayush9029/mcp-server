from mcp_client_server.ids import AccountId, KeyId
from mcp_client_server.limits import Limits
from mcp_client_server.push import PushDispatcher
from mcp_client_server.store import Store

_PENDING_BATCH = 100


class KeyPurger:
    """Deletes the data of a detached key in bounded steps. A step that did not run is run later."""

    def __init__(self, store: Store, push: PushDispatcher, limits: Limits) -> None:
        self._store = store
        self._push = push
        self._limits = limits

    async def purge(self, key_id: KeyId, account_id: AccountId | None = None) -> None:
        """Run the steps until the key has no data. With `account_id`, the Live Activities of its tasks end."""
        while True:
            done, ended = await self._store.purge_key(key_id, self._limits.purge_batch)
            if ended and account_id is not None:
                self._push.activities_ended(account_id, key_id, ended)
            if done:
                return

    async def finish_pending(self) -> int:
        """Finish the deletions that a lost process or a lost connection left behind. Returns how many."""
        pending = await self._store.keys_to_purge(_PENDING_BATCH)
        for key_id in pending:
            await self.purge(key_id)
        return len(pending)
