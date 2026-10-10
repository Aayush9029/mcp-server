from typing import Any

import pytest

from mcp_client_server import limits
from mcp_client_server.credentials import Principal, hash_secret, new_secret
from mcp_client_server.errors import (
    Forbidden,
    LimitReached,
    NotFound,
    RateLimited,
    Unauthorized,
    ValidationFailed,
)
from mcp_client_server.ids import new_id
from mcp_client_server.models import KeyCreate, NotifyInput, Task, TaskCreate, TaskPatch
from mcp_client_server.services import Services, tasks
from mcp_client_server.services.tasks import apply_patch, decode_cursor, encode_cursor
from mcp_client_server.store import Cursor
from tests.conftest import FakePush


async def agent(services: Services, label: str = "Agent") -> tuple[Principal, Principal]:
    created = await services.accounts.create()
    account = Principal(created.account.id)
    key = await services.keys.create(account, KeyCreate(label=label))
    return account, Principal(account.account_id, key.key.id)


def task(**fields: Any) -> Task:
    base: dict[str, Any] = {
        "id": "tsk_1",
        "key_id": "key_1",
        "title": "Task",
        "description": "",
        "status": "in_progress",
        "priority": "medium",
        "notify": True,
        "progress": 40,
        "status_note": "Working",
        "source": "agent",
        "created_at": 100.0,
        "updated_at": 100.0,
        "completed_at": None,
    }
    return Task(**{**base, **fields})


def test_ids_sort_by_time_and_secrets_are_long() -> None:
    first, second = new_id("tsk"), new_id("tsk")
    assert first.startswith("tsk_") and len(first) == 30
    assert first != second
    secret = new_secret("mck_")
    assert len(secret) == 47 and secret.isascii() and secret[4:].isalnum()
    assert len(hash_secret(secret)) == 64


def test_patch_done_sets_progress_and_completed_at() -> None:
    patched, changed = apply_patch(task(), {"status": "done", "progress": 50}, 200.0)
    assert (patched.status, patched.progress, patched.completed_at, patched.updated_at) == ("done", 100, 200.0, 200.0)
    assert changed == {"status", "progress"}


def test_patch_away_from_done_clears_completed_at() -> None:
    done = task(status="done", progress=100, completed_at=150.0)
    patched, changed = apply_patch(done, {"status": "in_progress"}, 200.0)
    assert (patched.status, patched.completed_at, patched.progress) == ("in_progress", None, 100)
    assert changed == {"status"}
    kept, _ = apply_patch(done, {"title": "New"}, 200.0)
    assert kept.completed_at == 150.0


def test_patch_null_and_empty_clear_the_optional_fields() -> None:
    patched, changed = apply_patch(task(), {"progress": None, "status_note": ""}, 200.0)
    assert (patched.progress, patched.status_note) == (None, None)
    assert changed == {"progress", "status_note"}


def test_patch_with_no_change_keeps_the_task() -> None:
    original = task()
    patched, changed = apply_patch(original, {"title": "Task", "progress": 40}, 200.0)
    assert patched is original
    assert changed == frozenset()


def test_patch_keeps_updated_at_increasing() -> None:
    patched, _ = apply_patch(task(updated_at=500.0), {"title": "New"}, 400.0)
    assert patched.updated_at == 500.000001


def test_cursor_round_trip_and_rejects_garbage() -> None:
    cursor = Cursor(1760112000.412345, "tsk_01abc")
    assert decode_cursor(encode_cursor(cursor)) == cursor
    assert decode_cursor(None) is None
    for bad in ("x", "!!!", "dGVzdA"):
        with pytest.raises(ValidationFailed) as error:
            decode_cursor(bad)
        assert "cursor" in error.value.fields


async def test_authenticate_rejects_malformed_and_unknown_secrets(services: Services) -> None:
    for secret in (None, "", "nope", "mck_" + "a" * 43, "sk_" + "a" * 43, "mck_" + "a" * 500):
        with pytest.raises(Unauthorized):
            await services.auth.authenticate(secret)


async def test_task_isolation_between_keys_and_accounts(services: Services) -> None:
    account_a, key_a = await agent(services)
    other_key = await services.keys.create(account_a, KeyCreate(label="Second"))
    key_a2 = Principal(account_a.account_id, other_key.key.id)
    account_b, key_b = await agent(services)
    made = await services.tasks.create(key_a, TaskCreate(title="Secret plan"))

    assert (await services.tasks.get(key_a, made.id)).id == made.id
    assert (await services.tasks.get(account_a, made.id)).id == made.id
    for stranger in (key_a2, key_b, account_b):
        with pytest.raises(NotFound):
            await services.tasks.get(stranger, made.id)
        with pytest.raises(NotFound):
            await services.tasks.update(stranger, made.id, TaskPatch(title="Mine now"))
        with pytest.raises(NotFound):
            await services.tasks.delete(stranger, made.id)
        assert (await services.tasks.page(stranger)).tasks == []
    with pytest.raises(NotFound):
        await services.tasks.page(account_b, key_id=key_a.key_id)
    with pytest.raises(NotFound):
        await services.tasks.create(account_b, TaskCreate(title="Intrusion", key_id=key_a.key_id))
    assert (await services.tasks.get(key_a, made.id)).title == "Secret plan"


async def test_key_id_rules_for_each_credential_type(services: Services) -> None:
    account, key = await agent(services)
    with pytest.raises(ValidationFailed) as error:
        await services.tasks.create(key, TaskCreate(title="T", key_id=key.key_id))
    assert "key_id" in error.value.fields
    with pytest.raises(ValidationFailed):
        await services.tasks.create(account, TaskCreate(title="T"))
    with pytest.raises(ValidationFailed):
        await services.tasks.page(key, key_id=key.key_id)
    made = await services.tasks.create(account, TaskCreate(title="T", key_id=key.key_id))
    assert (made.source, made.key_id) == ("app", key.key_id)
    assert (await services.tasks.create(key, TaskCreate(title="T"))).source == "agent"


async def test_open_task_limit_gives_a_limit_error(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_OPEN_TASKS_PER_KEY", 2)
    _, key = await agent(services)
    await services.tasks.create(key, TaskCreate(title="One"))
    await services.tasks.create(key, TaskCreate(title="Two"))
    with pytest.raises(LimitReached):
        await services.tasks.create(key, TaskCreate(title="Three"))
    closed = await services.tasks.create(key, TaskCreate(title="Done", status="done"))
    with pytest.raises(LimitReached):
        await services.tasks.update(key, closed.id, TaskPatch(status="todo"))


async def test_priority_filter_pages_within_the_scan_budget(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tasks, "_SCAN_BUDGET", 4)
    _, key = await agent(services)
    urgent: list[str] = []
    for number in range(12):
        priority = "urgent" if number % 4 == 0 else "low"
        made = await services.tasks.create(key, TaskCreate(title=f"T{number}", priority=priority))
        if priority == "urgent":
            urgent.append(made.id)

    found: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await services.tasks.page(key, priorities=["urgent"], limit=2, cursor=cursor)
        found += [item.id for item in page.tasks]
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert found == list(reversed(urgent))
    assert pages > 2


async def test_expiry_deletes_only_old_closed_tasks(services: Services, monkeypatch: pytest.MonkeyPatch) -> None:
    account, key = await agent(services)
    open_task = await services.tasks.create(key, TaskCreate(title="Open"))
    done = await services.tasks.create(key, TaskCreate(title="Done", status="done"))
    assert await services.maintenance.expire_closed_tasks() == 0

    monkeypatch.setattr(limits, "CLOSED_TASK_TTL_SECONDS", -60)
    assert await services.maintenance.expire_closed_tasks() == 1
    assert [item.id for item in (await services.tasks.page(account)).tasks] == [open_task.id]
    with pytest.raises(NotFound):
        await services.tasks.get(key, done.id)
    counts = (await services.keys.all(account))[0].counts
    assert (counts.todo, counts.done) == (1, 0)
    assert await services.maintenance.expire_closed_tasks() == 0


async def test_last_client_is_written_once_for_an_unchanged_client(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, key = await agent(services)
    writes: list[str] = []
    original = services.store.set_key_client

    async def spy(account_id: str, key_id: str, client: Any) -> Any:
        writes.append(client.name)
        return await original(account_id, key_id, client)

    monkeypatch.setattr(services.store, "set_key_client", spy)
    await services.keys.record_client(key, "claude-code", "2.1.290")
    await services.keys.record_client(key, "claude-code", "2.1.290")
    await services.keys.record_client(account, "ignored", "1")
    assert writes == ["claude-code"]
    await services.keys.record_client(key, "cursor", "3.0")
    assert writes == ["claude-code", "cursor"]
    client = (await services.keys.all(account))[0].last_client
    assert client is not None
    assert (client.name, client.version) == ("cursor", "3.0")


async def test_notify_user_limit_and_scope(services: Services, push: FakePush, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "NOTIFY_USER_PER_HOUR", 2)
    account, key = await agent(services)
    _, stranger = await agent(services)
    foreign = await services.tasks.create(stranger, TaskCreate(title="Foreign"))
    message = NotifyInput(title="Review needed", body="Please look at the pull request.")

    with pytest.raises(Forbidden):
        await services.tasks.notify_user(account, message)
    with pytest.raises(NotFound):
        await services.tasks.notify_user(key, message.model_copy(update={"task_id": foreign.id}))
    result = await services.tasks.notify_user(key, message)
    assert (result.sent, result.devices) == (False, 0)
    await services.tasks.notify_user(key, message)
    with pytest.raises(RateLimited) as error:
        await services.tasks.notify_user(key, message)
    assert 0 < error.value.retry_after <= 3600
    assert push.sent == []
