from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from redis.asyncio import Redis

from mcp_client_server.credentials import AccountPrincipal, AgentPrincipal, CredentialHash, Principal
from mcp_client_server.ids import AccountId, DeviceId, KeyId, QuestionId, TaskId
from mcp_client_server.limits import (
    ATTEST_CHALLENGE_TTL_SECONDS,
    LAST_USED_WRITE_INTERVAL_SECONDS,
    LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS,
    WIDGET_PUSH_INTERVAL_SECONDS,
    Limits,
)
from mcp_client_server.models import (
    QUESTION_STATUSES,
    STATUSES,
    Account,
    AccountSettings,
    ClientInfo,
    Device,
    InboxItem,
    Key,
    KeyCounts,
    Question,
    QuestionStatus,
    Status,
    Task,
)
from mcp_client_server.store import scripts
from mcp_client_server.store.client import (
    Arg,
    KeyValue,
    ReplyError,
    integer,
    items,
    opt_text,
    text,
    text_map,
    text_set,
    texts,
)
from mcp_client_server.store.schema import KeySpace

DeviceTokenField = Literal["apns_token", "push_to_start_token", "widget_push_token"]


@dataclass(frozen=True, slots=True)
class AuthResult:
    principal: Principal | None
    limited: bool = False
    # Unread inbox items of the key. The MCP surface shows this number with each tool result.
    inbox_unread: int = 0


@dataclass(frozen=True, slots=True)
class KeyTasks:
    key_id: KeyId


@dataclass(frozen=True, slots=True)
class AccountTasks:
    account_id: AccountId


type TaskScope = KeyTasks | AccountTasks


@dataclass(frozen=True, slots=True)
class StoredTask:
    """A task with the exact JSON that the store has. The JSON is the guard of the next write."""

    raw: str
    task: Task


@dataclass(frozen=True, slots=True)
class StoredQuestion:
    raw: str
    question: Question


@dataclass(frozen=True, slots=True)
class DeletedTask:
    activities: dict[DeviceId, str]
    pending_questions: list[QuestionId]


@dataclass(frozen=True, slots=True)
class Evicted:
    """The oldest closed task of a key, removed because the key had more closed tasks than the limit."""

    task_id: TaskId
    pending_questions: list[QuestionId]


@dataclass(frozen=True, slots=True)
class AttestedKey:
    key_id: str
    public_key: str
    counter: int


@dataclass(frozen=True, slots=True)
class Cursor:
    score: float
    task_id: TaskId


@dataclass(frozen=True, slots=True)
class RawPage:
    docs: list[str]
    last: Cursor | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class EndedActivity:
    task: Task
    tokens: dict[DeviceId, str]


@dataclass(frozen=True, slots=True)
class PushContext:
    key_label: str
    live_activities: bool
    devices: list[Device]
    activities: dict[DeviceId, str]
    # The oldest pending question of the task. The Live Activity shows it.
    question: Question | None = None


def _score(value: float) -> str:
    return repr(value)


def _one_of[T: str](reply: object, *allowed: T) -> T:
    for status in allowed:
        if reply == status:
            return status
    raise ReplyError(" or ".join(allowed), reply)


def _tokens(reply: object) -> dict[DeviceId, str]:
    return {DeviceId(device_id): token for device_id, token in text_map(reply).items()}


class Store:
    """The only code that talks to Key Value."""

    def __init__(self, redis: Redis, prefix: str, limits: Limits | None = None) -> None:
        self._kv = KeyValue(redis)
        self._limits = limits or Limits()
        self.keys = KeySpace(prefix)
        self._p = prefix
        reg = self._kv.script
        self._auth = reg(scripts.AUTH)
        self._rate_hit = reg(scripts.RATE_HIT)
        self._hash_update = reg(scripts.HASH_UPDATE)
        self._hash_clear = reg(scripts.HASH_CLEAR)
        self._device_put = reg(scripts.DEVICE_PUT)
        self._device_delete = reg(scripts.DEVICE_DELETE)
        self._key_create = reg(scripts.KEY_CREATE)
        self._key_detach = reg(scripts.KEY_DETACH)
        self._key_purge = reg(scripts.KEY_PURGE)
        self._account_detach = reg(scripts.ACCOUNT_DETACH)
        self._task_create = reg(scripts.TASK_CREATE)
        self._task_update = reg(scripts.TASK_UPDATE)
        self._task_delete = reg(scripts.TASK_DELETE)
        self._task_evict = reg(scripts.TASK_EVICT)
        self._task_page = reg(scripts.TASK_PAGE)
        self._activity_put = reg(scripts.ACTIVITY_PUT)
        self._activity_delete = reg(scripts.ACTIVITY_DELETE)
        self._account_create = reg(scripts.ACCOUNT_CREATE)
        self._question_create = reg(scripts.QUESTION_CREATE)
        self._question_close = reg(scripts.QUESTION_CLOSE)
        self._question_purge = reg(scripts.QUESTION_PURGE)
        self._question_list = reg(scripts.QUESTION_LIST)
        self._widget_slot = reg(scripts.WIDGET_SLOT)
        self._inbox_add = reg(scripts.INBOX_ADD)
        self._inbox_take = reg(scripts.INBOX_TAKE)
        self._inbox_view = reg(scripts.INBOX_VIEW)

    async def ping(self) -> None:
        await self._kv.call("PING")

    async def publish(self, account_id: AccountId, frame: str) -> None:
        await self._kv.call("PUBLISH", self.keys.channel(account_id), frame)

    # Credentials and rate limits

    async def authenticate(
        self, cred_hash: CredentialHash, *, now: float, rate_limit: int, rate_bucket: str, rate_ttl: int
    ) -> AuthResult:
        reply = items(
            await self._auth(
                args=[
                    self._p,
                    cred_hash,
                    _score(now),
                    rate_limit,
                    rate_bucket,
                    rate_ttl,
                    LAST_USED_WRITE_INTERVAL_SECONDS,
                    _score(now - self._limits.unread_inbox_ttl_seconds),
                ]
            )
        )
        code = integer(reply[0])
        if code == 0:
            return AuthResult(None)
        kind, _, rest = text(reply[1]).partition(":")
        principal: Principal
        if kind == "a":
            principal = AccountPrincipal(AccountId(rest))
        else:
            account_id, _, key_id = rest.partition(":")
            principal = AgentPrincipal(AccountId(account_id), KeyId(key_id))
        if code != 1:
            return AuthResult(principal, limited=True)
        return AuthResult(principal, inbox_unread=integer(reply[2]))

    async def hit(self, bucket: str, ttl: int) -> int:
        return integer(await self._rate_hit(keys=[self.keys.rate(bucket)], args=[ttl]))

    # Accounts

    async def create_account(
        self, account: Account, cred_hash: CredentialHash, attested: AttestedKey | None = None
    ) -> Literal["ok", "key_used"]:
        reply = await self._account_create(
            args=[
                self._p,
                account.id,
                cred_hash,
                _score(account.created_at),
                "1" if account.settings.live_activities else "0",
                attested.key_id if attested else "",
                attested.public_key if attested else "",
                attested.counter if attested else 0,
            ]
        )
        return _one_of(reply, "ok", "key_used")

    async def put_challenge(self, challenge: str, issued_at: float) -> None:
        # The store removes a challenge that nobody used. The service compares the time, so a test can move the clock.
        key = self.keys.attest_challenge(challenge)
        await self._kv.call("SET", key, _score(issued_at), "EX", 2 * ATTEST_CHALLENGE_TTL_SECONDS)

    async def take_challenge(self, challenge: str) -> float | None:
        """The time the challenge was made. Each challenge is given one time, then it is gone."""
        issued_at = opt_text(await self._kv.call("GETDEL", self.keys.attest_challenge(challenge)))
        return float(issued_at) if issued_at else None

    async def get_account(self, account_id: AccountId) -> Account | None:
        fields = text_map(await self._kv.call("HGETALL", self.keys.account(account_id)))
        return _account(fields) if fields else None

    async def set_live_activities(self, account_id: AccountId, *, enabled: bool) -> Account | None:
        reply = await self._hash_update(
            keys=[self.keys.account(account_id)],
            args=["id", account_id, "live_activities", "1" if enabled else "0"],
        )
        return None if reply is None else _account(text_map(items(reply)[1]))

    async def detach_account(self, account_id: AccountId, now: float, event: str) -> list[KeyId] | None:
        """Make an account unusable at once. Returns its keys, which wait for `purge_key`. None: no such account."""
        reply = await self._account_detach(args=[self._p, account_id, _score(now), event])
        if reply == 0:
            return None
        return [KeyId(key_id) for key_id in texts(items(reply))]

    # Devices

    async def put_device(self, account_id: AccountId, device: Device) -> Literal["ok", "limit", "no_account"]:
        fields = (
            "platform",
            device.platform,
            "model",
            device.model,
            "os_version",
            device.os_version,
            "app_version",
            device.app_version,
            "apns_environment",
            device.apns_environment,
            "apns_token",
            device.apns_token or "",
            "push_to_start_token",
            device.push_to_start_token or "",
            "widget_push_token",
            device.widget_push_token or "",
            "updated_at",
            _score(device.updated_at),
        )
        reply = await self._device_put(
            args=[self._p, account_id, device.id, self._limits.max_devices_per_account, *fields]
        )
        return _one_of(reply, "ok", "limit", "no_account")

    async def delete_device(self, account_id: AccountId, device_id: DeviceId) -> bool:
        return integer(await self._device_delete(args=[self._p, account_id, device_id])) == 1

    async def list_devices(self, account_id: AccountId) -> list[Device]:
        ids = text_set(await self._kv.call("SMEMBERS", self.keys.account_devices(account_id)))
        return await self._load_devices(account_id, ids)

    async def _load_devices(self, account_id: AccountId, device_ids: set[str]) -> list[Device]:
        if not device_ids:
            return []
        ordered = [DeviceId(device_id) for device_id in sorted(device_ids)]
        rows = await self._kv.batch(("HGETALL", self.keys.device(account_id, device_id)) for device_id in ordered)
        devices = ((device_id, text_map(row)) for device_id, row in zip(ordered, rows, strict=True))
        return [_device(device_id, fields) for device_id, fields in devices if fields]

    async def clear_device_token(
        self, account_id: AccountId, device_id: DeviceId, field: DeviceTokenField, token: str
    ) -> None:
        keys = [self.keys.device(account_id, device_id)]
        if field == "apns_token":
            keys.append(self.keys.token(token))
        await self._hash_clear(keys=keys, args=[field, token, "empty"])

    # Keys

    async def create_key(
        self, account_id: AccountId, key: Key, cred_hash: CredentialHash, event: str
    ) -> Literal["ok", "limit", "no_account"]:
        reply = await self._key_create(
            args=[
                self._p,
                account_id,
                key.id,
                cred_hash,
                self._limits.max_keys_per_account,
                _score(key.created_at),
                key.label,
                key.prefix,
                key.last4,
                event,
            ]
        )
        return _one_of(reply, "ok", "limit", "no_account")

    async def get_key(self, account_id: AccountId, key_id: KeyId) -> Key | None:
        fields = text_map(await self._kv.call("HGETALL", self.keys.key(key_id)))
        if not fields or fields.get("account_id") != account_id:
            return None
        return _key(key_id, fields)

    async def list_keys(self, account_id: AccountId) -> list[Key]:
        ids = [
            KeyId(key_id)
            for key_id in texts(items(await self._kv.call("ZREVRANGE", self.keys.account_keys(account_id), 0, -1)))
        ]
        if not ids:
            return []
        rows = await self._kv.batch(("HGETALL", self.keys.key(key_id)) for key_id in ids)
        keys = ((key_id, text_map(row)) for key_id, row in zip(ids, rows, strict=True))
        return [_key(key_id, fields) for key_id, fields in keys if fields]

    async def set_key_label(self, account_id: AccountId, key_id: KeyId, label: str) -> Key | None:
        reply = await self._hash_update(keys=[self.keys.key(key_id)], args=["account_id", account_id, "label", label])
        return None if reply is None else _key(key_id, text_map(items(reply)[1]))

    async def set_key_client(self, account_id: AccountId, key_id: KeyId, client: ClientInfo) -> Key | None:
        """Write `last_client`. Returns the key only if the client is a different one than before."""
        reply = await self._hash_update(
            keys=[self.keys.key(key_id)],
            args=["account_id", account_id, "client_name", client.name, "client_version", client.version],
        )
        if reply is None:
            return None
        changed, fields = items(reply)
        return _key(key_id, text_map(fields)) if integer(changed) == 1 else None

    async def detach_key(self, account_id: AccountId, key_id: KeyId, now: float, event: str) -> bool:
        """Make a key unusable at once. Its data waits for `purge_key`. False if the account has no such key."""
        reply = integer(await self._key_detach(args=[self._p, account_id, key_id, _score(now), event]))
        return reply != 0

    async def purge_key(self, key_id: KeyId, batch: int) -> tuple[bool, list[EndedActivity]]:
        """Delete up to `batch` items of a detached key. Returns (done, the Live Activities of the deleted tasks)."""
        reply = items(await self._key_purge(args=[self._p, key_id, batch]))
        ended: list[EndedActivity] = []
        for entry in reply[1:]:
            raw, tokens = items(entry)
            if raw is not None:
                ended.append(EndedActivity(Task.model_validate_json(text(raw)), _tokens(tokens)))
        return _one_of(reply[0], "done", "more") == "done", ended

    async def keys_to_purge(self, count: int) -> list[KeyId]:
        """Detached keys whose data is not all gone yet, oldest first."""
        reply = await self._kv.call("ZRANGE", self.keys.keys_to_purge, 0, count - 1)
        return [KeyId(key_id) for key_id in texts(items(reply))]

    # Tasks

    async def create_task(self, account_id: AccountId, task: Task, event: str) -> Literal["ok", "limit", "no_key"]:
        reply = await self._task_create(
            args=[
                self._p,
                account_id,
                task.key_id,
                task.id,
                task.model_dump_json(),
                task.status,
                _score(task.updated_at),
                self._limits.max_open_tasks_per_key,
                event,
            ]
        )
        return _one_of(reply, "ok", "limit", "no_key")

    async def get_task(self, scope: TaskScope, task_id: TaskId) -> StoredTask | None:
        score, raw = await self._kv.batch([("ZSCORE", self._index(scope), task_id), ("GET", self.keys.task(task_id))])
        if score is None or raw is None:
            return None
        return _stored(text(raw))

    async def update_task(
        self, account_id: AccountId, old: StoredTask, new: Task, event: str
    ) -> Literal["ok", "conflict", "limit"]:
        reply = await self._task_update(
            args=[
                self._p,
                account_id,
                new.key_id,
                new.id,
                old.raw,
                new.model_dump_json(),
                old.task.status,
                new.status,
                _score(new.updated_at),
                self._limits.max_open_tasks_per_key,
                event,
            ]
        )
        return _one_of(reply, "ok", "conflict", "limit")

    async def delete_task(self, account_id: AccountId, stored: StoredTask, event: str) -> DeletedTask | None:
        """Delete a task. Returns what the task had, or None on a concurrent write."""
        task = stored.task
        reply = await self._task_delete(
            args=[self._p, account_id, task.key_id, task.id, stored.raw, task.status, event]
        )
        if reply is None:
            return None
        activities, questions = items(reply)
        return DeletedTask(_tokens(activities), [QuestionId(item) for item in texts(items(questions))])

    async def evict_closed_task(self, account_id: AccountId, key_id: KeyId) -> Evicted | None:
        """Remove the oldest closed task of the key if the key has more closed tasks than the limit."""
        reply = await self._task_evict(args=[self._p, account_id, key_id, self._limits.max_closed_tasks_per_key])
        if reply is None:
            return None
        task_id, questions = items(reply)
        return Evicted(TaskId(text(task_id)), [QuestionId(item) for item in texts(items(questions))])

    async def page_tasks(
        self, scope: TaskScope, statuses: Sequence[Status], count: int, cursor: Cursor | None
    ) -> RawPage:
        reply = items(
            await self._task_page(
                args=[
                    self._index(scope),
                    self.keys.task_prefix,
                    count,
                    _score(cursor.score) if cursor else "",
                    cursor.task_id if cursor else "",
                    *(status for status in STATUSES if status in statuses),
                ]
            )
        )
        last_id = text(reply[2])
        last = Cursor(float(text(reply[1])), TaskId(last_id)) if last_id else None
        return RawPage(docs=texts(reply[3:]), last=last, has_more=integer(reply[0]) == 1)

    async def closed_before(self, cutoff: float, count: int) -> list[TaskId]:
        reply = await self._kv.call("ZRANGEBYSCORE", self.keys.closed, "-inf", _score(cutoff), "LIMIT", 0, count)
        return [TaskId(task_id) for task_id in texts(items(reply))]

    async def get_task_unscoped(self, task_id: TaskId) -> StoredTask | None:
        raw = opt_text(await self._kv.call("GET", self.keys.task(task_id)))
        return _stored(raw) if raw else None

    async def key_account(self, key_id: KeyId) -> AccountId | None:
        account_id = opt_text(await self._kv.call("HGET", self.keys.key(key_id), "account_id"))
        return AccountId(account_id) if account_id else None

    async def forget_closed(self, task_id: TaskId) -> None:
        await self._kv.call("ZREM", self.keys.closed, task_id)

    async def acquire_lock(self, name: str, ttl: int) -> bool:
        return await self._kv.call("SET", self.keys.lock(name), "1", "NX", "EX", ttl) is not None

    # Live Activities

    async def put_activity(
        self, account_id: AccountId, task_id: TaskId, device_id: DeviceId, token: str
    ) -> Literal["ok", "no_task", "no_device"]:
        reply = await self._activity_put(args=[self._p, account_id, task_id, device_id, token])
        return _one_of(reply, "ok", "no_task", "no_device")

    async def delete_activity(self, account_id: AccountId, task_id: TaskId, device_id: DeviceId) -> bool:
        return integer(await self._activity_delete(args=[self._p, account_id, task_id, device_id])) == 1

    async def clear_activities(self, task_id: TaskId) -> None:
        await self._kv.call("DEL", self.keys.activities(task_id))

    async def forget_activity(self, task_id: TaskId, device_id: DeviceId, token: str | None = None) -> None:
        """Remove one registration. With `token`, only if the registration still has that token."""
        key = self.keys.activities(task_id)
        if token is None:
            await self._kv.call("HDEL", key, device_id)
        else:
            await self._hash_clear(keys=[key], args=[device_id, token, "del"])

    # Push

    async def push_context(self, account_id: AccountId, key_id: KeyId, task_id: TaskId | None) -> PushContext:
        commands: list[tuple[Arg, ...]] = [
            ("HGET", self.keys.key(key_id), "label"),
            ("HGET", self.keys.account(account_id), "live_activities"),
            ("SMEMBERS", self.keys.account_devices(account_id)),
        ]
        if task_id:
            commands.append(("HGETALL", self.keys.activities(task_id)))
            commands.append(("ZRANGE", self.keys.task_questions(task_id), 0, 0))
        reply = await self._kv.batch(commands)
        waiting = texts(items(reply[4])) if task_id else []
        question = await self.get_question_unscoped(QuestionId(waiting[0])) if waiting else None
        return PushContext(
            key_label=opt_text(reply[0]) or "",
            live_activities=reply[1] == "1",
            devices=await self._load_devices(account_id, text_set(reply[2])),
            activities=_tokens(reply[3]) if task_id else {},
            question=question.question if question else None,
        )

    async def widget_slot(self, account_id: AccountId, device_id: DeviceId) -> bool:
        """True if a WidgetKit push can go to the device now. False marks the device for one push later."""
        keys = [self.keys.widget_throttle(account_id, device_id), self.keys.widget_dirty(account_id, device_id)]
        return integer(await self._widget_slot(keys=keys, args=[WIDGET_PUSH_INTERVAL_SECONDS])) == 1

    async def renew_widget_slot(self, account_id: AccountId, device_id: DeviceId) -> None:
        await self._kv.call(
            "SET", self.keys.widget_throttle(account_id, device_id), "1", "EX", WIDGET_PUSH_INTERVAL_SECONDS
        )

    async def widget_throttled(self, account_id: AccountId, device_id: DeviceId) -> bool:
        return integer(await self._kv.call("EXISTS", self.keys.widget_throttle(account_id, device_id))) == 1

    async def take_widget_mark(self, account_id: AccountId, device_id: DeviceId) -> bool:
        """True if a change came while the throttle of the device was on."""
        return await self._kv.call("GETDEL", self.keys.widget_dirty(account_id, device_id)) is not None

    async def activity_slot(self, task_id: TaskId) -> bool:
        """True if a Live Activity update can go for the task now. False marks the task for one update later."""
        keys = [self.keys.activity_throttle(task_id), self.keys.activity_dirty(task_id)]
        return integer(await self._widget_slot(keys=keys, args=[LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS])) == 1

    async def renew_activity_slot(self, task_id: TaskId) -> None:
        await self._kv.call(
            "SET", self.keys.activity_throttle(task_id), "1", "EX", LIVE_ACTIVITY_UPDATE_INTERVAL_SECONDS
        )

    async def activity_throttled(self, task_id: TaskId) -> bool:
        return integer(await self._kv.call("EXISTS", self.keys.activity_throttle(task_id))) == 1

    async def take_activity_mark(self, task_id: TaskId) -> bool:
        """True if a change came while the throttle of the task was on."""
        return await self._kv.call("GETDEL", self.keys.activity_dirty(task_id)) is not None

    # Questions

    async def create_question(
        self, account_id: AccountId, question: Question, event: str
    ) -> Literal["ok", "limit", "no_key", "no_task"]:
        reply = await self._question_create(
            args=[
                self._p,
                account_id,
                question.key_id,
                question.id,
                question.model_dump_json(),
                _score(question.created_at),
                _score(question.expires_at),
                self._limits.max_pending_questions_per_key,
                question.task_id or "",
                event,
            ]
        )
        return _one_of(reply, "ok", "limit", "no_key", "no_task")

    async def get_question(self, scope: TaskScope, question_id: QuestionId) -> StoredQuestion | None:
        """Read a question only if it is in the indexes of `scope` (one key or one account)."""
        base = self._question_index(scope)
        commands: list[tuple[Arg, ...]] = [("ZSCORE", base + status, question_id) for status in QUESTION_STATUSES]
        *scores, raw = await self._kv.batch([*commands, ("GET", self.keys.question(question_id))])
        if raw is None or all(score is None for score in scores):
            return None
        return _stored_question(text(raw))

    async def get_question_unscoped(self, question_id: QuestionId) -> StoredQuestion | None:
        raw = opt_text(await self._kv.call("GET", self.keys.question(question_id)))
        return _stored_question(raw) if raw else None

    async def list_questions(self, scope: TaskScope, statuses: Sequence[QuestionStatus], count: int) -> list[str]:
        """The stored JSON of the newest questions with one of the statuses, newest first."""
        wanted = [status for status in QUESTION_STATUSES if status in statuses]
        reply = await self._question_list(args=[self._question_index(scope), self.keys.question_prefix, count, *wanted])
        return texts(items(reply))

    async def close_question(
        self, account_id: AccountId, old: StoredQuestion, new: Question, closed_at: float, event: str
    ) -> Literal["ok", "conflict"]:
        reply = await self._question_close(
            args=[
                self._p,
                account_id,
                new.key_id,
                new.id,
                old.raw,
                new.model_dump_json(),
                new.status,
                _score(new.created_at),
                _score(closed_at),
                new.task_id or "",
                event,
            ]
        )
        return _one_of(reply, "ok", "conflict")

    async def purge_question(self, account_id: AccountId, question: Question) -> None:
        await self._question_purge(args=[self._p, account_id, question.key_id, question.id, question.status])

    async def questions_due(self, now: float, count: int) -> list[QuestionId]:
        """Pending questions with an `expires_at` that is not after `now`."""
        reply = await self._kv.call("ZRANGEBYSCORE", self.keys.questions_due, "-inf", _score(now), "LIMIT", 0, count)
        return [QuestionId(item) for item in texts(items(reply))]

    async def questions_closed_before(self, cutoff: float, count: int) -> list[QuestionId]:
        reply = await self._kv.call(
            "ZRANGEBYSCORE", self.keys.questions_closed, "-inf", _score(cutoff), "LIMIT", 0, count
        )
        return [QuestionId(item) for item in texts(items(reply))]

    async def forget_question(self, question_id: QuestionId, index: Literal["due", "closed"]) -> None:
        """Remove an ID that has no pending or closed question from one of the expiry indexes."""
        key = self.keys.questions_due if index == "due" else self.keys.questions_closed
        await self._kv.call("ZREM", key, question_id)

    # Inbox

    def _inbox_cutoffs(self, now: float) -> tuple[str, str]:
        """The scores below which an unread item, and a read item, is past its lifetime."""
        limits = self._limits
        return _score(now - limits.unread_inbox_ttl_seconds), _score(now - limits.read_inbox_ttl_seconds)

    async def add_inbox_item(self, account_id: AccountId, key_id: KeyId, item: InboxItem, now: float) -> int | None:
        """Put an item in the inbox of a key. Returns the unread count, or None if the account has no such key."""
        reply = await self._inbox_add(
            args=[
                self._p,
                account_id,
                key_id,
                item.id,
                item.model_dump_json(exclude={"read_at"}),
                _score(item.created_at),
                self._limits.max_unread_inbox_items,
                *self._inbox_cutoffs(now),
            ]
        )
        unread = integer(reply)
        return None if unread < 0 else unread

    async def take_inbox(self, key_id: KeyId, count: int, *, mark_read: bool, now: float) -> tuple[list[str], int]:
        """The JSON of the oldest unread items, and the unread count after the call."""
        reply = items(
            await self._inbox_take(
                args=[self._p, key_id, count, "1" if mark_read else "0", _score(now), *self._inbox_cutoffs(now)]
            )
        )
        return texts(reply[1:]), integer(reply[0])

    async def view_inbox(self, key_id: KeyId, count: int, now: float) -> tuple[list[tuple[str, float | None]], int]:
        """The newest items as (JSON, read time), newest first, and the unread count."""
        reply = items(await self._inbox_view(args=[self._p, key_id, count, *self._inbox_cutoffs(now)]))
        flat = texts(reply[1:])
        rows = [(doc, float(read_at) if read_at else None) for doc, read_at in zip(flat[::2], flat[1::2], strict=True)]
        return rows, integer(reply[0])

    def _question_index(self, scope: TaskScope) -> str:
        match scope:
            case KeyTasks(key_id):
                return self.keys.key_questions(key_id)
            case AccountTasks(account_id):
                return self.keys.account_questions(account_id)

    def _index(self, scope: TaskScope) -> str:
        match scope:
            case KeyTasks(key_id):
                return self.keys.key_tasks(key_id)
            case AccountTasks(account_id):
                return self.keys.account_tasks(account_id)


def _stored_question(raw: str) -> StoredQuestion:
    return StoredQuestion(raw, Question.model_validate_json(raw))


def _stored(raw: str) -> StoredTask:
    return StoredTask(raw, Task.model_validate_json(raw))


def _account(fields: dict[str, str]) -> Account:
    return Account(
        id=AccountId(fields["id"]),
        created_at=float(fields["created_at"]),
        settings=AccountSettings(live_activities=fields.get("live_activities", "1") == "1"),
    )


def _device(device_id: DeviceId, fields: dict[str, str]) -> Device:
    return Device.model_validate(
        {
            **fields,
            "id": device_id,
            "apns_token": fields.get("apns_token") or None,
            "push_to_start_token": fields.get("push_to_start_token") or None,
            "widget_push_token": fields.get("widget_push_token") or None,
            "updated_at": float(fields["updated_at"]),
        }
    )


def _count(fields: dict[str, str], status: Status) -> int:
    return max(0, int(fields.get(f"n:{status}", 0)))


def _key(key_id: KeyId, fields: dict[str, str]) -> Key:
    client = None
    if fields.get("client_name"):
        client = ClientInfo(name=fields["client_name"], version=fields.get("client_version", ""))
    last_used = fields.get("last_used_at")
    return Key(
        id=key_id,
        label=fields["label"],
        prefix=fields["prefix"],
        last4=fields["last4"],
        created_at=float(fields["created_at"]),
        last_used_at=float(last_used) if last_used else None,
        last_client=client,
        counts=KeyCounts(
            todo=_count(fields, "todo"),
            in_progress=_count(fields, "in_progress"),
            done=_count(fields, "done"),
            cancelled=_count(fields, "cancelled"),
        ),
    )
