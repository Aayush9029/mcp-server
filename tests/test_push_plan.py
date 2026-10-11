"""Which pushes a change gives, and the exact payloads of docs/push.md. No store and no network."""

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_client_server.ids import DeviceId, KeyId, TaskId
from mcp_client_server.models import PatchField, Task
from mcp_client_server.push import PushRequest, TaskChange, Topics
from mcp_client_server.push import payloads as p
from mcp_client_server.push.dispatcher import (
    ChangeKind,
    plan_activity_ends,
    plan_activity_update,
    plan_message,
    plan_task_pushes,
    updates_activity,
)
from mcp_client_server.push.payloads import TokenRef
from mcp_client_server.store import EndedActivity, PushContext
from tests.support import ACTIVITY_TOKEN, APNS_TOKEN, START_TOKEN, Json, make_device, make_task, payload_json

TOPICS = Topics(app="com.example.app", watch="com.example.app.watchkitapp")
TASK_ID = TaskId("tsk_01jb2m9x4e7q")
KEY_ID = KeyId("key_01jb2k3v9d")
DEV_1, DEV_2, DEV_3 = DeviceId("dev_1"), DeviceId("dev_2"), DeviceId("dev_3")
STATE: Json = {
    "title": "Add dark mode support",
    "status": "in_progress",
    "progress": 40,
    "note": "Updating the color tokens",
    "updatedAt": 1760112300.9,
    "question": None,
}


def ctx(devices: list[Json], activities: dict[DeviceId, str] | None = None, *, live: bool = True) -> PushContext:
    return PushContext(
        key_label="Claude Code",
        live_activities=live,
        devices=[make_device(**fields) for fields in devices],
        activities=activities or {},
    )


def change(
    kind: ChangeKind,
    task: Task,
    *changed: PatchField,
    by_agent: bool = True,
    activities: dict[DeviceId, str] | None = None,
) -> TaskChange:
    return TaskChange(kind, task, frozenset(changed), by_agent, activities)


def plan(item: TaskChange, context: PushContext, at: float = 50.0) -> list[PushRequest]:
    return plan_task_pushes(item, context, TOPICS, at)


def activity(requests: list[PushRequest]) -> list[PushRequest]:
    return [request for request in requests if request.push_type == "liveactivity"]


def aps(request: PushRequest) -> Json:
    body: Json = payload_json(request)["aps"]
    return body


def test_visible_payload_is_the_one_in_the_contract() -> None:
    payload = p.visible("task.updated", make_task(status="done"), "Claude Code", "Task done")
    assert payload == {
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
    urgent = p.visible("task.created", make_task(priority="urgent"), "Claude Code", "New task")
    assert urgent["aps"]["interruption-level"] == "time-sensitive"


def test_silent_payload_is_the_one_in_the_contract() -> None:
    assert p.silent("task.updated", make_task()) == {
        "aps": {"content-available": 1},
        "event": "task.updated",
        "task_id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
    }


def test_message_payload_has_a_task_id_only_if_the_agent_gave_one() -> None:
    payload = p.message(KEY_ID, "Cursor", "Review needed", "Open the pull request.", None)
    assert payload == {
        "aps": {
            "alert": {"title": "Cursor", "subtitle": "Review needed", "body": "Open the pull request."},
            "sound": "default",
            "thread-id": "key_01jb2k3v9d",
            "category": "MESSAGE",
            "interruption-level": "active",
        },
        "event": "message",
        "key_id": "key_01jb2k3v9d",
    }
    about = p.message(KEY_ID, "Cursor", "Now", "Body", make_task(priority="urgent"))
    assert about.get("task_id") == "tsk_01jb2m9x4e7q"
    assert about["aps"]["interruption-level"] == "time-sensitive"


def test_live_activity_payloads_use_the_exact_activitykit_keys() -> None:
    task = make_task()
    assert p.content_state(make_task(progress=None, status_note=None), None) == {
        **STATE,
        "progress": None,
        "note": None,
    }
    assert p.activity_start(task, "Claude Code", 1760112301.7) == {
        "aps": {
            "timestamp": 1760112301,
            "event": "start",
            "content-state": STATE,
            "stale-date": 1760112301 + 30 * 60,
            "relevance-score": 75,
            "attributes-type": "TaskActivityAttributes",
            "attributes": {"taskID": "tsk_01jb2m9x4e7q", "keyLabel": "Claude Code"},
            "alert": {"title": "Claude Code", "body": "Add dark mode support"},
        }
    }
    assert p.activity_update(task, 1760112301.7) == {
        "aps": {
            "timestamp": 1760112301,
            "event": "update",
            "content-state": STATE,
            "stale-date": 1760112301 + 30 * 60,
            "relevance-score": 75,
        }
    }
    # A task that waits for the person goes first, then one that runs, then the others.
    assert p.activity_update(make_task(status="todo"), 1.0)["aps"]["relevance-score"] == 50
    assert p.activity_end(make_task(status="done", progress=100), 1760112301.7) == {
        "aps": {
            "timestamp": 1760112301,
            "event": "end",
            "content-state": {**STATE, "status": "done", "progress": 100},
            "dismissal-date": 1760112301 + 15 * 60,
        }
    }


@given(body=st.text(st.characters(codec="utf-8", exclude_categories=["Cs"]), min_size=1, max_size=1000))
def test_an_encoded_alert_is_never_above_the_apns_limit(body: str) -> None:
    payload = p.message(KEY_ID, "Cursor", "Title", body, None)
    data = p.encode(payload)
    sent: str = json.loads(data)["aps"]["alert"]["body"]

    assert len(data) <= p.MAX_PAYLOAD_BYTES
    assert sent == body or (sent.endswith("…") and body.startswith(sent[:-1].rstrip()))
    assert payload["aps"]["alert"]["body"] == body


def test_a_large_alert_is_cut_and_marked_and_the_request_keeps_the_full_text() -> None:
    payload = p.message(KEY_ID, "Cursor", "Title", "🚀" * 1000, None)
    body: str = json.loads(p.encode(payload))["aps"]["alert"]["body"]
    assert body.startswith("🚀" * 500)
    assert body.endswith("…")
    assert payload["aps"]["alert"]["body"] == "🚀" * 1000


def test_an_agent_change_with_notify_alerts_each_device_that_has_a_token() -> None:
    devices: list[Json] = [
        {"device_id": "dev_1"},
        {"device_id": "dev_2", "apns_token": None},
        {"device_id": "dev_3", "platform": "watchos", "apns_environment": "sandbox"},
    ]
    requests = plan(change("created", make_task(status="todo")), ctx(devices, live=False))

    assert [(r.ref, r.topic, r.environment) for r in requests] == [
        (TokenRef("apns_token", DEV_1), "com.example.app", "production"),
        (TokenRef("apns_token", DEV_3), "com.example.app.watchkitapp", "sandbox"),
    ]
    first = requests[0]
    assert (first.push_type, first.priority, first.collapse_id, first.token, first.ttl_seconds) == (
        "alert",
        10,
        "tsk_01jb2m9x4e7q",
        APNS_TOKEN,
        24 * 3600,
    )
    assert (aps(first)["alert"]["subtitle"], payload_json(first)["event"]) == ("New task", "task.created")


@pytest.mark.parametrize(
    ("item", "subtitle"),
    [
        (change("created", make_task()), "New task"),
        (change("updated", make_task(status="in_progress"), "status"), "Task started"),
        (change("updated", make_task(status="done"), "status", "progress"), "Task done"),
        (change("updated", make_task(status="cancelled"), "status"), "Task cancelled"),
        (change("updated", make_task(status="todo"), "status"), "Task updated"),
        (change("updated", make_task(), "title", "description"), "Task updated"),
        (change("updated", make_task(), "priority", "progress"), "Task updated"),
        (change("deleted", make_task()), "Task deleted"),
    ],
)
def test_alert_subtitle_says_what_happened(item: TaskChange, subtitle: str) -> None:
    requests = plan(item, ctx([{}], live=False))
    assert [aps(r)["alert"]["subtitle"] for r in requests] == [subtitle]


@pytest.mark.parametrize(
    "quiet",
    [
        change("updated", make_task(notify=False), "status"),
        change("created", make_task(notify=False)),
        change("deleted", make_task(notify=False)),
        change("updated", make_task(), "title", by_agent=False),
        change("created", make_task(), by_agent=False),
        change("deleted", make_task(), by_agent=False),
        change("updated", make_task(), "progress"),
        change("updated", make_task(), "progress", "status_note"),
    ],
)
def test_a_quiet_change_gives_only_the_silent_push(quiet: TaskChange) -> None:
    requests = plan(quiet, ctx([{}], live=False))
    assert [(r.push_type, r.priority, r.topic, r.ttl_seconds) for r in requests] == [
        ("background", 5, "com.example.app", 3600)
    ]
    assert payload_json(requests[0]) == {
        "aps": {"content-available": 1},
        "event": f"task.{quiet.kind}",
        "task_id": "tsk_01jb2m9x4e7q",
        "key_id": "key_01jb2k3v9d",
    }


def test_agent_start_sends_push_to_start_only_where_no_activity_runs() -> None:
    devices: list[Json] = [
        {"device_id": "dev_1"},
        {"device_id": "dev_2"},
        {"device_id": "dev_3", "push_to_start_token": None},
    ]
    context = ctx(devices, {DEV_2: ACTIVITY_TOKEN})
    live = activity(plan(change("updated", make_task(), "status"), context))

    by_event = {(aps(r)["event"], r.ref.device_id): r for r in live}
    # The `update` of DEV_2 is not planned here: it goes through the throttle of the task.
    assert set(by_event) == {("start", DEV_1)}
    start = by_event["start", DEV_1]
    assert (start.token, start.topic, start.priority, start.collapse_id) == (
        START_TOKEN,
        "com.example.app.push-type.liveactivity",
        10,
        None,
    )
    assert start.ref == TokenRef("push_to_start_token", DEV_1, TASK_ID)
    updates = plan_activity_update(make_task(), context, TOPICS, 50.0)
    assert [(r.ref, r.token, aps(r)["event"]) for r in updates] == [
        (TokenRef("activity", DEV_2, TASK_ID), ACTIVITY_TOKEN, "update")
    ]


def test_only_an_agent_that_moves_a_task_to_in_progress_starts_an_activity() -> None:
    context = ctx([{}])

    def events(item: TaskChange) -> list[str]:
        return [aps(r)["event"] for r in activity(plan(item, context))]

    assert events(change("created", make_task())) == ["start"]
    assert events(change("updated", make_task(), "status")) == ["start"]
    assert events(change("created", make_task(status="todo"))) == []
    assert events(change("updated", make_task(), "title")) == []
    assert events(change("updated", make_task(), "status", by_agent=False)) == []
    assert events(change("created", make_task(), by_agent=False)) == []


def test_activity_update_fields_and_priority() -> None:
    """Each field that the Live Activity shows gives an update, and each update has the priority 10.

    A priority of 5 can wait for a long time on a locked device, so the progress on the Lock Screen would not
    move. The throttle of the task, not the priority, keeps the pushes in the budget.
    """
    context = ctx([{}], {DEV_1: ACTIVITY_TOKEN, DeviceId("dev_gone"): "ffff"})

    def updates(*changed: PatchField) -> bool:
        item = change("updated", make_task(), *changed)
        assert activity(plan(item, context)) == []
        return updates_activity(item)

    assert updates("progress") is True
    assert updates("status_note", "progress") is True
    assert updates("title") is True
    assert updates("description") is False
    assert updates("priority", "notify") is False
    assert updates_activity(change("updated", make_task(status="done"), "status")) is False
    assert updates_activity(change("deleted", make_task(), "title")) is False
    requests = plan_activity_update(make_task(), context, TOPICS, 50.0)
    assert [(aps(r)["event"], r.priority, r.ref.device_id, r.push_type) for r in requests] == [
        ("update", 10, DEV_1, "liveactivity")
    ]
    assert plan_activity_update(make_task(), ctx([{}], {DEV_1: ACTIVITY_TOKEN}, live=False), TOPICS, 50.0) == []


@pytest.mark.parametrize(
    "ending",
    [
        change("updated", make_task(status="done", progress=100), "status", "progress"),
        change("updated", make_task(status="cancelled"), "status", by_agent=False),
        change("deleted", make_task(), activities={DEV_1: ACTIVITY_TOKEN}),
    ],
)
def test_a_closed_or_deleted_task_ends_its_activity_with_the_final_state(ending: TaskChange) -> None:
    context = ctx([{}], {} if ending.kind == "deleted" else {DEV_1: ACTIVITY_TOKEN})
    ended = activity(plan(ending, context, at=50.0))

    assert [(r.token, r.priority) for r in ended] == [(ACTIVITY_TOKEN, 10)]
    assert aps(ended[0]) == {
        "timestamp": 50,
        "event": "end",
        "content-state": {**STATE, "status": ending.task.status, "progress": ending.task.progress},
        "dismissal-date": 50 + 900,
    }


def test_no_activity_push_if_the_account_turned_live_activities_off() -> None:
    off = ctx([{}], {DEV_1: ACTIVITY_TOKEN}, live=False)
    for item in (change("updated", make_task(), "status"), change("updated", make_task(status="done"), "status")):
        assert activity(plan(item, off)) == []
    ended = [EndedActivity(make_task(), {DEV_1: ACTIVITY_TOKEN})]
    assert plan_activity_ends(ended, off, TOPICS, 50.0) == []


def test_a_deleted_key_ends_the_activities_of_its_tasks() -> None:
    ended = [
        EndedActivity(make_task(id="tsk_a"), {DEV_1: "aa11", DeviceId("dev_gone"): "ffff"}),
        EndedActivity(make_task(id="tsk_b"), {DEV_1: "bb22"}),
    ]
    requests = plan_activity_ends(ended, ctx([{}]), TOPICS, 50.0)
    assert [(r.token, r.ref.task_id, aps(r)["event"], r.topic) for r in requests] == [
        ("aa11", "tsk_a", "end", "com.example.app.push-type.liveactivity"),
        ("bb22", "tsk_b", "end", "com.example.app.push-type.liveactivity"),
    ]


def test_a_message_goes_to_each_device_with_a_token_and_has_no_collapse_id() -> None:
    context = ctx([{"device_id": "dev_1"}, {"device_id": "dev_2", "apns_token": None}])
    requests = plan_message(KEY_ID, "Review needed", "Body", None, context, TOPICS)
    assert [(r.ref.device_id, r.push_type, r.priority, r.collapse_id) for r in requests] == [(DEV_1, "alert", 10, None)]
    assert aps(requests[0])["category"] == "MESSAGE"
