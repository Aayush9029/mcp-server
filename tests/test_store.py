"""The store on a real Valkey (or on fakeredis): the indexes, the guards, and the atomic deletes."""

from redis.asyncio import Redis

from mcp_client_server.credentials import CredentialHash, Secret, hash_secret
from mcp_client_server.ids import AccountId, DeviceId, KeyId, TaskId
from mcp_client_server.limits import Limits
from mcp_client_server.models import STATUSES, Account, ClientInfo, Key, Status, Task
from mcp_client_server.store import AccountTasks, Cursor, DeletedTask, KeyTasks, Store, TaskScope
from tests.support import RawStore, make_device, make_task

EVENT = "event: test\ndata: {}\n\n"
DEV_1 = DeviceId("dev_1")
DEV_2 = DeviceId("dev_2")


class Seed:
    """Makes accounts, keys, and tasks with predictable IDs."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._count = 0

    def _next(self) -> int:
        self._count += 1
        return self._count

    def cred(self, secret: str) -> CredentialHash:
        return hash_secret(Secret(secret))

    async def account(self, raw: str = "mca_test") -> AccountId:
        account = Account(id=AccountId(f"acc_{self._next():03d}"), created_at=1000.0)
        await self.store.create_account(account, self.cred(raw))
        return account.id

    async def key(self, account_id: AccountId, raw: str = "mck_test") -> KeyId:
        secret = raw
        key = Key(
            id=KeyId(f"key_{self._next():03d}"), label="Agent", prefix=secret[:8], last4=secret[-4:], created_at=1000.0
        )
        assert await self.store.create_key(account_id, key, self.cred(secret), EVENT) == "ok"
        return key.id

    async def task(self, account_id: AccountId, key_id: KeyId, **fields: object) -> Task:
        task = make_task(id=f"tsk_{self._next():03d}", key_id=key_id, status="todo", updated_at=1000.0, **fields)
        assert await self.store.create_task(account_id, task, EVENT) == "ok"
        return task


async def index_state(raw: RawStore, store: Store, account_id: AccountId, key_id: KeyId) -> dict[str, list[str]]:
    keys = store.keys
    state = {
        "key": await raw.sorted_set(keys.key_tasks(key_id)),
        "account": await raw.sorted_set(keys.account_tasks(account_id)),
        "closed": await raw.sorted_set(keys.closed),
    }
    for status in STATUSES:
        state[f"key:{status}"] = await raw.sorted_set(f"{keys.key_tasks(key_id)}:{status}")
        state[f"account:{status}"] = await raw.sorted_set(f"{keys.account_tasks(account_id)}:{status}")
    return {name: ids for name, ids in state.items() if ids}


async def counts(store: Store, account_id: AccountId, key_id: KeyId) -> dict[str, int]:
    key = await store.get_key(account_id, key_id)
    assert key is not None
    return key.counts.model_dump()


async def test_each_task_write_keeps_the_indexes_and_the_counts_in_step(store: Store, raw: RawStore) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    task = await seed.task(account_id, key_id)
    scope = KeyTasks(key_id)

    assert await index_state(raw, store, account_id, key_id) == {
        "key": [task.id],
        "account": [task.id],
        "key:todo": [task.id],
        "account:todo": [task.id],
    }
    assert await counts(store, account_id, key_id) == {"todo": 1, "in_progress": 0, "done": 0, "cancelled": 0}

    stored = await store.get_task(scope, task.id)
    assert stored is not None
    done = task.model_copy(update={"status": "done", "updated_at": 2000.0})
    assert await store.update_task(account_id, stored, done, EVENT) == "ok"
    assert await index_state(raw, store, account_id, key_id) == {
        "key": [task.id],
        "account": [task.id],
        "closed": [task.id],
        "key:done": [task.id],
        "account:done": [task.id],
    }
    assert await counts(store, account_id, key_id) == {"todo": 0, "in_progress": 0, "done": 1, "cancelled": 0}

    stored = await store.get_task(AccountTasks(account_id), task.id)
    assert stored is not None
    assert await store.delete_task(account_id, stored, EVENT) == DeletedTask({}, [])
    assert await index_state(raw, store, account_id, key_id) == {}
    assert await raw.exists(store.keys.task(task.id)) == 0
    assert await counts(store, account_id, key_id) == {"todo": 0, "in_progress": 0, "done": 0, "cancelled": 0}


async def test_a_write_from_an_old_read_is_refused(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    task = await seed.task(account_id, key_id)
    snapshot = await store.get_task(KeyTasks(key_id), task.id)
    assert snapshot is not None

    first = task.model_copy(update={"status": "in_progress", "updated_at": 2000.0})
    second = task.model_copy(update={"status": "done", "updated_at": 3000.0})
    assert await store.update_task(account_id, snapshot, first, EVENT) == "ok"
    assert await store.update_task(account_id, snapshot, second, EVENT) == "conflict"
    assert await store.delete_task(account_id, snapshot, EVENT) is None
    assert await counts(store, account_id, key_id) == {"todo": 0, "in_progress": 1, "done": 0, "cancelled": 0}


async def test_a_create_that_is_sent_twice_counts_one_time(store: Store, raw: RawStore) -> None:
    # redis-py sends a command again after a lost connection. The first one can have reached the store.
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    task = await seed.task(account_id, key_id)

    assert await store.create_task(account_id, task, EVENT) == "ok"
    assert await counts(store, account_id, key_id) == {"todo": 1, "in_progress": 0, "done": 0, "cancelled": 0}
    assert await raw.sorted_set(store.keys.key_tasks(key_id)) == [task.id]


async def test_a_task_is_read_only_through_its_own_key_or_account(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_a = await seed.key(account_id, "mck_a")
    key_b = await seed.key(account_id, "mck_b")
    task = await seed.task(account_id, key_a)

    scopes: list[tuple[TaskScope, bool]] = [
        (KeyTasks(key_a), True),
        (AccountTasks(account_id), True),
        (KeyTasks(key_b), False),
        (AccountTasks(AccountId("acc_other")), False),
    ]
    for scope, visible in scopes:
        assert (await store.get_task(scope, task.id) is not None) is visible, scope
        assert (len((await store.page_tasks(scope, [], 10, None)).docs) == 1) is visible, scope


async def test_create_needs_a_key_of_the_same_account(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    assert await store.create_task(AccountId("acc_other"), make_task(key_id=key_id), EVENT) == "no_key"
    assert await store.create_task(account_id, make_task(key_id="key_missing"), EVENT) == "no_key"


async def test_open_task_limit_counts_only_open_tasks(redis: Redis) -> None:
    store = Store(redis, "t", Limits(max_open_tasks_per_key=2))
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    await seed.task(account_id, key_id)
    assert await store.create_task(account_id, make_task(id="tsk_b", key_id=key_id), EVENT) == "ok"
    assert await store.create_task(account_id, make_task(id="tsk_c", key_id=key_id, status="todo"), EVENT) == "limit"
    closed = make_task(id="tsk_d", key_id=key_id, status="done")
    assert await store.create_task(account_id, closed, EVENT) == "ok"

    stored = await store.get_task(KeyTasks(key_id), closed.id)
    assert stored is not None
    reopened = closed.model_copy(update={"status": "todo"})
    assert await store.update_task(account_id, stored, reopened, EVENT) == "limit"
    assert await counts(store, account_id, key_id) == {"todo": 1, "in_progress": 1, "done": 1, "cancelled": 0}


async def walk(store: Store, scope: TaskScope, statuses: list[Status], count: int) -> list[str]:
    ids: list[str] = []
    cursor: Cursor | None = None
    for _ in range(50):
        page = await store.page_tasks(scope, statuses, count, cursor)
        assert len(page.docs) <= count
        ids += [Task.model_validate_json(doc).id for doc in page.docs]
        if not page.has_more:
            return ids
        cursor = page.last
    raise AssertionError("pagination did not end")


async def test_pages_give_each_task_one_time_newest_first_also_with_equal_timestamps(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    statuses: list[Status] = ["todo", "in_progress", "done", "todo", "cancelled", "todo", "in_progress"]
    tasks = [
        # Three timestamps for seven tasks, so that several tasks have the same score.
        make_task(id=f"tsk_{number}", key_id=key_id, status=status, updated_at=1000.0 + number % 3)
        for number, status in enumerate(statuses)
    ]
    for task in tasks:
        assert await store.create_task(account_id, task, EVENT) == "ok"

    def expected(*wanted: Status) -> list[str]:
        chosen = [task for task in tasks if task.status in wanted]
        return [task.id for task in sorted(chosen, key=lambda t: (t.updated_at, t.id), reverse=True)]

    for scope in (KeyTasks(key_id), AccountTasks(account_id)):
        for count in (1, 2, 3, 7, 10):
            assert await walk(store, scope, [], count) == expected(*STATUSES)
            assert await walk(store, scope, ["todo", "in_progress"], count) == expected("todo", "in_progress")
            assert await walk(store, scope, ["cancelled", "todo"], count) == expected("todo", "cancelled")
            assert await walk(store, scope, ["done"], count) == expected("done")


async def test_page_of_an_empty_index(store: Store) -> None:
    page = await store.page_tasks(KeyTasks(KeyId("key_none")), [], 10, None)
    assert (page.docs, page.last, page.has_more) == ([], None, False)


async def purge(store: Store, key_id: KeyId, batch: int) -> tuple[int, list[tuple[TaskId, dict[DeviceId, str]]]]:
    """Run the purge steps of a key to the end. Returns the number of steps and the Live Activities that ended."""
    steps = 0
    ended: list[tuple[TaskId, dict[DeviceId, str]]] = []
    while True:
        steps += 1
        done, activities = await store.purge_key(key_id, batch)
        ended += [(item.task.id, item.tokens) for item in activities]
        if done:
            return steps, ended


async def test_a_detached_key_is_unusable_at_once_and_its_data_goes_in_bounded_steps(
    store: Store, raw: RawStore
) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_a = await seed.key(account_id, "mck_a")
    key_b = await seed.key(account_id, "mck_b")
    doomed = [await seed.task(account_id, key_a) for _ in range(5)]
    kept = await seed.task(account_id, key_b)
    await store.put_device(account_id, make_device())
    assert await store.put_activity(account_id, doomed[1].id, DEV_1, "aa11") == "ok"
    cred_a = seed.cred("mck_a")

    assert await store.detach_key(AccountId("acc_other"), key_a, 5.0, EVENT) is False
    assert await store.detach_key(account_id, key_a, 5.0, EVENT) is True

    # The first step alone: the credential, the key, and the account indexes have nothing of the key.
    assert (await store.authenticate(cred_a, now=6.0, rate_limit=0, rate_bucket="b", rate_ttl=60)).principal is None
    assert await store.get_key(account_id, key_a) is None
    assert await store.key_account(key_a) is None
    assert [key.id for key in await store.list_keys(account_id)] == [key_b]
    assert await index_state(raw, store, account_id, key_a) == {
        "key": [task.id for task in doomed],
        "key:todo": [task.id for task in doomed],
        "account": [kept.id],
        "account:todo": [kept.id],
    }
    assert await store.get_task(AccountTasks(account_id), doomed[0].id) is None
    assert await store.create_task(account_id, make_task(id="tsk_late", key_id=key_a), EVENT) == "no_key"
    assert await store.keys_to_purge(10) == [key_a]
    # The same call again is not an error: the key is detached, and its data can still go.
    assert await store.detach_key(account_id, key_a, 7.0, EVENT) is True

    steps, ended = await purge(store, key_a, 2)
    assert steps == 4  # 2 + 2 + 1 tasks, then the step that finds nothing
    assert ended == [(doomed[1].id, {DEV_1: "aa11"})]
    assert await index_state(raw, store, account_id, key_a) == {"account": [kept.id], "account:todo": [kept.id]}
    for task in doomed:
        assert await raw.exists(store.keys.task(task.id), store.keys.activities(task.id)) == 0
    assert await raw.exists(store.keys.key(key_a), store.keys.credential(cred_a)) == 0
    assert await store.keys_to_purge(10) == []
    assert await store.purge_key(key_a, 2) == (True, [])
    # A key that is in use is never purged.
    assert await store.purge_key(key_b, 2) == (True, [])
    assert await store.get_task(KeyTasks(key_b), kept.id) is not None


async def test_a_detached_account_leaves_nothing_once_its_keys_are_purged(store: Store, raw: RawStore) -> None:
    seed = Seed(store)
    other = await seed.account("mca_other")
    other_key = await seed.key(other, "mck_other")
    await seed.task(other, other_key)
    before = await raw.keys()

    account_id = await seed.account()
    key_id = await seed.key(account_id)
    for status in STATUSES:
        task = await seed.task(account_id, key_id)
        stored = await store.get_task(KeyTasks(key_id), task.id)
        assert stored is not None
        await store.update_task(account_id, stored, task.model_copy(update={"status": status}), EVENT)
    await store.put_device(account_id, make_device())
    assert await store.put_activity(account_id, TaskId("tsk_008"), DEV_1, "aa11") == "ok"

    assert await store.detach_account(account_id, 5.0, EVENT) == [key_id]
    assert (
        await store.authenticate(seed.cred("mca_test"), now=6.0, rate_limit=0, rate_bucket="b", rate_ttl=60)
    ).principal is None
    assert (
        await store.authenticate(seed.cred("mck_test"), now=6.0, rate_limit=0, rate_bucket="b", rate_ttl=60)
    ).principal is None
    assert await store.get_account(account_id) is None
    assert await store.list_devices(account_id) == []
    assert await store.detach_account(account_id, 7.0, EVENT) is None
    assert await store.keys_to_purge(10) == [key_id]
    await purge(store, key_id, 500)
    assert await raw.keys() == before

    assert await store.detach_account(other, 8.0, EVENT) == [other_key]
    await purge(store, other_key, 500)
    assert await raw.keys() == []


async def test_an_apns_token_belongs_to_its_newest_registration(store: Store) -> None:
    seed = Seed(store)
    old_account = await seed.account("mca_old")
    new_account = await seed.account("mca_new")
    await store.put_device(old_account, make_device("dev_1", apns_token="ab" * 32, widget_push_token="99" * 32))
    await store.put_device(new_account, make_device("dev_2", apns_token="ab" * 32))

    old = (await store.list_devices(old_account))[0]
    assert (old.apns_token, old.push_to_start_token, old.widget_push_token) == (None, None, None)
    assert (await store.list_devices(new_account))[0].apns_token == "ab" * 32

    await store.put_device(new_account, make_device("dev_2", apns_token="ef" * 32))
    await store.put_device(old_account, make_device("dev_1", apns_token="ab" * 32))
    assert (await store.list_devices(old_account))[0].apns_token == "ab" * 32
    assert (await store.list_devices(new_account))[0].apns_token == "ef" * 32


async def test_device_limit_replace_and_delete(redis: Redis) -> None:
    store = Store(redis, "t", Limits(max_devices_per_account=1))
    account_id = await Seed(store).account()
    assert await store.put_device(AccountId("acc_missing"), make_device()) == "no_account"
    assert await store.put_device(account_id, make_device("dev_1")) == "ok"
    assert await store.put_device(account_id, make_device("dev_1", apns_token=None, push_to_start_token=None)) == "ok"
    assert await store.put_device(account_id, make_device("dev_2", apns_token="12" * 32)) == "limit"
    assert await store.delete_device(account_id, DEV_1) is True
    assert await store.delete_device(account_id, DEV_1) is False
    assert await store.list_devices(account_id) == []


async def test_a_dead_token_is_cleared_only_if_the_device_still_has_it(store: Store) -> None:
    account_id = await Seed(store).account()
    await store.put_device(account_id, make_device("dev_1", apns_token="ab" * 32, push_to_start_token="cd" * 32))

    await store.clear_device_token(account_id, DEV_1, "apns_token", "00" * 32)
    assert (await store.list_devices(account_id))[0].apns_token == "ab" * 32
    await store.clear_device_token(account_id, DEV_1, "apns_token", "ab" * 32)
    device = (await store.list_devices(account_id))[0]
    assert (device.apns_token, device.push_to_start_token) == (None, "cd" * 32)

    # The token is free again: a different device can register it and keeps the tokens of the first device.
    await store.put_device(account_id, make_device("dev_2", apns_token="ab" * 32))
    assert (await store.list_devices(account_id))[0].push_to_start_token == "cd" * 32


async def test_authenticate_resolves_the_principal_counts_the_rate_and_throttles_last_used(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account("mca_secret")
    key_id = await seed.key(account_id, "mck_secret")

    async def auth(secret: str, at: float, limit: int = 0) -> tuple[object, bool]:
        result = await store.authenticate(seed.cred(secret), now=at, rate_limit=limit, rate_bucket="b", rate_ttl=60)
        return result.principal, result.limited

    assert await auth("mck_unknown", 1.0) == (None, False)
    account, _ = await auth("mca_secret", 1.0)
    assert (type(account).__name__, getattr(account, "account_id", None)) == ("AccountPrincipal", account_id)
    agent, limited = await auth("mck_secret", 100.5)
    assert (getattr(agent, "account_id", None), getattr(agent, "key_id", None), limited) == (account_id, key_id, False)

    await auth("mck_secret", 130.0)
    key = await store.get_key(account_id, key_id)
    assert key is not None
    assert key.last_used_at == 100.5

    assert [(await auth("mck_secret", 1.0, limit=2))[1] for _ in range(3)] == [False, False, True]
    assert (await auth("mck_secret", 1.0, limit=2))[0] == agent


async def test_last_client_write_reports_only_a_real_change(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    cursor = ClientInfo(name="cursor", version="3.0")

    changed = await store.set_key_client(account_id, key_id, cursor)
    assert changed is not None
    assert changed.last_client == cursor
    assert await store.set_key_client(account_id, key_id, cursor) is None
    assert await store.set_key_client(account_id, key_id, ClientInfo(name="cursor", version="3.1")) is not None
    assert await store.set_key_client(AccountId("acc_other"), key_id, cursor) is None
    assert await store.set_key_label(AccountId("acc_other"), key_id, "Stolen") is None


async def test_live_activity_registration_needs_the_task_and_the_device_of_the_account(store: Store) -> None:
    seed = Seed(store)
    account_id = await seed.account()
    key_id = await seed.key(account_id)
    task = await seed.task(account_id, key_id)
    await store.put_device(account_id, make_device())

    assert await store.put_activity(account_id, TaskId("tsk_missing"), DEV_1, "aa") == "no_task"
    assert await store.put_activity(account_id, task.id, DEV_2, "aa") == "no_device"
    assert await store.put_activity(AccountId("acc_other"), task.id, DEV_1, "aa") == "no_task"
    assert await store.put_activity(account_id, task.id, DEV_1, "aa") == "ok"
    assert (await store.push_context(account_id, key_id, task.id)).activities == {DEV_1: "aa"}

    await store.forget_activity(task.id, DEV_1, "stale")
    assert (await store.push_context(account_id, key_id, task.id)).activities == {DEV_1: "aa"}
    assert await store.delete_activity(account_id, task.id, DEV_1) is True
    assert (await store.push_context(account_id, key_id, task.id)).activities == {}
    assert await store.delete_activity(account_id, TaskId("tsk_missing"), DEV_1) is False
