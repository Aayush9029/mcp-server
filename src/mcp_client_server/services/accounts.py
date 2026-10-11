from typing import assert_never

from mcp_client_server.clock import Clock
from mcp_client_server.credentials import ACCOUNT_TOKEN_PREFIX, AccountPrincipal, hash_secret, new_secret
from mcp_client_server.errors import AttestationInvalid, LimitReached, NotFound
from mcp_client_server.events import frames
from mcp_client_server.ids import AccountId, DeviceId, IdSource, KeyId
from mcp_client_server.limits import Limits
from mcp_client_server.models import Account, AccountCreated, AccountPatch, Device, DeviceInput
from mcp_client_server.store import AttestedKey, Store


class AccountService:
    def __init__(self, store: Store, clock: Clock, ids: IdSource) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    async def create(self, attested: AttestedKey | None = None) -> AccountCreated:
        account = Account(id=self._ids.account_id(), created_at=self._clock.now())
        token = new_secret(ACCOUNT_TOKEN_PREFIX)
        if await self._store.create_account(account, hash_secret(token), attested) == "key_used":
            raise AttestationInvalid("This attested key has an account already.")
        return AccountCreated(account=account, token=token)

    async def get(self, principal: AccountPrincipal) -> Account:
        account = await self._store.get_account(principal.account_id)
        if account is None:
            raise NotFound("Account not found.")
        return account

    async def update(self, principal: AccountPrincipal, patch: AccountPatch) -> Account:
        account = await self._store.set_live_activities(principal.account_id, enabled=patch.settings.live_activities)
        if account is None:
            raise NotFound("Account not found.")
        return account

    async def delete(self, principal: AccountPrincipal) -> bool:
        """Delete the account with all its data. False if there is no such account."""
        return await self._store.delete_account(principal.account_id, frames.account_deleted())

    async def account_of_key(self, key_id: KeyId) -> AccountId | None:
        """The account that owns a key, for an operator who has only the key ID from a support request."""
        return await self._store.key_account(key_id)


class DeviceService:
    def __init__(self, store: Store, clock: Clock, limits: Limits) -> None:
        self._store = store
        self._clock = clock
        self._limits = limits

    async def put(self, principal: AccountPrincipal, device_id: DeviceId, data: DeviceInput) -> Device:
        device = Device(
            id=device_id,
            platform=data.platform,
            model=data.model,
            os_version=data.os_version,
            app_version=data.app_version,
            apns_environment=data.apns_environment,
            apns_token=data.apns_token,
            push_to_start_token=data.push_to_start_token,
            widget_push_token=data.widget_push_token,
            updated_at=self._clock.now(),
        )
        status = await self._store.put_device(principal.account_id, device)
        match status:
            case "ok":
                return device
            case "limit":
                raise LimitReached(f"An account can have {self._limits.max_devices_per_account} devices at most.")
            case "no_account":
                raise NotFound("Account not found.")
            case _:
                assert_never(status)

    async def delete(self, principal: AccountPrincipal, device_id: DeviceId) -> None:
        if not await self._store.delete_device(principal.account_id, device_id):
            raise NotFound("Device not found.")
