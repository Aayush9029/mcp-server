from fastapi import APIRouter, Request, Response

from mcp_client_server.api.clientip import client_ip
from mcp_client_server.api.deps import AccountPrincipal, ServicesDep
from mcp_client_server.models import (
    Account,
    AccountCreated,
    AccountPatch,
    Device,
    DeviceId,
    DeviceInput,
    EmptyBody,
    Key,
    KeyCreate,
    KeyCreated,
    KeyList,
    KeyPatch,
)

router = APIRouter(prefix="/v1")


@router.post("/accounts", status_code=201)
async def create_account(request: Request, services: ServicesDep, _: EmptyBody | None = None) -> AccountCreated:
    await services.auth.limit_account_creation(client_ip(request, services.settings.render))
    return await services.accounts.create()


@router.get("/account")
async def get_account(principal: AccountPrincipal, services: ServicesDep) -> Account:
    return await services.accounts.get(principal)


@router.patch("/account")
async def update_account(patch: AccountPatch, principal: AccountPrincipal, services: ServicesDep) -> Account:
    return await services.accounts.update(principal, patch)


@router.delete("/account", status_code=204, response_class=Response)
async def delete_account(principal: AccountPrincipal, services: ServicesDep) -> None:
    await services.accounts.delete(principal)


@router.put("/devices/{device_id}")
async def put_device(
    device_id: DeviceId, data: DeviceInput, principal: AccountPrincipal, services: ServicesDep
) -> Device:
    return await services.devices.put(principal, device_id, data)


@router.delete("/devices/{device_id}", status_code=204, response_class=Response)
async def delete_device(device_id: str, principal: AccountPrincipal, services: ServicesDep) -> None:
    await services.devices.delete(principal, device_id)


@router.get("/keys")
async def list_keys(principal: AccountPrincipal, services: ServicesDep) -> KeyList:
    return KeyList(keys=await services.keys.all(principal))


@router.post("/keys", status_code=201)
async def create_key(data: KeyCreate, principal: AccountPrincipal, services: ServicesDep) -> KeyCreated:
    return await services.keys.create(principal, data)


@router.patch("/keys/{key_id}")
async def rename_key(key_id: str, patch: KeyPatch, principal: AccountPrincipal, services: ServicesDep) -> Key:
    return await services.keys.rename(principal, key_id, patch)


@router.delete("/keys/{key_id}", status_code=204, response_class=Response)
async def delete_key(key_id: str, principal: AccountPrincipal, services: ServicesDep) -> None:
    await services.keys.delete(principal, key_id)
