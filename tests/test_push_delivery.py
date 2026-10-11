"""Pushes after real requests: they leave the request path, reach the right devices, and clean up dead tokens."""

import asyncio
import logging
from typing import override

import pytest

from mcp_client_server.ids import AccountId, DeviceId, KeyId, TaskId
from mcp_client_server.models import Device
from mcp_client_server.push import PushDispatcher, PushRequest, PushResult, TaskChange, Topics
from mcp_client_server.push.dispatcher import widget_request
from mcp_client_server.push.payloads import TokenRef
from mcp_client_server.store import PushContext, Store, StoredTask
from tests.conftest import Launch
from tests.support import (
    ACTIVITY_TOKEN,
    APNS_TOKEN,
    DEVICE_A,
    DEVICE_B,
    START_TOKEN,
    WIDGET_TOKEN,
    FakeClock,
    RecordingPushSender,
    Running,
    make_device,
    make_task,
    payload_json,
)

OTHER_TOKEN = "0f" * 32


async def test_a_task_goes_through_its_life_and_each_device_gets_the_right_pushes(running: Running) -> None:
    api, push = running.api, running.push
    account, key, key_info = await api.agent("Claude Code")
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token=OTHER_TOKEN, push_to_start_token=None, platform="ipados")

    created = await api.task(key, title="Add dark mode support")
    await running.pushes_done()
    alerts = push.payloads("alert")
    assert [payload["aps"]["alert"] for payload in alerts] == [
        {"title": "Claude Code", "subtitle": "New task", "body": "Add dark mode support"}
    ] * 2
    assert {payload["aps"]["thread-id"] for payload in alerts} == {key_info["id"]}
    assert push.of_type("liveactivity") == []

    push.sent.clear()
    await api.patch(key, created["id"], status="in_progress")
    await running.pushes_done()
    assert len(push.of_type("alert")) == 2
    starts = push.of_type("liveactivity")
    assert [(r.ref.device_id, r.token) for r in starts] == [(DEVICE_A, START_TOKEN)]
    assert payload_json(starts[0])["aps"]["attributes"] == {"taskID": created["id"], "keyLabel": "Claude Code"}

    await api.live_activity(account, created["id"], DEVICE_A)
    push.sent.clear()
    await api.patch(key, created["id"], progress=60, status_note="Half done")
    await running.pushes_done()
    assert (len(push.of_type("background")), push.of_type("alert")) == (2, [])
    updates = push.of_type("liveactivity")
    assert [(r.token, r.priority) for r in updates] == [(ACTIVITY_TOKEN, 5)]
    state = payload_json(updates[0])["aps"]["content-state"]
    assert (state["progress"], state["note"]) == (60, "Half done")

    push.sent.clear()
    await api.patch(account, created["id"], title="Renamed by the app")
    await running.pushes_done()
    assert (len(push.of_type("background")), push.of_type("alert")) == (2, [])
    assert [payload["aps"]["event"] for payload in push.payloads("liveactivity")] == ["update"]

    push.sent.clear()
    await api.patch(key, created["id"], status="done")
    await running.pushes_done()
    assert {payload["aps"]["alert"]["subtitle"] for payload in push.payloads("alert")} == {"Task done"}
    ends = push.payloads("liveactivity")
    assert [(payload["aps"]["event"], payload["aps"]["content-state"]["status"]) for payload in ends] == [
        ("end", "done")
    ]

    # The server removes the registration after it sends `end`, so a later change sends no activity push.
    push.sent.clear()
    await api.http.delete(f"/v1/tasks/{created['id']}", headers=key)
    await running.pushes_done()
    assert {payload["aps"]["alert"]["subtitle"] for payload in push.payloads("alert")} == {"Task deleted"}
    assert {payload_json(request)["event"] for request in push.sent} == {"task.deleted"}
    assert push.of_type("liveactivity") == []


async def test_a_deleted_task_and_a_deleted_key_end_their_live_activities(running: Running) -> None:
    api, push = running.api, running.push
    account, key, key_info = await api.agent()
    await api.device(account, DEVICE_A)
    first = await api.task(key, status="in_progress", notify=False)
    second = await api.task(key, status="in_progress", notify=False)
    for task in (first, second):
        await api.live_activity(account, task["id"], DEVICE_A)
    await running.pushes_done()
    push.sent.clear()

    await api.http.delete(f"/v1/tasks/{first['id']}", headers=account)
    await running.pushes_done()
    assert [payload["aps"]["event"] for payload in push.payloads("liveactivity")] == ["end"]
    assert [r.push_type for r in push.sent if r.push_type != "liveactivity"] == ["background"]

    push.sent.clear()
    await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)
    await running.pushes_done()
    assert [(r.push_type, payload_json(r)["aps"]["event"], r.ref.task_id) for r in push.sent] == [
        ("liveactivity", "end", second["id"])
    ]


async def test_live_activities_off_in_the_account_settings_sends_no_activity_push(running: Running) -> None:
    api, push = running.api, running.push
    account, key, _ = await api.agent()
    await api.device(account, DEVICE_A)
    await api.http.patch("/v1/account", json={"settings": {"live_activities": False}}, headers=account)

    task = await api.task(key, status="in_progress")
    await api.live_activity(account, task["id"], DEVICE_A)
    await api.patch(key, task["id"], progress=10)
    await api.patch(key, task["id"], status="done")
    await running.pushes_done()
    assert push.of_type("liveactivity") == []
    assert len(push.of_type("alert")) == 2


async def test_tokens_that_apns_reports_as_dead_are_removed_and_the_others_stay(running: Running, store: Store) -> None:
    api, push = running.api, running.push
    account, key, key_info = await api.agent()
    account_id = await api.account_id(account)
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token=OTHER_TOKEN, push_to_start_token="1e" * 32)
    task_id = (await api.task(key, notify=False))["id"]
    await running.pushes_done()
    await api.live_activity(account, task_id, DEVICE_B)

    push.replies = {
        APNS_TOKEN: PushResult(410, "Unregistered"),
        START_TOKEN: PushResult(400, "BadDeviceToken"),
        ACTIVITY_TOKEN: PushResult(410, "ExpiredToken"),
        OTHER_TOKEN: PushResult(429, "TooManyRequests"),
        "1e" * 32: PushResult(503, "ServiceUnavailable"),
    }
    await api.patch(key, task_id, status="in_progress")
    await running.pushes_done()

    context = await store.push_context(account_id, key_info["id"], TaskId(task_id))
    tokens = {device.id: (device.apns_token, device.push_to_start_token) for device in context.devices}
    assert tokens == {DEVICE_A: (None, None), DEVICE_B: (OTHER_TOKEN, "1e" * 32)}
    assert context.activities == {}


async def test_a_failed_send_is_logged_and_does_not_stop_the_other_pushes(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    api, push = running.api, running.push
    account, key, _ = await api.agent()
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token=OTHER_TOKEN)
    push.replies = {APNS_TOKEN: RuntimeError("boom"), OTHER_TOKEN: PushResult(429, "TooManyRequests")}
    caplog.set_level(logging.WARNING, logger="mcp_client_server.push.dispatcher")

    await api.task(key, notify=False)
    await running.pushes_done()

    assert {r.token for r in push.sent} == {APNS_TOKEN, OTHER_TOKEN}
    messages = sorted(record.getMessage() for record in caplog.records)
    assert messages == [
        f"APNs refused a background push to device {DEVICE_B}: 429 TooManyRequests",
        f"push to device {DEVICE_A} failed: RuntimeError('boom')",
    ]
    assert not [message for message in messages if APNS_TOKEN in message or OTHER_TOKEN in message]


async def test_notify_user_goes_to_the_devices_with_a_token_and_is_time_sensitive_for_an_urgent_task(
    running: Running,
) -> None:
    api, push = running.api, running.push
    account, key, key_info = await api.agent("Codex")
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token=None)
    urgent = await api.task(key, priority="urgent", notify=False)
    await running.pushes_done()
    push.sent.clear()

    arguments = {"title": "Approve the deploy", "body": "The build is ready.", "task_id": urgent["id"]}
    call = {"name": "notify_user", "arguments": arguments}
    response = await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "notify_user"})
    assert response.json()["result"]["structuredContent"] == {"sent": True, "devices": 1}
    await running.pushes_done()

    assert [(r.ref.device_id, r.collapse_id) for r in push.sent] == [(DEVICE_A, None)]
    assert payload_json(push.sent[0]) == {
        "aps": {
            "alert": {"title": "Codex", "subtitle": "Approve the deploy", "body": "The build is ready."},
            "sound": "default",
            "thread-id": key_info["id"],
            "category": "MESSAGE",
            "interruption-level": "time-sensitive",
        },
        "event": "message",
        "key_id": key_info["id"],
        "task_id": urgent["id"],
    }


async def test_push_is_off_without_the_apns_settings_and_the_api_still_works(
    launch: Launch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="mcp_client_server.main")
    async with launch(apns_bundle_id=None) as running:
        api = running.api
        assert running.services.push.enabled is False
        account, key, _ = await api.agent()
        await api.device(account, DEVICE_A)
        task = await api.task(key)
        await api.patch(key, task["id"], status="done")
        call = {"name": "notify_user", "arguments": {"title": "Hi", "body": "There"}}
        response = await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "notify_user"})
        assert response.json()["result"]["structuredContent"] == {"sent": False, "devices": 0}
        await running.pushes_done()
    assert running.push.sent == []


class SlowStore:
    """A push store that does not answer until the test lets it."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.reads = 0

    async def push_context(self, account_id: AccountId, key_id: KeyId, task_id: TaskId | None) -> PushContext:
        self.reads += 1
        await self.release.wait()
        return PushContext(key_label="Agent", live_activities=True, devices=[make_device()], activities={})

    async def clear_activities(self, task_id: TaskId) -> None:
        raise AssertionError("not used")

    async def forget_activity(self, task_id: TaskId, device_id: DeviceId, token: str | None = None) -> None:
        raise AssertionError("not used")

    async def clear_device_token(self, account_id: AccountId, device_id: DeviceId, field: str, token: str) -> None:
        raise AssertionError("not used")

    async def get_task_unscoped(self, task_id: TaskId) -> StoredTask | None:
        raise AssertionError("not used")

    async def list_devices(self, account_id: AccountId) -> list[Device]:
        raise AssertionError("not used")

    async def widget_slot(self, account_id: AccountId, device_id: DeviceId) -> bool:
        raise AssertionError("not used")

    async def renew_widget_slot(self, account_id: AccountId, device_id: DeviceId) -> None:
        raise AssertionError("not used")

    async def take_widget_mark(self, account_id: AccountId, device_id: DeviceId) -> bool:
        raise AssertionError("not used")

    async def widget_throttled(self, account_id: AccountId, device_id: DeviceId) -> bool:
        raise AssertionError("not used")


async def test_jobs_above_the_pending_limit_are_dropped_and_the_caller_does_not_wait(
    clock: FakeClock, push: RecordingPushSender, caplog: pytest.LogCaptureFixture
) -> None:
    store = SlowStore()
    dispatcher = PushDispatcher(store, push, Topics("com.example.app", "com.example.app"), clock, max_pending_jobs=3)
    change = TaskChange("created", make_task(), frozenset(), by_agent=True)
    caplog.set_level(logging.WARNING, logger="mcp_client_server.push.dispatcher")

    for _ in range(10):
        dispatcher.task_changed(AccountId("acc_1"), change)
    await asyncio.sleep(0)
    assert store.reads == 3
    assert [record.getMessage() for record in caplog.records] == ["push is behind: 3 jobs wait, 1 dropped so far"]

    store.release.set()
    await dispatcher.aclose()
    assert len(push.of_type("alert")) == 3
    assert push.closed


class InstantStore(SlowStore):
    @override
    async def push_context(self, account_id: AccountId, key_id: KeyId, task_id: TaskId | None) -> PushContext:
        return PushContext(key_label="Agent", live_activities=False, devices=[make_device()], activities={})


async def test_drain_returns_when_a_job_ended_in_the_same_turn_of_the_loop(
    clock: FakeClock, push: RecordingPushSender
) -> None:
    dispatcher = PushDispatcher(InstantStore(), push, Topics("com.example.app", "com.example.app"), clock)
    dispatcher.task_changed(AccountId("acc_1"), TaskChange("created", make_task(), frozenset(), by_agent=True))
    # The job runs to its end in this one turn. Its "done" callback has not run when drain starts.
    await asyncio.sleep(0)
    await dispatcher.drain()
    assert len(push.sent) == 1


async def widget_pushes(running: Running) -> list[PushRequest]:
    """The WidgetKit pushes since the last call, after the push jobs of the requests are done."""
    await running.pushes_done()
    sent = running.push.of_type("widgets")
    running.push.sent.clear()
    return sent


async def test_a_change_reloads_the_widgets_at_most_one_time_in_15_seconds_and_the_last_change_is_not_lost(
    running: Running,
) -> None:
    api = running.api
    account, key, _ = await api.agent()
    registered = await api.device(account, DEVICE_A, widget_push_token=WIDGET_TOKEN.upper())
    assert registered["widget_push_token"] == WIDGET_TOKEN
    await api.device(account, DEVICE_B, apns_token=OTHER_TOKEN)

    task = await api.task(key, notify=False)
    first = await widget_pushes(running)
    assert [(r.token, r.topic, r.priority, r.collapse_id, r.ref) for r in first] == [
        (WIDGET_TOKEN, "com.example.app.push-type.widgets", None, None, TokenRef("widget_push_token", DEVICE_A))
    ]
    assert payload_json(first[0]) == {"aps": {"content-changed": True}}

    await api.patch(key, task["id"], progress=10)
    await api.patch(account, task["id"], title="Renamed")
    await api.ask(key)
    assert await widget_pushes(running) == []

    # The interval ends, and one push goes for the three changes. The dispatcher sleeps for the interval, and
    # the clock moves only after that sleep started.
    await running.clock.sleeping(15)
    running.clock.advance(15)
    await running.push.sent_of("widgets", 1)
    assert len(await widget_pushes(running)) == 1

    # No change came in the second interval, so no push goes when it ends.
    await running.clock.sleeping(15)
    running.clock.advance(15)
    await running.clock.sleeping(15)
    assert await widget_pushes(running) == []

    await api.http.delete(f"/v1/tasks/{task['id']}", headers=key)
    assert await widget_pushes(running) == []
    running.clock.advance(15)
    await running.push.sent_of("widgets", 1)


async def test_a_question_reloads_the_widgets_and_a_dead_widget_token_is_removed(
    running: Running, store: Store
) -> None:
    api, push = running.api, running.push
    account, key, _ = await api.agent()
    account_id = await api.account_id(account)
    await api.device(account, DEVICE_A, widget_push_token=WIDGET_TOKEN)
    push.replies = {WIDGET_TOKEN: PushResult(410, "Unregistered")}

    await api.ask(key)
    assert len(await widget_pushes(running)) == 1
    device = (await store.list_devices(account_id))[0]
    assert (device.widget_push_token, device.apns_token) == (None, APNS_TOKEN)


def test_a_watch_gets_the_widget_push_on_the_topic_of_the_watch_app() -> None:
    topics = Topics(app="com.example.app", watch="com.example.app.watchkitapp")
    watch = make_device("dev_w", platform="watchos", apns_environment="sandbox", widget_push_token=WIDGET_TOKEN)
    request = widget_request(watch, WIDGET_TOKEN, topics)
    assert (request.topic, request.environment, request.push_type) == (
        "com.example.app.watchkitapp.push-type.widgets",
        "sandbox",
        "widgets",
    )
