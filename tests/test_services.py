"""Service rules that need a clock, a limit, or a store that misbehaves."""

import asyncio
from typing import Literal, override

import pytest
from redis.asyncio import Redis

from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, Secret, hash_secret
from mcp_client_server.errors import Conflict, NotFound, RateLimited, Unauthorized
from mcp_client_server.events import EventHub
from mcp_client_server.ids import AccountId, KeyId
from mcp_client_server.limits import Limits
from mcp_client_server.models import Account, Key, KeyCreate, NotifyInput, Priority, Task, TaskCreate, TaskPatch
from mcp_client_server.push import PushDispatcher
from mcp_client_server.services import Services
from mcp_client_server.services.questions import QuestionService
from mcp_client_server.services.tasks import TaskService
from mcp_client_server.store import Store, StoredTask
from tests.conftest import Launch
from tests.support import EventListener, FakeBus, FakeClock, RecordingPushSender, Running, SequentialIds

DAY = 24 * 3600


async def new_agent(services: Services, label: str = "Agent") -> tuple[AccountPrincipal, AgentPrincipal]:
    created = await services.accounts.create()
    account = AccountPrincipal(created.account.id)
    key = await services.keys.create(account, KeyCreate(label=label))
    return account, AgentPrincipal(account.account_id, key.key.id)


@pytest.mark.parametrize("secret", [None, "", "nope", "mck_" + "a" * 43, "sk_" + "a" * 43, "mck_" + "a" * 500])
async def test_a_malformed_or_unknown_secret_is_unauthorized(services: Services, secret: str | None) -> None:
    with pytest.raises(Unauthorized):
        await services.auth.authenticate(Secret(secret) if secret is not None else None)


async def test_credential_rate_limit_is_for_one_minute_and_one_credential(launch: Launch) -> None:
    async with launch(rate_limit_per_minute=2) as running:
        clock, auth = running.clock, running.services.auth
        clock.time = 1_760_000_000.0 - (1_760_000_000.0 % 60) + 10
        created = await running.services.accounts.create()
        other = await running.services.accounts.create()

        await auth.authenticate(created.token)
        await auth.authenticate(created.token)
        with pytest.raises(RateLimited) as limited:
            await auth.authenticate(created.token)
        assert 45 <= limited.value.retry_after <= 50
        await auth.authenticate(other.token)

        clock.advance(limited.value.retry_after)
        assert await auth.authenticate(created.token) == AccountPrincipal(created.account.id)


async def test_account_creation_limit_is_for_one_hour_and_one_address(launch: Launch) -> None:
    async with launch(rate_limit_accounts_per_hour=1) as running:
        auth = running.services.auth
        await auth.limit_account_creation("198.51.100.4")
        with pytest.raises(RateLimited) as limited:
            await auth.limit_account_creation("198.51.100.4")
        assert 0 < limited.value.retry_after <= 3600
        await auth.limit_account_creation("198.51.100.5")

        running.clock.advance(limited.value.retry_after)
        await auth.limit_account_creation("198.51.100.4")


async def test_priority_filter_continues_with_a_cursor_when_the_scan_budget_ends(launch: Launch) -> None:
    async with launch(limits=Limits(filter_scan_budget=4)) as running:
        tasks = running.services.tasks
        _, key = await new_agent(running.services)
        urgent: list[str] = []
        for number in range(12):
            priority: Priority = "urgent" if number % 4 == 0 else "low"
            made = await tasks.create(key, TaskCreate(title=f"T{number}", priority=priority))
            if priority == "urgent":
                urgent.append(made.id)

        found: list[str] = []
        sizes: list[int] = []
        cursor: str | None = None
        while True:
            page = await tasks.page(key, priorities=["urgent"], limit=2, cursor=cursor)
            found += [item.id for item in page.tasks]
            sizes.append(len(page.docs))
            cursor = page.next_cursor
            if cursor is None:
                break
        assert found == list(reversed(urgent))
        # The budget ends a page early: a page can be short, or empty, and still have a cursor.
        assert len(sizes) > 2
        assert min(sizes[:-1]) < 2


async def test_a_closed_task_expires_90_days_after_its_last_update(
    running: Running, redis: Redis, store: Store
) -> None:
    services, clock = running.services, running.clock
    # This test does the work of the maintenance loop itself, so it takes the lock of the loop.
    assert await store.acquire_lock("expire", 600)
    account, key = await new_agent(services)
    open_task = await services.tasks.create(key, TaskCreate(title="Open"))
    done = await services.tasks.create(key, TaskCreate(title="Done", status="done"))
    cancelled = await services.tasks.create(key, TaskCreate(title="Cancelled", status="cancelled"))

    clock.advance(89 * DAY)
    assert await services.maintenance.expire_closed_tasks() == 0
    # An update starts the 90 days again.
    await services.tasks.update(key, cancelled.id, TaskPatch(title="Still here"))

    clock.advance(2 * DAY)
    listener = EventListener(redis, account.account_id)
    await listener.start()
    assert await services.maintenance.expire_closed_tasks() == 1
    assert await listener.names() == ["task.deleted"]

    assert {item.id for item in (await services.tasks.page(account)).tasks} == {open_task.id, cancelled.id}
    with pytest.raises(NotFound):
        await services.tasks.get(key, done.id)
    counts = (await services.keys.all(account))[0].counts
    assert (counts.todo, counts.done, counts.cancelled) == (1, 0, 1)
    assert await services.maintenance.expire_closed_tasks() == 0


async def test_a_task_that_was_opened_again_does_not_expire(running: Running) -> None:
    services, clock = running.services, running.clock
    _, key = await new_agent(services)
    task = await services.tasks.create(key, TaskCreate(title="Back", status="done"))
    await services.tasks.update(key, task.id, TaskPatch(status="todo"))

    clock.advance(200 * DAY)
    assert await services.maintenance.expire_closed_tasks() == 0
    assert (await services.tasks.get(key, task.id)).status == "todo"


async def test_the_maintenance_loop_expires_tasks_and_one_worker_has_the_lock(running: Running) -> None:
    services, clock = running.services, running.clock
    _, key = await new_agent(services)
    task = await services.tasks.create(key, TaskCreate(title="Old", status="done"))
    clock.advance(91 * DAY)

    loop = asyncio.create_task(services.maintenance.run())
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    await services.tasks.get(key, task.id)
                except NotFound:
                    break
                await asyncio.sleep(0)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    # Each pass waits from half an interval to one interval. The 10 second sleeps are the question sweep.
    assert all(450 <= seconds <= 900 for seconds in clock.sleeps if seconds != 10)


class ConflictStore(Store):
    """A store where a different writer always wins."""

    @override
    async def update_task(
        self, account_id: AccountId, old: StoredTask, new: Task, event: str
    ) -> Literal["ok", "conflict", "limit"]:
        return "conflict"


async def test_a_write_that_always_loses_the_race_ends_as_a_conflict(redis: Redis, clock: FakeClock) -> None:
    store = ConflictStore(redis, "t")
    push = PushDispatcher(store, None, None, clock)
    ids, limits = SequentialIds(), Limits()
    hub = EventHub(FakeBus(clock).connect, store.keys, clock)
    tasks = TaskService(store, push, QuestionService(store, hub, push, clock, ids, limits), clock, ids, limits)
    real = Store(redis, "t")
    account_id, key_id = AccountId("acc_1"), KeyId("key_1")
    await real.create_account(Account(id=account_id, created_at=1.0), hash_secret(Secret("mca_x")))
    key = Key(id=key_id, label="Agent", prefix="mck_xxxx", last4="xxxx", created_at=1.0)
    await real.create_key(account_id, key, hash_secret(Secret("mck_x")), "event: x\ndata: {}\n\n")
    agent = AgentPrincipal(account_id, key_id)
    task = await tasks.create(agent, TaskCreate(title="Contested"))

    with pytest.raises(Conflict):
        await tasks.update(agent, task.id, TaskPatch(title="Mine"))
    assert (await tasks.get(agent, task.id)).title == "Contested"


async def test_last_client_gives_one_event_for_each_change_and_one_write_each_minute(
    running: Running, redis: Redis
) -> None:
    services, clock = running.services, running.clock
    account, key = await new_agent(services)
    listener = EventListener(redis, account.account_id)
    await listener.start()

    await services.keys.record_client(key, "claude-code", "2.1.290")
    await services.keys.record_client(key, "claude-code", "2.1.290")
    clock.advance(61)
    await services.keys.record_client(key, "claude-code", "2.1.290")
    await services.keys.record_client(key, "cursor", "3.0")
    await services.keys.record_client(key, "", "1")
    assert await listener.names() == ["key.updated", "key.updated"]

    client = (await services.keys.all(account))[0].last_client
    assert client is not None
    assert (client.name, client.version) == ("cursor", "3.0")


async def test_a_second_worker_does_not_send_the_event_again_for_the_same_client(launch: Launch, redis: Redis) -> None:
    async with launch() as first, launch() as second:
        account, key = await new_agent(first.services)
        listener = EventListener(redis, account.account_id)
        await listener.start()
        await first.services.keys.record_client(key, "claude-code", "2.1.290")
        await second.services.keys.record_client(key, "claude-code", "2.1.290")
        assert await listener.names() == ["key.updated"]


async def test_notify_user_limit_is_for_one_hour_and_one_key(launch: Launch, push: RecordingPushSender) -> None:
    async with launch(limits=Limits(notify_user_per_hour=2)) as running:
        services = running.services
        account, key = await new_agent(services)
        second = await services.keys.create(account, KeyCreate(label="Second"))
        other_key = AgentPrincipal(account.account_id, second.key.id)
        message = NotifyInput(title="Review needed", body="Please look at the pull request.")

        first = await services.tasks.notify_user(key, message)
        assert (first.sent, first.devices) == (False, 0)
        await services.tasks.notify_user(key, message)
        with pytest.raises(RateLimited) as limited:
            await services.tasks.notify_user(key, message)
        assert 0 < limited.value.retry_after <= 3600
        await services.tasks.notify_user(other_key, message)

        running.clock.advance(limited.value.retry_after)
        await services.tasks.notify_user(key, message)
        assert push.sent == []


async def test_notify_user_about_a_task_of_a_different_key_is_not_found_and_not_counted(launch: Launch) -> None:
    async with launch(limits=Limits(notify_user_per_hour=1)) as running:
        services = running.services
        _, key = await new_agent(services)
        _, stranger = await new_agent(services)
        foreign = await services.tasks.create(stranger, TaskCreate(title="Foreign"))
        message = NotifyInput(title="Hi", body="There")

        with pytest.raises(NotFound):
            await services.tasks.notify_user(key, message.model_copy(update={"task_id": foreign.id}))
        await services.tasks.notify_user(key, message)
