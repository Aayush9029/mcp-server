from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from starlette.requests import Request

from mcp_client_server import limits
from mcp_client_server.api.clientip import client_ip
from mcp_client_server.config import Settings
from mcp_client_server.credentials import hash_secret
from mcp_client_server.main import create_app
from tests.conftest import DEVICE_A, DEVICE_B, Api, Connect, FakePush, key_names, running


def error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()["error"]
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    return body


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok", "store": "ok"})


async def test_health_is_503_if_the_store_does_not_answer(settings: Settings, push: FakePush) -> None:
    dead = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True, socket_connect_timeout=0.2)
    app = create_app(settings, redis=dead, event_redis=dead, push_sender=push)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/health")
    assert response.status_code == 503
    assert response.json()["store"] != "ok"


async def test_unknown_route_and_method_use_the_error_shape(client: httpx.AsyncClient) -> None:
    error(await client.get("/v1/nothing"), 404, "not_found")
    error(await client.put("/v1/accounts"), 405, "method_not_allowed")


async def test_create_account_returns_the_token_once_and_stores_only_its_hash(
    client: httpx.AsyncClient, redis: Redis
) -> None:
    response = await client.post("/v1/accounts", json={})
    assert response.status_code == 201
    body = response.json()
    account, token = body["account"], body["token"]
    assert account["id"].startswith("acc_")
    assert account["settings"] == {"live_activities": True}
    assert isinstance(account["created_at"], float)
    assert token.startswith("mca_") and len(token) >= 47

    stored = {name: await redis.type(name) for name in await redis.keys("t:*")}
    assert f"t:cred:{hash_secret(token)}" in stored
    values = [await redis.get(name) for name, kind in stored.items() if kind == "string"]
    hashes = [await redis.hgetall(name) for name, kind in stored.items() if kind == "hash"]
    assert token not in str(list(stored)) + str(values) + str(hashes)

    fetched = await client.get("/v1/account", headers={"Authorization": f"Bearer {token}"})
    assert fetched.json() == account


async def test_create_account_accepts_no_body_and_rejects_unknown_fields(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/accounts")).status_code == 201
    body = error(await client.post("/v1/accounts", json={"email": "a@b.c"}), 422, "validation_failed")
    assert "email" in body["fields"]


async def test_account_routes_need_the_account_token(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    for method, path in [
        ("GET", "/v1/account"),
        ("PATCH", "/v1/account"),
        ("DELETE", "/v1/account"),
        ("GET", "/v1/keys"),
        ("POST", "/v1/keys"),
        ("GET", "/v1/events"),
    ]:
        missing = await client.request(method, path)
        assert error(missing, 401, "unauthorized") and missing.headers["www-authenticate"] == "Bearer"
        error(await client.request(method, path, headers={"Authorization": "Bearer mca_wrong"}), 401, "unauthorized")
        error(await client.request(method, path, headers={"Authorization": "Basic abc"}), 401, "unauthorized")
        error(await client.request(method, path, headers=key), 403, "forbidden")


async def test_patch_account_settings(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    response = await client.patch("/v1/account", json={"settings": {"live_activities": False}}, headers=account)
    assert response.status_code == 200
    assert response.json()["settings"] == {"live_activities": False}
    assert (await client.get("/v1/account", headers=account)).json()["settings"] == {"live_activities": False}

    for bad in ({}, {"settings": {}}, {"settings": {"live_activities": "no"}}, {"settings": {"x": 1}}, {"name": "x"}):
        error(await client.patch("/v1/account", json=bad, headers=account), 422, "validation_failed")
    error(
        await client.patch(
            "/v1/account", content=b"{not json", headers={**account, "Content-Type": "application/json"}
        ),
        400,
        "bad_request",
    )


async def test_delete_account_removes_devices_keys_and_tasks(client: httpx.AsyncClient, api: Api, redis: Redis) -> None:
    other = await api.account()
    account = await api.account()
    key, _ = await api.key(account)
    await api.task(key)
    await api.device(account, DEVICE_A)

    assert (await client.delete("/v1/account", headers=account)).status_code == 204
    error(await client.get("/v1/account", headers=account), 401, "unauthorized")
    error(await client.get("/v1/tasks", headers=key), 401, "unauthorized")
    assert (await client.get("/v1/account", headers=other)).status_code == 200
    names = [name for name in await key_names(redis) if ":rl:" not in name]
    assert sorted(name.split(":")[1] for name in names) == ["acc", "cred"]


async def test_account_creation_has_a_strict_limit_for_each_ip(
    settings: Settings, connect: Connect, redis: Redis, push: FakePush
) -> None:
    limited = settings.model_copy(update={"rate_limit_accounts_per_hour": 2})
    app = create_app(limited, redis=connect(), event_redis=connect(), push_sender=push)
    async with running(app) as http:
        assert (await http.post("/v1/accounts", json={})).status_code == 201
        assert (await http.post("/v1/accounts", json={})).status_code == 201
        # A proxy header from the caller does not give a new bucket when the server is not on Render.
        spoofed = {"X-Forwarded-For": "203.0.113.9", "CF-Connecting-IP": "203.0.113.9"}
        response = await http.post("/v1/accounts", json={}, headers=spoofed)
        error(response, 429, "rate_limited")
        assert 0 < int(response.headers["retry-after"]) <= 3600


async def test_credential_rate_limit(settings: Settings, connect: Connect, redis: Redis, push: FakePush) -> None:
    limited = settings.model_copy(update={"rate_limit_per_minute": 3})
    app = create_app(limited, redis=connect(), event_redis=connect(), push_sender=push)
    async with running(app) as http:
        api = Api(http)
        account = await api.account()
        other = await api.account()
        codes = [(await http.get("/v1/account", headers=account)).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        limited_response = await http.get("/v1/account", headers=account)
        error(limited_response, 429, "rate_limited")
        assert 0 < int(limited_response.headers["retry-after"]) <= 60
        assert (await http.get("/v1/account", headers=other)).status_code == 200


def test_client_ip_trusts_proxy_headers_only_on_render() -> None:
    def request(headers: dict[str, str]) -> Request:
        raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("10.0.0.7", 1234)})

    spoof = {"X-Forwarded-For": "1.1.1.1, 198.51.100.4, 172.16.0.1", "CF-Connecting-IP": "198.51.100.4"}
    assert client_ip(request(spoof), behind_render=False) == "10.0.0.7"
    assert client_ip(request(spoof), behind_render=True) == "198.51.100.4"
    assert client_ip(request({"X-Forwarded-For": "1.1.1.1, 198.51.100.4"}), behind_render=True) == "198.51.100.4"
    assert client_ip(request({}), behind_render=True) == "10.0.0.7"


async def test_keys_create_list_rename_delete(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    created = await client.post("/v1/keys", json={"label": "  Claude   Code "}, headers=account)
    assert created.status_code == 201
    key, secret = created.json()["key"], created.json()["secret"]
    assert key["id"].startswith("key_") and key["label"] == "Claude Code"
    assert secret.startswith("mck_") and key["prefix"] == secret[:8] and key["last4"] == secret[-4:]
    assert key["counts"] == {"todo": 0, "in_progress": 0, "done": 0, "cancelled": 0}
    assert key["last_used_at"] is None and key["last_client"] is None

    _, second = await api.key(account, "Cursor")
    listed = (await client.get("/v1/keys", headers=account)).json()["keys"]
    assert [item["id"] for item in listed] == [second["id"], key["id"]]
    assert "secret" not in str(listed) and secret not in str(listed)

    renamed = await client.patch(f"/v1/keys/{key['id']}", json={"label": "Cursor (work)"}, headers=account)
    assert renamed.status_code == 200 and renamed.json()["label"] == "Cursor (work)"

    agent = {"Authorization": f"Bearer {secret}"}
    task = await api.task(agent)
    used = (await client.get("/v1/keys", headers=account)).json()["keys"][1]
    assert used["counts"]["todo"] == 1 and isinstance(used["last_used_at"], float)

    assert (await client.delete(f"/v1/keys/{key['id']}", headers=account)).status_code == 204
    error(await client.get("/v1/tasks", headers=agent), 401, "unauthorized")
    error(await client.get(f"/v1/tasks/{task['id']}", headers=account), 404, "not_found")
    error(await client.delete(f"/v1/keys/{key['id']}", headers=account), 404, "not_found")
    assert len((await client.get("/v1/keys", headers=account)).json()["keys"]) == 1


async def test_key_validation_and_isolation(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    stranger = await api.account()
    _, key = await api.key(account)
    for bad in ({}, {"label": ""}, {"label": "   "}, {"label": "x" * 41}, {"label": 5}, {"label": "ok", "extra": 1}):
        body = error(await client.post("/v1/keys", json=bad, headers=account), 422, "validation_failed")
        assert body["fields"]
        error(await client.patch(f"/v1/keys/{key['id']}", json=bad, headers=account), 422, "validation_failed")
    error(await client.patch(f"/v1/keys/{key['id']}", json={"label": "Mine"}, headers=stranger), 404, "not_found")
    error(await client.delete(f"/v1/keys/{key['id']}", headers=stranger), 404, "not_found")
    assert (await client.get("/v1/keys", headers=stranger)).json() == {"keys": []}
    assert (await client.get("/v1/keys", headers=account)).json()["keys"][0]["label"] == "Claude Code"


async def test_key_limit(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    for number in range(limits.MAX_KEYS_PER_ACCOUNT):
        await api.key(account, f"Key {number}")
    error(await client.post("/v1/keys", json={"label": "One more"}, headers=account), 409, "conflict")
    assert len((await client.get("/v1/keys", headers=account)).json()["keys"]) == limits.MAX_KEYS_PER_ACCOUNT


async def test_put_device_creates_and_replaces(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    device = await api.device(account, DEVICE_A, apns_token="AABBCCDD" * 8)
    assert device["id"] == DEVICE_A
    assert device["apns_token"] == "aabbccdd" * 8
    assert device["platform"] == "ios" and isinstance(device["updated_at"], float)

    replaced = await api.device(account, DEVICE_A, platform="ipados", apns_token=None, push_to_start_token=None)
    assert (replaced["platform"], replaced["apns_token"], replaced["push_to_start_token"]) == ("ipados", None, None)

    assert (await client.delete(f"/v1/devices/{DEVICE_A}", headers=account)).status_code == 204
    error(await client.delete(f"/v1/devices/{DEVICE_A}", headers=account), 404, "not_found")


async def test_device_validation_and_isolation(
    client: httpx.AsyncClient, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    account = await api.account()
    stranger = await api.account()
    key, _ = await api.key(account)
    valid = {
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "27.0",
        "app_version": "1.0 (1)",
        "apns_environment": "sandbox",
        "apns_token": None,
        "push_to_start_token": None,
    }

    body = error(await client.put("/v1/devices/phone-1", json=valid, headers=account), 422, "validation_failed")
    assert "device_id" in body["fields"]
    for change in (
        {"platform": "android"},
        {"apns_environment": "dev"},
        {"apns_token": "not-hex"},
        {"model": ""},
        {"extra": True},
    ):
        error(
            await client.put(f"/v1/devices/{DEVICE_A}", json={**valid, **change}, headers=account),
            422,
            "validation_failed",
        )
    error(await client.put(f"/v1/devices/{DEVICE_A}", json=valid, headers=key), 403, "forbidden")

    await api.device(account, DEVICE_A)
    error(await client.delete(f"/v1/devices/{DEVICE_A}", headers=stranger), 404, "not_found")

    monkeypatch.setattr(limits, "MAX_DEVICES_PER_ACCOUNT", 1)
    error(await client.put(f"/v1/devices/{DEVICE_B}", json=valid, headers=account), 409, "conflict")


async def test_live_activity_registration(client: httpx.AsyncClient, api: Api, app: FastAPI) -> None:
    account = await api.account()
    stranger = await api.account()
    key, key_info = await api.key(account)
    task = await api.task(key)
    await api.device(account, DEVICE_A)
    path = f"/v1/tasks/{task['id']}/live-activity"
    body = {"device_id": DEVICE_A, "push_token": "E5F6" * 16}

    assert (await client.put(path, json=body, headers=account)).status_code == 204
    context = await app.state.services.store.push_context(key_info["id"], key_info["id"], task["id"])
    assert context.activities == {DEVICE_A: "e5f6" * 16}

    error(await client.put(path, json=body, headers=key), 403, "forbidden")
    error(await client.put(path, json=body, headers=stranger), 404, "not_found")
    error(await client.put("/v1/tasks/tsk_missing/live-activity", json=body, headers=account), 404, "not_found")
    error(await client.put(path, json={**body, "device_id": DEVICE_B}, headers=account), 404, "not_found")
    error(await client.put(path, json={"device_id": DEVICE_A}, headers=account), 422, "validation_failed")
    error(await client.put(path, json={**body, "push_token": "zz"}, headers=account), 422, "validation_failed")

    removal = {"device_id": DEVICE_A}
    error(await client.request("DELETE", path, json=removal, headers=stranger), 404, "not_found")
    assert (await client.request("DELETE", path, json=removal, headers=account)).status_code == 204
    assert (await client.request("DELETE", path, json=removal, headers=account)).status_code == 204
    context = await app.state.services.store.push_context(key_info["id"], key_info["id"], task["id"])
    assert context.activities == {}
