from fastapi import APIRouter, Request, Response

from mcp_client_server.api.clientip import client_ip
from mcp_client_server.api.deps import AccountAuth, AppCode, ServicesDep
from mcp_client_server.ids import DeviceId, KeyId
from mcp_client_server.models import (
    Account,
    AccountCreate,
    AccountCreated,
    AccountPatch,
    AttestChallenge,
    ClientDeviceId,
    Device,
    DeviceInput,
    Key,
    KeyCreate,
    KeyCreated,
    KeyList,
    KeyPatch,
)

router = APIRouter(prefix="/v1")


@router.post("/attest/challenge", dependencies=[AppCode])
async def attest_challenge(request: Request, services: ServicesDep) -> AttestChallenge:
    await services.auth.limit_challenges(client_ip(request, behind_render=services.settings.render))
    return await services.integrity.new_challenge()


@router.post("/accounts", status_code=201, dependencies=[AppCode])
async def create_account(request: Request, services: ServicesDep, data: AccountCreate | None = None) -> AccountCreated:
    await services.auth.limit_account_creation(client_ip(request, behind_render=services.settings.render))
    attested = await services.integrity.attested_key(data.attestation if data else None)
    return await services.accounts.create(attested)


@router.get("/account")
async def get_account(principal: AccountAuth, services: ServicesDep) -> Account:
    return await services.accounts.get(principal)


@router.patch("/account")
async def update_account(patch: AccountPatch, principal: AccountAuth, services: ServicesDep) -> Account:
    return await services.accounts.update(principal, patch)


@router.delete("/account", status_code=204, response_class=Response)
async def delete_account(principal: AccountAuth, services: ServicesDep) -> None:
    await services.accounts.delete(principal)


@router.put("/devices/{device_id}")
async def put_device(
    device_id: ClientDeviceId, data: DeviceInput, principal: AccountAuth, services: ServicesDep
) -> Device:
    return await services.devices.put(principal, device_id, data)


@router.delete("/devices/{device_id}", status_code=204, response_class=Response)
async def delete_device(device_id: DeviceId, principal: AccountAuth, services: ServicesDep) -> None:
    await services.devices.delete(principal, device_id)


@router.get("/keys")
async def list_keys(principal: AccountAuth, services: ServicesDep) -> KeyList:
    return KeyList(keys=await services.keys.all(principal))


@router.post("/keys", status_code=201)
async def create_key(data: KeyCreate, principal: AccountAuth, services: ServicesDep) -> KeyCreated:
    return await services.keys.create(principal, data)


@router.patch("/keys/{key_id}")
async def rename_key(key_id: KeyId, patch: KeyPatch, principal: AccountAuth, services: ServicesDep) -> Key:
    return await services.keys.rename(principal, key_id, patch)


@router.delete("/keys/{key_id}", status_code=204, response_class=Response)
async def delete_key(key_id: KeyId, principal: AccountAuth, services: ServicesDep) -> None:
    await services.keys.delete(principal, key_id)
