import asyncio
import logging
from collections.abc import Coroutine, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from mcp_client_server.clock import Clock
from mcp_client_server.ids import AccountId, DeviceId, KeyId, TaskId
from mcp_client_server.limits import LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS, WIDGET_PUSH_INTERVAL_SECONDS
from mcp_client_server.models import CLOSED_STATUSES, Device, PatchField, Question, Task
from mcp_client_server.push import payloads
from mcp_client_server.push.apns import PushSender
from mcp_client_server.push.payloads import ActivityPayload, PushRequest, TaskEvent, TokenKind, TokenRef
from mcp_client_server.store import EndedActivity, PushContext, StoredTask

logger = logging.getLogger(__name__)

ChangeKind = Literal["created", "updated", "deleted"]

_LIVE_FIELDS: frozenset[PatchField] = frozenset({"title", "status", "progress", "status_note"})
# A change of only these fields is a progress tick. It updates the devices but does not alert the person.
_QUIET_FIELDS: frozenset[PatchField] = frozenset({"progress", "status_note"})
# Each Live Activity push has this priority. A priority of 5 can wait for a long time on a locked device, so the
# progress on the Lock Screen would not move. The throttle on the task keeps the number of pushes in the budget.
_ACTIVITY_PRIORITY: Literal[10] = 10
_STATUS_SUBTITLES = {"in_progress": "Task started", "done": "Task done", "cancelled": "Task cancelled"}
_EVENTS: dict[ChangeKind, TaskEvent] = {
    "created": "task.created",
    "updated": "task.updated",
    "deleted": "task.deleted",
}
# Jobs that wait for the store or for APNs. A job above this number is dropped, so a slow APNs cannot fill the memory.
_MAX_PENDING_JOBS = 2000
_CLOSE_SECONDS = 10.0


class PushStore(Protocol):
    """The part of the store that the dispatcher needs."""

    async def push_context(self, account_id: AccountId, key_id: KeyId, task_id: TaskId | None) -> PushContext: ...

    async def clear_activities(self, task_id: TaskId) -> None: ...

    async def forget_activity(self, task_id: TaskId, device_id: DeviceId, token: str | None = None) -> None: ...

    async def clear_device_token(
        self,
        account_id: AccountId,
        device_id: DeviceId,
        field: Literal["apns_token", "push_to_start_token", "widget_push_token"],
        token: str,
    ) -> None: ...

    async def get_task_unscoped(self, task_id: TaskId) -> StoredTask | None: ...

    async def list_devices(self, account_id: AccountId) -> list[Device]: ...

    async def widget_slot(self, account_id: AccountId, device_id: DeviceId) -> bool: ...

    async def renew_widget_slot(self, account_id: AccountId, device_id: DeviceId) -> None: ...

    async def take_widget_mark(self, account_id: AccountId, device_id: DeviceId) -> bool: ...

    async def widget_throttled(self, account_id: AccountId, device_id: DeviceId) -> bool: ...

    async def activity_slot(self, task_id: TaskId) -> bool: ...

    async def renew_activity_slot(self, task_id: TaskId) -> None: ...

    async def activity_throttled(self, task_id: TaskId) -> bool: ...

    async def take_activity_mark(self, task_id: TaskId) -> bool: ...


@dataclass(frozen=True, slots=True)
class TaskChange:
    kind: ChangeKind
    task: Task
    changed: frozenset[PatchField]
    by_agent: bool
    # For a delete, the Live Activity tokens that the task had. The store has removed them by then.
    activities: Mapping[DeviceId, str] | None = None


@dataclass(frozen=True, slots=True)
class QuestionChange:
    kind: Literal["created", "updated"]
    question: Question


@dataclass(frozen=True, slots=True)
class Topics:
    app: str
    watch: str

    def for_device(self, device: Device) -> str:
        return self.watch if device.platform == "watchos" else self.app

    @property
    def live_activity(self) -> str:
        return self.app + payloads.LIVE_ACTIVITY_TOPIC_SUFFIX

    def widgets(self, device: Device) -> str:
        return self.for_device(device) + payloads.WIDGET_TOPIC_SUFFIX


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


def updates_activity(change: TaskChange) -> bool:
    """True for a change that a running Live Activity shows. The dispatcher sends it through the throttle."""
    return change.kind == "updated" and bool(change.changed & _LIVE_FIELDS) and not ends_activity(change)


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


def _activity_request(
    device: Device,
    token: str,
    kind: TokenKind,
    task: Task,
    payload: ActivityPayload,
    topics: Topics,
    priority: Literal[5, 10],
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


def _plan_activity_pushes(change: TaskChange, ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    task = change.task
    devices = {device.id: device for device in ctx.devices}
    registered = ctx.activities if change.activities is None else change.activities
    live = [(devices[device_id], token) for device_id, token in registered.items() if device_id in devices]

    if ends_activity(change):
        end = payloads.activity_end(task, at)
        return [_activity_request(device, token, "activity", task, end, topics, 10) for device, token in live]

    # An `update` is not planned here: the dispatcher sends it through the throttle of the task.
    requests: list[PushRequest] = []
    if starts_activity(change):
        start = payloads.activity_start(task, ctx.key_label, at, ctx.question)
        running = {device.id for device, _ in live}
        requests += [
            _activity_request(device, device.push_to_start_token, "push_to_start_token", task, start, topics, 10)
            for device in ctx.devices
            if device.push_to_start_token and device.id not in running
        ]
    return requests


def plan_activity_update(task: Task, ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    """The latest state of the task to each registered activity of it."""
    if not ctx.live_activities:
        return []
    devices = {device.id: device for device in ctx.devices}
    update = payloads.activity_update(task, at, ctx.question)
    return [
        _activity_request(devices[device_id], token, "activity", task, update, topics, _ACTIVITY_PRIORITY)
        for device_id, token in ctx.activities.items()
        if device_id in devices
    ]


def plan_message(
    key_id: KeyId, title: str, body: str, task: Task | None, ctx: PushContext, topics: Topics
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


def plan_question_pushes(change: QuestionChange, ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    """A new question alerts each device. A status change removes the alert."""
    question = change.question
    asked = payloads.question_asked(question, ctx.key_label) if change.kind == "created" else None
    return [
        PushRequest(
            token=device.apns_token,
            environment=device.apns_environment,
            topic=topics.for_device(device),
            push_type="alert" if asked else "background",
            priority=10 if asked else 5,
            payload=asked or payloads.question_changed(question),
            # A question that is expired cannot be answered, so its alert must not arrive after that time.
            ttl_seconds=max(1, int(question.expires_at - at)) if asked else payloads.TRANSIENT_TTL_SECONDS,
            ref=TokenRef("apns_token", device.id),
            collapse_id=question.id,
        )
        for device in ctx.devices
        if device.apns_token
    ]


def widget_request(device: Device, token: str, topics: Topics) -> PushRequest:
    return PushRequest(
        token=token,
        environment=device.apns_environment,
        topic=topics.widgets(device),
        push_type="widgets",
        # Apple's page for this push type gives no priority, so the header is left out.
        priority=None,
        payload=payloads.widget_reload(),
        ttl_seconds=payloads.TRANSIENT_TTL_SECONDS,
        ref=TokenRef("widget_push_token", device.id),
    )


def plan_activity_ends(ended: list[EndedActivity], ctx: PushContext, topics: Topics, at: float) -> list[PushRequest]:
    if not ctx.live_activities:
        return []
    devices = {device.id: device for device in ctx.devices}
    return [
        _activity_request(
            devices[device_id], token, "activity", item.task, payloads.activity_end(item.task, at), topics, 10
        )
        for item in ended
        for device_id, token in item.tokens.items()
        if device_id in devices
    ]


class PushDispatcher:
    """Sends pushes in background jobs, so that a request does not wait on APNs."""

    def __init__(
        self,
        store: PushStore,
        sender: PushSender | None,
        topics: Topics | None,
        clock: Clock,
        *,
        max_pending_jobs: int = _MAX_PENDING_JOBS,
    ) -> None:
        self._store = store
        self._sender = sender if topics else None
        self._topics = topics or Topics("", "")
        self._clock = clock
        self._max_pending_jobs = max_pending_jobs
        self._jobs: set[asyncio.Task[None]] = set()
        # One for each device with a WidgetKit throttle that this process started. It sends the push that waited.
        self._widget_windows: dict[tuple[AccountId, DeviceId], asyncio.Task[None]] = {}
        # The same for each task with a Live Activity update throttle.
        self._activity_windows: dict[TaskId, asyncio.Task[None]] = {}
        self._dropped = 0

    @property
    def enabled(self) -> bool:
        return self._sender is not None

    def task_changed(self, account_id: AccountId, change: TaskChange) -> None:
        if self._sender:
            self._spawn(self._task_changed(account_id, change))

    def question_changed(self, account_id: AccountId, change: QuestionChange) -> None:
        if self._sender:
            self._spawn(self._question_changed(account_id, change))

    def activities_ended(self, account_id: AccountId, key_id: KeyId, ended: list[EndedActivity]) -> None:
        if self._sender and ended:
            self._spawn(self._activities_ended(account_id, key_id, ended))

    async def message(self, account_id: AccountId, key_id: KeyId, title: str, body: str, task: Task | None) -> int:
        """Queue a notify_user push. Returns the number of devices that get it."""
        if not self._sender:
            return 0
        ctx = await self._store.push_context(account_id, key_id, None)
        requests = plan_message(key_id, title, body, task, ctx, self._topics)
        if requests:
            self._spawn(self._deliver(account_id, requests))
        return len(requests)

    async def drain(self) -> None:
        while self._jobs:
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
            # gather does not give control to the loop if each job is done already. A job leaves `_jobs` in a
            # callback, and without this line that callback never runs and this loop never ends.
            await asyncio.sleep(0)

    async def aclose(self) -> None:
        try:
            async with asyncio.timeout(_CLOSE_SECONDS):
                await self.drain()
        except TimeoutError:
            logger.warning("shutdown: %d push jobs did not finish and are cancelled", len(self._jobs))
            for job in tuple(self._jobs):
                job.cancel()
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
        windows = (*self._widget_windows.values(), *self._activity_windows.values())
        for window in windows:
            window.cancel()
        await asyncio.gather(*windows, return_exceptions=True)
        if self._sender:
            await self._sender.aclose()

    async def _task_changed(self, account_id: AccountId, change: TaskChange) -> None:
        task = change.task
        ctx = await self._store.push_context(account_id, task.key_id, task.id)
        await self._deliver(account_id, plan_task_pushes(change, ctx, self._topics, self._clock.now()))
        await self._reload_widgets(account_id, ctx.devices)
        if change.kind == "deleted" or not ctx.activities:
            return
        if ends_activity(change):
            await self._store.clear_activities(task.id)
            return
        if updates_activity(change) and ctx.live_activities:
            await self._update_activity(account_id, task.key_id, task.id)
        known = {device.id for device in ctx.devices}
        for device_id in ctx.activities.keys() - known:
            await self._store.forget_activity(task.id, device_id)

    async def _question_changed(self, account_id: AccountId, change: QuestionChange) -> None:
        question = change.question
        ctx = await self._store.push_context(account_id, question.key_id, question.task_id)
        await self._deliver(account_id, plan_question_pushes(change, ctx, self._topics, self._clock.now()))
        await self._reload_widgets(account_id, ctx.devices)
        if question.task_id and ctx.activities and ctx.live_activities:
            await self._update_activity(account_id, question.key_id, question.task_id)

    async def _update_activity(self, account_id: AccountId, key_id: KeyId, task_id: TaskId) -> None:
        """Send the latest state of the task to its Live Activity, one time in each interval at most.

        A change in the interval marks the task, and the window of the task sends the state that is then
        current when the interval ends. So the last change is never lost, and the activity shows it promptly.
        """
        if await self._store.activity_slot(task_id):
            await self._send_activity_update(account_id, key_id, task_id)
            self._open_activity_window(account_id, key_id, task_id)

    async def _send_activity_update(self, account_id: AccountId, key_id: KeyId, task_id: TaskId) -> None:
        stored = await self._store.get_task_unscoped(task_id)
        if stored is None:
            return
        ctx = await self._store.push_context(account_id, key_id, task_id)
        await self._deliver(account_id, plan_activity_update(stored.task, ctx, self._topics, self._clock.now()))

    def _open_activity_window(self, account_id: AccountId, key_id: KeyId, task_id: TaskId) -> None:
        if task_id in self._activity_windows:
            return
        window = asyncio.create_task(self._activity_window(account_id, key_id, task_id))
        self._activity_windows[task_id] = window
        window.add_done_callback(lambda _: self._activity_windows.pop(task_id, None))

    async def _activity_window(self, account_id: AccountId, key_id: KeyId, task_id: TaskId) -> None:
        while True:
            await self._clock.sleep(LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS)
            try:
                if not await self._store.take_activity_mark(task_id):
                    if await self._store.activity_throttled(task_id):
                        continue
                    return
                await self._store.renew_activity_slot(task_id)
                await self._send_activity_update(account_id, key_id, task_id)
            except Exception:
                logger.exception("Live Activity update for task %s failed", task_id)
                return

    async def _reload_widgets(self, account_id: AccountId, devices: list[Device]) -> None:
        requests: list[PushRequest] = []
        for device in devices:
            if device.widget_push_token and await self._store.widget_slot(account_id, device.id):
                requests.append(widget_request(device, device.widget_push_token, self._topics))
                self._open_widget_window(account_id, device.id)
        await self._deliver(account_id, requests)

    def _open_widget_window(self, account_id: AccountId, device_id: DeviceId) -> None:
        key = (account_id, device_id)
        if key in self._widget_windows:
            return
        window = asyncio.create_task(self._widget_window(account_id, device_id))
        self._widget_windows[key] = window
        window.add_done_callback(lambda _: self._widget_windows.pop(key, None))

    async def _widget_window(self, account_id: AccountId, device_id: DeviceId) -> None:
        """After each throttle interval, send the one push that stands for the changes that came in the interval.

        The window stays while the throttle is on in the store, also if a different worker turned it on again.
        """
        while True:
            await self._clock.sleep(WIDGET_PUSH_INTERVAL_SECONDS)
            try:
                if not await self._store.take_widget_mark(account_id, device_id):
                    if await self._store.widget_throttled(account_id, device_id):
                        continue
                    return
                device = next((d for d in await self._store.list_devices(account_id) if d.id == device_id), None)
                if device is None or not device.widget_push_token:
                    return
                await self._store.renew_widget_slot(account_id, device_id)
                await self._deliver(account_id, [widget_request(device, device.widget_push_token, self._topics)])
            except Exception:
                logger.exception("widget push for device %s failed", device_id)
                return

    async def _activities_ended(self, account_id: AccountId, key_id: KeyId, ended: list[EndedActivity]) -> None:
        ctx = await self._store.push_context(account_id, key_id, None)
        await self._deliver(account_id, plan_activity_ends(ended, ctx, self._topics, self._clock.now()))

    async def _deliver(self, account_id: AccountId, requests: list[PushRequest]) -> None:
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

    async def _forget(self, account_id: AccountId, request: PushRequest) -> None:
        ref = request.ref
        logger.info("removing dead %s of device %s", ref.kind, ref.device_id)
        if ref.kind == "activity":
            if ref.task_id:
                await self._store.forget_activity(ref.task_id, ref.device_id, request.token)
        else:
            await self._store.clear_device_token(account_id, ref.device_id, ref.kind, request.token)

    def _spawn(self, job: Coroutine[object, None, None]) -> None:
        if len(self._jobs) >= self._max_pending_jobs:
            job.close()
            self._dropped += 1
            # One line for the first drop and then for each thousand, so a flood does not fill the log.
            if self._dropped % 1000 == 1:
                logger.warning("push is behind: %d jobs wait, %d dropped so far", len(self._jobs), self._dropped)
            return
        task = asyncio.create_task(job)
        self._jobs.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._jobs.discard(task)
        if not task.cancelled() and task.exception():
            logger.error("push job failed", exc_info=task.exception())
