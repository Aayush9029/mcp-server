"""REST: health, accounts, keys, devices, and Live Activity registration."""

import httpx2
import pytest
from starlette.requests import Request

from mcp_client_server.api.clientip import client_ip
from mcp_client_server.credentials import Secret, hash_secret
from mcp_client_server.limits import Limits
from mcp_client_server.store import Store
from tests.conftest import Launch
from tests.support import DEVICE_A, DEVICE_B, Api, Json, RawStore, Running, error, secret_of

ACCOUNT_ROUTES = [
    ("GET", "/v1/account"),
    ("PATCH", "/v1/account"),
    ("DELETE", "/v1/account"),
    ("GET", "/v1/keys"),
    ("POST", "/v1/keys"),
    ("PATCH", "/v1/keys/key_1"),
    ("DELETE", "/v1/keys/key_1"),
    ("PUT", f"/v1/devices/{DEVICE_A}"),
    ("DELETE", f"/v1/devices/{DEVICE_A}"),
    ("PUT", "/v1/tasks/tsk_1/live-activity"),
    ("DELETE", "/v1/tasks/tsk_1/live-activity"),
    ("GET", "/v1/events"),
]


async def test_health(client: httpx2.AsyncClient) -> None:
    response = await client.get("/health")
    assert (response.status_code, response.json()) == (200, {"status": "ok", "store": "ok"})


async def test_head_health_answers_like_get(client: httpx2.AsyncClient) -> None:
    head = await client.head("/health")
    assert head.status_code == 200
    assert head.content == b""


async def test_hsts_goes_on_a_response_over_https_and_the_proxy_header_is_trusted_only_on_render(
    launch: Launch,
) -> None:
    hsts = "max-age=31536000; includeSubDomains"
    forwarded = {"X-Forwarded-Proto": "https"}
    async with launch(render=True) as running:
        assert (await running.http.get("/health", headers=forwarded)).headers["strict-transport-security"] == hsts
        assert "strict-transport-security" not in (await running.http.get("/health")).headers
        assert (await running.http.get("https://test/health")).headers["strict-transport-security"] == hsts
    async with launch() as running:
        assert "strict-transport-security" not in (await running.http.get("/health", headers=forwarded)).headers
        assert (await running.http.get("https://test/health")).headers["strict-transport-security"] == hsts
        _, key, _ = await running.api.agent()
        page = await running.http.get("https://test/v1/tasks", headers=key)
        assert page.headers["strict-transport-security"] == hsts
        assert (await running.http.get("https://test/docs")).headers["strict-transport-security"] == hsts


async def test_unknown_route_and_method_use_the_error_shape(client: httpx2.AsyncClient) -> None:
    assert error(await client.get("/v1/nothing"), 404, "not_found") == {"code": "not_found", "message": "Not found."}
    error(await client.put("/v1/accounts"), 405, "method_not_allowed")


async def test_create_account_returns_the_token_once_and_the_store_has_only_its_hash(
    client: httpx2.AsyncClient, raw: RawStore
) -> None:
    response = await client.post("/v1/accounts", json={})
    assert response.status_code == 201
    body = response.json()
    account, token = body["account"], body["token"]
    assert account["id"].startswith("acc_")
    assert account["settings"] == {"live_activities": True}
    assert isinstance(account["created_at"], float)
    assert token.startswith("mca_")
    assert len(token) >= 47

    stored = await raw.everything()
    assert f"t:cred:{hash_secret(Secret(token))}=a:{account['id']}" in stored
    assert token[4:] not in stored
    assert (await client.get("/v1/account", headers={"Authorization": f"Bearer {token}"})).json() == account


async def test_create_account_accepts_no_body_and_rejects_unknown_fields(client: httpx2.AsyncClient) -> None:
    assert (await client.post("/v1/accounts")).status_code == 201
    assert error(await client.post("/v1/accounts", json={"email": "a@b.c"}), 422, "validation_failed")["fields"] == {
        "email": "Extra inputs are not permitted"
    }


@pytest.mark.parametrize(("method", "path"), ACCOUNT_ROUTES)
async def test_account_routes_need_the_account_token(api: Api, method: str, path: str) -> None:
    account = await api.account()
    key, _ = await api.key(account)

    missing = await api.http.request(method, path)
    error(missing, 401, "unauthorized")
    assert missing.headers["www-authenticate"] == "Bearer"
    for wrong in (
        {"Authorization": "Bearer mca_wrong"},
        {"Authorization": "Basic abc"},
        {"X-API-Key": secret_of(account)},
    ):
        error(await api.http.request(method, path, headers=wrong), 401, "unauthorized")
    error(await api.http.request(method, path, headers=key), 403, "forbidden")


async def test_patch_account_settings(client: httpx2.AsyncClient, api: Api) -> None:
    account = await api.account()
    response = await client.patch("/v1/account", json={"settings": {"live_activities": False}}, headers=account)
    assert (response.status_code, response.json()["settings"]) == (200, {"live_activities": False})
    assert (await client.get("/v1/account", headers=account)).json()["settings"] == {"live_activities": False}

    bad: list[Json] = [
        {},
        {"settings": {}},
        {"settings": {"live_activities": "no"}},
        {"settings": {"x": 1}},
        {"name": 1},
    ]
    for body in bad:
        error(await client.patch("/v1/account", json=body, headers=account), 422, "validation_failed")
    not_json = await client.patch(
        "/v1/account", content=b"{not json", headers={**account, "Content-Type": "application/json"}
    )
    error(not_json, 400, "bad_request")


async def test_delete_account_removes_its_devices_keys_and_tasks_and_closes_its_credentials(
    client: httpx2.AsyncClient, api: Api, raw: RawStore
) -> None:
    other = await api.account()
    account, key, _ = await api.agent()
    await api.task(key)
    await api.device(account)

    assert (await client.delete("/v1/account", headers=account)).status_code == 204
    error(await client.get("/v1/account", headers=account), 401, "unauthorized")
    error(await client.get("/v1/tasks", headers=key), 401, "unauthorized")
    assert (await client.get("/v1/account", headers=other)).status_code == 200
    assert [name.split(":")[1] for name in await raw.keys()] == ["acc", "cred"]


async def test_account_creation_has_a_strict_limit_for_each_address(launch: Launch) -> None:
    async with launch(rate_limit_accounts_per_hour=2) as running:
        http = running.http
        assert [(await http.post("/v1/accounts", json={})).status_code for _ in range(2)] == [201, 201]
        # A proxy header from the caller does not give a new bucket when the server is not on Render.
        spoofed = {"X-Forwarded-For": "203.0.113.9", "CF-Connecting-IP": "203.0.113.9"}
        response = await http.post("/v1/accounts", json={}, headers=spoofed)
        error(response, 429, "rate_limited")
        assert 0 < int(response.headers["retry-after"]) <= 3600


async def test_credential_rate_limit_gives_429_with_retry_after(launch: Launch) -> None:
    async with launch(rate_limit_per_minute=3) as running:
        http, api = running.http, running.api
        account = await api.account()
        other = await api.account()
        assert [(await http.get("/v1/account", headers=account)).status_code for _ in range(4)] == [200, 200, 200, 429]
        limited = await http.get("/v1/account", headers=account)
        error(limited, 429, "rate_limited")
        assert 0 < int(limited.headers["retry-after"]) <= 60
        assert (await http.get("/v1/account", headers=other)).status_code == 200

        running.clock.advance(int(limited.headers["retry-after"]))
        assert (await http.get("/v1/account", headers=account)).status_code == 200


def test_client_ip_trusts_proxy_headers_only_on_render() -> None:
    def request(*headers: tuple[str, str]) -> Request:
        raw = [(name.lower().encode(), value.encode()) for name, value in headers]
        return Request({"type": "http", "headers": raw, "client": ("10.0.0.7", 1234)})

    spoof = request(("X-Forwarded-For", "1.1.1.1, 198.51.100.4, 172.16.0.1"), ("CF-Connecting-IP", "198.51.100.4"))
    assert client_ip(spoof, behind_render=False) == "10.0.0.7"
    assert client_ip(spoof, behind_render=True) == "198.51.100.4"
    assert client_ip(request(("X-Forwarded-For", "1.1.1.1, 198.51.100.4")), behind_render=True) == "198.51.100.4"
    assert client_ip(request(), behind_render=True) == "10.0.0.7"
    # The caller sent the header already. The address that the proxy adds is the last one, in the last header.
    twice = request(("X-Forwarded-For", "1.1.1.1"), ("X-Forwarded-For", "2.2.2.2, 198.51.100.4"))
    assert client_ip(twice, behind_render=True) == "198.51.100.4"
    appended = request(("X-Forwarded-For", "1.1.1.1,"), ("X-Forwarded-For", "198.51.100.4"))
    assert client_ip(appended, behind_render=True) == "198.51.100.4"
    assert client_ip(request(("X-Forwarded-For", " , ")), behind_render=True) == "10.0.0.7"


async def test_keys_create_list_rename_delete(client: httpx2.AsyncClient, api: Api) -> None:
    account = await api.account()
    created = await client.post("/v1/keys", json={"label": "  Claude   Code "}, headers=account)
    assert created.status_code == 201
    key, secret = created.json()["key"], created.json()["secret"]
    assert key == {
        "id": key["id"],
        "label": "Claude Code",
        "prefix": secret[:8],
        "last4": secret[-4:],
        "created_at": key["created_at"],
        "last_used_at": None,
        "last_client": None,
        "counts": {"todo": 0, "in_progress": 0, "done": 0, "cancelled": 0},
    }
    assert key["id"].startswith("key_")
    assert secret.startswith("mck_")

    _, second = await api.key(account, "Cursor")
    listed = (await client.get("/v1/keys", headers=account)).json()["keys"]
    assert [item["id"] for item in listed] == [second["id"], key["id"]]
    assert secret not in str(listed)

    renamed = await client.patch(f"/v1/keys/{key['id']}", json={"label": "Cursor (work)"}, headers=account)
    assert (renamed.status_code, renamed.json()["label"]) == (200, "Cursor (work)")

    agent = {"Authorization": f"Bearer {secret}"}
    task = await api.task(agent)
    used = (await client.get("/v1/keys", headers=account)).json()["keys"][1]
    assert used["counts"]["todo"] == 1
    assert isinstance(used["last_used_at"], float)

    assert (await client.delete(f"/v1/keys/{key['id']}", headers=account)).status_code == 204
    error(await client.get("/v1/tasks", headers=agent), 401, "unauthorized")
    error(await client.get(f"/v1/tasks/{task['id']}", headers=account), 404, "not_found")
    error(await client.delete(f"/v1/keys/{key['id']}", headers=account), 404, "not_found")
    assert [item["id"] for item in (await client.get("/v1/keys", headers=account)).json()["keys"]] == [second["id"]]


async def test_key_validation_and_isolation(client: httpx2.AsyncClient, api: Api) -> None:
    account = await api.account()
    stranger = await api.account()
    _, key = await api.key(account)
    path = f"/v1/keys/{key['id']}"
    bad: list[Json] = [
        {},
        {"label": ""},
        {"label": "   "},
        {"label": "x" * 41},
        {"label": 5},
        {"label": "ok", "extra": 1},
    ]
    for body in bad:
        assert error(await client.post("/v1/keys", json=body, headers=account), 422, "validation_failed")["fields"]
        error(await client.patch(path, json=body, headers=account), 422, "validation_failed")
    assert (await client.patch(path, json={"label": "x" * 40}, headers=account)).status_code == 200

    error(await client.patch(path, json={"label": "Mine"}, headers=stranger), 404, "not_found")
    error(await client.delete(path, headers=stranger), 404, "not_found")
    assert (await client.get("/v1/keys", headers=stranger)).json() == {"keys": []}
    assert (await client.get("/v1/keys", headers=account)).json()["keys"][0]["label"] == "x" * 40


async def test_key_limit_is_a_conflict(launch: Launch) -> None:
    async with launch(limits=Limits(max_keys_per_account=2)) as running:
        api = running.api
        account = await api.account()
        await api.key(account, "One")
        _, second = await api.key(account, "Two")
        error(await api.http.post("/v1/keys", json={"label": "Three"}, headers=account), 409, "conflict")

        await api.http.delete(f"/v1/keys/{second['id']}", headers=account)
        await api.key(account, "Three")


async def test_put_device_creates_and_replaces(client: httpx2.AsyncClient, api: Api) -> None:
    account = await api.account()
    device = await api.device(account, DEVICE_A, apns_token="AABBCCDD" * 8)
    assert device == {
        "id": DEVICE_A,
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "27.0",
        "app_version": "1.0 (1)",
        "apns_environment": "production",
        "apns_token": "aabbccdd" * 8,
        "push_to_start_token": "c3d4e5f6" * 8,
        "widget_push_token": None,
        "updated_at": device["updated_at"],
    }

    replaced = await client.put(
        f"/v1/devices/{DEVICE_A}",
        json={
            "platform": "ipados",
            "model": "iPad",
            "os_version": "27.0",
            "app_version": "1",
            "apns_environment": "sandbox",
        },
        headers=account,
    )
    assert (replaced.json()["platform"], replaced.json()["apns_token"], replaced.json()["push_to_start_token"]) == (
        "ipados",
        None,
        None,
    )
    assert replaced.json()["updated_at"] > device["updated_at"]

    assert (await client.delete(f"/v1/devices/{DEVICE_A}", headers=account)).status_code == 204
    error(await client.delete(f"/v1/devices/{DEVICE_A}", headers=account), 404, "not_found")


async def test_device_validation_isolation_and_limit(launch: Launch) -> None:
    async with launch(limits=Limits(max_devices_per_account=1)) as running:
        api, client = running.api, running.http
        account = await api.account()
        stranger = await api.account()
        valid: Json = {
            "platform": "ios",
            "model": "iPhone18,1",
            "os_version": "27.0",
            "app_version": "1.0 (1)",
            "apns_environment": "sandbox",
            "apns_token": None,
            "push_to_start_token": None,
        }
        path = f"/v1/devices/{DEVICE_A}"

        bad_id = error(await client.put("/v1/devices/phone-1", json=valid, headers=account), 422, "validation_failed")
        assert list(bad_id["fields"]) == ["device_id"]
        changes: list[Json] = [
            {"platform": "android"},
            {"apns_environment": "dev"},
            {"apns_token": "not-hex"},
            {"apns_token": "abc"},
            {"model": ""},
            {"os_version": "x" * 101},
            {"extra": True},
        ]
        for change in changes:
            problem = error(await client.put(path, json={**valid, **change}, headers=account), 422, "validation_failed")
            assert list(problem["fields"]) == list(change)

        await api.device(account, DEVICE_A)
        error(await client.delete(path, headers=stranger), 404, "not_found")
        error(await client.put(f"/v1/devices/{DEVICE_B}", json=valid, headers=account), 409, "conflict")
        assert (await client.put(path, json=valid, headers=account)).status_code == 200


async def test_live_activity_registration(running: Running, store: Store) -> None:
    api, client = running.api, running.http
    account, key, key_info = await api.agent()
    stranger = await api.account()
    account_id = await api.account_id(account)
    task = await api.task(key)
    await api.device(account, DEVICE_A)
    path = f"/v1/tasks/{task['id']}/live-activity"
    body: Json = {"device_id": DEVICE_A, "push_token": "E5F6" * 16}

    assert (await client.put(path, json=body, headers=account)).status_code == 204
    assert (await store.push_context(account_id, key_info["id"], task["id"])).activities == {DEVICE_A: "e5f6" * 16}

    error(await client.put(path, json=body, headers=stranger), 404, "not_found")
    error(await client.put("/v1/tasks/tsk_missing/live-activity", json=body, headers=account), 404, "not_found")
    error(await client.put(path, json={**body, "device_id": DEVICE_B}, headers=account), 404, "not_found")
    for bad in (
        {"device_id": DEVICE_A},
        {**body, "push_token": "zz"},
        {**body, "device_id": "phone"},
        {**body, "x": 1},
    ):
        error(await client.put(path, json=bad, headers=account), 422, "validation_failed")

    removal = {"device_id": DEVICE_A}
    error(await client.request("DELETE", path, json=removal, headers=stranger), 404, "not_found")
    # DELETE gives 204 also when the device has no registered activity.
    for _ in range(2):
        assert (await client.request("DELETE", path, json=removal, headers=account)).status_code == 204
    assert (await store.push_context(account_id, key_info["id"], task["id"])).activities == {}
