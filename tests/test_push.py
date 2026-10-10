import json
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from redis.asyncio import Redis

from mcp_client_server.config import Settings
from mcp_client_server.credentials import Principal
from mcp_client_server.main import create_app
from mcp_client_server.models import Device, NotifyInput, Task
from mcp_client_server.push import ApnsClient, PushRequest, TaskChange, Topics
from mcp_client_server.push import payloads as p
from mcp_client_server.push.dispatcher import plan_message, plan_task_pushes
from mcp_client_server.push.payloads import TokenRef
from mcp_client_server.services import Services
from mcp_client_server.store import PushContext
from tests.conftest import DEVICE_A, DEVICE_B, Api, Connect, FakePush, running

TOPICS = Topics(app="com.example.app", watch="com.example.app.watchkitapp")
APNS_TOKEN = "a1b2c3d4" * 8
START_TOKEN = "c3d4e5f6" * 8
ACTIVITY_TOKEN = "e5f6a7b8" * 8


def task(**fields: Any) -> Task:
    base: dict[str, Any] = {
        "id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
        "title": "Add dark mode support",
        "description": "Implement a dark theme in all views.",
        "status": "in_progress",
        "priority": "high",
        "notify": True,
        "progress": 40,
        "status_note": "Updating the color tokens",
        "source": "agent",
        "created_at": 1760112000.412,
        "updated_at": 1760112300.9,
        "completed_at": None,
    }
    return Task(**{**base, **fields})


def device(device_id: str = "dev_1", **fields: Any) -> Device:
    base: dict[str, Any] = {
        "id": device_id,
        "platform": "ios",
        "model": "iPhone18,1",
        "os_version": "27.0",
        "app_version": "1.0 (1)",
        "apns_environment": "production",
        "apns_token": APNS_TOKEN,
        "push_to_start_token": START_TOKEN,
        "updated_at": 1.0,
    }
    return Device(**{**base, **fields})


def context(*devices: Device, activities: dict[str, str] | None = None, live: bool = True) -> PushContext:
    return PushContext(
        key_label="Claude Code", live_activities=live, devices=list(devices), activities=activities or {}
    )


def change(kind: Any, subject: Task, *changed: str, by_agent: bool = True, **extra: Any) -> TaskChange:
    return TaskChange(kind, subject, frozenset(changed), by_agent, **extra)


def test_visible_payload_is_the_one_in_the_contract() -> None:
    done = task(status="done")
    assert p.visible("task.updated", done, "Claude Code", "Task done") == {
        "aps": {
            "alert": {"title": "Claude Code", "subtitle": "Task done", "body": "Add dark mode support"},
            "sound": "default",
            "thread-id": "key_01jb2k3v9d",
            "category": "TASK",
            "interruption-level": "active",
        },
        "event": "task.updated",
        "task_id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
    }
    urgent = p.visible("task.created", task(priority="urgent"), "Claude Code", "New task")
    assert urgent["aps"]["interruption-level"] == "time-sensitive"


def test_silent_payload_is_the_one_in_the_contract() -> None:
    assert p.silent("task.updated", task()) == {
        "aps": {"content-available": 1},
        "event": "task.updated",
        "task_id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
    }


def test_message_payload() -> None:
    payload = p.message("key_1", "Cursor", "Review needed", "Open the pull request.", None)
    assert payload == {
        "aps": {
            "alert": {"title": "Cursor", "subtitle": "Review needed", "body": "Open the pull request."},
            "sound": "default",
            "thread-id": "key_1",
            "category": "MESSAGE",
            "interruption-level": "active",
        },
        "event": "message",
        "key_id": "key_1",
    }
    about = p.message("key_1", "Cursor", "Now", "Body", task(priority="urgent"))
    assert about["task_id"] == "tsk_01jb2m9x4e7q"
    assert about["aps"]["interruption-level"] == "time-sensitive"


def test_live_activity_payloads_use_the_exact_activitykit_keys() -> None:
    state = {
        "title": "Add dark mode support",
        "status": "in_progress",
        "progress": 40,
        "note": "Updating the color tokens",
        "updatedAt": 1760112300.9,
    }
    assert p.content_state(task()) == state
    assert p.content_state(task(progress=None, status_note=None)) == {**state, "progress": None, "note": None}

    assert p.activity_start(task(), "Claude Code", 1760112301.7) == {
        "aps": {
            "timestamp": 1760112301,
            "event": "start",
            "content-state": state,
            "attributes-type": "TaskActivityAttributes",
            "attributes": {"taskID": "tsk_01jb2m9x4e7q", "keyLabel": "Claude Code"},
            "alert": {"title": "Claude Code", "body": "Add dark mode support"},
        }
    }
    assert p.activity_update(task(), 1760112301.7) == {
        "aps": {"timestamp": 1760112301, "event": "update", "content-state": state}
    }
    done = task(status="done", progress=100)
    assert p.activity_end(done, 1760112301.7) == {
        "aps": {
            "timestamp": 1760112301,
            "event": "end",
            "content-state": {**state, "status": "done", "progress": 100},
            "dismissal-date": 1760112301 + 15 * 60,
        }
    }


def test_a_large_alert_is_cut_to_the_apns_payload_limit() -> None:
    payload = p.message("key_1", "Cursor", "Title", "🚀" * 1000, None)
    data = p.encode(payload)
    assert len(data) <= p.MAX_PAYLOAD_BYTES
    body = json.loads(data)["aps"]["alert"]["body"]
    assert body.endswith("…") and body.startswith("🚀" * 500)
    assert payload["aps"]["alert"]["body"] == "🚀" * 1000
    small = p.silent("task.updated", task())
    assert json.loads(p.encode(small)) == small


def test_agent_change_with_notify_sends_an_alert_to_each_device_with_a_token() -> None:
    devices = [
        device("dev_1"),
        device("dev_2", apns_token=None),
        device("dev_3", platform="watchos", apns_environment="sandbox"),
    ]
    requests = plan_task_pushes(change("created", task(status="todo")), context(*devices, live=False), TOPICS, 1.0)
    assert [(r.ref.device_id, r.topic, r.environment) for r in requests] == [
        ("dev_1", "com.example.app", "production"),
        ("dev_3", "com.example.app.watchkitapp", "sandbox"),
    ]
    first = requests[0]
    assert (first.push_type, first.priority, first.collapse_id, first.token) == (
        "alert",
        10,
        "tsk_01jb2m9x4e7q",
        APNS_TOKEN,
    )
    assert first.ref == TokenRef("apns_token", "dev_1")
    assert first.payload["aps"]["alert"]["subtitle"] == "New task"
    assert first.payload["event"] == "task.created"


@pytest.mark.parametrize(
    ("kind", "subject", "changed", "subtitle"),
    [
        ("created", task(), (), "New task"),
        ("updated", task(status="in_progress"), ("status",), "Task started"),
        ("updated", task(status="done"), ("status", "progress"), "Task done"),
        ("updated", task(status="cancelled"), ("status",), "Task cancelled"),
        ("updated", task(status="todo"), ("status",), "Task updated"),
        ("updated", task(), ("title", "description"), "Task updated"),
        ("deleted", task(), (), "Task deleted"),
    ],
)
def test_alert_subtitles(kind: str, subject: Task, changed: tuple[str, ...], subtitle: str) -> None:
    requests = plan_task_pushes(change(kind, subject, *changed), context(device(), live=False), TOPICS, 1.0)
    assert [r.payload["aps"]["alert"]["subtitle"] for r in requests] == [subtitle]


@pytest.mark.parametrize(
    "quiet",
    [
        change("updated", task(notify=False), "status"),
        change("updated", task(), "title", by_agent=False),
        change("created", task(), by_agent=False),
        change("updated", task(), "progress"),
        change("updated", task(), "progress", "status_note"),
        change("deleted", task(notify=False)),
    ],
)
def test_silent_push_for_quiet_changes(quiet: TaskChange) -> None:
    requests = plan_task_pushes(quiet, context(device(), live=False), TOPICS, 1.0)
    assert len(requests) == 1
    request = requests[0]
    assert (request.push_type, request.priority, request.topic) == ("background", 5, "com.example.app")
    assert request.payload["aps"] == {"content-available": 1}
    assert request.payload["task_id"] == "tsk_01jb2m9x4e7q"


def activity_requests(requests: list[PushRequest]) -> list[PushRequest]:
    return [request for request in requests if request.push_type == "liveactivity"]


def test_agent_start_sends_push_to_start_only_where_no_activity_is_registered() -> None:
    devices = [device("dev_1"), device("dev_2"), device("dev_3", push_to_start_token=None)]
    ctx = context(*devices, activities={"dev_2": ACTIVITY_TOKEN})
    started = change("updated", task(), "status")
    live = activity_requests(plan_task_pushes(started, ctx, TOPICS, 50.0))

    by_event = {(r.payload["aps"]["event"], r.ref.device_id): r for r in live}
    assert set(by_event) == {("update", "dev_2"), ("start", "dev_1")}
    start = by_event["start", "dev_1"]
    assert (start.token, start.topic, start.priority) == (START_TOKEN, "com.example.app.push-type.liveactivity", 10)
    assert start.ref == TokenRef("push_to_start_token", "dev_1", "tsk_01jb2m9x4e7q")
    assert start.collapse_id is None
    update = by_event["update", "dev_2"]
    assert (update.token, update.ref) == (ACTIVITY_TOKEN, TokenRef("activity", "dev_2", "tsk_01jb2m9x4e7q"))

    created = activity_requests(plan_task_pushes(change("created", task()), ctx, TOPICS, 50.0))
    assert [r.payload["aps"]["event"] for r in created] == ["start"]
    by_app = change("updated", task(), "status", by_agent=False)
    assert [r.payload["aps"]["event"] for r in activity_requests(plan_task_pushes(by_app, ctx, TOPICS, 50.0))] == [
        "update"
    ]


def test_activity_update_and_end_rules() -> None:
    ctx = context(device("dev_1"), activities={"dev_1": ACTIVITY_TOKEN, "dev_gone": "ffff"})

    tick = activity_requests(plan_task_pushes(change("updated", task(), "progress"), ctx, TOPICS, 50.0))
    assert [(r.payload["aps"]["event"], r.priority, r.ref.device_id) for r in tick] == [("update", 5, "dev_1")]
    renamed = activity_requests(plan_task_pushes(change("updated", task(), "title"), ctx, TOPICS, 50.0))
    assert [(r.payload["aps"]["event"], r.priority) for r in renamed] == [("update", 10)]
    assert activity_requests(plan_task_pushes(change("updated", task(), "description"), ctx, TOPICS, 50.0)) == []

    for ending in (
        change("updated", task(status="done", progress=100), "status", "progress"),
        change("updated", task(status="cancelled"), "status", by_agent=False),
        change("deleted", task(), activities={"dev_1": ACTIVITY_TOKEN}),
    ):
        ended = activity_requests(plan_task_pushes(ending, ctx, TOPICS, 50.0))
        assert [(r.payload["aps"]["event"], r.token) for r in ended] == [("end", ACTIVITY_TOKEN)]
        assert ended[0].payload["aps"]["dismissal-date"] == 50 + 900
        assert ended[0].payload["aps"]["content-state"]["status"] == ending.task.status

    off = context(device("dev_1"), activities={"dev_1": ACTIVITY_TOKEN}, live=False)
    assert activity_requests(plan_task_pushes(change("updated", task(), "status"), off, TOPICS, 50.0)) == []


def test_message_goes_to_each_device_with_a_token() -> None:
    ctx = context(device("dev_1"), device("dev_2", apns_token=None))
    requests = plan_message("key_1", "Review needed", "Body", None, ctx, TOPICS)
    assert [(r.ref.device_id, r.push_type, r.collapse_id) for r in requests] == [("dev_1", "alert", None)]
    assert requests[0].payload["aps"]["category"] == "MESSAGE"


def apns_key() -> tuple[str, Any]:
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return pem, private.public_key()


def request(**fields: Any) -> PushRequest:
    base: dict[str, Any] = {
        "token": APNS_TOKEN,
        "environment": "production",
        "topic": "com.example.app",
        "push_type": "alert",
        "priority": 10,
        "payload": p.visible("task.updated", task(), "Claude Code", "Task updated"),
        "ttl_seconds": 3600,
        "ref": TokenRef("apns_token", "dev_1"),
        "collapse_id": "tsk_01jb2m9x4e7q",
    }
    return PushRequest(**{**base, **fields})


class Apns:
    """A fake APNs HTTP layer."""

    def __init__(self, *replies: tuple[int, dict[str, str]]) -> None:
        self.requests: list[httpx.Request] = []
        self._replies = list(replies)
        self.clock = 1_000_000.0
        pem, self.public_key = apns_key()
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.client = ApnsClient(
            private_key=pem, key_id="KEY123", team_id="TEAM456", http=http, clock=lambda: self.clock
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self._replies.pop(0) if self._replies else (200, {})
        return httpx.Response(status, json=body) if body else httpx.Response(status)

    def bearer(self, index: int = -1) -> str:
        scheme, token = self.requests[index].headers["authorization"].split(" ")
        assert scheme == "bearer"
        return token


async def test_apns_alert_request() -> None:
    apns = Apns()
    result = await apns.client.send(request())
    assert result.ok and not result.token_is_dead

    sent = apns.requests[0]
    assert str(sent.url) == f"https://api.push.apple.com/3/device/{APNS_TOKEN}"
    assert sent.headers["apns-topic"] == "com.example.app"
    assert sent.headers["apns-push-type"] == "alert"
    assert sent.headers["apns-priority"] == "10"
    assert sent.headers["apns-collapse-id"] == "tsk_01jb2m9x4e7q"
    assert sent.headers["apns-expiration"] == str(1_000_000 + 3600)
    assert json.loads(sent.content)["aps"]["alert"]["title"] == "Claude Code"

    claims = jwt.decode(apns.bearer(), apns.public_key, algorithms=["ES256"], options={"verify_iat": False})
    assert claims == {"iss": "TEAM456", "iat": 1_000_000}
    assert jwt.get_unverified_header(apns.bearer()) == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}


async def test_apns_silent_and_live_activity_requests() -> None:
    apns = Apns()
    silent = request(
        push_type="background", priority=5, payload=p.silent("task.updated", task()), environment="sandbox"
    )
    await apns.client.send(silent)
    live = request(
        token=ACTIVITY_TOKEN,
        push_type="liveactivity",
        topic=TOPICS.live_activity,
        payload=p.activity_update(task(), 5.0),
        collapse_id=None,
        ref=TokenRef("activity", "dev_1", "tsk_1"),
    )
    await apns.client.send(live)

    background, activity = apns.requests
    assert str(background.url) == f"https://api.sandbox.push.apple.com/3/device/{APNS_TOKEN}"
    assert (background.headers["apns-push-type"], background.headers["apns-priority"]) == ("background", "5")
    assert json.loads(background.content)["aps"] == {"content-available": 1}

    assert str(activity.url) == f"https://api.push.apple.com/3/device/{ACTIVITY_TOKEN}"
    assert activity.headers["apns-topic"] == "com.example.app.push-type.liveactivity"
    assert activity.headers["apns-push-type"] == "liveactivity"
    assert "apns-collapse-id" not in activity.headers
    assert json.loads(activity.content)["aps"]["event"] == "update"


async def test_apns_provider_token_is_reused_for_less_than_one_hour() -> None:
    apns = Apns()
    await apns.client.send(request())
    apns.clock += 49 * 60
    await apns.client.send(request())
    assert apns.bearer(0) == apns.bearer(1)
    apns.clock += 2 * 60
    await apns.client.send(request())
    assert apns.bearer(2) != apns.bearer(1)


async def test_apns_dead_tokens_and_errors() -> None:
    apns = Apns(
        (410, {"reason": "Unregistered"}),
        (400, {"reason": "BadDeviceToken"}),
        (400, {"reason": "BadTopic"}),
        (429, {"reason": "TooManyRequests"}),
    )
    gone = await apns.client.send(request())
    assert (gone.status, gone.reason, gone.token_is_dead) == (410, "Unregistered", True)
    bad = await apns.client.send(request())
    assert (bad.status, bad.token_is_dead) == (400, True)
    topic = await apns.client.send(request())
    assert (topic.reason, topic.token_is_dead, topic.ok) == ("BadTopic", False, False)
    busy = await apns.client.send(request())
    assert (busy.status, busy.token_is_dead) == (429, False)


async def test_apns_retries_once_with_a_new_provider_token() -> None:
    apns = Apns((403, {"reason": "ExpiredProviderToken"}), (200, {}))
    apns.clock += 1
    result = await apns.client.send(request())
    assert result.ok and len(apns.requests) == 2

    def broken(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("closed")

    pem, _ = apns_key()
    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    client = ApnsClient(private_key=pem, key_id="K", team_id="T", http=http)
    failed = await client.send(request())
    assert (failed.status, failed.reason, failed.token_is_dead) == (0, "ConnectError", False)
    await client.aclose()


async def test_task_changes_reach_the_devices_off_the_request_path(
    api: Api, services: Services, push: FakePush
) -> None:
    account = await api.account()
    key, key_info = await api.key(account, "Claude Code")
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token="0f" * 32, push_to_start_token=None, platform="ipados")

    created = await api.task(key, title="Add dark mode support")
    await services.push.drain()
    assert [(r.push_type, r.payload["aps"]["alert"]["subtitle"]) for r in push.sent] == [("alert", "New task")] * 2
    assert {r.payload["aps"]["alert"]["title"] for r in push.sent} == {"Claude Code"}
    assert {r.payload["aps"]["thread-id"] for r in push.sent} == {key_info["id"]}

    push.sent.clear()
    path = f"/v1/tasks/{created['id']}"
    await api.http.patch(path, json={"status": "in_progress"}, headers=key)
    await services.push.drain()
    assert len(push.of_type("alert")) == 2
    starts = push.of_type("liveactivity")
    assert [(r.ref.device_id, r.payload["aps"]["event"]) for r in starts] == [(DEVICE_A, "start")]
    assert starts[0].payload["aps"]["attributes"] == {"taskID": created["id"], "keyLabel": "Claude Code"}

    activity = {"device_id": DEVICE_A, "push_token": ACTIVITY_TOKEN}
    assert (await api.http.put(f"{path}/live-activity", json=activity, headers=account)).status_code == 204
    push.sent.clear()
    await api.http.patch(path, json={"progress": 60, "status_note": "Half done"}, headers=key)
    await services.push.drain()
    assert len(push.of_type("background")) == 2 and push.of_type("alert") == []
    updates = push.of_type("liveactivity")
    assert [(r.token, r.payload["aps"]["event"]) for r in updates] == [(ACTIVITY_TOKEN, "update")]
    assert updates[0].payload["aps"]["content-state"]["progress"] == 60
    assert updates[0].payload["aps"]["content-state"]["note"] == "Half done"

    push.sent.clear()
    await api.http.patch(path, json={"title": "Renamed by the app"}, headers=account)
    await services.push.drain()
    assert push.of_type("alert") == [] and len(push.of_type("background")) == 2
    assert [r.payload["aps"]["event"] for r in push.of_type("liveactivity")] == ["update"]

    push.sent.clear()
    await api.http.patch(path, json={"status": "done"}, headers=key)
    await services.push.drain()
    assert {r.payload["aps"]["alert"]["subtitle"] for r in push.of_type("alert")} == {"Task done"}
    ends = push.of_type("liveactivity")
    assert [(r.token, r.payload["aps"]["event"]) for r in ends] == [(ACTIVITY_TOKEN, "end")]
    assert ends[0].payload["aps"]["content-state"]["status"] == "done"
    assert (await services.store.push_context(key_info["id"], key_info["id"], created["id"])).activities == {}

    push.sent.clear()
    await api.http.delete(path, headers=key)
    await services.push.drain()
    assert {r.payload["aps"]["alert"]["subtitle"] for r in push.of_type("alert")} == {"Task deleted"}
    assert {r.payload["event"] for r in push.sent} == {"task.deleted"}
    assert push.of_type("liveactivity") == []


async def test_delete_ends_the_live_activity_of_the_task_and_of_a_deleted_key(
    api: Api, services: Services, push: FakePush
) -> None:
    account = await api.account()
    key, key_info = await api.key(account)
    await api.device(account, DEVICE_A)
    first = await api.task(key, status="in_progress", notify=False)
    second = await api.task(key, status="in_progress", notify=False)
    for item in (first, second):
        body = {"device_id": DEVICE_A, "push_token": ACTIVITY_TOKEN}
        await api.http.put(f"/v1/tasks/{item['id']}/live-activity", json=body, headers=account)
    await services.push.drain()
    push.sent.clear()

    await api.http.delete(f"/v1/tasks/{first['id']}", headers=account)
    await services.push.drain()
    assert [r.payload["aps"]["event"] for r in push.of_type("liveactivity")] == ["end"]
    assert [r.push_type for r in push.sent if r.push_type != "liveactivity"] == ["background"]

    push.sent.clear()
    await api.http.delete(f"/v1/keys/{key_info['id']}", headers=account)
    await services.push.drain()
    assert [(r.push_type, r.payload["aps"]["event"], r.ref.task_id) for r in push.sent] == [
        ("liveactivity", "end", second["id"])
    ]


async def test_dead_tokens_are_removed(api: Api, services: Services, push: FakePush) -> None:
    account = await api.account()
    account_id = (await api.http.get("/v1/account", headers=account)).json()["id"]
    key, key_info = await api.key(account)
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token="0f" * 32, push_to_start_token="1e" * 32)
    task_id = (await api.task(key, notify=False))["id"]
    await services.push.drain()
    body = {"device_id": DEVICE_B, "push_token": ACTIVITY_TOKEN}
    await api.http.put(f"/v1/tasks/{task_id}/live-activity", json=body, headers=account)

    push.dead = {APNS_TOKEN, START_TOKEN, ACTIVITY_TOKEN}
    await api.http.patch(f"/v1/tasks/{task_id}", json={"status": "in_progress"}, headers=key)
    await services.push.drain()

    context = await services.store.push_context(account_id, key_info["id"], task_id)
    tokens = {item.id: (item.apns_token, item.push_to_start_token) for item in context.devices}
    assert tokens == {DEVICE_A: (None, None), DEVICE_B: ("0f" * 32, "1e" * 32)}
    assert context.activities == {}


async def test_notify_user_counts_devices_and_sends_a_message(api: Api, services: Services, push: FakePush) -> None:
    account = await api.account()
    key, key_info = await api.key(account, "Codex")
    await api.device(account, DEVICE_A)
    await api.device(account, DEVICE_B, apns_token=None)
    account_info = (await api.http.get("/v1/account", headers=account)).json()
    principal = Principal(account_info["id"], key_info["id"])
    urgent = await api.task(key, priority="urgent", notify=False)
    await services.push.drain()
    push.sent.clear()

    message = NotifyInput(title="Approve the deploy", body="The build is ready.", task_id=urgent["id"])
    result = await services.tasks.notify_user(principal, message)
    assert (result.sent, result.devices) == (True, 1)
    await services.push.drain()
    assert len(push.sent) == 1
    sent = push.sent[0]
    assert sent.payload["aps"]["alert"] == {
        "title": "Codex",
        "subtitle": "Approve the deploy",
        "body": "The build is ready.",
    }
    assert (sent.payload["event"], sent.payload["task_id"], sent.payload["aps"]["category"]) == (
        "message",
        urgent["id"],
        "MESSAGE",
    )
    assert sent.payload["aps"]["interruption-level"] == "time-sensitive"


async def test_push_is_off_without_apns_configuration(
    settings: Settings, connect: Connect, redis: Redis, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("WARNING")
    app = create_app(settings.model_copy(update={"apns_bundle_id": None}), redis=connect(), event_redis=connect())
    assert [record.message for record in caplog.records if "push is off" in record.message] != []
    assert len([record for record in caplog.records if "push is off" in record.message]) == 1
    services: Services = app.state.services
    assert services.push.enabled is False
    async with running(app) as http:
        api = Api(http)
        account = await api.account()
        key, _ = await api.key(account)
        await api.device(account, DEVICE_A)
        task_id = (await api.task(key))["id"]
        assert (await http.patch(f"/v1/tasks/{task_id}", json={"status": "done"}, headers=key)).status_code == 200
        await services.push.drain()
