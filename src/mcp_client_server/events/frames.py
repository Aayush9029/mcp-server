import json
from typing import Any

from mcp_client_server.models import Key, Task

PING = ": ping\n\n"
ACCOUNT_DELETED = "account.deleted"


def frame(event: str, data: dict[str, Any]) -> str:
    """One server-sent event. json.dumps never writes a raw newline, so the data is one line."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def event_name(sse_frame: str) -> str:
    return sse_frame.partition("\n")[0].removeprefix("event: ")


def ready(account_id: str) -> str:
    return frame("ready", {"account_id": account_id})


def task_event(kind: str, task: Task) -> str:
    return f'event: task.{kind}\ndata: {{"task":{task.model_dump_json()}}}\n\n'


def task_deleted(task: Task) -> str:
    return frame("task.deleted", {"task_id": task.id, "key_id": task.key_id})


def key_event(kind: str, key: Key) -> str:
    return frame(f"key.{kind}", {"key": key.model_dump(mode="json")})


def key_deleted(key_id: str) -> str:
    return frame("key.deleted", {"key_id": key_id})


def account_deleted() -> str:
    return frame(ACCOUNT_DELETED, {})
