"""REST: tasks with both credential types, validation, isolation, filters, and pages."""

import asyncio

import httpx2

from mcp_client_server.limits import Limits
from tests.conftest import Launch
from tests.support import Api, Headers, Json, error, secret_of


async def test_agent_creates_a_task_with_defaults(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, key_info = await api.agent()
    response = await client.post("/v1/tasks", json={"title": "  Add dark\nmode  support "}, headers=key)
    assert response.status_code == 201
    task = response.json()
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
        "seen_at": task["created_at"],
        "host": None,
    }
    assert task["id"].startswith("tsk_")
    assert (await client.get(f"/v1/tasks/{task['id']}", headers=key)).json() == task
    assert (await client.get(f"/v1/tasks/{task['id']}", headers=account)).json() == task


async def test_x_api_key_header_works_for_keys_only_and_a_query_key_does_not(
    client: httpx2.AsyncClient, api: Api
) -> None:
    account, key, _ = await api.agent()
    assert (await client.get("/v1/tasks", headers={"X-API-Key": secret_of(key)})).status_code == 200
    error(await client.get(f"/v1/tasks?key={secret_of(key)}"), 401, "unauthorized")
    error(await client.get("/v1/account", headers={"X-API-Key": secret_of(account)}), 401, "unauthorized")


async def test_account_creates_a_task_for_one_of_its_keys(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, key_info = await api.agent()
    full: Json = {
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

    missing = error(await client.post("/v1/tasks", json={"title": "No key"}, headers=account), 422, "validation_failed")
    assert list(missing["fields"]) == ["key_id"]
    forbidden = error(await client.post("/v1/tasks", json=full, headers=key), 422, "validation_failed")
    assert list(forbidden["fields"]) == ["key_id"]
    error(await client.post("/v1/tasks", json={**full, "key_id": "key_missing"}, headers=account), 404, "not_found")


async def test_create_validation_names_the_field(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    cases: list[tuple[Json, str]] = [
        ({}, "title"),
        ({"title": ""}, "title"),
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"title": 7}, "title"),
        ({"title": "ok", "description": "x" * 20_001}, "description"),
        ({"title": "ok", "status": "started"}, "status"),
        ({"title": "ok", "priority": "critical"}, "priority"),
        ({"title": "ok", "notify": "yes"}, "notify"),
        ({"title": "ok", "notify": 1}, "notify"),
        ({"title": "ok", "progress": 10}, "progress"),
        ({"title": "ok", "color": "red"}, "color"),
    ]
    for body, field in cases:
        problem = error(await client.post("/v1/tasks", json=body, headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body
    at_the_limit = await client.post("/v1/tasks", json={"title": "x" * 200, "description": "y" * 20_000}, headers=key)
    assert at_the_limit.status_code == 201
    assert (await client.get("/v1/tasks", headers=key)).json()["tasks"][0]["title"] == "x" * 200


async def test_patch_changes_only_the_fields_in_the_body(api: Api) -> None:
    _, key, _ = await api.agent()
    task = await api.task(key, description="Keep me")

    started = await api.patch(key, task["id"], status="in_progress", progress=40, status_note=" Updating\n the tokens ")
    assert (started["status"], started["progress"], started["status_note"]) == (
        "in_progress",
        40,
        "Updating the tokens",
    )
    assert (started["title"], started["description"], started["completed_at"]) == ("Write tests", "Keep me", None)
    assert started["updated_at"] > task["updated_at"]
    assert started["created_at"] == task["created_at"]

    done = await api.patch(key, task["id"], status="done", progress=70)
    assert (done["status"], done["progress"], done["completed_at"]) == ("done", 100, done["updated_at"])

    reopened = await api.patch(key, task["id"], status="todo", progress=None, status_note=None)
    assert (reopened["status"], reopened["progress"], reopened["status_note"], reopened["completed_at"]) == (
        "todo",
        None,
        None,
        None,
    )

    renamed = await api.patch(key, task["id"], title="New", priority="high", notify=False, description="")
    assert (renamed["title"], renamed["priority"], renamed["notify"], renamed["description"]) == (
        "New",
        "high",
        False,
        "",
    )


async def test_patch_without_a_change_does_not_touch_the_task(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    task = await api.task(key)
    bodies: list[Json] = [{}, {"title": "Write tests"}, {"status": "todo", "progress": None}, {"status_note": ""}]
    for body in bodies:
        response = await client.patch(f"/v1/tasks/{task['id']}", json=body, headers=key)
        assert (response.status_code, response.json()) == (200, task)


async def test_patch_validation_names_the_field_and_changes_nothing(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    cases: list[tuple[Json, str]] = [
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
        ({"progress": True}, "progress"),
        ({"status_note": "x" * 141}, "status_note"),
        ({"status": "DONE"}, "status"),
        ({"key_id": "key_other"}, "key_id"),
        ({"id": "tsk_other"}, "id"),
    ]
    for body, field in cases:
        problem = error(await client.patch(path, json=body, headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body
    assert (await client.get(path, headers=key)).json() == task


async def test_delete_task(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, _ = await api.agent()
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    response = await client.delete(path, headers=key)
    assert (response.status_code, response.content) == (204, b"")
    error(await client.get(path, headers=key), 404, "not_found")
    error(await client.delete(path, headers=key), 404, "not_found")
    error(await client.patch(path, json={"title": "Gone"}, headers=account), 404, "not_found")
    assert (await client.get("/v1/keys", headers=account)).json()["keys"][0]["counts"]["todo"] == 0


async def test_a_credential_reaches_only_the_tasks_of_its_own_key_or_account(
    client: httpx2.AsyncClient, api: Api
) -> None:
    account = await api.account()
    key_a, info_a = await api.key(account, "A")
    key_b, info_b = await api.key(account, "B")
    stranger, stranger_key, _ = await api.agent()
    task = await api.task(key_a, title="Private")
    path = f"/v1/tasks/{task['id']}"

    for outsider in (key_b, stranger, stranger_key):
        error(await client.get(path, headers=outsider), 404, "not_found")
        error(await client.patch(path, json={"title": "Taken"}, headers=outsider), 404, "not_found")
        error(await client.delete(path, headers=outsider), 404, "not_found")
        assert (await client.get("/v1/tasks", headers=outsider)).json()["tasks"] == []
    error(await client.get(f"/v1/tasks?key_id={info_a['id']}", headers=stranger), 404, "not_found")
    error(
        await client.post("/v1/tasks", json={"title": "x", "key_id": info_a["id"]}, headers=stranger), 404, "not_found"
    )
    scoped = error(await client.get(f"/v1/tasks?key_id={info_a['id']}", headers=key_a), 422, "validation_failed")
    assert list(scoped["fields"]) == ["key_id"]

    await api.task(key_b, title="Other")

    async def titles(auth: Headers, query: str = "") -> list[str]:
        return [item["title"] for item in (await client.get(f"/v1/tasks{query}", headers=auth)).json()["tasks"]]

    assert sorted(await titles(account)) == ["Other", "Private"]
    assert await titles(account, f"?key_id={info_a['id']}") == ["Private"]
    assert await titles(account, f"?key_id={info_b['id']}") == ["Other"]
    assert await titles(key_a) == ["Private"]
    assert (await client.get(path, headers=account)).json()["title"] == "Private"


async def test_list_sorts_by_update_and_filters(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    first = await api.task(key, title="first", priority="low")
    await api.task(key, title="second", priority="urgent", status="in_progress")
    await api.task(key, title="third", priority="urgent", status="done")
    await api.task(key, title="fourth", status="cancelled")

    async def titles(query: str = "") -> list[str]:
        response = await client.get(f"/v1/tasks{query}", headers=key)
        assert response.status_code == 200, response.text
        assert response.json()["next_cursor"] is None
        return [item["title"] for item in response.json()["tasks"]]

    assert await titles() == ["fourth", "third", "second", "first"]
    await api.patch(key, first["id"], status_note="moved up")
    assert await titles() == ["first", "fourth", "third", "second"]
    assert await titles("?status=todo") == ["first"]
    assert await titles("?status=todo,in_progress") == ["first", "second"]
    assert await titles("?status=done, cancelled") == ["fourth", "third"]
    assert await titles("?priority=urgent") == ["third", "second"]
    assert await titles("?priority=urgent,low&status=in_progress,todo") == ["first", "second"]
    assert await titles("?priority=high") == []
    for query in ("status=open", "priority=p1", "status=todo,bogus"):
        problem = error(await client.get(f"/v1/tasks?{query}", headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [query.split("=")[0]]


async def test_pages_return_each_task_one_time(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, _ = await api.agent()
    made = [
        (await api.task(key, title=f"T{number}", status="todo" if number % 2 else "in_progress"))["id"]
        for number in range(11)
    ]

    async def walk(query: str, auth: Headers) -> list[str]:
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
    assert await walk("limit=4&priority=medium", key) == list(reversed(made))

    exact = (await client.get("/v1/tasks?limit=11", headers=key)).json()
    assert (len(exact["tasks"]), exact["next_cursor"]) == (11, None)
    assert len((await client.get("/v1/tasks?limit=500", headers=key)).json()["tasks"]) == 11
    for query in ("limit=0", "limit=501", "limit=abc", "cursor=%21%21", "cursor=bmFuOnRza18x"):
        problem = error(await client.get(f"/v1/tasks?{query}", headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [query.split("=")[0]]


async def test_a_task_that_changes_between_two_pages_is_not_lost(client: httpx2.AsyncClient, api: Api) -> None:
    _, key, _ = await api.agent()
    made = [(await api.task(key, title=f"T{number}"))["id"] for number in range(4)]
    first = (await client.get("/v1/tasks?limit=2", headers=key)).json()
    assert [item["id"] for item in first["tasks"]] == [made[3], made[2]]

    # The oldest task moves to the top after the first page. The next page still has the task between them.
    await api.patch(key, made[0], progress=1)
    second = (await client.get(f"/v1/tasks?limit=2&cursor={first['next_cursor']}", headers=key)).json()
    assert [item["id"] for item in second["tasks"]] == [made[1]]
    assert second["next_cursor"] is None


async def test_open_task_limit_is_a_conflict(launch: Launch) -> None:
    async with launch(limits=Limits(max_open_tasks_per_key=2)) as running:
        api, client = running.api, running.http
        _, key, _ = await api.agent()
        await api.task(key)
        await api.task(key, status="in_progress")
        error(await client.post("/v1/tasks", json={"title": "Too many"}, headers=key), 409, "conflict")
        done = await api.task(key, status="done")
        path = f"/v1/tasks/{done['id']}"
        error(await client.patch(path, json={"status": "in_progress"}, headers=key), 409, "conflict")
        assert (await client.patch(path, json={"title": "Still done"}, headers=key)).status_code == 200


async def test_concurrent_patches_do_not_lose_an_update_or_break_the_counts(
    client: httpx2.AsyncClient, api: Api
) -> None:
    account, key, _ = await api.agent()
    task = await api.task(key)
    path = f"/v1/tasks/{task['id']}"
    bodies: list[Json] = [
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
