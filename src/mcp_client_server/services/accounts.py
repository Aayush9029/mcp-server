from mcp_client_server.credentials import ACCOUNT_TOKEN_PREFIX, Principal, hash_secret, new_secret
from mcp_client_server.errors import LimitReached, NotFound
from mcp_client_server.events import frames
from mcp_client_server.ids import new_id, now
from mcp_client_server.limits import MAX_DEVICES_PER_ACCOUNT
from mcp_client_server.models import Account, AccountCreated, AccountPatch, Device, DeviceInput
from mcp_client_server.store import Store


class AccountService:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def create(self) -> AccountCreated:
        account = Account(id=new_id("acc"), created_at=now())
        token = new_secret(ACCOUNT_TOKEN_PREFIX)
        await self._store.create_account(account, hash_secret(token))
        return AccountCreated(account=account, token=token)

    async def get(self, principal: Principal) -> Account:
        account = await self._store.get_account(principal.account_id)
        if account is None:
            raise NotFound("Account not found.")
        return account

    async def update(self, principal: Principal, patch: AccountPatch) -> Account:
        account = await self._store.set_live_activities(principal.account_id, patch.settings.live_activities)
        if account is None:
            raise NotFound("Account not found.")
        return account

    async def delete(self, principal: Principal) -> None:
        await self._store.delete_account(principal.account_id, frames.account_deleted())


class DeviceService:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def put(self, principal: Principal, device_id: str, data: DeviceInput) -> Device:
        device = Device(id=device_id, updated_at=now(), **data.model_dump())
        status = await self._store.put_device(principal.account_id, device)
        if status == "limit":
            raise LimitReached(f"An account can have {MAX_DEVICES_PER_ACCOUNT} devices at most.")
        if status != "ok":
            raise NotFound("Account not found.")
        return device

    async def delete(self, principal: Principal, device_id: str) -> None:
        if not await self._store.delete_device(principal.account_id, device_id):
            raise NotFound("Device not found.")
