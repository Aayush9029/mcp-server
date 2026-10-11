from collections.abc import Iterable, Sequence

from mcp_client_server.clock import Clock
from mcp_client_server.credentials import AgentPrincipal
from mcp_client_server.events import frames
from mcp_client_server.ids import TaskId
from mcp_client_server.models import Task
from mcp_client_server.store import KeyTasks, Store

_ATTEMPTS = 3


class SeenMarker:
    """Sets `seen_at` of a task the first time the agent of its key gets the task."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def tasks(self, principal: AgentPrincipal, tasks: Sequence[Task]) -> list[Task]:
        """The same tasks, each with its `seen_at`. A task that the agent has seen costs no store call."""
        return [task if task.seen_at is not None else await self._mark(principal, task.id) or task for task in tasks]

    async def ids(self, principal: AgentPrincipal, task_ids: Iterable[TaskId]) -> None:
        for task_id in dict.fromkeys(task_ids):
            await self._mark(principal, task_id)

    async def _mark(self, principal: AgentPrincipal, task_id: TaskId) -> Task | None:
        """The task with `seen_at`, or None if the key has no such task."""
        for _ in range(_ATTEMPTS):
            stored = await self._store.get_task(KeyTasks(principal.key_id), task_id)
            if stored is None or stored.task.seen_at is not None:
                return stored.task if stored else None
            # `updated_at` stays: the agent got the task, it did not change it.
            seen = stored.task.model_copy(update={"seen_at": self._clock.now()})
            status = await self._store.update_task(principal.account_id, stored, seen, frames.task_updated(seen))
            if status == "ok":
                return seen
        return None
