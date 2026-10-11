"""Server-sent event frames from docs/api.md. A frame is the unit that goes through pub/sub."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from mcp_client_server.ids import AccountId, KeyId, QuestionId, TaskId
from mcp_client_server.models import Key, Question, Task

EventName = Literal[
    "ready",
    "task.created",
    "task.updated",
    "task.deleted",
    "key.created",
    "key.updated",
    "key.deleted",
    "question.created",
    "question.updated",
    "inbox.updated",
    "account.deleted",
]

PING = ": ping\n\n"
_FINAL_PREFIX = "event: account.deleted\n"
_KEY_DELETED_PREFIX = "event: key.deleted\n"
_INBOX_PREFIX = "event: inbox.updated\n"


class _Data(BaseModel):
    model_config = ConfigDict(frozen=True)


class _Ready(_Data):
    account_id: AccountId


class _TaskData(_Data):
    task: Task


class _TaskDeleted(_Data):
    task_id: TaskId
    key_id: KeyId


class _KeyData(_Data):
    key: Key


class _KeyDeleted(_Data):
    key_id: KeyId


class _QuestionData(_Data):
    question: Question


class _InboxData(_Data):
    key_id: KeyId
    unread: int


def _frame(event: EventName, data: BaseModel) -> str:
    # pydantic writes JSON with no raw newline, so the data is one line.
    return f"event: {event}\ndata: {data.model_dump_json()}\n\n"


def is_final(frame: str) -> bool:
    """True for the frame after which the server closes the stream."""
    return frame.startswith(_FINAL_PREFIX)


def can_change_question(frame: str, question_id: QuestionId) -> bool:
    """True for a frame after which a question can have a new status, or can be gone with its key."""
    return question_id in frame or frame.startswith(_KEY_DELETED_PREFIX)


def can_change_inbox(frame: str, key_id: KeyId) -> bool:
    """True for a frame after which the inbox of a key can have a new item, or can be gone with its key."""
    return key_id in frame and frame.startswith((_INBOX_PREFIX, _KEY_DELETED_PREFIX))


def is_key_deleted(frame: str) -> bool:
    return frame.startswith(_KEY_DELETED_PREFIX)


def ready(account_id: AccountId) -> str:
    return _frame("ready", _Ready(account_id=account_id))


def task_created(task: Task) -> str:
    return _frame("task.created", _TaskData(task=task))


def task_updated(task: Task) -> str:
    return _frame("task.updated", _TaskData(task=task))


def task_deleted(task: Task) -> str:
    return _frame("task.deleted", _TaskDeleted(task_id=task.id, key_id=task.key_id))


def key_created(key: Key) -> str:
    return _frame("key.created", _KeyData(key=key))


def key_updated(key: Key) -> str:
    return _frame("key.updated", _KeyData(key=key))


def key_deleted(key_id: KeyId) -> str:
    return _frame("key.deleted", _KeyDeleted(key_id=key_id))


def question_created(question: Question) -> str:
    return _frame("question.created", _QuestionData(question=question))


def question_updated(question: Question) -> str:
    return _frame("question.updated", _QuestionData(question=question))


def inbox_updated(key_id: KeyId, unread: int) -> str:
    return _frame("inbox.updated", _InboxData(key_id=key_id, unread=unread))


def account_deleted() -> str:
    return _frame("account.deleted", _Data())
