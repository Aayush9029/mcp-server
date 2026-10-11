"""What an agent gets back from a tool: the structured result and a short text summary."""

from datetime import UTC, datetime

from pydantic import BaseModel

from mcp_client_server.ids import TaskId
from mcp_client_server.models import Agent, InboxItem, Question, Task

_DESCRIPTION_PREVIEW = 4000


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class TaskOut(Task):
    created_at_iso: str
    updated_at_iso: str
    completed_at_iso: str | None

    @classmethod
    def of(cls, task: Task) -> "TaskOut":
        return cls(
            id=task.id,
            key_id=task.key_id,
            title=task.title,
            description=task.description,
            status=task.status,
            priority=task.priority,
            notify=task.notify,
            progress=task.progress,
            status_note=task.status_note,
            source=task.source,
            created_at=task.created_at,
            updated_at=task.updated_at,
            completed_at=task.completed_at,
            seen_at=task.seen_at,
            host=task.host,
            created_at_iso=iso(task.created_at),
            updated_at_iso=iso(task.updated_at),
            completed_at_iso=None if task.completed_at is None else iso(task.completed_at),
        )


class TaskListOut(BaseModel):
    tasks: list[TaskOut]
    next_cursor: str | None


class OpenTasksOut(BaseModel):
    tasks: list[TaskOut]


class DeleteOut(BaseModel):
    deleted: bool
    task_id: TaskId


def question_summary(question: Question) -> str:
    match question.status:
        case "answered":
            return f'Question {question.id} has the answer "{question.answer}".'
        case "pending":
            return (
                f"Question {question.id} has no answer yet. Call get_answer with this question_id to wait again, "
                "or continue with other work. Do not guess the answer."
            )
        case "expired":
            return f"Question {question.id} expired with no answer. Do not guess the answer."
        case "cancelled":
            return f"Question {question.id} was cancelled."


def inbox_note(unread: int) -> str:
    """The line that ends the text of each tool result while the inbox has unread items."""
    return f"\nInbox: {unread} unread. Call check_inbox." if unread > 0 else ""


def _inbox_line(item: InboxItem) -> str:
    sender = "the person" if item.sender.type == "person" else f"{item.sender.label} ({item.sender.key_id})"
    about = "".join(
        f", {name} {value}" for name, value in (("task", item.task_id), ("question", item.question_id)) if value
    )
    return f"{item.id} [{item.kind}] from {sender}{about}: {item.text}"


def inbox_listing(items: list[InboxItem]) -> str:
    if not items:
        return "The inbox has no unread items."
    count = "1 item" if len(items) == 1 else f"{len(items)} items"
    return "\n".join([f"{count}, oldest first.", *(_inbox_line(item) for item in items)])


def agent_listing(agents: list[Agent]) -> str:
    lines = [
        f"{agent.key_id} {agent.label}{' (you)' if agent.is_self else ''}{f' on {agent.host}' if agent.host else ''}"
        for agent in agents
    ]
    return "\n".join(["The keys of this account. Use a key ID or a label as `to` of send_message.", *lines])


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
