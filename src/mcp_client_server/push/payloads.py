"""APNs payloads from docs/api.md. All functions here are pure."""

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from mcp_client_server import limits
from mcp_client_server.models import ApnsEnvironment, Task

MAX_PAYLOAD_BYTES = 4096
ATTRIBUTES_TYPE = "TaskActivityAttributes"
LIVE_ACTIVITY_TOPIC_SUFFIX = ".push-type.liveactivity"

ALERT_TTL_SECONDS = 24 * 3600
TRANSIENT_TTL_SECONDS = 3600

PushType = Literal["alert", "background", "liveactivity"]
TokenKind = Literal["apns_token", "push_to_start_token", "activity"]
TaskEvent = Literal["task.created", "task.updated", "task.deleted"]


@dataclass(frozen=True, slots=True)
class TokenRef:
    """Where a token is stored, so that a dead token can be removed."""

    kind: TokenKind
    device_id: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class PushRequest:
    token: str
    environment: ApnsEnvironment
    topic: str
    push_type: PushType
    priority: int
    payload: dict[str, Any]
    ttl_seconds: int
    ref: TokenRef
    collapse_id: str | None = None


def visible(event: TaskEvent, task: Task, key_label: str, subtitle: str) -> dict[str, Any]:
    return {
        "aps": {
            "alert": {"title": key_label, "subtitle": subtitle, "body": task.title},
            "sound": "default",
            "thread-id": task.key_id,
            "category": "TASK",
            "interruption-level": "time-sensitive" if task.priority == "urgent" else "active",
        },
        "event": event,
        "task_id": task.id,
        "key_id": task.key_id,
    }


def silent(event: TaskEvent, task: Task) -> dict[str, Any]:
    return {"aps": {"content-available": 1}, "event": event, "task_id": task.id, "key_id": task.key_id}


def message(key_id: str, key_label: str, title: str, body: str, task: Task | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "aps": {
            "alert": {"title": key_label, "subtitle": title, "body": body},
            "sound": "default",
            "thread-id": key_id,
            "category": "MESSAGE",
            "interruption-level": "time-sensitive" if task and task.priority == "urgent" else "active",
        },
        "event": "message",
        "key_id": key_id,
    }
    if task:
        payload["task_id"] = task.id
    return payload


def content_state(task: Task) -> dict[str, Any]:
    return {
        "title": task.title,
        "status": task.status,
        "progress": task.progress,
        "note": task.status_note,
        "updatedAt": task.updated_at,
    }


def activity_start(task: Task, key_label: str, now: float) -> dict[str, Any]:
    return {
        "aps": {
            "timestamp": int(now),
            "event": "start",
            "content-state": content_state(task),
            "attributes-type": ATTRIBUTES_TYPE,
            "attributes": {"taskID": task.id, "keyLabel": key_label},
            # ActivityKit shows a push-to-start only if it has an alert.
            "alert": {"title": key_label, "body": task.title},
        }
    }


def activity_update(task: Task, now: float) -> dict[str, Any]:
    return {"aps": {"timestamp": int(now), "event": "update", "content-state": content_state(task)}}


def activity_end(task: Task, now: float) -> dict[str, Any]:
    return {
        "aps": {
            "timestamp": int(now),
            "event": "end",
            "content-state": content_state(task),
            "dismissal-date": int(now) + limits.LIVE_ACTIVITY_DISMISS_SECONDS,
        }
    }


def encode(payload: dict[str, Any]) -> bytes:
    """JSON for APNs. If an alert makes the payload too large, the alert body gets shorter."""
    data = _dumps(payload)
    aps: dict[str, Any] = payload.get("aps", {})
    alert: Any = aps.get("alert")
    if len(data) <= MAX_PAYLOAD_BYTES or not isinstance(alert, dict):
        return data
    fields = cast(dict[str, Any], alert)
    body = str(fields.get("body", ""))
    while len(data) > MAX_PAYLOAD_BYTES and len(body) > 1:
        # Each character is one byte or more, so this cut is always enough progress.
        body = body[: max(1, len(body) - (len(data) - MAX_PAYLOAD_BYTES) - 1)]
        data = _dumps({**payload, "aps": {**aps, "alert": {**fields, "body": body.rstrip() + "…"}}})
    return data


def _dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
