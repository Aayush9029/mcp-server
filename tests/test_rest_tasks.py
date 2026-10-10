import asyncio
from typing import Any

import httpx
import pytest

from mcp_client_server import limits
from tests.conftest import Api
from tests.test_rest_accounts import error


async def test_agent_creates_a_task_with_defaults(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, key_info = await api.key(account)
    response = await client.post("/v1/tasks", json={"title": "  Add dark\nmode  support "}, headers=key)
    assert response.status_code == 201
    task = response.json()
    assert task["id"].startswith("tsk_")
    assert task == {
        "id": task["id"],
        "key_id": key_info["id"],
        "title": "Add dark mode support",
        "description": "",
        "status": "todo",
        "priority": "medium",
        "notify": True,
        "progress": None,
        "status_note": None,
        "source": "agent",
        "created_at": task["created_at"],
        "updated_at": task["created_at"],
        "completed_at": None,
    }
    assert (await client.get(f"/v1/tasks/{task['id']}", headers=key)).json() == task
    assert (await client.get(f"/v1/tasks/{task['id']}", headers=account)).json() == task


async def test_api_key_header_works_and_query_key_does_not(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    secret = key["Authorization"].removeprefix("Bearer ")
    assert (await client.get("/v1/tasks", headers={"X-API-Key": secret})).status_code == 200
    error(await client.get(f"/v1/tasks?key={secret}"), 401, "unauthorized")
    token = account["Authorization"].removeprefix("Bearer ")
    error(await client.get("/v1/account", headers={"X-API-Key": token}), 401, "unauthorized")


async def test_account_creates_a_task_for_one_of_its_keys(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, key_info = await api.key(account)
    full = {
        "key_id": key_info["id"],
        "title": "Write unit tests",
        "description": "# Plan\n- one",
        "status": "done",
        "priority": "urgent",
        "notify": False,
    }
    response = await client.post("/v1/tasks", json=full, headers=account)
    assert response.status_code == 201
    task = response.json()
    assert (task["source"], task["status"], task["progress"], task["notify"]) == ("app", "done", 100, False)
    assert task["completed_at"] == task["created_at"]
    assert task["description"] == "# Plan\n- one"

    body = error(await client.post("/v1/tasks", json={"title": "No key"}, headers=account), 422, "validation_failed")
    assert "key_id" in body["fields"]
    body = error(await client.post("/v1/tasks", json=full, headers=key), 422, "validation_failed")
    assert "key_id" in body["fields"]
    error(await client.post("/v1/tasks", json={**full, "key_id": "key_missing"}, headers=account), 404, "not_found")


async def test_create_validation(client: httpx.AsyncClient, api: Api) -> None:
    key, _ = await api.key(await api.account())
    cases: list[tuple[dict[str, Any], str]] = [
        ({}, "title"),
        ({"title": ""}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"title": 7}, "title"),
        ({"title": "ok", "description": "x" * 20_001}, "description"),
        ({"title": "ok", "status": "started"}, "status"),
        ({"title": "ok", "priority": "critical"}, "priority"),
        ({"title": "ok", "notify": "yes"}, "notify"),
        ({"title": "ok", "progress": 10}, "progress"),
        ({"title": "ok", "color": "red"}, "color"),
    ]
    for body, field in cases:
        problem = error(await client.post("/v1/tasks", json=body, headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body
    assert (
        await client.post("/v1/tasks", json={"title": "x" * 200, "description": "y" * 20_000}, headers=key)
    ).status_code == 201
    assert (await client.get("/v1/tasks", headers=key)).json()["tasks"][0]["title"] == "x" * 200


async def test_patch_changes_only_the_fields_in_the_body(client: httpx.AsyncClient, api: Api) -> None:
    key, _ = await api.key(await api.account())
    task = await api.task(key, description="Keep me")
    path = f"/v1/tasks/{task['id']}"

    started = (
        await client.patch(
            path, json={"status": "in_progress", "progress": 40, "status_note": " Updating\n the tokens "}, headers=key
        )
    ).json()
    assert (started["status"], started["progress"], started["status_note"]) == (
        "in_progress",
        40,
        "Updating the tokens",
    )
    assert (started["title"], started["description"], started["completed_at"]) == ("Write tests", "Keep me", None)
    assert started["updated_at"] > task["updated_at"] and started["created_at"] == task["created_at"]

    done = (await client.patch(path, json={"status": "done", "progress": 70}, headers=key)).json()
    assert (done["status"], done["progress"]) == ("done", 100)
    assert done["completed_at"] == done["updated_at"]

    reopened = (
        await client.patch(path, json={"status": "todo", "progress": None, "status_note": None}, headers=key)
    ).json()
    assert (reopened["status"], reopened["progress"], reopened["status_note"], reopened["completed_at"]) == (
        "todo",
        None,
        None,
        None,
    )

    renamed = (
        await client.patch(
            path, json={"title": "New", "priority": "high", "notify": False, "description": ""}, headers=key
        )
    ).json()
    assert (renamed["title"], renamed["priority"], renamed["notify"], renamed["description"]) == (
        "New",
        "high",
        False,
        "",
    )


async def test_patch_without_a_change_does_not_touch_the_task(client: httpx.AsyncClient, api: Api) -> None:
    key, _ = await api.key(await api.account())
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    for body in ({}, {"title": "Write tests"}, {"status": "todo", "progress": None}):
        response = await client.patch(path, json=body, headers=key)
        assert response.status_code == 200 and response.json() == task


async def test_patch_validation(client: httpx.AsyncClient, api: Api) -> None:
    key, _ = await api.key(await api.account())
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    cases: list[tuple[dict[str, Any], str]] = [
        ({"title": None}, "title"),
        ({"description": None}, "description"),
        ({"status": None}, "status"),
        ({"priority": None}, "priority"),
        ({"notify": None}, "notify"),
        ({"title": ""}, "title"),
        ({"progress": 101}, "progress"),
        ({"progress": -1}, "progress"),
        ({"progress": "40"}, "progress"),
        ({"progress": 4.5}, "progress"),
        ({"status_note": "x" * 141}, "status_note"),
        ({"status": "DONE"}, "status"),
        ({"key_id": "key_other"}, "key_id"),
        ({"id": "tsk_other"}, "id"),
    ]
    for body, field in cases:
        problem = error(await client.patch(path, json=body, headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body
    assert (await client.get(path, headers=key)).json() == task


async def test_delete_task(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    response = await client.delete(path, headers=key)
    assert (response.status_code, response.content) == (204, b"")
    error(await client.get(path, headers=key), 404, "not_found")
    error(await client.delete(path, headers=key), 404, "not_found")
    error(await client.patch(path, json={"title": "Gone"}, headers=account), 404, "not_found")
    assert (await client.get("/v1/keys", headers=account)).json()["keys"][0]["counts"]["todo"] == 0


async def test_a_key_cannot_reach_the_tasks_of_another_key_or_account(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key_a, info_a = await api.key(account, "A")
    key_b, info_b = await api.key(account, "B")
    stranger = await api.account()
    stranger_key, _ = await api.key(stranger)
    task = await api.task(key_a, title="Private")
    path = f"/v1/tasks/{task['id']}"

    for outsider in (key_b, stranger, stranger_key):
        error(await client.get(path, headers=outsider), 404, "not_found")
        error(await client.patch(path, json={"title": "Taken"}, headers=outsider), 404, "not_found")
        error(await client.delete(path, headers=outsider), 404, "not_found")
        assert (await client.get("/v1/tasks", headers=outsider)).json()["tasks"] == []
    error(await client.get(f"/v1/tasks?key_id={info_a['id']}", headers=stranger), 404, "not_found")
    body = error(await client.get(f"/v1/tasks?key_id={info_a['id']}", headers=key_a), 422, "validation_failed")
    assert "key_id" in body["fields"]

    await api.task(key_b, title="Other")
    everything = (await client.get("/v1/tasks", headers=account)).json()["tasks"]
    assert {item["title"] for item in everything} == {"Private", "Other"}
    only_a = (await client.get(f"/v1/tasks?key_id={info_a['id']}", headers=account)).json()["tasks"]
    assert [item["title"] for item in only_a] == ["Private"]
    only_b = (await client.get(f"/v1/tasks?key_id={info_b['id']}", headers=account)).json()["tasks"]
    assert [item["title"] for item in only_b] == ["Other"]
    assert (await client.get(path, headers=account)).json()["title"] == "Private"


async def test_list_sorts_by_update_and_filters(client: httpx.AsyncClient, api: Api) -> None:
    key, _ = await api.key(await api.account())
    first = await api.task(key, title="first", priority="low")
    second = await api.task(key, title="second", priority="urgent", status="in_progress")
    third = await api.task(key, title="third", priority="urgent", status="done")
    fourth = await api.task(key, title="fourth", status="cancelled")

    async def titles(query: str = "") -> list[str]:
        response = await client.get(f"/v1/tasks{query}", headers=key)
        assert response.status_code == 200, response.text
        return [item["title"] for item in response.json()["tasks"]]

    assert await titles() == ["fourth", "third", "second", "first"]
    await client.patch(f"/v1/tasks/{first['id']}", json={"status_note": "moved up"}, headers=key)
    assert await titles() == ["first", "fourth", "third", "second"]
    assert await titles("?status=todo") == ["first"]
    assert await titles("?status=todo,in_progress") == ["first", "second"]
    assert await titles("?status=done, cancelled") == ["fourth", "third"]
    assert await titles("?priority=urgent") == ["third", "second"]
    assert await titles("?priority=urgent,low&status=in_progress,todo") == ["first", "second"]
    assert await titles("?priority=high") == []
    for query in ("?status=open", "?priority=p1", "?status=todo,bogus"):
        problem = error(await client.get(f"/v1/tasks{query}", headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [query[1:].split("=")[0]]
    assert {second["id"], third["id"], fourth["id"]}


async def test_pagination_returns_each_task_one_time(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    made = [
        (await api.task(key, title=f"T{number}", status="todo" if number % 2 else "in_progress"))["id"]
        for number in range(11)
    ]

    async def walk(query: str, auth: dict[str, str]) -> list[str]:
        seen: list[str] = []
        cursor = ""
        for _ in range(20):
            response = await client.get(f"/v1/tasks?{query}{cursor}", headers=auth)
            assert response.status_code == 200, response.text
            page = response.json()
            assert len(page["tasks"]) <= 4
            seen += [item["id"] for item in page["tasks"]]
            if page["next_cursor"] is None:
                return seen
            cursor = f"&cursor={page['next_cursor']}"
        raise AssertionError("pagination did not end")

    assert await walk("limit=4", key) == list(reversed(made))
    assert await walk("limit=4", account) == list(reversed(made))
    assert await walk("limit=4&status=todo,in_progress", key) == list(reversed(made))
    assert await walk("limit=4&status=todo", key) == list(reversed(made[1::2]))

    exact = (await client.get("/v1/tasks?limit=11", headers=key)).json()
    assert len(exact["tasks"]) == 11 and exact["next_cursor"] is None
    for query in ("limit=0", "limit=501", "limit=abc", "cursor=%21%21"):
        error(await client.get(f"/v1/tasks?{query}", headers=key), 422, "validation_failed")
    assert len((await client.get("/v1/tasks?limit=500", headers=key)).json()["tasks"]) == 11


async def test_open_task_limit_is_a_conflict(
    client: httpx.AsyncClient, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "MAX_OPEN_TASKS_PER_KEY", 2)
    key, _ = await api.key(await api.account())
    await api.task(key)
    await api.task(key)
    error(await client.post("/v1/tasks", json={"title": "Too many"}, headers=key), 409, "conflict")
    done = await api.task(key, status="done")
    error(await client.patch(f"/v1/tasks/{done['id']}", json={"status": "in_progress"}, headers=key), 409, "conflict")


async def test_concurrent_patches_do_not_lose_updates_or_break_counts(client: httpx.AsyncClient, api: Api) -> None:
    account = await api.account()
    key, _ = await api.key(account)
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    bodies = [
        {"progress": 10},
        {"status_note": "note"},
        {"priority": "high"},
        {"status": "in_progress"},
        {"title": "Renamed"},
    ]
    responses = await asyncio.gather(*(client.patch(path, json=body, headers=key) for body in bodies))
    assert [response.status_code for response in responses] == [200] * len(bodies)

    final = (await client.get(path, headers=key)).json()
    assert (final["progress"], final["status_note"], final["priority"], final["status"], final["title"]) == (
        10,
        "note",
        "high",
        "in_progress",
        "Renamed",
    )
    counts = (await client.get("/v1/keys", headers=account)).json()["keys"][0]["counts"]
    assert counts == {"todo": 0, "in_progress": 1, "done": 0, "cancelled": 0}
