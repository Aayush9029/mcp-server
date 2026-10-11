"""The inbox of a key: what the person and other agents send to an agent, and how the agent gets it."""

import asyncio

import httpx2
import pytest

from mcp_client_server.limits import Limits
from mcp_client_server.models import PatchField
from mcp_client_server.services.inbox import describe_change
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
    make_task,
    mcp_client,
    settle,
    structured,
    tool_text,
)

DAY = 24 * 3600
HOUR = 3600
PERSON = {"type": "person"}
NOTE = "\nInbox: {} unread. Call check_inbox."


def waiting_inbox(api: Api, key: Headers, wait: int = 50, **query: object) -> asyncio.Task[Json]:
    return asyncio.create_task(api.inbox(key, wait=wait, **query))


def texts(page: Json) -> list[str]:
    return [item["text"] for item in page["items"]]


async def test_each_change_by_the_person_adds_one_item_and_the_agent_reads_them_in_order(api: Api) -> None:
    account, key, key_info = await api.agent()
    task = await api.task(account, key_id=key_info["id"], title="Fix the login loop")
    await api.patch(account, task["id"], status="cancelled", priority="urgent", title="Fix the loop", description="x")
    question = await api.ask(key)
    await api.answer(account, question["id"], "No")
    await api.http.delete(f"/v1/tasks/{task['id']}", headers=account)

    page = await api.inbox(key)
    read_at = page["items"][0]["read_at"]
    assert page == {
        "unread": 0,
        "items": [
            {
                "id": page["items"][0]["id"],
                "kind": "task_assigned",
                "from": PERSON,
                "task_id": task["id"],
                "question_id": None,
                "text": "Fix the login loop",
                "created_at": page["items"][0]["created_at"],
                "read_at": read_at,
            },
            {
                "id": page["items"][1]["id"],
                "kind": "task_changed",
                "from": PERSON,
                "task_id": task["id"],
                "question_id": None,
                "text": (
                    "Status changed from todo to cancelled. Priority changed from medium to urgent. "
                    'Title changed to "Fix the loop". Description changed.'
                ),
                "created_at": page["items"][1]["created_at"],
                "read_at": read_at,
            },
            {
                "id": page["items"][2]["id"],
                "kind": "answer",
                "from": PERSON,
                "task_id": None,
                "question_id": question["id"],
                "text": "No",
                "created_at": page["items"][2]["created_at"],
                "read_at": read_at,
            },
            {
                "id": page["items"][3]["id"],
                "kind": "task_deleted",
                "from": PERSON,
                "task_id": task["id"],
                "question_id": None,
                "text": "Fix the loop",
                "created_at": page["items"][3]["created_at"],
                "read_at": read_at,
            },
        ],
    }
    assert all(item["id"].startswith("inb_") for item in page["items"])
    assert isinstance(read_at, float)
    assert await api.inbox(key) == {"items": [], "unread": 0}


async def test_a_change_that_the_key_makes_itself_adds_no_item(api: Api) -> None:
    account, key, key_info = await api.agent()
    own = await api.task(key)
    await api.patch(key, own["id"], status="in_progress", title="Mine", priority="high")
    question = await api.ask(key)
    await api.http.delete(f"/v1/questions/{question['id']}", headers=key)
    await api.http.delete(f"/v1/tasks/{own['id']}", headers=key)
    # A change by the person of a field that is the agent's own is no news for the agent.
    from_app = await api.task(account, key_id=key_info["id"])
    await api.inbox(key)
    await api.patch(account, from_app["id"], progress=50, status_note="Half", notify=False)

    assert await api.inbox(key) == {"items": [], "unread": 0}


@pytest.mark.parametrize(
    ("changed", "text"),
    [
        (("status",), "Status changed from todo to cancelled."),
        (("priority", "progress"), "Priority changed from medium to urgent."),
        (("title", "description"), 'Title changed to "New". Description changed.'),
        (("progress", "status_note", "notify"), None),
    ],
)
def test_the_line_of_a_changed_task_names_only_what_the_agent_must_know(
    changed: tuple[PatchField, ...], text: str | None
) -> None:
    old = make_task(status="todo", priority="medium", title="Old")
    new = make_task(status="cancelled", priority="urgent", title="New", description="More")
    assert describe_change(old, new, frozenset(changed)) == text


async def test_the_person_and_other_agents_send_messages(running: Running) -> None:
    api, http = running.api, running.http
    account = await api.account()
    cursor, cursor_info = await api.key(account, "Cursor")
    claude, claude_info = await api.key(account, "Claude Code")
    await api.device(account)
    task = await api.task(claude, notify=False)
    await running.pushes_done()
    running.push.sent.clear()

    from_person = await api.message(account, key_id=cursor_info["id"], text="  Stop and rebase first. ")
    assert (from_person["kind"], from_person["from"], from_person["text"]) == (
        "message",
        PERSON,
        "Stop and rebase first.",
    )
    by_id = await api.message(claude, to=cursor_info["id"], text="The API is ready.", task_id=task["id"])
    by_label = await api.message(claude, to="Cursor", text="Use v2.")
    sender = {"type": "agent", "key_id": claude_info["id"], "label": "Claude Code"}
    assert (by_id["from"], by_id["task_id"], by_label["from"], by_label["read_at"]) == (
        sender,
        task["id"],
        sender,
        None,
    )

    assert texts(await api.inbox(cursor)) == ["Stop and rebase first.", "The API is ready.", "Use v2."]
    assert await api.inbox(claude) == {"items": [], "unread": 0}
    # A message goes to an agent, not to the person: no device gets a push.
    await running.pushes_done()
    assert running.push.sent == []

    for to in ("Claude Code", claude_info["id"], "Nobody", "key_missing", "cursor"):
        error(await http.post("/v1/messages", json={"to": to, "text": "Hi"}, headers=claude), 404, "not_found")
    error(
        await http.post("/v1/messages", json={"to": "Cursor", "text": "Hi", "task_id": "tsk_missing"}, headers=claude),
        404,
        "not_found",
    )
    error(await http.post("/v1/messages", json={"text": "Hi"}), 401, "unauthorized")


async def test_a_label_that_two_keys_have_does_not_name_a_receiver(api: Api) -> None:
    account = await api.account()
    sender, _ = await api.key(account, "Sender")
    _, twin = await api.key(account, "Twin")
    await api.key(account, "Twin")

    error(await api.http.post("/v1/messages", json={"to": "Twin", "text": "Hi"}, headers=sender), 404, "not_found")
    assert (await api.message(sender, to=twin["id"], text="Hi"))["kind"] == "message"


async def test_message_body_rules_for_each_credential_type(api: Api) -> None:
    account, key, key_info = await api.agent()
    stranger = await api.account()
    other_id = key_info["id"]
    cases: list[tuple[Headers, Json, str]] = [
        (account, {"text": "Hi"}, "key_id"),
        (account, {"key_id": other_id, "to": "Cursor", "text": "Hi"}, "to"),
        (key, {"text": "Hi"}, "to"),
        (key, {"to": "Cursor", "key_id": other_id, "text": "Hi"}, "key_id"),
        (account, {"key_id": other_id}, "text"),
        (account, {"key_id": other_id, "text": "   "}, "text"),
        (account, {"key_id": other_id, "text": "x" * 2001}, "text"),
        (account, {"key_id": other_id, "text": "Hi", "kind": "answer"}, "kind"),
    ]
    for auth, body, field in cases:
        problem = error(await api.http.post("/v1/messages", json=body, headers=auth), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body
    assert len((await api.message(account, key_id=other_id, text="x" * 2000))["text"]) == 2000
    error(
        await api.http.post("/v1/messages", json={"key_id": other_id, "text": "Hi"}, headers=stranger), 404, "not_found"
    )


async def test_reading_marks_items_read_unless_mark_read_is_false(api: Api) -> None:
    account, key, key_info = await api.agent()
    for number in range(3):
        await api.message(account, key_id=key_info["id"], text=f"M{number}")
    view_path = f"/v1/keys/{key_info['id']}/inbox"

    peek = await api.inbox(key, limit=2, mark_read="false")
    assert (texts(peek), peek["unread"], [item["read_at"] for item in peek["items"]]) == (["M0", "M1"], 3, [None, None])
    view = (await api.http.get(view_path, headers=account)).json()
    assert (texts(view), view["unread"]) == (["M2", "M1", "M0"], 3)

    first = await api.inbox(key, limit=2)
    assert (texts(first), first["unread"]) == (["M0", "M1"], 1)
    assert all(isinstance(item["read_at"], float) for item in first["items"])
    rest = await api.inbox(key)
    assert (texts(rest), rest["unread"]) == (["M2"], 0)

    # The person sees the read state. The view changes nothing.
    view = (await api.http.get(view_path, headers=account)).json()
    assert (texts(view), view["unread"]) == (["M2", "M1", "M0"], 0)
    assert [item["read_at"] for item in view["items"]] == [
        rest["items"][0]["read_at"],
        *[first["items"][0]["read_at"]] * 2,
    ]

    for query in ("limit=0", "limit=51", "wait=51", "wait=-1", "mark_read=maybe"):
        problem = error(await api.http.get(f"/v1/inbox?{query}", headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [query.split("=")[0]]


async def test_a_credential_reaches_only_its_own_inbox(api: Api) -> None:
    account = await api.account()
    key_a, info_a = await api.key(account, "A")
    key_b, info_b = await api.key(account, "B")
    stranger, stranger_key, _ = await api.agent("Stranger")
    await api.message(account, key_id=info_a["id"], text="For A")
    view_path = f"/v1/keys/{info_a['id']}/inbox"

    assert await api.inbox(key_b) == {"items": [], "unread": 0}
    assert await api.inbox(stranger_key) == {"items": [], "unread": 0}
    error(await api.http.get(view_path, headers=stranger), 404, "not_found")
    error(await api.http.get("/v1/keys/key_missing/inbox", headers=account), 404, "not_found")
    for path, auth in ((view_path, key_a), ("/v1/inbox", account), ("/v1/agents", account)):
        error(await api.http.get(path, headers=auth), 403, "forbidden")
    error(
        await api.http.post("/v1/messages", json={"to": info_a["id"], "text": "Hi"}, headers=stranger_key),
        404,
        "not_found",
    )
    assert texts(await api.inbox(key_a)) == ["For A"]

    agents = (await api.http.get("/v1/agents", headers=key_b)).json()["agents"]
    assert agents == [
        {
            "key_id": info_b["id"],
            "label": "B",
            "is_self": True,
            "last_client": None,
            "last_used_at": agents[0]["last_used_at"],
        },
        {
            "key_id": info_a["id"],
            "label": "A",
            "is_self": False,
            "last_client": None,
            "last_used_at": agents[1]["last_used_at"],
        },
    ]


async def test_the_inbox_keeps_the_newest_unread_items_up_to_its_limit(launch: Launch) -> None:
    async with launch(limits=Limits(max_unread_inbox_items=3)) as running:
        api = running.api
        account, key, key_info = await api.agent()
        for number in range(5):
            await api.message(account, key_id=key_info["id"], text=f"M{number}")
        page = await api.inbox(key)
        assert (texts(page), page["unread"]) == (["M2", "M3", "M4"], 0)
        # The read items do not count: three more unread items fit.
        for number in range(5, 8):
            await api.message(account, key_id=key_info["id"], text=f"M{number}")
        assert texts(await api.inbox(key)) == ["M5", "M6", "M7"]


async def test_an_unread_item_lives_7_days_and_a_read_item_24_hours(running: Running) -> None:
    api, clock = running.api, running.clock
    account, key, key_info = await api.agent()
    view_path = f"/v1/keys/{key_info['id']}/inbox"

    async def view() -> tuple[list[str], int]:
        body = (await api.http.get(view_path, headers=account)).json()
        return texts(body), body["unread"]

    await api.message(account, key_id=key_info["id"], text="First")
    clock.advance(6 * DAY)
    await api.message(account, key_id=key_info["id"], text="Second")
    assert texts(await api.inbox(key, limit=1)) == ["First"]

    clock.advance(23 * HOUR)
    assert await view() == (["Second", "First"], 1)
    # "First" was read 25 hours ago. "Second" is unread and 25 hours old.
    clock.advance(2 * HOUR)
    assert await view() == (["Second"], 1)

    clock.advance(6 * DAY)
    assert await view() == ([], 0)
    assert await api.inbox(key) == {"items": [], "unread": 0}
    listed = await api.tool(key, "list_tasks", {})
    assert "Inbox:" not in listed["content"][0]["text"]


async def test_messages_from_one_sender_have_an_hourly_limit(launch: Launch) -> None:
    async with launch(limits=Limits(messages_per_hour=2)) as running:
        api, http = running.api, running.http
        account = await api.account()
        sender, _ = await api.key(account, "Sender")
        other, other_info = await api.key(account, "Other")
        for _ in range(2):
            await api.message(sender, to="Other", text="Hi")
        limited = await http.post("/v1/messages", json={"to": "Other", "text": "Again"}, headers=sender)
        error(limited, 429, "rate_limited")
        assert 0 < int(limited.headers["retry-after"]) <= 3600
        tool = await api.tool(sender, "send_message", {"to": "Other", "text": "Again"})
        assert tool["isError"] is True
        assert "2 messages in one hour" in tool["content"][0]["text"]

        # The person and the other agent are different senders.
        await api.message(account, key_id=other_info["id"], text="From the person")
        await api.message(other, to="Sender", text="From the other agent")
        running.clock.advance(int(limited.headers["retry-after"]))
        await api.message(sender, to="Other", text="After the hour")


async def test_seen_at_is_the_time_the_agent_first_gets_the_task(
    running: Running, redis_listener: EventListener
) -> None:
    api = running.api
    account, key, key_info = await api.agent()

    async def seen_at(task: Json, auth: Headers = account) -> float | None:
        value: float | None = (await api.http.get(f"/v1/tasks/{task['id']}", headers=auth)).json()["seen_at"]
        return value

    own = await api.task(key)
    assert own["seen_at"] == own["created_at"]

    through_inbox = await api.task(account, key_id=key_info["id"], title="Through the inbox")
    assert through_inbox["seen_at"] is None
    # A read with the REST API is not the agent's inbox and not a tool result.
    assert await seen_at(through_inbox, key) is None
    await redis_listener.start()
    await api.inbox(key, mark_read="false")
    seen = await seen_at(through_inbox)
    assert seen is not None
    assert seen > through_inbox["created_at"]
    assert await redis_listener.names() == ["task.updated"]
    stored = (await api.http.get(f"/v1/tasks/{through_inbox['id']}", headers=account)).json()
    assert stored == {**through_inbox, "seen_at": seen}

    through_tool = await api.task(account, key_id=key_info["id"], title="Through a tool")
    listed = await api.task(account, key_id=key_info["id"], title="Through the list")
    got = await api.tool(key, "get_task", {"task_id": through_tool["id"]})
    assert got["structuredContent"]["seen_at"] == await seen_at(through_tool) is not None
    assert await seen_at(listed) is None
    tasks = (await api.tool(key, "list_tasks", {}))["structuredContent"]["tasks"]
    assert all(task["seen_at"] is not None for task in tasks)
    # The first time counts: a second read does not move it.
    await api.inbox(key)
    assert await seen_at(through_inbox) == seen


async def test_a_call_that_waits_returns_when_an_item_arrives(running: Running, redis_listener: EventListener) -> None:
    api = running.api
    account, key, key_info = await api.agent()
    await redis_listener.start()

    held = waiting_inbox(api, key)
    await running.clock.sleeping(50)
    await api.message(account, key_id=key_info["id"], text="Stop.")

    # The clock did not move, so the wait did not end by its time limit: the inbox event woke the call.
    page = await asyncio.wait_for(held, SIGNAL_SECONDS)
    assert (texts(page), page["unread"]) == (["Stop."], 0)
    assert running.services.events.stream_count == 0
    assert await redis_listener.names() == ["inbox.updated", "inbox.updated"]


async def test_a_call_that_waits_returns_an_empty_inbox_after_its_time(running: Running) -> None:
    api = running.api
    _, key, _ = await api.agent()

    held = waiting_inbox(api, key, wait=20)
    await running.clock.sleeping(20)
    running.clock.advance(19)
    await settle()
    assert not held.done()

    running.clock.advance(1)
    assert await asyncio.wait_for(held, SIGNAL_SECONDS) == {"items": [], "unread": 0}
    assert running.services.events.stream_count == 0


async def test_a_call_that_waits_wakes_for_an_item_that_a_different_worker_adds(launch: Launch) -> None:
    async with launch() as first, launch() as second:
        account, key, key_info = await first.api.agent()

        held = waiting_inbox(first.api, key)
        await first.clock.sleeping(50)
        await second.api.message(account, key_id=key_info["id"], text="From the other worker")

        assert texts(await asyncio.wait_for(held, SIGNAL_SECONDS)) == ["From the other worker"]


async def test_many_calls_wait_at_once_and_hold_no_store_connection(launch: Launch, store_url: str | None) -> None:
    # With a real store, the application has its own pool of two connections. 100 calls that each kept one
    # connection could not all wait.
    pool = {"own_connections": True, "redis_max_connections": 2} if store_url else {}
    count = 100 if store_url else 40
    async with launch(**pool) as running:
        api = running.api
        account, key, key_info = await api.agent()

        held = [waiting_inbox(api, key, mark_read="false") for _ in range(count)]
        await running.clock.sleeping(50, count)
        assert running.services.events.stream_count == count
        assert (await api.http.get("/health")).status_code == 200
        await api.message(account, key_id=key_info["id"], text="For all")

        pages = await asyncio.wait_for(asyncio.gather(*held), SIGNAL_SECONDS)
        assert {(tuple(texts(page)), page["unread"]) for page in pages} == {(("For all",), 1)}
        assert running.services.events.stream_count == 0


async def test_one_item_goes_to_one_of_two_calls_that_wait_and_the_other_call_waits_on(running: Running) -> None:
    api = running.api
    account, key, key_info = await api.agent()

    held = [waiting_inbox(api, key, wait=30), waiting_inbox(api, key, wait=30)]
    await running.clock.sleeping(30, 2)
    await api.message(account, key_id=key_info["id"], text="Only one")
    done, pending = await asyncio.wait(held, timeout=SIGNAL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    await settle()
    assert [texts(task.result()) for task in done] == [["Only one"]]
    assert len(pending) == 1

    running.clock.advance(30)
    assert await asyncio.wait_for(pending.pop(), SIGNAL_SECONDS) == {"items": [], "unread": 0}


async def test_a_call_that_waits_ends_when_its_key_is_deleted(running: Running) -> None:
    api = running.api
    account, key, key_info = await api.agent()

    held = waiting_inbox(api, key)
    await running.clock.sleeping(50)
    await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)
    assert await asyncio.wait_for(held, SIGNAL_SECONDS) == {"items": [], "unread": 0}


async def test_each_tool_result_ends_with_the_inbox_note_while_items_are_unread(api: Api) -> None:
    account, key, key_info = await api.agent()

    def text_of(result: Json) -> str:
        value: str = result["content"][0]["text"]
        return value

    created = await api.tool(key, "create_task", {"title": "Quiet"})
    assert "Inbox:" not in text_of(created)

    await api.message(account, key_id=key_info["id"], text="First")
    await api.message(account, key_id=key_info["id"], text="Second")
    listed = await api.tool(key, "list_tasks", {})
    assert text_of(listed).endswith("Quiet" + NOTE.format(2))
    assert set(listed["structuredContent"]) == {"tasks", "next_cursor"}
    missing = await api.tool(key, "get_task", {"task_id": "tsk_missing"})
    assert missing["isError"] is True
    assert text_of(missing).endswith(NOTE.format(2))

    one = await api.tool(key, "check_inbox", {"limit": 1})
    assert (texts(one["structuredContent"]), one["structuredContent"]["unread"]) == (["First"], 1)
    item = one["structuredContent"]["items"][0]
    assert text_of(one) == f"1 item, oldest first.\n{item['id']} [message] from the person: First" + NOTE.format(1)

    last = await api.tool(key, "check_inbox", {})
    assert text_of(last).endswith(": Second")
    assert text_of(await api.tool(key, "check_inbox", {})) == "The inbox has no unread items."
    assert "Inbox:" not in text_of(await api.tool(key, "list_tasks", {}))


async def test_check_inbox_holds_the_tool_call_until_an_item_arrives(running: Running) -> None:
    api = running.api
    account = await api.account()
    key, _ = await api.key(account, "Claude Code")
    cursor, cursor_info = await api.key(account, "Cursor")

    call = asyncio.create_task(api.tool(key, "check_inbox", {"wait_seconds": 30}))
    await running.clock.sleeping(30)
    sent = await api.tool(cursor, "send_message", {"to": "Claude Code", "text": "The API is ready for you."})
    assert sent["structuredContent"]["from"] == {"type": "agent", "key_id": cursor_info["id"], "label": "Cursor"}

    result = await asyncio.wait_for(call, SIGNAL_SECONDS)
    assert result["isError"] is False
    assert result["structuredContent"]["items"] == [
        {**sent["structuredContent"], "read_at": result["structuredContent"]["items"][0]["read_at"]}
    ]
    assert result["content"][0]["text"].endswith(
        f"[message] from Cursor ({cursor_info['id']}): The API is ready for you."
    )

    timed = asyncio.create_task(api.tool(key, "check_inbox", {"wait_seconds": 30}))
    await running.clock.sleeping(30)
    running.clock.advance(30)
    assert (await asyncio.wait_for(timed, SIGNAL_SECONDS))["structuredContent"] == {"items": [], "unread": 0}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_inbox_tools_through_the_sdk_client(launch: Launch, mode: str) -> None:
    async with launch() as running:
        api = running.api
        account = await api.account()
        key, key_info = await api.key(account, "Claude Code")
        _, other_info = await api.key(account, "Cursor")
        task = await api.task(account, key_id=key_info["id"], title="From the app")
        async with mcp_client(running.app, key, mode) as client:
            assert client.instructions is not None
            assert "6. Call `check_inbox` when you start and after each unit of work." in client.instructions
            assert "7. A `task_changed` item with a `cancelled` status means: stop that work." in client.instructions

            agents = structured(await client.call_tool("list_agents", {}))["agents"]
            assert [(agent["label"], agent["is_self"]) for agent in agents] == [
                ("Cursor", False),
                ("Claude Code", True),
            ]

            page = structured(await client.call_tool("check_inbox", {"mark_read": False}))
            assert [(item["kind"], item["task_id"], item["from"]) for item in page["items"]] == [
                ("task_assigned", task["id"], PERSON)
            ]
            assert page["unread"] == 1

            sent = structured(await client.call_tool("send_message", {"to": other_info["id"], "text": "Hello"}))
            assert (sent["kind"], sent["text"]) == ("message", "Hello")
            for arguments, word in [
                ({"to": "Claude Code", "text": "To me"}, "list_agents"),
                ({"to": "Cursor", "text": ""}, "text"),
                ({"to": "Cursor", "text": "Hi", "task_id": "tsk_missing"}, "Task not found"),
            ]:
                result = await client.call_tool("send_message", arguments)
                assert result.is_error is True, arguments
                assert word in tool_text(result), tool_text(result)
            # A receiver that is not found is not a task problem, so the text does not send the model to list_tasks.
            wrong_to = await client.call_tool("send_message", {"to": "Nobody", "text": "Hi"})
            assert wrong_to.is_error is True
            assert "list_agents" in tool_text(wrong_to)
            assert "list_tasks" not in tool_text(wrong_to)
            wrong = await client.call_tool("check_inbox", {"limit": 51})
            assert wrong.is_error is True


async def test_a_deleted_key_or_account_takes_its_inbox_with_it(running: Running, raw: RawStore) -> None:
    api = running.api
    account = await api.account()
    key, key_info = await api.key(account, "Doomed")
    other, other_info = await api.key(account, "Other")
    for receiver in (key_info, other_info):
        await api.message(account, key_id=receiver["id"], text="Unread")
        await api.message(account, key_id=receiver["id"], text="Read")
    await api.inbox(key, limit=1)
    await api.inbox(other, limit=1)

    assert (await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)).status_code == 204
    assert len([name for name in await raw.keys() if ":inb:" in name]) == 2
    assert not [name for name in await raw.keys() if key_info["id"] in name]
    assert (await api.http.delete("/v1/account", headers=account)).status_code == 204
    assert [name for name in await raw.keys() if ":rl:" not in name] == []


async def test_a_message_to_a_key_of_an_account_that_is_gone_is_not_found(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, key_info = await api.agent()
    await client.delete(f"/v1/keys/{key_info['id']}", headers=account)
    error(
        await client.post("/v1/messages", json={"key_id": key_info["id"], "text": "Hi"}, headers=account),
        404,
        "not_found",
    )
    error(await client.get("/v1/inbox", headers=key), 401, "unauthorized")
