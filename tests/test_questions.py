"""Questions: an agent asks, the person answers, and a call that waits wakes through pub/sub."""

import asyncio

import httpx2
import pytest

from mcp_client_server.limits import Limits
from mcp_client_server.store import Store
from tests.conftest import Launch
from tests.support import (
    DEVICE_A,
    DEVICE_B,
    SIGNAL_SECONDS,
    Api,
    EventListener,
    FakeClock,
    Headers,
    Json,
    RawStore,
    Running,
    error,
    mcp_client,
    payload_json,
    settle,
    structured,
    tool_text,
)

DAY = 24 * 3600
# The sweep that expires questions sleeps this long between two runs.
SWEEP = 10


async def waiting(clock: FakeClock, wait: float = 50, count: int = 1) -> None:
    """Until `count` calls wait for an answer with a limit of `wait` seconds.

    A call arms its time limit last: it has its event subscription and its second read of the question by then.
    Thus an answer after this point reaches the call only as an event, and the clock can move for the limit.
    """
    await clock.sleeping(wait, count)


async def swept(clock: FakeClock) -> None:
    """Until the sweep completed its run and sleeps again."""
    await clock.sleeping(SWEEP)


def get_with_wait(api: Api, auth: Headers, question_id: str, wait: int = 50) -> asyncio.Task[httpx2.Response]:
    return asyncio.create_task(api.http.get(f"/v1/questions/{question_id}?wait={wait}", headers=auth))


async def test_an_agent_asks_and_the_question_has_the_shape_of_the_contract(api: Api) -> None:
    account, key, key_info = await api.agent()
    response = await api.http.post("/v1/questions", json={"prompt": "  Deploy to\nproduction now? "}, headers=key)
    assert response.status_code == 201
    question = response.json()
    assert question == {
        "id": question["id"],
        "key_id": key_info["id"],
        "task_id": None,
        "prompt": "Deploy to production now?",
        "kind": "confirm",
        "options": ["Yes", "No"],
        "status": "pending",
        "answer": None,
        "created_at": question["created_at"],
        "answered_at": None,
        "expires_at": question["created_at"] + 3600,
    }
    assert question["id"].startswith("qst_")
    for auth in (key, account):
        assert (await api.http.get(f"/v1/questions/{question['id']}", headers=auth)).json() == question
        assert (await api.http.get("/v1/questions", headers=auth)).json() == {"questions": [question]}


async def test_each_kind_has_its_own_options_rule(api: Api) -> None:
    _, key, _ = await api.agent()
    task = await api.task(key)

    choice = await api.ask(key, kind="choice", options=["Staging", " Production "], task_id=task["id"], expires_in=60)
    assert (choice["options"], choice["task_id"]) == (["Staging", "Production"], task["id"])
    assert choice["expires_at"] == choice["created_at"] + 60
    assert (await api.ask(key, kind="text", expires_in=86_400))["options"] == []

    cases: list[tuple[Json, str]] = [
        ({}, "prompt"),
        ({"prompt": ""}, "prompt"),
        ({"prompt": "x" * 301}, "prompt"),
        ({"prompt": "ok", "kind": "poll"}, "kind"),
        ({"prompt": "ok", "options": ["Yes", "No"]}, "options"),
        ({"prompt": "ok", "kind": "text", "options": ["A", "B"]}, "options"),
        ({"prompt": "ok", "kind": "choice"}, "options"),
        ({"prompt": "ok", "kind": "choice", "options": ["Only"]}, "options"),
        ({"prompt": "ok", "kind": "choice", "options": ["1", "2", "3", "4", "5"]}, "options"),
        ({"prompt": "ok", "kind": "choice", "options": ["Same", "Same"]}, "options"),
        ({"prompt": "ok", "kind": "choice", "options": ["A", "x" * 31]}, "options.1"),
        ({"prompt": "ok", "expires_in": 59}, "expires_in"),
        ({"prompt": "ok", "expires_in": 86_401}, "expires_in"),
        ({"prompt": "ok", "answer": "Yes"}, "answer"),
    ]
    for body, field in cases:
        problem = error(await api.http.post("/v1/questions", json=body, headers=key), 422, "validation_failed")
        assert list(problem["fields"]) == [field], body


async def test_only_an_api_key_asks_and_cancels_and_only_the_account_answers(api: Api) -> None:
    account, key, _ = await api.agent()
    question = await api.ask(key)
    path = f"/v1/questions/{question['id']}"

    error(await api.http.post("/v1/questions", json={"prompt": "Mine?"}, headers=account), 403, "forbidden")
    error(await api.http.delete(path, headers=account), 403, "forbidden")
    error(await api.answer(key, question["id"]), 403, "forbidden")
    for method, url in (("GET", "/v1/questions"), ("POST", "/v1/questions"), ("GET", path), ("DELETE", path)):
        error(await api.http.request(method, url), 401, "unauthorized")
    assert (await api.http.get(path, headers=key)).json()["status"] == "pending"


async def test_the_person_answers_one_time_with_one_of_the_options(api: Api) -> None:
    account, key, _ = await api.agent()
    question = await api.ask(key, kind="choice", options=["Staging", "Production"])

    wrong = error(await api.answer(account, question["id"], "Yes"), 422, "validation_failed")
    assert wrong["fields"] == {"answer": "Use one of the options: Staging, Production."}
    answered = await api.answer(account, question["id"], "Production")
    assert answered.status_code == 200
    assert answered.json() == {
        **question,
        "status": "answered",
        "answer": "Production",
        "answered_at": answered.json()["answered_at"],
    }
    assert answered.json()["answered_at"] > question["created_at"]

    error(await api.answer(account, question["id"], "Staging"), 409, "conflict")
    assert (await api.http.get(f"/v1/questions/{question['id']}", headers=key)).json()["answer"] == "Production"


async def test_a_typed_answer_has_1_to_1000_characters(api: Api) -> None:
    account, key, _ = await api.agent()
    question = await api.ask(key, kind="text")
    for bad in ("", "   ", "x" * 1001):
        problem = error(await api.answer(account, question["id"], bad), 422, "validation_failed")
        assert list(problem["fields"]) == ["answer"]
    assert (await api.answer(account, question["id"], "  Use the blue one.\nIt is ready. ")).json()["answer"] == (
        "Use the blue one.\nIt is ready."
    )


async def test_two_answers_at_the_same_time_give_one_winner(running: Running, redis_listener: EventListener) -> None:
    api = running.api
    account, key, _ = await api.agent()
    await api.device(account)
    question = await api.ask(key)
    await running.pushes_done()
    running.push.sent.clear()
    await redis_listener.start()

    responses = await asyncio.gather(*(api.answer(account, question["id"], answer) for answer in ("Yes", "No") * 4))
    assert sorted(response.status_code for response in responses) == [200] + [409] * 7
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert (await api.http.get(f"/v1/questions/{question['id']}", headers=key)).json() == winner

    await running.pushes_done()
    # One status change, and one answer in the inbox of the agent.
    assert await redis_listener.names() == ["question.updated", "inbox.updated"]
    assert len(running.push.of_type("background")) == 1


async def test_a_credential_reaches_only_the_questions_of_its_own_key_or_account(api: Api) -> None:
    account = await api.account()
    key_a, _ = await api.key(account, "A")
    key_b, _ = await api.key(account, "B")
    stranger, stranger_key, _ = await api.agent()
    question = await api.ask(key_a, prompt="Private?")
    await api.ask(key_b, prompt="Other?")
    path = f"/v1/questions/{question['id']}"

    for outsider in (key_b, stranger, stranger_key):
        error(await api.http.get(path, headers=outsider), 404, "not_found")
        error(await api.http.get(f"{path}?wait=5", headers=outsider), 404, "not_found")
    error(await api.http.delete(path, headers=key_b), 404, "not_found")
    error(await api.http.delete(path, headers=stranger_key), 404, "not_found")
    error(await api.answer(stranger, question["id"]), 404, "not_found")

    async def prompts(auth: Headers) -> list[str]:
        return [item["prompt"] for item in (await api.http.get("/v1/questions", headers=auth)).json()["questions"]]

    assert await prompts(key_a) == ["Private?"]
    assert await prompts(key_b) == ["Other?"]
    assert await prompts(account) == ["Other?", "Private?"]
    assert await prompts(stranger) == await prompts(stranger_key) == []
    assert (await api.http.get(path, headers=key_a)).json()["status"] == "pending"


async def test_a_question_about_a_task_needs_a_task_of_the_same_key(api: Api) -> None:
    account = await api.account()
    key_a, _ = await api.key(account, "A")
    key_b, _ = await api.key(account, "B")
    foreign = await api.task(key_b)
    for task_id in (foreign["id"], "tsk_missing"):
        body = {"prompt": "About this?", "task_id": task_id}
        error(await api.http.post("/v1/questions", json=body, headers=key_a), 404, "not_found")
    assert (await api.http.get("/v1/questions", headers=account)).json() == {"questions": []}


async def test_cancel_sets_cancelled_and_a_closed_question_cannot_change(api: Api) -> None:
    account, key, _ = await api.agent()
    first = await api.ask(key)
    second = await api.ask(key)

    for _ in range(2):
        assert (await api.http.delete(f"/v1/questions/{first['id']}", headers=key)).status_code == 204
    cancelled = (await api.http.get(f"/v1/questions/{first['id']}", headers=key)).json()
    assert (cancelled["status"], cancelled["answer"], cancelled["answered_at"]) == ("cancelled", None, None)
    error(await api.answer(account, first["id"]), 409, "conflict")

    await api.answer(account, second["id"])
    error(await api.http.delete(f"/v1/questions/{second['id']}", headers=key), 409, "conflict")


async def test_list_filters_by_status_and_shows_the_newest_first(api: Api) -> None:
    account, key, _ = await api.agent()
    made = [await api.ask(key, prompt=f"Q{number}?") for number in range(4)]
    await api.answer(account, made[0]["id"])
    await api.http.delete(f"/v1/questions/{made[1]['id']}", headers=key)

    async def prompts(query: str = "") -> list[str]:
        response = await api.http.get(f"/v1/questions{query}", headers=account)
        assert response.status_code == 200, response.text
        return [item["prompt"] for item in response.json()["questions"]]

    assert await prompts() == ["Q3?", "Q2?"]
    assert await prompts("?status=pending") == ["Q3?", "Q2?"]
    assert await prompts("?status=answered") == ["Q0?"]
    assert await prompts("?status=cancelled, answered") == ["Q1?", "Q0?"]
    assert await prompts("?status=pending,answered,expired,cancelled") == ["Q3?", "Q2?", "Q1?", "Q0?"]
    problem = error(await api.http.get("/v1/questions?status=open", headers=key), 422, "validation_failed")
    assert list(problem["fields"]) == ["status"]


async def test_pending_question_limit_is_for_each_key(launch: Launch) -> None:
    async with launch(limits=Limits(max_pending_questions_per_key=2)) as running:
        api = running.api
        account = await api.account()
        key, _ = await api.key(account, "A")
        other, _ = await api.key(account, "B")
        first = await api.ask(key)
        await api.ask(key)
        error(await api.http.post("/v1/questions", json={"prompt": "Third?"}, headers=key), 409, "conflict")
        await api.ask(other)

        await api.answer(account, first["id"])
        await api.ask(key)


async def test_a_call_that_waits_returns_when_the_person_answers(running: Running) -> None:
    api = running.api
    account, key, _ = await api.agent()
    question = await api.ask(key)

    held = get_with_wait(api, key, question["id"])
    await waiting(running.clock)
    await api.answer(account, question["id"], "No")

    # The clock did not move, so the wait did not end by its time limit: the answer event woke the call.
    response = await asyncio.wait_for(held, SIGNAL_SECONDS)
    assert (response.status_code, response.json()["status"], response.json()["answer"]) == (200, "answered", "No")
    assert running.services.events.stream_count == 0


async def test_a_call_that_waits_returns_the_pending_question_after_its_time(running: Running) -> None:
    api = running.api
    _, key, _ = await api.agent()
    question = await api.ask(key)

    held = get_with_wait(api, key, question["id"], wait=20)
    await waiting(running.clock, 20)
    running.clock.advance(19)
    await settle()
    assert not held.done()

    running.clock.advance(1)
    response = await asyncio.wait_for(held, SIGNAL_SECONDS)
    assert response.json() == question
    assert running.services.events.stream_count == 0

    assert (await api.http.get(f"/v1/questions/{question['id']}?wait=0", headers=key)).json() == question
    for bad in ("-1", "51", "soon"):
        problem = error(
            await api.http.get(f"/v1/questions/{question['id']}?wait={bad}", headers=key), 422, "validation_failed"
        )
        assert list(problem["fields"]) == ["wait"]


async def test_a_call_that_waits_wakes_for_an_answer_that_a_different_worker_gets(launch: Launch) -> None:
    async with launch() as first, launch() as second:
        account, key, _ = await first.api.agent()
        question = await first.api.ask(key)

        held = get_with_wait(first.api, key, question["id"])
        await waiting(first.clock)
        assert (await second.api.answer(account, question["id"])).status_code == 200

        assert (await asyncio.wait_for(held, SIGNAL_SECONDS)).json()["status"] == "answered"
        assert second.services.events.stream_count == 0


async def test_a_call_that_waits_ends_when_the_question_is_cancelled_or_its_task_is_deleted(running: Running) -> None:
    api = running.api
    account, key, _ = await api.agent()
    task = await api.task(key)
    cancelled = await api.ask(key)
    about_task = await api.ask(key, task_id=task["id"])

    held = [get_with_wait(api, account, cancelled["id"]), get_with_wait(api, key, about_task["id"])]
    await waiting(running.clock, count=2)
    await api.http.delete(f"/v1/questions/{cancelled['id']}", headers=key)
    await api.http.delete(f"/v1/tasks/{task['id']}", headers=account)

    responses = await asyncio.wait_for(asyncio.gather(*held), SIGNAL_SECONDS)
    statuses = [response.json()["status"] for response in responses]
    assert statuses == ["cancelled", "cancelled"]


async def test_many_calls_wait_at_once_and_hold_no_store_connection(launch: Launch, store_url: str | None) -> None:
    # With a real store, the application has its own pool of two connections. 100 calls that each kept one
    # connection could not all wait.
    pool = {"own_connections": True, "redis_max_connections": 2} if store_url else {}
    # The plain test client for fakeredis has a small pool that does not queue, so fewer calls start at once.
    count = 100 if store_url else 40
    async with launch(limits=Limits(max_waits_per_key=count), **pool) as running:
        api = running.api
        account, key, _ = await api.agent()
        question = await api.ask(key)

        held = [get_with_wait(api, key, question["id"]) for _ in range(count)]
        await waiting(running.clock, count=count)
        assert running.services.events.stream_count == count
        assert (await api.http.get("/health")).status_code == 200
        await api.answer(account, question["id"])

        responses = await asyncio.wait_for(asyncio.gather(*held), SIGNAL_SECONDS)
        assert {(response.status_code, response.json()["status"]) for response in responses} == {(200, "answered")}
        assert running.services.events.stream_count == 0


async def test_a_pending_question_expires_at_its_time_and_the_sweep_tells_the_clients(
    running: Running, redis_listener: EventListener
) -> None:
    api = running.api
    account, key, _ = await api.agent()
    question = await api.ask(key, expires_in=60)
    kept = await api.ask(key, expires_in=3600)
    await redis_listener.start()

    async def listed(status: str) -> list[str]:
        response = await api.http.get(f"/v1/questions?status={status}", headers=key)
        return [item["id"] for item in response.json()["questions"]]

    await swept(running.clock)
    running.clock.advance(59)
    await swept(running.clock)
    assert await listed("pending") == [kept["id"], question["id"]]

    running.clock.advance(11)
    await swept(running.clock)
    # The list does not expire a question itself, so this is the work of the sweep.
    assert await listed("expired") == [question["id"]]
    assert await redis_listener.names() == ["question.updated"]
    error(await api.answer(account, question["id"]), 409, "conflict")
    assert (await api.http.get(f"/v1/questions/{kept['id']}", headers=key)).json()["status"] == "pending"


async def test_an_answer_or_a_read_after_the_time_sees_expired_also_before_the_sweep(running: Running) -> None:
    api = running.api
    account, key, _ = await api.agent()
    first = await api.ask(key, expires_in=60)
    second = await api.ask(key, expires_in=60)
    # The sweep runs at 51 seconds and sleeps until 61. The questions expire at 60, between two runs.
    await swept(running.clock)
    running.clock.advance(51)
    await swept(running.clock)
    running.clock.advance(9)

    error(await api.answer(account, first["id"]), 409, "conflict")
    assert (await api.http.get(f"/v1/questions/{first['id']}", headers=key)).json()["status"] == "expired"
    assert (await api.http.get(f"/v1/questions/{second['id']}", headers=key)).json()["status"] == "expired"


async def test_a_call_that_waits_past_the_expiry_returns_expired(running: Running) -> None:
    api = running.api
    _, key, _ = await api.agent()
    question = await api.ask(key, expires_in=60)
    await swept(running.clock)
    running.clock.advance(35)
    await swept(running.clock)

    held = get_with_wait(api, key, question["id"], wait=50)
    # The question expires in 25 seconds, before the 50 seconds of the wait. The limit of the call is the nearer one.
    await running.clock.sleeping(25, tolerance=0.5)
    running.clock.advance(25)
    assert (await asyncio.wait_for(held, SIGNAL_SECONDS)).json()["status"] == "expired"


async def test_a_new_question_alerts_each_device_and_a_status_change_removes_the_alert(running: Running) -> None:
    api, push = running.api, running.push
    account, key, key_info = await api.agent("Claude Code")
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token="0f" * 32, platform="watchos")
    task = await api.task(key, notify=False)
    await running.pushes_done()
    push.sent.clear()

    question = await api.ask(key, kind="choice", options=["Staging", "Production"], task_id=task["id"], expires_in=600)
    await running.pushes_done()
    alerts = push.of_type("alert")
    assert [(r.ref.device_id, r.priority, r.collapse_id) for r in alerts] == [
        (DEVICE_A, 10, question["id"]),
        (DEVICE_B, 10, question["id"]),
    ]
    assert 590 <= alerts[0].ttl_seconds <= 600
    assert payload_json(alerts[0]) == {
        "aps": {
            "alert": {"title": "Claude Code", "subtitle": "Question", "body": "Deploy now?"},
            "sound": "default",
            "thread-id": key_info["id"],
            "category": "QUESTION_CHOICE",
            "interruption-level": "time-sensitive",
            "mutable-content": 1,
        },
        "event": "question.created",
        "question_id": question["id"],
        "kind": "choice",
        "options": ["Staging", "Production"],
        "task_id": task["id"],
        "key_id": key_info["id"],
    }

    push.sent.clear()
    await api.answer(account, question["id"], "Staging")
    await running.pushes_done()
    assert push.of_type("alert") == []
    silent = push.of_type("background")
    assert [(r.ref.device_id, r.priority, r.collapse_id) for r in silent] == [
        (DEVICE_A, 5, question["id"]),
        (DEVICE_B, 5, question["id"]),
    ]
    assert payload_json(silent[0]) == {
        "aps": {"content-available": 1},
        "event": "question.updated",
        "question_id": question["id"],
        "key_id": key_info["id"],
    }


@pytest.mark.parametrize(("kind", "category"), [("confirm", "QUESTION_CONFIRM"), ("text", "QUESTION_TEXT")])
async def test_the_notification_category_follows_the_kind(running: Running, kind: str, category: str) -> None:
    account, key, _ = await running.api.agent()
    await running.api.device(account)
    await running.api.ask(key, kind=kind)
    await running.pushes_done()
    payload = running.push.payloads("alert")[0]
    assert (payload["aps"]["category"], payload["kind"], payload["task_id"]) == (category, kind, None)


async def test_the_live_activity_of_the_task_shows_its_oldest_pending_question(running: Running) -> None:
    api, push = running.api, running.push
    account, key, _ = await api.agent()
    await api.device(account, DEVICE_A)
    task = await api.task(key, status="in_progress", notify=False)
    other_task = await api.task(key, status="in_progress", notify=False)
    await api.live_activity(account, task["id"], DEVICE_A)
    await running.pushes_done()

    async def shown(*, flush: bool = True) -> list[Json | None]:
        """The `question` of each Live Activity push since the last call.

        With `flush`, the interval of the update throttle ends first, so the update that waited goes.
        """
        await running.pushes_done()
        if flush:
            await running.clock.sleeping(5)
            running.clock.advance(5)
            await running.push.sent_of("liveactivity", 1)
            await running.pushes_done()
        updates = push.payloads("liveactivity")
        assert {update["aps"]["event"] for update in updates} <= {"update"}
        push.sent.clear()
        return [update["aps"]["content-state"]["question"] for update in updates]

    push.sent.clear()
    first = await api.ask(key, task_id=task["id"])
    first_state = {"id": first["id"], "prompt": "Deploy now?", "kind": "confirm", "options": ["Yes", "No"]}
    await running.pushes_done()
    assert [request.priority for request in push.of_type("liveactivity")] == [10]
    assert await shown(flush=False) == [first_state]

    second = await api.ask(key, kind="text", prompt="Which branch?", task_id=task["id"])
    assert await shown() == [first_state]
    await api.ask(key, task_id=other_task["id"])
    await api.ask(key)
    await settle()
    assert await shown(flush=False) == []

    # A change of the task keeps the question in the content state.
    await api.patch(key, task["id"], progress=50)
    assert await shown() == [first_state]

    await api.answer(account, first["id"])
    assert await shown() == [{"id": second["id"], "prompt": "Which branch?", "kind": "text", "options": []}]
    await api.http.delete(f"/v1/questions/{second['id']}", headers=key)
    assert await shown() == [None]


async def test_a_deleted_key_or_account_takes_its_questions_with_it(running: Running, raw: RawStore) -> None:
    api = running.api
    keeper = await api.account()
    keeper_key, _ = await api.key(keeper)
    kept = await api.ask(keeper_key)
    before = await raw.keys()

    account, key, key_info = await api.agent()
    second_key, second_info = await api.key(account, "Second")
    task = await api.task(key)
    answered = await api.ask(key, task_id=task["id"])
    await api.answer(account, answered["id"])
    await api.ask(key, task_id=task["id"])
    await api.ask(second_key)

    assert (await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)).status_code == 204
    left = (await api.http.get("/v1/questions?status=pending,answered", headers=account)).json()["questions"]
    assert [item["key_id"] for item in left] == [second_info["id"]]
    assert not [name for name in await raw.keys() if key_info["id"] in name or ":tq:" in name]

    assert (await api.http.delete("/v1/account", headers=account)).status_code == 204
    assert [name for name in await raw.keys() if ":rl:" not in name] == before
    assert (await api.http.get(f"/v1/questions/{kept['id']}", headers=keeper_key)).json()["status"] == "pending"


async def test_a_closed_question_is_deleted_after_the_retention_time(running: Running, store: Store) -> None:
    api, questions = running.api, running.services.questions
    # This test does the work of the maintenance loop itself, so it takes the lock of the loop.
    assert await store.acquire_lock("expire", 600)
    account, key, _ = await api.agent()
    answered = await api.ask(key)
    await api.answer(account, answered["id"])
    pending = await api.ask(key, expires_in=86_400)

    running.clock.advance(89 * DAY)
    assert await questions.purge_closed() == 0
    running.clock.advance(2 * DAY)
    # The question that was pending expired on its first day, so it is not 90 days closed yet.
    assert await questions.purge_closed() == 1
    error(await api.http.get(f"/v1/questions/{answered['id']}", headers=key), 404, "not_found")
    assert (await api.http.get(f"/v1/questions/{pending['id']}", headers=key)).json()["status"] == "expired"


async def test_ask_user_holds_the_tool_call_until_the_answer(running: Running) -> None:
    api = running.api
    account, key, _ = await api.agent()
    task = await api.task(key)
    arguments = {"prompt": "Deploy to production now?", "task_id": task["id"]}

    call = asyncio.create_task(api.tool(key, "ask_user", arguments))
    await waiting(running.clock, 30)
    pending = (await api.http.get("/v1/questions", headers=account)).json()["questions"]
    assert [(item["prompt"], item["task_id"]) for item in pending] == [("Deploy to production now?", task["id"])]
    await api.answer(account, pending[0]["id"])

    result = await asyncio.wait_for(call, SIGNAL_SECONDS)
    assert result["isError"] is False
    assert result["structuredContent"] == {
        **pending[0],
        "status": "answered",
        "answer": "Yes",
        "answered_at": result["structuredContent"]["answered_at"],
    }
    assert result["content"][0]["text"] == f'Question {pending[0]["id"]} has the answer "Yes".'


async def test_ask_user_returns_pending_after_wait_seconds_and_get_answer_waits_again(running: Running) -> None:
    api = running.api
    account, key, _ = await api.agent()

    asked = await api.tool(key, "ask_user", {"prompt": "Which color?", "kind": "text", "wait_seconds": 0})
    question = asked["structuredContent"]
    assert (question["status"], question["kind"], question["options"]) == ("pending", "text", [])
    assert "Call get_answer" in asked["content"][0]["text"]

    again = asyncio.create_task(api.tool(key, "get_answer", {"question_id": question["id"], "wait_seconds": 30}))
    await waiting(running.clock, 30)
    running.clock.advance(30)
    assert (await asyncio.wait_for(again, SIGNAL_SECONDS))["structuredContent"]["status"] == "pending"

    last = asyncio.create_task(api.tool(key, "get_answer", {"question_id": question["id"]}))
    await waiting(running.clock, 30)
    await api.answer(account, question["id"], "Blue")
    assert (await asyncio.wait_for(last, SIGNAL_SECONDS))["structuredContent"]["answer"] == "Blue"


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_question_tools_through_the_sdk_client_and_their_errors(launch: Launch, mode: str) -> None:
    async with launch(limits=Limits(max_pending_questions_per_key=1)) as running:
        api = running.api
        account, key, _ = await api.agent()
        _, other_key, _ = await api.agent("Other")
        foreign = await api.ask(other_key)
        async with mcp_client(running.app, key, mode) as client:
            asked = structured(
                await client.call_tool(
                    "ask_user", {"prompt": "Pick one", "kind": "choice", "options": ["A", "B"], "wait_seconds": 0}
                )
            )
            assert (asked["status"], asked["options"]) == ("pending", ["A", "B"])

            full = await client.call_tool("ask_user", {"prompt": "One more?", "wait_seconds": 0})
            assert full.is_error is True
            assert "1 pending questions at most" in tool_text(full)

            for name, arguments, word in [
                ("ask_user", {"prompt": "ok", "options": ["A", "B"]}, "options"),
                ("ask_user", {"prompt": "ok", "wait_seconds": 51}, "wait_seconds"),
                ("ask_user", {"prompt": "ok", "task_id": "tsk_missing"}, "Task not found"),
                ("get_answer", {"question_id": foreign["id"]}, "Use the `id` that ask_user returned"),
            ]:
                result = await client.call_tool(name, arguments)
                assert result.is_error is True, (name, arguments)
                assert word in tool_text(result), tool_text(result)

            await api.answer(account, asked["id"], "B")
            answered = await client.call_tool("get_answer", {"question_id": asked["id"], "wait_seconds": 0})
            assert structured(answered)["answer"] == "B"
            assert client.instructions is not None
            assert "5. Use `ask_user` when a decision belongs to the person" in client.instructions
