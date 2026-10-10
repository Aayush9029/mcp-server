from typing import Any

import pytest
from redis.asyncio import Redis

from mcp_client_server import limits
from mcp_client_server.credentials import hash_secret
from mcp_client_server.ids import new_id, now
from mcp_client_server.models import STATUSES, Account, Device, Key, Status, Task
from mcp_client_server.store import Cursor, Store
from tests.conftest import key_names

EVENT = "event: test\ndata: {}\n\n"


async def make_account(store: Store, secret: str = "mca_test") -> str:
    account = Account(id=new_id("acc"), created_at=now())
    await store.create_account(account, hash_secret(secret))
    return account.id


async def make_key(store: Store, account_id: str, secret: str = "mck_test") -> str:
    key = Key(id=new_id("key"), label="Agent", prefix=secret[:8], last4=secret[-4:], created_at=now())
    assert await store.create_key(account_id, key, hash_secret(secret), EVENT) == "ok"
    return key.id


def make_task(key_id: str, **fields: Any) -> Task:
    at = now()
    base: dict[str, Any] = {
        "id": new_id("tsk"),
        "key_id": key_id,
        "title": "Task",
        "description": "",
        "status": "todo",
        "priority": "medium",
        "notify": True,
        "progress": None,
        "status_note": None,
        "source": "agent",
        "created_at": at,
        "updated_at": at,
        "completed_at": None,
    }
    return Task(**{**base, **fields})


async def members(redis: Redis, key: str) -> list[str]:
    return [str(member) for member in await redis.zrange(key, 0, -1)]


async def index_state(redis: Redis, store: Store, account_id: str, key_id: str) -> dict[str, list[str]]:
    keys = store.keys
    state = {
        "key": await members(redis, keys.key_tasks(key_id)),
        "account": await members(redis, keys.account_tasks(account_id)),
        "closed": await members(redis, keys.closed),
    }
    for status in STATUSES:
        state[f"key:{status}"] = await members(redis, f"{keys.key_tasks(key_id)}:{status}")
        state[f"account:{status}"] = await members(redis, f"{keys.account_tasks(account_id)}:{status}")
    return {name: ids for name, ids in state.items() if ids}


async def test_task_write_keeps_all_indexes_and_counts_in_step(store: Store, redis: Redis) -> None:
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    task = make_task(key_id)
    assert await store.create_task(account_id, task, EVENT) == "ok"

    assert await index_state(redis, store, account_id, key_id) == {
        "key": [task.id],
        "account": [task.id],
        "key:todo": [task.id],
        "account:todo": [task.id],
    }
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.counts.model_dump() == {"todo": 1, "in_progress": 0, "done": 0, "cancelled": 0}

    stored = await store.get_task(store.keys.key_tasks(key_id), task.id)
    assert stored is not None
    done = task.model_copy(update={"status": "done", "updated_at": task.updated_at + 1})
    assert await store.update_task(account_id, stored, done, EVENT) == "ok"

    assert await index_state(redis, store, account_id, key_id) == {
        "key": [task.id],
        "account": [task.id],
        "closed": [task.id],
        "key:done": [task.id],
        "account:done": [task.id],
    }
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.counts.model_dump() == {"todo": 0, "in_progress": 0, "done": 1, "cancelled": 0}

    stored = await store.get_task(store.keys.account_tasks(account_id), task.id)
    assert stored is not None
    assert await store.delete_task(account_id, stored, EVENT) == {}
    assert await index_state(redis, store, account_id, key_id) == {}
    assert await redis.exists(store.keys.task(task.id)) == 0
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.counts.done == 0


async def test_update_is_a_compare_and_swap(store: Store) -> None:
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    task = make_task(key_id)
    await store.create_task(account_id, task, EVENT)
    index = store.keys.key_tasks(key_id)
    snapshot = await store.get_task(index, task.id)
    assert snapshot is not None

    first = task.model_copy(update={"status": "in_progress", "updated_at": task.updated_at + 1})
    second = task.model_copy(update={"status": "done", "updated_at": task.updated_at + 2})
    assert await store.update_task(account_id, snapshot, first, EVENT) == "ok"
    assert await store.update_task(account_id, snapshot, second, EVENT) == "conflict"
    assert await store.delete_task(account_id, snapshot, EVENT) is None

    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.counts.model_dump() == {"todo": 0, "in_progress": 1, "done": 0, "cancelled": 0}


async def test_task_read_is_scoped_to_its_index(store: Store) -> None:
    account_id = await make_account(store)
    key_a = await make_key(store, account_id, "mck_a")
    key_b = await make_key(store, account_id, "mck_b")
    task = make_task(key_a)
    await store.create_task(account_id, task, EVENT)

    assert await store.get_task(store.keys.key_tasks(key_a), task.id) is not None
    assert await store.get_task(store.keys.key_tasks(key_b), task.id) is None
    assert await store.get_task(store.keys.account_tasks(account_id), task.id) is not None
    assert await store.get_task(store.keys.account_tasks("acc_other"), task.id) is None


async def test_create_needs_a_key_of_the_same_account(store: Store) -> None:
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    assert await store.create_task("acc_other", make_task(key_id), EVENT) == "no_key"
    assert await store.create_task(account_id, make_task("key_missing"), EVENT) == "no_key"


async def test_open_task_limit(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_OPEN_TASKS_PER_KEY", 2)
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    first = make_task(key_id)
    assert await store.create_task(account_id, first, EVENT) == "ok"
    assert await store.create_task(account_id, make_task(key_id, status="in_progress"), EVENT) == "ok"
    assert await store.create_task(account_id, make_task(key_id), EVENT) == "limit"
    closed = make_task(key_id, status="done")
    assert await store.create_task(account_id, closed, EVENT) == "ok"

    stored = await store.get_task(store.keys.key_tasks(key_id), closed.id)
    assert stored is not None
    reopened = closed.model_copy(update={"status": "todo"})
    assert await store.update_task(account_id, stored, reopened, EVENT) == "limit"
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.counts.model_dump() == {"todo": 1, "in_progress": 1, "done": 1, "cancelled": 0}


async def collect(store: Store, index: str, statuses: list[Status], count: int) -> list[str]:
    ids: list[str] = []
    cursor: Cursor | None = None
    for _ in range(50):
        page = await store.page_tasks(index, statuses, count, cursor)
        ids += [Task.model_validate_json(doc).id for doc in page.docs]
        if not page.has_more:
            return ids
        cursor = page.last
    raise AssertionError("pagination did not end")


async def test_pages_are_newest_first_and_exact_with_equal_timestamps(store: Store) -> None:
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    statuses: list[Status] = ["todo", "in_progress", "done", "todo", "cancelled", "todo", "in_progress"]
    tasks = [
        # Three timestamps for seven tasks, so that several tasks have the same score.
        make_task(key_id, status=status, updated_at=1000.0 + (number % 3), created_at=1000.0)
        for number, status in enumerate(statuses)
    ]
    for task in tasks:
        assert await store.create_task(account_id, task, EVENT) == "ok"

    def expected(wanted: set[str]) -> list[str]:
        chosen = [task for task in tasks if task.status in wanted]
        return [task.id for task in sorted(chosen, key=lambda t: (t.updated_at, t.id), reverse=True)]

    index = store.keys.key_tasks(key_id)
    for count in (1, 2, 3, 10):
        assert await collect(store, index, [], count) == expected(set(STATUSES))
        assert await collect(store, index, ["todo", "in_progress"], count) == expected({"todo", "in_progress"})
        assert await collect(store, index, ["done"], count) == expected({"done"})
    assert await collect(store, store.keys.account_tasks(account_id), ["todo", "cancelled"], 2) == expected(
        {"todo", "cancelled"}
    )


async def test_page_of_an_empty_index(store: Store) -> None:
    page = await store.page_tasks(store.keys.key_tasks("key_none"), [], 10, None)
    assert (page.docs, page.last, page.has_more) == ([], None, False)


async def test_delete_key_removes_its_tasks_and_its_credential(store: Store, redis: Redis) -> None:
    account_id = await make_account(store)
    key_a = await make_key(store, account_id, "mck_a")
    key_b = await make_key(store, account_id, "mck_b")
    doomed = [make_task(key_a, status=status) for status in STATUSES]
    kept = make_task(key_b)
    for task in [*doomed, kept]:
        await store.create_task(account_id, task, EVENT)
    await store.put_device(account_id, device("dev_1"))
    assert await store.put_activity(account_id, doomed[1].id, "dev_1", "aa11") == "ok"

    assert await store.delete_key("acc_other", key_a, EVENT) is None
    ended = await store.delete_key(account_id, key_a, EVENT)
    assert ended is not None
    assert [(item.task.id, item.tokens) for item in ended] == [(doomed[1].id, {"dev_1": "aa11"})]

    assert await index_state(redis, store, account_id, key_a) == {
        "account": [kept.id],
        "account:todo": [kept.id],
    }
    for task in doomed:
        assert await redis.exists(store.keys.task(task.id), store.keys.activities(task.id)) == 0
    assert await redis.exists(store.keys.key(key_a), store.keys.credential(hash_secret("mck_a"))) == 0
    assert [key.id for key in await store.list_keys(account_id)] == [key_b]
    assert await store.delete_key(account_id, key_a, EVENT) is None


async def test_delete_account_leaves_no_keys_behind(store: Store, redis: Redis) -> None:
    other = await make_account(store, "mca_other")
    other_key = await make_key(store, other, "mck_other")
    await store.create_task(other, make_task(other_key), EVENT)

    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    for status in STATUSES:
        await store.create_task(account_id, make_task(key_id, status=status), EVENT)
    await store.put_device(account_id, device("dev_1"))
    before = await key_names(redis)

    assert await store.delete_account(account_id, EVENT) is True
    left = await key_names(redis)
    assert left < before
    assert not [name for name in left if account_id in name or key_id in name]
    assert await store.delete_account(account_id, EVENT) is False
    assert len(await store.list_keys(other)) == 1

    await store.delete_account(other, EVENT)
    assert await key_names(redis) == set()


def device(device_id: str, token: str | None = "ab" * 32, start: str | None = "cd" * 32) -> Device:
    return Device(
        id=device_id,
        platform="ios",
        model="iPhone18,1",
        os_version="27.0",
        app_version="1.0",
        apns_environment="production",
        apns_token=token,
        push_to_start_token=start,
        updated_at=now(),
    )


async def test_an_apns_token_belongs_to_its_newest_registration(store: Store) -> None:
    old_account = await make_account(store, "mca_old")
    new_account = await make_account(store, "mca_new")
    await store.put_device(old_account, device("dev_1"))
    await store.put_device(new_account, device("dev_2"))

    assert (await store.list_devices(old_account))[0].apns_token is None
    assert (await store.list_devices(old_account))[0].push_to_start_token is None
    assert (await store.list_devices(new_account))[0].apns_token == "ab" * 32

    await store.put_device(new_account, device("dev_2", token="ef" * 32))
    await store.put_device(old_account, device("dev_1"))
    assert (await store.list_devices(old_account))[0].apns_token == "ab" * 32
    assert (await store.list_devices(new_account))[0].apns_token == "ef" * 32


async def test_device_limit_and_replace(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_DEVICES_PER_ACCOUNT", 1)
    account_id = await make_account(store)
    assert await store.put_device("acc_missing", device("dev_1")) == "no_account"
    assert await store.put_device(account_id, device("dev_1")) == "ok"
    assert await store.put_device(account_id, device("dev_1", token=None, start=None)) == "ok"
    assert await store.put_device(account_id, device("dev_2", token="12" * 32)) == "limit"
    assert await store.delete_device(account_id, "dev_1") is True
    assert await store.delete_device(account_id, "dev_1") is False
    assert await store.list_devices(account_id) == []


async def test_clear_device_token_only_if_it_is_still_the_same(store: Store) -> None:
    account_id = await make_account(store)
    await store.put_device(account_id, device("dev_1"))
    await store.clear_device_token(account_id, "dev_1", "apns_token", "00" * 32)
    assert (await store.list_devices(account_id))[0].apns_token == "ab" * 32
    await store.clear_device_token(account_id, "dev_1", "apns_token", "ab" * 32)
    assert (await store.list_devices(account_id))[0].apns_token is None
    assert (await store.list_devices(account_id))[0].push_to_start_token == "cd" * 32


async def test_authenticate_resolves_limits_and_throttles_last_used(store: Store) -> None:
    account_id = await make_account(store, "mca_secret")
    key_id = await make_key(store, account_id, "mck_secret")

    async def auth(secret: str, at: float, limit: int = 0) -> Any:
        return await store.authenticate(hash_secret(secret), now=at, rate_limit=limit, rate_bucket="b", rate_ttl=60)

    assert (await auth("mck_unknown", 1.0)).status == "unknown"
    account = await auth("mca_secret", 1.0)
    assert (account.status, account.account_id, account.key_id) == ("ok", account_id, None)
    agent = await auth("mck_secret", 100.5)
    assert (agent.status, agent.account_id, agent.key_id) == ("ok", account_id, key_id)

    await auth("mck_secret", 200.0)
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.last_used_at == 100.5

    assert (await auth("mck_secret", 1.0, limit=2)).status == "ok"
    assert (await auth("mck_secret", 1.0, limit=2)).status == "ok"
    limited = await auth("mck_secret", 1.0, limit=2)
    assert (limited.status, limited.key_id) == ("limited", key_id)


async def test_live_activity_registration_needs_the_task_and_the_device(store: Store) -> None:
    account_id = await make_account(store)
    key_id = await make_key(store, account_id)
    task = make_task(key_id)
    await store.create_task(account_id, task, EVENT)
    await store.put_device(account_id, device("dev_1"))

    assert await store.put_activity(account_id, "tsk_missing", "dev_1", "aa") == "no_task"
    assert await store.put_activity(account_id, task.id, "dev_missing", "aa") == "no_device"
    assert await store.put_activity("acc_other", task.id, "dev_1", "aa") == "no_task"
    assert await store.put_activity(account_id, task.id, "dev_1", "aa") == "ok"
    assert (await store.push_context(account_id, key_id, task.id)).activities == {"dev_1": "aa"}

    await store.forget_activity(task.id, "dev_1", "stale")
    assert (await store.push_context(account_id, key_id, task.id)).activities == {"dev_1": "aa"}
    assert await store.delete_activity(account_id, task.id, "dev_1") is True
    assert (await store.push_context(account_id, key_id, task.id)).activities == {}
    assert await store.delete_activity(account_id, "tsk_missing", "dev_1") is False
