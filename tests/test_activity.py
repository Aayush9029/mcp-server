"""The host name of an agent, and the activity log of each key: what it has, what it never has, and what it costs."""

import asyncio
import json
from collections.abc import Iterable, Sequence

import pytest

from mcp_client_server.activity import host_name, invalid_fields
from mcp_client_server.credentials import Principal, Secret, hash_secret
from mcp_client_server.ids import AccountId, ActivityId, KeyId, TaskId
from mcp_client_server.limits import Limits
from mcp_client_server.models import Account, ActivityEntry, Key, Task
from mcp_client_server.services.tasks import TaskService
from mcp_client_server.store import Store
from mcp_client_server.store.client import Arg, KeyValue, Script
from tests.conftest import Launch
from tests.support import (
    SIGNAL_SECONDS,
    Api,
    EventListener,
    Headers,
    Json,
    RawStore,
    Running,
    error,
    secret_of,
    settle,
)

MARKER = "ZEBRA-7731 private text"
EVENT = "event: test\ndata: {}\n\n"
WEEK = 7 * 24 * 3600


def on_host(key: Headers, name: str) -> Headers:
    return {**key, "X-Relaydeck-Host": name}


async def log_of(api: Api, account: Headers, key_id: str, **query: object) -> list[Json]:
    """The activity log of a key as the app reads it. The route first writes the entries that still wait."""
    params = {name: str(value) for name, value in query.items()}
    response = await api.http.get(f"/v1/keys/{key_id}/activity", params=params, headers=account)
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["items"]
    return items


def actions(entries: list[Json]) -> list[str]:
    return [entry["action"] for entry in entries]


def ids(entries: list[Json]) -> list[str]:
    return [entry["id"] for entry in entries]


def failures(entries: list[Json]) -> list[tuple[str, str, str | None]]:
    return [(entry["action"], entry["code"], entry["message"]) for entry in entries if entry["status"] == "error"]


def make_entry(number: int, at: float) -> ActivityEntry:
    return ActivityEntry(
        id=ActivityId(f"act_{number:06d}"),
        at=at,
        surface="rest",
        action="GET /v1/tasks",
        status="ok",
        code=None,
        message=None,
        duration_ms=3,
        client=None,
        host=None,
    )


def count_store_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record each call that the application makes to the store. One call is one round trip."""
    seen: list[str] = []
    call, batch, run = KeyValue.call, KeyValue.batch, Script.__call__

    async def counted_call(self: KeyValue, *args: Arg) -> object:
        seen.append("command")
        return await call(self, *args)

    async def counted_batch(self: KeyValue, commands: Iterable[Sequence[Arg]], *, atomic: bool = False) -> list[object]:
        seen.append("batch")
        return await batch(self, commands, atomic=atomic)

    async def counted_script(self: Script, *, keys: Sequence[str] = (), args: Sequence[Arg] = ()) -> object:
        seen.append("script")
        return await run(self, keys=keys, args=args)

    monkeypatch.setattr(KeyValue, "call", counted_call)
    monkeypatch.setattr(KeyValue, "batch", counted_batch)
    monkeypatch.setattr(Script, "__call__", counted_script)
    return seen


def hold_log_writes(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, list[int]]:
    """Make each write of the log wait for the returned event. The list gets the size of each batch that waits."""
    gate = asyncio.Event()
    held: list[int] = []
    add = Store.add_activity

    async def waiting_add(self: Store, entries: Sequence[tuple[AccountId, KeyId, ActivityEntry]]) -> None:
        held.append(len(entries))
        await gate.wait()
        await add(self, entries)

    monkeypatch.setattr(Store, "add_activity", waiting_add)
    return gate, held


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("studio-mac", "studio-mac"),
        ("  Mac mini (lab 2)  ", "Mac mini (lab 2)"),
        ("h" * 64, "h" * 64),
        (None, None),
        ("", None),
        ("   ", None),
        ("h" * 65, None),
        ("tab\there", None),
        ("line\nbreak", None),
        ("café", None),
        ("del\x7f", None),
    ],
)
def test_a_host_name_is_1_to_64_printable_characters_and_a_bad_value_is_ignored(
    sent: str | None, kept: str | None
) -> None:
    assert host_name(sent) == kept


def test_a_summary_of_invalid_fields_has_names_and_drops_a_name_that_can_be_text() -> None:
    assert invalid_fields(["title", "options.0", "title"]) == "Invalid fields: options.0, title."
    assert invalid_fields(["my private field", "a" * 41, "x;y"]) == "The request is not valid."
    assert invalid_fields(f"f{number:02d}" for number in range(10)).endswith("f06, f07, and 2 more.")


async def test_the_host_of_a_request_becomes_last_host_of_its_key_with_one_event_for_each_change(
    running: Running, redis_listener: EventListener
) -> None:
    api = running.api
    account, key, info = await api.agent()
    _, other_info = await api.key(account, "Other")
    await redis_listener.start()

    async def last_host() -> str | None:
        keys = (await api.http.get("/v1/keys", headers=account)).json()["keys"]
        host: str | None = next(item["last_host"] for item in keys if item["id"] == info["id"])
        return host

    # The first MCP request gives the key its client: one event that is not about the host.
    await api.tool(key, "list_tasks", {})
    assert await last_host() is None

    await api.task(on_host(key, "studio-mac"))
    await api.task(on_host(key, "studio-mac"))
    assert await last_host() == "studio-mac"

    # A request with no header, or with a header that is not valid, succeeds and keeps the last host.
    await api.task(key)
    await api.task(on_host(key, "h" * 65))
    await api.task(on_host(key, "bad\tname"))
    assert await last_host() == "studio-mac"

    await api.tool(on_host(key, " build-box "), "list_tasks", {})
    assert await last_host() == "build-box"
    # The account token has no key, so its header changes nothing.
    await api.task(on_host(account, "phone"), key_id=info["id"])
    assert await last_host() == "build-box"

    agents = (await api.http.get("/v1/agents", headers=key)).json()["agents"]
    assert {agent["key_id"]: agent["host"] for agent in agents} == {info["id"]: "build-box", other_info["id"]: None}
    listed = await api.tool(key, "list_agents", {})
    assert f"{info['id']} Claude Code (you) on build-box" in listed["content"][0]["text"]
    assert (await redis_listener.names()).count("key.updated") == 3


async def test_the_store_reports_a_host_only_when_it_is_a_different_one(store: Store) -> None:
    account = Account(id=AccountId("acc_1"), created_at=1.0)
    key = Key(id=KeyId("key_1"), label="Agent", prefix="mck_test", last4="test", created_at=1.0)
    await store.create_account(account, hash_secret(Secret("mca_test")))
    assert await store.create_key(account.id, key, hash_secret(Secret("mck_test")), EVENT) == "ok"

    async def changed(secret: str, host: str | None, limit: int = 0) -> bool:
        cred = hash_secret(Secret(secret))
        result = await store.authenticate(cred, now=5.0, rate_limit=limit, rate_bucket="b", rate_ttl=60, host=host)
        return result.host_changed

    async def last_host() -> str | None:
        stored = await store.get_key(account.id, key.id)
        assert stored is not None
        return stored.last_host

    assert [await changed("mck_test", host) for host in ("studio", "studio", None, "build", "studio")] == [
        True,
        False,
        False,
        True,
        True,
    ]
    assert await last_host() == "studio"
    # An account has no key, and a request above the rate limit changes nothing.
    assert not await changed("mca_test", "phone")
    assert [await changed("mck_test", "late", limit=1) for _ in range(2)] == [True, False]
    assert not await changed("mck_test", "later", limit=1)
    assert await last_host() == "late"


async def test_a_task_has_the_host_of_the_agent_request_that_made_it(api: Api) -> None:
    account, key, info = await api.agent()

    made = await api.task(on_host(key, "studio-mac"))
    assert made["host"] == "studio-mac"
    # A later change from a different host does not move the task.
    assert (await api.patch(on_host(key, "build-box"), made["id"], progress=10))["host"] == "studio-mac"

    through_tool = await api.tool(on_host(key, "build-box"), "create_task", {"title": "From a tool"})
    assert through_tool["structuredContent"]["host"] == "build-box"

    assert (await api.task(key))["host"] is None
    assert (await api.task(on_host(key, "h" * 65)))["host"] is None
    assert (await api.task(on_host(account, "phone"), key_id=info["id"]))["host"] is None

    tasks = (await api.http.get("/v1/tasks", headers=account)).json()["tasks"]
    assert [task["host"] for task in tasks] == [None, None, None, "build-box", "studio-mac"]


async def test_each_request_of_a_key_has_one_entry_newest_first_with_the_route_and_not_the_id(running: Running) -> None:
    api = running.api
    account, key, info = await api.agent()

    task = await api.task(on_host(key, "studio-mac"))
    await api.http.get(f"/v1/tasks/{task['id']}", headers=key)
    await api.patch(key, task["id"], progress=10)
    question = await api.ask(key, task_id=task["id"])
    await api.http.get(f"/v1/questions/{question['id']}", headers=key)
    await api.tool(on_host(key, "studio-mac"), "update_task", {"task_id": task["id"], "status": "done"})
    await api.rpc(key, "tools/list")
    await api.http.delete(f"/v1/tasks/{task['id']}", headers=key)

    entries = await log_of(api, account, info["id"])
    assert [(entry["surface"], entry["action"]) for entry in entries] == [
        ("rest", "DELETE /v1/tasks/{task_id}"),
        ("mcp", "tools/list"),
        ("mcp", "update_task"),
        ("rest", "GET /v1/questions/{question_id}"),
        ("rest", "POST /v1/questions"),
        ("rest", "PATCH /v1/tasks/{task_id}"),
        ("rest", "GET /v1/tasks/{task_id}"),
        ("rest", "POST /v1/tasks"),
    ]
    assert entries[2] == {
        "id": "act_000006",
        "at": entries[2]["at"],
        "surface": "mcp",
        "action": "update_task",
        "status": "ok",
        "code": None,
        "message": None,
        "duration_ms": 0,
        "client": {"name": "raw-client", "version": "0.1"},
        "host": "studio-mac",
    }
    assert entries[-1] == {
        "id": "act_000001",
        "at": entries[-1]["at"],
        "surface": "rest",
        "action": "POST /v1/tasks",
        "status": "ok",
        "code": None,
        "message": None,
        "duration_ms": 0,
        "client": None,
        "host": "studio-mac",
    }
    # `at` is the time that the request came.
    assert info["created_at"] < entries[-1]["at"] < task["created_at"]
    times = [entry["at"] for entry in entries]
    assert times == sorted(times, reverse=True)
    assert len(set(times)) == len(times)
    stored = json.dumps(entries)
    assert task["id"] not in stored
    assert question["id"] not in stored


async def test_a_failed_request_has_its_code_and_a_summary_that_names_fields_and_not_values(running: Running) -> None:
    api = running.api
    account, key, info = await api.agent()
    task = await api.task(key)

    error(await api.http.get("/v1/tasks/tsk_unknown", headers=key), 404, "not_found")
    error(
        await api.http.post("/v1/tasks", json={"title": "", "my note": MARKER, "extra": 1}, headers=key),
        422,
        "validation_failed",
    )
    error(await api.http.get("/v1/tasks?status=waiting", headers=key), 422, "validation_failed")
    error(await api.http.get("/v1/keys", headers=key), 403, "forbidden")
    assert (await api.tool(key, "get_task", {"task_id": "tsk_unknown"}))["isError"] is True
    assert (await api.tool(key, "create_task", {"title": "", "priority": MARKER}))["isError"] is True
    assert (await api.tool(key, "ask_user", {"prompt": "Which?", "kind": "choice", "options": [MARKER]}))["isError"]
    assert (await api.tool(key, "update_task", {"task_id": task["id"]}))["isError"] is True
    assert (await api.tool(key, MARKER, {}))["isError"] is True
    assert (await api.rpc(key, MARKER)).status_code == 404
    uri = f"tasks://task/{MARKER}"
    assert (await api.rpc(key, "resources/read", {"uri": uri}, headers={"Mcp-Name": uri})).status_code == 400
    broken = {**key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    assert (await api.http.post("/mcp", content=b"{", headers=broken)).status_code == 400

    entries = await log_of(api, account, info["id"])
    assert failures(entries)[::-1] == [
        ("GET /v1/tasks/{task_id}", "not_found", "Task not found."),
        ("POST /v1/tasks", "validation_failed", "Invalid fields: extra, title."),
        ("GET /v1/tasks", "validation_failed", "Invalid fields: status."),
        ("GET /v1/keys", "forbidden", "This route needs the account token. An API key cannot use it."),
        ("get_task", "not_found", "Task not found."),
        ("create_task", "validation_failed", "Invalid fields: priority, title."),
        ("ask_user", "validation_failed", "Invalid fields: options."),
        ("update_task", "validation_failed", "No field to change."),
        ("tools/call", "not_found", "Unknown tool."),
        ("unknown method", "not_found", "Unknown method."),
        ("resources/read", "validation_failed", "The request parameters are not valid."),
        ("POST /mcp", "bad_request", "The MCP request is not valid."),
    ]
    assert actions(entries)[-1] == "POST /v1/tasks"
    assert MARKER not in json.dumps(entries)


async def test_a_defect_in_the_server_has_an_entry_with_no_detail(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = running.api
    account, key, info = await api.agent()

    async def defect(self: TaskService, principal: Principal, task_id: TaskId) -> Task:
        raise RuntimeError(f"{MARKER} {task_id}")

    monkeypatch.setattr(TaskService, "get", defect)
    with pytest.raises(RuntimeError):
        await api.http.get("/v1/tasks/tsk_any", headers=key)
    assert (await api.tool(key, "get_task", {"task_id": "tsk_any"}))["isError"] is True

    assert failures(await log_of(api, account, info["id"])) == [
        ("get_task", "internal", "Internal error."),
        ("GET /v1/tasks/{task_id}", "internal", "Internal error."),
    ]


async def test_a_request_above_the_rate_limit_has_an_entry(launch: Launch) -> None:
    async with launch(rate_limit_per_minute=2) as running:
        api = running.api
        account, key, info = await api.agent()
        for _ in range(2):
            assert (await api.http.get("/v1/tasks", headers=key)).status_code == 200
        error(await api.http.get("/v1/tasks", headers=key), 429, "rate_limited")
        assert (await api.rpc(key, "tools/list")).status_code == 429

        entries = await log_of(api, account, info["id"])
        assert failures(entries) == [
            ("POST /mcp", "rate_limited", "Too many requests. Slow down."),
            ("GET /v1/tasks", "rate_limited", "Too many requests. Slow down."),
        ]
        assert len(entries) == 4


async def test_an_entry_never_has_content_or_a_credential(running: Running, raw: RawStore) -> None:
    api = running.api
    account, key, info = await api.agent(label=f"Agent {MARKER}"[:40])
    other_key, other_info = await api.key(account, "Other")
    secret = secret_of(key)

    task = await api.task(key, title=MARKER, description=f"{MARKER} in the description")
    await api.patch(key, task["id"], status_note=MARKER, title=f"{MARKER} again")
    await api.ask(key, prompt=f"{MARKER}?", kind="choice", options=[f"{MARKER[:20]} A", f"{MARKER[:20]} B"])
    await api.message(key, to=other_info["id"], text=MARKER, task_id=task["id"])
    error(await api.http.post("/v1/messages", json={"to": MARKER, "text": MARKER}, headers=key), 404, "not_found")
    error(await api.http.post("/v1/tasks", json={"title": MARKER * 20}, headers=key), 422, "validation_failed")
    await api.tool(key, "create_task", {"title": MARKER, "description": MARKER})
    await api.tool(key, "update_task", {"task_id": task["id"], "status_note": MARKER})
    await api.tool(key, "notify_user", {"title": MARKER, "body": MARKER})
    await api.tool(key, "send_message", {"to": "Other", "text": MARKER})
    await api.tool(key, "ask_user", {"prompt": f"{MARKER}?", "kind": "text", "wait_seconds": 0})
    assert (await api.tool(key, "create_task", {"title": MARKER * 20}))["isError"] is True
    # The key in the URL is the credential of a client that cannot send headers.
    in_url = await api.rpc(
        {}, "tools/call", {"name": "list_tasks", "arguments": {}}, headers={"Mcp-Name": "list_tasks"}
    )
    assert in_url.status_code == 401
    call: Json = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {}}}
    accept = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    assert (await api.http.post(f"/mcp?key={secret}", json=call, headers=accept)).status_code in {200, 400}
    assert len((await api.inbox(other_key))["items"]) == 2

    entries = await log_of(api, account, info["id"])
    assert len(entries) == 13
    stored = "\n".join(await raw.sorted_set(f"t:key:{info['id']}:log"))
    assert len(stored.splitlines()) == 13
    for text in (json.dumps(entries), stored):
        assert MARKER not in text
        assert "ZEBRA" not in text
        assert secret not in text
        assert secret_of(account) not in text
        assert task["id"] not in text
    assert {name for entry in entries for name in entry} == {
        "id",
        "at",
        "surface",
        "action",
        "status",
        "code",
        "message",
        "duration_ms",
        "client",
        "host",
    }


async def test_a_log_belongs_to_one_key_and_only_its_account_reads_it(running: Running) -> None:
    api = running.api
    account, key, info = await api.agent()
    second_key, second_info = await api.key(account, "Second")
    stranger, stranger_key, stranger_info = await api.agent()

    await api.task(key)
    await api.http.get("/v1/tasks", headers=second_key)
    await api.http.get("/v1/inbox", headers=second_key)
    await api.tool(stranger_key, "list_tasks", {})

    assert actions(await log_of(api, account, info["id"])) == ["POST /v1/tasks"]
    assert actions(await log_of(api, account, second_info["id"])) == ["GET /v1/inbox", "GET /v1/tasks"]
    assert actions(await log_of(api, stranger, stranger_info["id"])) == ["list_tasks"]

    path = f"/v1/keys/{info['id']}/activity"
    error(await api.http.get(path, headers=stranger), 404, "not_found")
    error(await api.http.get("/v1/keys/key_unknown/activity", headers=account), 404, "not_found")
    error(await api.http.get(path, headers=key), 403, "forbidden")
    error(await api.http.get(path, headers=second_key), 403, "forbidden")
    error(await api.http.get(path), 401, "unauthorized")
    # The refused read of a key is a request of that key, so it is in the log of that key and of no other.
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/keys/{key_id}/activity", "POST /v1/tasks"]
    assert actions(await log_of(api, stranger, stranger_info["id"])) == ["list_tasks"]


async def test_requests_with_no_api_key_and_calls_that_only_wait_have_no_entry(running: Running) -> None:
    api, clock = running.api, running.clock
    account, key, info = await api.agent()
    task = await api.task(account, key_id=info["id"])

    assert (await api.http.get("/health", headers=key)).status_code == 200
    assert (await api.http.get("/", headers=key)).status_code == 200
    assert (await api.http.get("/v1/unknown", headers=key)).status_code == 404
    assert (await api.http.get(f"/v1/tasks/{task['id']}", headers=account)).status_code == 200
    error(await api.http.get("/v1/tasks", headers={"Authorization": "Bearer mck_" + "a" * 43}), 401, "unauthorized")
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    accept = {**key, "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    assert (await api.http.post("/mcp", json=initialized, headers=accept)).status_code == 202
    assert await log_of(api, account, info["id"]) == []

    # The item from the person is unread. Take it, so that the next read of the inbox waits.
    assert len((await api.inbox(key))["items"]) == 1
    question = await api.ask(key)
    empty = asyncio.create_task(api.inbox(key, wait=20))
    pending = asyncio.create_task(api.http.get(f"/v1/questions/{question['id']}?wait=20", headers=key))
    through_tool = asyncio.create_task(api.tool(key, "check_inbox", {"wait_seconds": 20}))
    await clock.sleeping(20, 3)
    clock.advance(20)
    assert (await asyncio.wait_for(empty, SIGNAL_SECONDS))["items"] == []
    assert (await asyncio.wait_for(pending, SIGNAL_SECONDS)).json()["status"] == "pending"
    assert (await asyncio.wait_for(through_tool, SIGNAL_SECONDS))["structuredContent"]["items"] == []
    assert actions(await log_of(api, account, info["id"])) == ["POST /v1/questions", "GET /v1/inbox"]

    # A call that waits and then gets an item has one entry, with the time that it waited.
    held = asyncio.create_task(api.inbox(key, wait=50))
    await clock.sleeping(50)
    clock.advance(12)
    await api.message(account, key_id=info["id"], text="Stop.")
    assert len((await asyncio.wait_for(held, SIGNAL_SECONDS))["items"]) == 1
    newest = (await log_of(api, account, info["id"]))[0]
    assert (newest["action"], newest["status"], newest["duration_ms"]) == ("GET /v1/inbox", "ok", 12_000)


async def test_a_log_keeps_its_newest_entries_up_to_the_limit(launch: Launch, raw: RawStore) -> None:
    async with launch(limits=Limits(activity_log_entries=5)) as running:
        api = running.api
        account, key, info = await api.agent()
        for number in range(8):
            await api.task(key, title=f"Task {number}")

        entries = await log_of(api, account, info["id"])
        assert ids(entries) == ["act_000008", "act_000007", "act_000006", "act_000005", "act_000004"]
        assert len(await raw.sorted_set(f"t:key:{info['id']}:log")) == 5


async def test_an_entry_goes_after_7_days(running: Running, raw: RawStore) -> None:
    api, clock = running.api, running.clock
    account, key, info = await api.agent()
    log_key = f"t:key:{info['id']}:log"

    await api.task(key)
    clock.advance(WEEK - 60)
    await api.http.get("/v1/tasks", headers=key)
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/tasks", "POST /v1/tasks"]

    # The read hides an entry that is past its time. The next write of the key removes it from the store.
    clock.advance(120)
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/tasks"]
    assert len(await raw.sorted_set(log_key)) == 2
    await api.http.get("/v1/agents", headers=key)
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/agents", "GET /v1/tasks"]
    assert len(await raw.sorted_set(log_key)) == 2

    clock.advance(WEEK)
    assert await log_of(api, account, info["id"]) == []


async def test_the_app_reads_a_log_in_pages(api: Api) -> None:
    account, key, info = await api.agent()
    for number in range(5):
        await api.task(key, title=f"Task {number}")
    path = f"/v1/keys/{info['id']}/activity"

    async def page(cursor: str | None = None) -> Json:
        params = {"limit": "2"} if cursor is None else {"limit": "2", "cursor": cursor}
        body: Json = (await api.http.get(path, params=params, headers=account)).json()
        return body

    first = await page()
    assert ids(first["items"]) == ["act_000005", "act_000004"]
    # An entry that comes between two pages moves no entry to a different page.
    await api.http.get("/v1/tasks", headers=key)
    second = await page(first["next_cursor"])
    assert ids(second["items"]) == ["act_000003", "act_000002"]
    last = await page(second["next_cursor"])
    assert (ids(last["items"]), last["next_cursor"]) == (["act_000001"], None)

    everything = (await api.http.get(path, headers=account)).json()
    assert (len(everything["items"]), everything["next_cursor"]) == (6, None)
    for query in ("limit=0", "limit=101", "cursor=nope"):
        body = error(await api.http.get(f"{path}?{query}", headers=account), 422, "validation_failed")
        assert list(body["fields"]) == [query.partition("=")[0]]


async def test_a_deleted_key_or_account_takes_its_log_with_it(launch: Launch, raw: RawStore) -> None:
    async with launch(limits=Limits(purge_batch=2)) as running:
        api = running.api
        account, key, info = await api.agent()
        kept_key, kept_info = await api.key(account, "Kept")
        for number in range(5):
            await api.task(key, title=f"Task {number}")
        await api.task(kept_key)
        await running.services.activity.drain()
        logs = [f"t:key:{info['id']}:log", f"t:key:{kept_info['id']}:log"]
        assert await raw.exists(*logs) == 2

        assert (await api.http.delete(f"/v1/keys/{info['id']}", headers=account)).status_code == 204
        assert [name for name in await raw.keys() if info["id"] in name] == []
        error(await api.http.get(f"/v1/keys/{info['id']}/activity", headers=account), 404, "not_found")
        assert actions(await log_of(api, account, kept_info["id"])) == ["POST /v1/tasks"]

        assert (await api.http.delete("/v1/account", headers=account)).status_code == 204
        assert [name for name in await raw.keys() if ":rl:" not in name] == []


async def test_an_entry_that_waits_when_its_key_is_deleted_is_not_written(
    running: Running, raw: RawStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = running.api
    account, key, info = await api.agent()
    kept_key, kept_info = await api.key(account, "Kept")
    gate, held = hold_log_writes(monkeypatch)

    await api.task(key)
    await api.task(kept_key)
    await settle()
    assert (await api.http.delete(f"/v1/keys/{info['id']}", headers=account)).status_code == 204
    gate.set()
    await running.services.activity.drain()

    assert sum(held) == 2
    assert [name for name in await raw.keys() if info["id"] in name] == []
    assert actions(await log_of(api, account, kept_info["id"])) == ["POST /v1/tasks"]


async def test_the_store_writes_an_entry_only_for_a_key_that_its_account_has(store: Store, raw: RawStore) -> None:
    account = Account(id=AccountId("acc_1"), created_at=1.0)
    key = Key(id=KeyId("key_1"), label="Agent", prefix="mck_test", last4="test", created_at=1.0)
    await store.create_account(account, hash_secret(Secret("mca_test")))
    assert await store.create_key(account.id, key, hash_secret(Secret("mck_test")), EVENT) == "ok"
    log_key = store.keys.activity_log(key.id)

    await store.add_activity(
        [
            (account.id, key.id, make_entry(1, 100.0)),
            (AccountId("acc_other"), key.id, make_entry(2, 101.0)),
            (account.id, KeyId("key_unknown"), make_entry(3, 102.0)),
        ]
    )
    assert [name for name in await raw.keys() if name.endswith(":log")] == [log_key]
    assert await store.activity_log(AccountId("acc_other"), key.id, 200.0) is None
    docs = await store.activity_log(account.id, key.id, 200.0)
    assert docs is not None
    assert [ActivityEntry.model_validate_json(doc) for doc in docs] == [make_entry(1, 100.0)]

    # The log goes with the first step of the deletion, and an entry that comes after that step is not written.
    assert await store.detach_key(account.id, key.id, 300.0, EVENT)
    await store.add_activity([(account.id, key.id, make_entry(4, 301.0))])
    assert await raw.exists(log_key) == 0

    # A key that an earlier version of the server detached can still have a log. The last purge step removes it.
    await raw.add_to_sorted_set(log_key, 302.0, make_entry(5, 302.0).model_dump_json())
    done, _ = await store.purge_key(key.id, 10)
    assert done
    assert [name for name in await raw.keys() if key.id in name] == []


async def test_a_tool_call_makes_no_store_call_for_its_entry_and_does_not_wait_for_it(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = running.api
    account, key, info = await api.agent()
    task = (await api.tool(key, "create_task", {"title": "Measured"}))["structuredContent"]
    await running.services.activity.drain()
    gate, held = hold_log_writes(monkeypatch)
    calls = count_store_calls(monkeypatch)

    got = await api.tool(key, "get_task", {"task_id": task["id"]})
    # The result is here. The request made its two store calls: the credential, then the task.
    assert got["structuredContent"]["id"] == task["id"]
    assert calls == ["script", "batch"]
    by_app = len(calls)
    assert (await api.http.get(f"/v1/tasks/{task['id']}", headers=account)).status_code == 200
    by_app = len(calls) - by_app
    by_key = len(calls)
    assert (await api.http.get(f"/v1/tasks/{task['id']}", headers=key)).status_code == 200
    # A request of a key, which has an entry, makes as many store calls as the same request of the app, which has none.
    assert len(calls) - by_key == by_app == 2

    # The writer has the entries, and the store has none of them: the three responses did not wait for it.
    await settle()
    assert held == [1]
    gate.set()
    await running.services.activity.drain()
    assert held == [1, 1]
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/tasks/{task_id}", "get_task", "create_task"]


async def test_a_store_that_refuses_an_entry_does_not_fail_the_request(
    running: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = running.api
    account, key, info = await api.agent()
    add = Store.add_activity
    refused: list[int] = []

    async def refuse_once(self: Store, entries: Sequence[tuple[AccountId, KeyId, ActivityEntry]]) -> None:
        if not refused:
            refused.append(len(entries))
            raise ConnectionError("the store went away")
        await add(self, entries)

    monkeypatch.setattr(Store, "add_activity", refuse_once)
    await api.task(key)
    await running.services.activity.drain()
    assert (await api.http.get("/v1/tasks", headers=key)).status_code == 200

    assert refused == [1]
    assert actions(await log_of(api, account, info["id"])) == ["GET /v1/tasks"]
