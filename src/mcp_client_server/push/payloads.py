"""APNs payloads from docs/push.md. All functions here are pure."""

import json
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict

from mcp_client_server import limits
from mcp_client_server.ids import DeviceId, KeyId, QuestionId, TaskId
from mcp_client_server.models import ApnsEnvironment, Question, QuestionKind, Status, Task

MAX_PAYLOAD_BYTES = 4096
ATTRIBUTES_TYPE = "TaskActivityAttributes"
LIVE_ACTIVITY_TOPIC_SUFFIX = ".push-type.liveactivity"
# Apple, "Updating widgets with WidgetKit push notifications": the push type, the topic suffix, and the payload.
WIDGET_TOPIC_SUFFIX = ".push-type.widgets"

ALERT_TTL_SECONDS = 24 * 3600
TRANSIENT_TTL_SECONDS = 3600

PushType = Literal["alert", "background", "liveactivity", "widgets"]
TokenKind = Literal["apns_token", "push_to_start_token", "widget_push_token", "activity"]
QuestionCategory = Literal["QUESTION_CONFIRM", "QUESTION_CHOICE", "QUESTION_TEXT"]
TaskEvent = Literal["task.created", "task.updated", "task.deleted"]
InterruptionLevel = Literal["active", "time-sensitive"]


class Alert(TypedDict):
    title: str
    subtitle: NotRequired[str]
    body: str


AlertAps = TypedDict(
    "AlertAps",
    {
        "alert": Alert,
        "sound": Literal["default"],
        "thread-id": KeyId,
        "category": Literal["TASK", "MESSAGE"],
        "interruption-level": InterruptionLevel,
    },
)
SilentAps = TypedDict("SilentAps", {"content-available": Literal[1]})


class VisiblePayload(TypedDict):
    aps: AlertAps
    event: TaskEvent
    task_id: TaskId
    key_id: KeyId


class SilentPayload(TypedDict):
    aps: SilentAps
    event: TaskEvent
    task_id: TaskId
    key_id: KeyId


class MessagePayload(TypedDict):
    aps: AlertAps
    event: Literal["message"]
    key_id: KeyId
    task_id: NotRequired[TaskId]


QuestionAps = TypedDict(
    "QuestionAps",
    {
        "alert": Alert,
        "sound": Literal["default"],
        "thread-id": KeyId,
        "category": QuestionCategory,
        "interruption-level": Literal["time-sensitive"],
        "mutable-content": Literal[1],
    },
)


class QuestionPayload(TypedDict):
    aps: QuestionAps
    event: Literal["question.created"]
    question_id: QuestionId
    kind: QuestionKind
    options: list[str]
    task_id: TaskId | None
    key_id: KeyId


class QuestionSilentPayload(TypedDict):
    aps: SilentAps
    event: Literal["question.updated"]
    question_id: QuestionId
    key_id: KeyId


class QuestionState(TypedDict):
    id: QuestionId
    prompt: str
    kind: QuestionKind
    options: list[str]


class ContentState(TypedDict):
    title: str
    status: Status
    progress: int | None
    note: str | None
    updatedAt: float
    question: QuestionState | None


WidgetAps = TypedDict("WidgetAps", {"content-changed": Literal[True]})


class WidgetPayload(TypedDict):
    aps: WidgetAps


class ActivityAttributes(TypedDict):
    taskID: TaskId
    keyLabel: str


ActivityStartAps = TypedDict(
    "ActivityStartAps",
    {
        "timestamp": int,
        "event": Literal["start"],
        "content-state": ContentState,
        "stale-date": int,
        "relevance-score": int,
        "attributes-type": str,
        "attributes": ActivityAttributes,
        "alert": Alert,
    },
)
ActivityUpdateAps = TypedDict(
    "ActivityUpdateAps",
    {
        "timestamp": int,
        "event": Literal["update"],
        "content-state": ContentState,
        "stale-date": int,
        "relevance-score": int,
    },
)
ActivityEndAps = TypedDict(
    "ActivityEndAps",
    {"timestamp": int, "event": Literal["end"], "content-state": ContentState, "dismissal-date": int},
)


class ActivityStartPayload(TypedDict):
    aps: ActivityStartAps


class ActivityUpdatePayload(TypedDict):
    aps: ActivityUpdateAps


class ActivityEndPayload(TypedDict):
    aps: ActivityEndAps


type ActivityPayload = ActivityStartPayload | ActivityUpdatePayload | ActivityEndPayload
type Payload = (
    VisiblePayload
    | SilentPayload
    | MessagePayload
    | QuestionPayload
    | QuestionSilentPayload
    | ActivityPayload
    | WidgetPayload
)


@dataclass(frozen=True, slots=True)
class TokenRef:
    """Where a token is stored, so that a dead token can be removed."""

    kind: TokenKind
    device_id: DeviceId
    task_id: TaskId | None = None


@dataclass(frozen=True, slots=True)
class PushRequest:
    token: str
    environment: ApnsEnvironment
    topic: str
    push_type: PushType
    # None leaves the header out, and APNs uses its default.
    priority: Literal[5, 10] | None
    payload: Payload
    ttl_seconds: int
    ref: TokenRef
    collapse_id: str | None = None


def _level(task: Task | None) -> InterruptionLevel:
    return "time-sensitive" if task and task.priority == "urgent" else "active"


def visible(event: TaskEvent, task: Task, key_label: str, subtitle: str) -> VisiblePayload:
    return {
        "aps": {
            "alert": {"title": key_label, "subtitle": subtitle, "body": task.title},
            "sound": "default",
            "thread-id": task.key_id,
            "category": "TASK",
            "interruption-level": _level(task),
        },
        "event": event,
        "task_id": task.id,
        "key_id": task.key_id,
    }


def silent(event: TaskEvent, task: Task) -> SilentPayload:
    return {"aps": {"content-available": 1}, "event": event, "task_id": task.id, "key_id": task.key_id}


def message(key_id: KeyId, key_label: str, title: str, body: str, task: Task | None) -> MessagePayload:
    payload: MessagePayload = {
        "aps": {
            "alert": {"title": key_label, "subtitle": title, "body": body},
            "sound": "default",
            "thread-id": key_id,
            "category": "MESSAGE",
            "interruption-level": _level(task),
        },
        "event": "message",
        "key_id": key_id,
    }
    if task:
        payload["task_id"] = task.id
    return payload


_CATEGORIES: dict[QuestionKind, QuestionCategory] = {
    "confirm": "QUESTION_CONFIRM",
    "choice": "QUESTION_CHOICE",
    "text": "QUESTION_TEXT",
}


def question_asked(question: Question, key_label: str) -> QuestionPayload:
    return {
        "aps": {
            "alert": {"title": key_label, "subtitle": "Question", "body": question.prompt},
            "sound": "default",
            "thread-id": question.key_id,
            "category": _CATEGORIES[question.kind],
            "interruption-level": "time-sensitive",
            "mutable-content": 1,
        },
        "event": "question.created",
        "question_id": question.id,
        "kind": question.kind,
        "options": question.options,
        "task_id": question.task_id,
        "key_id": question.key_id,
    }


def question_changed(question: Question) -> QuestionSilentPayload:
    return {
        "aps": {"content-available": 1},
        "event": "question.updated",
        "question_id": question.id,
        "key_id": question.key_id,
    }


def widget_reload() -> WidgetPayload:
    return {"aps": {"content-changed": True}}


def content_state(task: Task, question: Question | None) -> ContentState:
    """`question` is the pending question that the Live Activity shows, or None."""
    waiting: QuestionState | None = None
    if question:
        waiting = {"id": question.id, "prompt": question.prompt, "kind": question.kind, "options": question.options}
    return {
        "title": task.title,
        "status": task.status,
        "progress": task.progress,
        "note": task.status_note,
        "updatedAt": task.updated_at,
        "question": waiting,
    }


def relevance_score(task: Task, question: Question | None) -> int:
    """Which Live Activity of the account goes first: one that waits for the person, then one that runs."""
    if question is not None:
        return 100
    return 75 if task.status == "in_progress" else 50


def activity_start(task: Task, key_label: str, now: float, question: Question | None = None) -> ActivityStartPayload:
    return {
        "aps": {
            "timestamp": int(now),
            "event": "start",
            "content-state": content_state(task, question),
            "stale-date": int(now) + limits.LIVE_ACTIVITY_STALE_SECONDS,
            "relevance-score": relevance_score(task, question),
            "attributes-type": ATTRIBUTES_TYPE,
            "attributes": {"taskID": task.id, "keyLabel": key_label},
            # ActivityKit shows a push-to-start only if it has an alert.
            "alert": {"title": key_label, "body": task.title},
        }
    }


def activity_update(task: Task, now: float, question: Question | None = None) -> ActivityUpdatePayload:
    return {
        "aps": {
            "timestamp": int(now),
            "event": "update",
            "content-state": content_state(task, question),
            "stale-date": int(now) + limits.LIVE_ACTIVITY_STALE_SECONDS,
            "relevance-score": relevance_score(task, question),
        }
    }


def activity_end(task: Task, now: float) -> ActivityEndPayload:
    return {
        "aps": {
            "timestamp": int(now),
            "event": "end",
            "content-state": content_state(task, None),
            "dismissal-date": int(now) + limits.LIVE_ACTIVITY_DISMISS_SECONDS,
        }
    }


def encode(payload: Payload) -> bytes:
    """JSON for APNs. If an alert makes the payload too large, the alert body gets shorter."""
    data = _dumps(payload)
    aps = payload["aps"]
    if len(data) <= MAX_PAYLOAD_BYTES or "alert" not in aps:
        return data
    alert = aps["alert"]
    body = alert["body"]
    while len(data) > MAX_PAYLOAD_BYTES and len(body) > 1:
        # Each character is one byte or more, so this cut is always enough progress.
        body = body[: max(1, len(body) - (len(data) - MAX_PAYLOAD_BYTES) - 1)]
        data = _dumps({**payload, "aps": {**aps, "alert": {**alert, "body": body.rstrip() + "…"}}})
    return data


def _dumps(payload: object) -> bytes:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
