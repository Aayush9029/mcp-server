"""What an agent gets back from a tool: the structured result and a short text summary."""

from datetime import UTC, datetime

from pydantic import BaseModel

from mcp_client_server.models import Task

_DESCRIPTION_PREVIEW = 4000


def iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class TaskOut(Task):
    created_at_iso: str
    updated_at_iso: str
    completed_at_iso: str | None

    @classmethod
    def of(cls, task: Task) -> "TaskOut":
        return cls(
            **task.model_dump(),
            created_at_iso=iso(task.created_at) or "",
            updated_at_iso=iso(task.updated_at) or "",
            completed_at_iso=iso(task.completed_at),
        )


class TaskListOut(BaseModel):
    tasks: list[TaskOut]
    next_cursor: str | None


class DeleteOut(BaseModel):
    deleted: bool
    task_id: str


def line(task: Task) -> str:
    progress = f" {task.progress}%" if task.progress is not None else ""
    note = f" - {task.status_note}" if task.status_note else ""
    return f"{task.id} [{task.status}, {task.priority}]{progress} {task.title}{note}"


def detail(task: Task) -> str:
    parts = [line(task), f"Updated {iso(task.updated_at)}."]
    if task.description:
        text = task.description
        if len(text) > _DESCRIPTION_PREVIEW:
            text = text[:_DESCRIPTION_PREVIEW] + "\n[The description continues in structuredContent.description.]"
        parts.append(text)
    return "\n".join(parts)


def listing(tasks: list[Task], next_cursor: str | None) -> str:
    if not tasks:
        return "No tasks match."
    count = "1 task" if len(tasks) == 1 else f"{len(tasks)} tasks"
    more = f' More tasks follow: call list_tasks again with cursor "{next_cursor}".' if next_cursor else ""
    return "\n".join([f"{count}, newest update first.{more}", *(line(task) for task in tasks)])
