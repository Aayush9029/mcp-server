"""Deletion of a key or an account in bounded steps, and the limit of closed tasks for each key."""

import io

from mcp_client_server import admin
from mcp_client_server.credentials import AccountPrincipal
from mcp_client_server.ids import AccountId, KeyId
from mcp_client_server.limits import Limits
from mcp_client_server.store import Store
from tests.conftest import Launch
from tests.support import DEVICE_A, Api, EventListener, Headers, Json, RawStore, Running, error

EVENT = "event: test\ndata: {}\n\n"


async def fill_key(api: Api, account: Headers, key: Headers, key_info: Json, tasks: int = 5) -> list[Json]:
    """A key with tasks, a Live Activity, questions, and inbox items."""
    made = [await api.task(key, title=f"Task {number}") for number in range(tasks)]
    await api.device(account, DEVICE_A)
    await api.live_activity(account, made[1]["id"])
    for number in range(3):
        await api.ask(key, prompt=f"Question {number}?", task_id=made[0]["id"])
    for number in range(3):
        await api.message(account, key_id=key_info["id"], text=f"Message {number}")
    return made


async def about(raw: RawStore, *ids: str) -> list[str]:
    """The keys in the store that name one of the IDs. A rate limit counter is not data: it expires."""
    return [name for name in await raw.keys() if ":rl:" not in name and any(item in name for item in ids)]


async def test_a_deleted_key_is_unusable_at_once_and_loses_its_data_in_steps(launch: Launch, raw: RawStore) -> None:
    async with launch(limits=Limits(purge_batch=2)) as running:
        api = running.api
        account, key, key_info = await api.agent()
        other_key, other_info = await api.key(account, "Other")
        tasks = await fill_key(api, account, key, key_info)
        kept = await api.task(other_key, title="Kept")

        assert (await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)).status_code == 204

        error(await api.http.get("/v1/tasks", headers=key), 401, "unauthorized")
        assert [item["id"] for item in (await api.http.get("/v1/keys", headers=account)).json()["keys"]] == [
            other_info["id"]
        ]
        assert [t["id"] for t in (await api.http.get("/v1/tasks", headers=account)).json()["tasks"]] == [kept["id"]]
        assert (await api.http.get("/v1/questions", headers=account)).json()["questions"] == []
        assert await about(raw, key_info["id"], *(task["id"] for task in tasks)) == []
        await running.pushes_done()
        ends = [p for p in running.push.payloads("liveactivity") if p["aps"]["event"] == "end"]
        assert len(ends) == 1
        assert [p.ref.task_id for p in running.push.of_type("liveactivity")][-1] == tasks[1]["id"]
        assert (await api.http.get("/v1/tasks", headers=other_key)).json()["tasks"] == [kept]
        error(await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account), 404, "not_found")


async def test_a_deletion_that_stopped_after_its_first_step_serves_nothing_and_a_retry_finishes_it(
    launch: Launch, store: Store, raw: RawStore
) -> None:
    async with launch(limits=Limits(purge_batch=2)) as running:
        api = running.api
        account, key, key_info = await api.agent()
        account_id = await api.account_id(account)
        tasks = await fill_key(api, account, key, key_info)

        # The first step ran, then the process was lost.
        assert await store.detach_key(account_id, KeyId(key_info["id"]), 1.0, EVENT) is True

        error(await api.http.get("/v1/tasks", headers=key), 401, "unauthorized")
        assert (await api.http.get("/v1/keys", headers=account)).json()["keys"] == []
        assert (await api.http.get("/v1/tasks", headers=account)).json()["tasks"] == []
        error(await api.http.get(f"/v1/tasks/{tasks[0]['id']}", headers=account), 404, "not_found")
        error(await api.http.get(f"/v1/tasks?key_id={key_info['id']}", headers=account), 404, "not_found")
        assert (await api.http.get("/v1/questions", headers=account)).json()["questions"] == []
        error(await api.http.get(f"/v1/keys/{key_info['id']}/inbox", headers=account), 404, "not_found")
        error(
            await api.http.post("/v1/messages", json={"key_id": key_info["id"], "text": "Hi"}, headers=account),
            404,
            "not_found",
        )
        assert await about(raw, tasks[0]["id"]) != []

        # The person tries again: the retry finishes the work.
        assert (await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)).status_code == 204
        assert await about(raw, key_info["id"], *(task["id"] for task in tasks)) == []


async def test_the_maintenance_loop_finishes_a_deletion_that_stopped(
    launch: Launch, store: Store, raw: RawStore
) -> None:
    async with launch(limits=Limits(purge_batch=2)) as running:
        api, clock = running.api, running.clock
        account, key, key_info = await api.agent()
        account_id = await api.account_id(account)
        tasks = await fill_key(api, account, key, key_info)
        assert await store.detach_key(account_id, KeyId(key_info["id"]), 1.0, EVENT) is True
        assert await about(raw, key_info["id"]) != []

        await clock.sleeping(675, tolerance=225)
        clock.advance(900)
        await clock.sleeping(675, tolerance=225)

        assert await about(raw, key_info["id"], *(task["id"] for task in tasks)) == []


async def test_a_deleted_account_is_unusable_at_once_and_the_admin_command_finishes_a_stopped_deletion(
    launch: Launch, store: Store, raw: RawStore
) -> None:
    async with launch(limits=Limits(purge_batch=2)) as running:
        api = running.api
        other = await api.account()
        account, key, key_info = await api.agent()
        account_id = await api.account_id(account)
        tasks = await fill_key(api, account, key, key_info)

        assert await store.detach_account(account_id, 1.0, EVENT) == [key_info["id"]]
        error(await api.http.get("/v1/account", headers=account), 401, "unauthorized")
        error(await api.http.get("/v1/tasks", headers=key), 401, "unauthorized")
        error(await api.http.post("/mcp", headers=key), 401, "unauthorized")
        assert await about(raw, tasks[0]["id"]) != []

        out = io.StringIO()
        assert await admin.delete_account(running.services, account_id=account_id, out=out) == admin.NOT_FOUND
        assert await about(raw, account_id, key_info["id"], *(task["id"] for task in tasks)) == []
        assert (await api.http.get("/v1/account", headers=other)).status_code == 200

        # A full deletion through the service leaves nothing of the other account either.
        other_id = await api.account_id(other)
        assert await running.services.accounts.delete(AccountPrincipal(AccountId(other_id))) is True
        assert await about(raw, other_id) == []


async def test_a_key_keeps_a_limited_number_of_closed_tasks_and_the_oldest_goes_first(
    launch: Launch, redis_listener: EventListener
) -> None:
    async with launch(limits=Limits(max_closed_tasks_per_key=2)) as running:
        api = running.api
        account, key, key_info = await api.agent()
        await redis_listener.start()
        first = await api.task(key, title="First", status="done")
        second = await api.task(key, title="Second")
        third = await api.task(key, title="Third")
        question = await api.ask(key, task_id=first["id"])
        await api.patch(key, second["id"], status="cancelled")
        assert (await api.http.get(f"/v1/tasks/{first['id']}", headers=key)).status_code == 200

        await api.patch(key, third["id"], status="done")

        error(await api.http.get(f"/v1/tasks/{first['id']}", headers=key), 404, "not_found")
        listed = (await api.http.get("/v1/tasks", headers=account)).json()["tasks"]
        assert sorted(task["id"] for task in listed) == sorted([second["id"], third["id"]])
        keys = (await api.http.get("/v1/keys", headers=account)).json()["keys"]
        assert keys[0]["counts"] == {"todo": 0, "in_progress": 0, "done": 1, "cancelled": 1}
        assert (await api.http.get(f"/v1/questions/{question['id']}", headers=key)).json()["status"] == "cancelled"
        names = await redis_listener.names()
        assert names.count("task.deleted") == 1
        assert names.index("task.deleted") > names.index("task.updated")
        # A task that opens again does not count, and a task that is created closed counts.
        await api.patch(key, third["id"], status="todo")
        await api.task(key, title="Fourth", status="cancelled")
        assert (await api.http.get(f"/v1/tasks/{second['id']}", headers=key)).status_code == 200
        await api.task(key, title="Fifth", status="done")
        error(await api.http.get(f"/v1/tasks/{second['id']}", headers=key), 404, "not_found")
        assert key_info["id"] in {task["key_id"] for task in listed}


async def test_the_closed_task_limit_is_for_each_key(running: Running) -> None:
    """The default limit is in the contract, and a second key is counted on its own."""
    assert Limits().max_closed_tasks_per_key == 5000
    api = running.api
    _, key, _ = await api.agent()
    _, other_key, _ = await api.agent("Other")
    mine = await api.task(key, status="done")
    theirs = await api.task(other_key, status="done")
    assert (await api.http.get(f"/v1/tasks/{mine['id']}", headers=key)).status_code == 200
    assert (await api.http.get(f"/v1/tasks/{theirs['id']}", headers=other_key)).status_code == 200
