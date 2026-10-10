import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from mcp_client_server.ids import now
from mcp_client_server.models import CLOSED_STATUSES, Device, Task
from mcp_client_server.push import payloads
from mcp_client_server.push.apns import PushSender
from mcp_client_server.push.payloads import PushRequest, TaskEvent, TokenRef
from mcp_client_server.store import EndedActivity, PushContext, Store

logger = logging.getLogger(__name__)

ChangeKind = Literal["created", "updated", "deleted"]

_LIVE_FIELDS = frozenset({"title", "status", "progress", "status_note"})
# A change of only these fields is a progress tick. It updates the devices but does not alert the person.
_QUIET_FIELDS = frozenset({"progress", "status_note"})
_STATUS_SUBTITLES = {"in_progress": "Task started", "done": "Task done", "cancelled": "Task cancelled"}
_EVENTS: dict[ChangeKind, TaskEvent] = {
    "created": "task.created",
    "updated": "task.updated",
    "deleted": "task.deleted",
}


@dataclass(frozen=True, slots=True)
class TaskChange:
    kind: ChangeKind
    task: Task
    changed: frozenset[str]
    by_agent: bool
    # For a delete, the Live Activity tokens that the task had. The store has removed them by then.
    activities: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Topics:
    app: str
    watch: str

    def for_device(self, device: Device) -> str:
        return self.watch if device.platform == "watchos" else self.app

    @property
    def live_activity(self) -> str:
        return self.app + payloads.LIVE_ACTIVITY_TOPIC_SUFFIX


def subtitle(change: TaskChange) -> str:
    if change.kind == "created":
        return "New task"
    if change.kind == "deleted":
        return "Task deleted"
    if "status" in change.changed:
        return _STATUS_SUBTITLES.get(change.task.status, "Task updated")
    return "Task updated"


def is_visible(change: TaskChange) -> bool:
    if not (change.by_agent and change.task.notify):
        return False
    return change.kind != "updated" or bool(change.changed - _QUIET_FIELDS)


def ends_activity(change: TaskChange) -> bool:
    return change.kind == "deleted" or ("status" in change.changed and change.task.status in CLOSED_STATUSES)


def starts_activity(change: TaskChange) -> bool:
    if not change.by_agent or change.task.status != "in_progress":
        return False
    return change.kind == "created" or (change.kind == "updated" and "status" in change.changed)


def plan_task_pushes(change: TaskChange, ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    task = change.task
    event = _EVENTS[change.kind]
    alert = payloads.visible(event, task, ctx.key_label, subtitle(change)) if is_visible(change) else None
    requests: list[PushRequest] = []
    for device in ctx.devices:
        if not device.apns_token:
            continue
        requests.append(
            PushRequest(
                token=device.apns_token,
                environment=device.apns_environment,
                topic=topics.for_device(device),
                push_type="alert" if alert else "background",
                priority=10 if alert else 5,
                payload=alert or payloads.silent(event, task),
                ttl_seconds=payloads.ALERT_TTL_SECONDS if alert else payloads.TRANSIENT_TTL_SECONDS,
                ref=TokenRef("apns_token", device.id),
                collapse_id=task.id,
            )
        )
    if ctx.live_activities:
        requests.extend(_plan_activity_pushes(change, ctx, topics, at))
    return requests


def _plan_activity_pushes(change: TaskChange, ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    task = change.task
    devices = {device.id: device for device in ctx.devices}
    registered = ctx.activities if change.activities is None else change.activities
    live = {device_id: token for device_id, token in registered.items() if device_id in devices}

    def request(
        device: Device, token: str, kind: payloads.TokenKind, payload: dict[str, Any], priority: int
    ) -> PushRequest:
        return PushRequest(
            token=token,
            environment=device.apns_environment,
            topic=topics.live_activity,
            push_type="liveactivity",
            priority=priority,
            payload=payload,
            ttl_seconds=payloads.TRANSIENT_TTL_SECONDS,
            ref=TokenRef(kind, device.id, task.id),
        )

    if ends_activity(change):
        end = payloads.activity_end(task, at)
        return [request(devices[device_id], token, "activity", end, 10) for device_id, token in live.items()]

    requests: list[PushRequest] = []
    if change.kind == "updated" and change.changed & _LIVE_FIELDS:
        update = payloads.activity_update(task, at)
        priority = 5 if change.changed <= _QUIET_FIELDS else 10
        requests += [
            request(devices[device_id], token, "activity", update, priority) for device_id, token in live.items()
        ]
    if starts_activity(change):
        start = payloads.activity_start(task, ctx.key_label, at)
        requests += [
            request(device, device.push_to_start_token, "push_to_start_token", start, 10)
            for device in ctx.devices
            if device.push_to_start_token and device.id not in live
        ]
    return requests


def plan_message(
    key_id: str, title: str, body: str, task: Task | None, ctx: PushContext, topics: Topics
) -> list[PushRequest]:
    payload = payloads.message(key_id, ctx.key_label, title, body, task)
    return [
        PushRequest(
            token=device.apns_token,
            environment=device.apns_environment,
            topic=topics.for_device(device),
            push_type="alert",
            priority=10,
            payload=payload,
            ttl_seconds=payloads.ALERT_TTL_SECONDS,
            ref=TokenRef("apns_token", device.id),
        )
        for device in ctx.devices
        if device.apns_token
    ]


def plan_activity_ends(ended: list[EndedActivity], ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    if not ctx.live_activities:
        return []
    devices = {device.id: device for device in ctx.devices}
    return [
        PushRequest(
            token=token,
            environment=devices[device_id].apns_environment,
            topic=topics.live_activity,
            push_type="liveactivity",
            priority=10,
            payload=payloads.activity_end(item.task, at),
            ttl_seconds=payloads.TRANSIENT_TTL_SECONDS,
            ref=TokenRef("activity", device_id, item.task.id),
        )
        for item in ended
        for device_id, token in item.tokens.items()
        if device_id in devices
    ]


class PushDispatcher:
    """Sends pushes in background tasks, so that a request does not wait on APNs."""

    def __init__(self, store: Store, sender: PushSender | None, topics: Topics | None) -> None:
        self._store = store
        self._sender = sender if topics else None
        self._topics = topics or Topics("", "")
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        return self._sender is not None

    def task_changed(self, account_id: str, change: TaskChange) -> None:
        if self._sender:
            self._spawn(self._task_changed(account_id, change))

    def activities_ended(self, account_id: str, key_id: str, ended: list[EndedActivity]) -> None:
        if self._sender and ended:
            self._spawn(self._activities_ended(account_id, key_id, ended))

    async def message(self, account_id: str, key_id: str, title: str, body: str, task: Task | None) -> int:
        """Queue a notify_user push. Returns the number of devices that get it."""
        if not self._sender:
            return 0
        ctx = await self._store.push_context(account_id, key_id, None)
        requests = plan_message(key_id, title, body, task, ctx, self._topics)
        if requests:
            self._spawn(self._deliver(account_id, requests))
        return len(requests)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        await self.drain()
        if self._sender:
            await self._sender.aclose()

    async def _task_changed(self, account_id: str, change: TaskChange) -> None:
        task = change.task
        ctx = await self._store.push_context(account_id, task.key_id, task.id)
        await self._deliver(account_id, plan_task_pushes(change, ctx, self._topics, now()))
        if change.kind == "deleted" or not ctx.activities:
            return
        if ends_activity(change):
            await self._store.clear_activities(task.id)
            return
        known = {device.id for device in ctx.devices}
        for device_id in ctx.activities.keys() - known:
            await self._store.forget_activity(task.id, device_id)

    async def _activities_ended(self, account_id: str, key_id: str, ended: list[EndedActivity]) -> None:
        ctx = await self._store.push_context(account_id, key_id, None)
        await self._deliver(account_id, plan_activity_ends(ended, ctx, self._topics, now()))

    async def _deliver(self, account_id: str, requests: list[PushRequest]) -> None:
        sender = self._sender
        if not sender or not requests:
            return
        results = await asyncio.gather(*(sender.send(request) for request in requests), return_exceptions=True)
        for request, result in zip(requests, results, strict=True):
            if isinstance(result, BaseException):
                logger.error("push to device %s failed: %r", request.ref.device_id, result)
            elif result.token_is_dead:
                await self._forget(account_id, request)
            elif not result.ok:
                logger.warning(
                    "APNs refused a %s push to device %s: %s %s",
                    request.push_type,
                    request.ref.device_id,
                    result.status,
                    result.reason,
                )

    async def _forget(self, account_id: str, request: PushRequest) -> None:
        ref = request.ref
        logger.info("removing dead %s of device %s", ref.kind, ref.device_id)
        if ref.kind == "activity":
            if ref.task_id:
                await self._store.forget_activity(ref.task_id, ref.device_id, request.token)
        else:
            await self._store.clear_device_token(account_id, ref.device_id, ref.kind, request.token)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("push task failed", exc_info=task.exception())
