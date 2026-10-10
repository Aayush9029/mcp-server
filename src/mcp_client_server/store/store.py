from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from mcp_client_server import limits
from mcp_client_server.models import (
    STATUSES,
    Account,
    AccountSettings,
    ClientInfo,
    Device,
    Key,
    KeyCounts,
    Status,
    Task,
)
from mcp_client_server.store import scripts
from mcp_client_server.store.schema import KeySpace

AuthStatus = Literal["ok", "unknown", "limited"]
WriteStatus = Literal["ok", "conflict", "limit", "no_key", "no_account", "no_task", "no_device"]


@dataclass(frozen=True, slots=True)
class AuthResult:
    status: AuthStatus
    account_id: str = ""
    key_id: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTask:
    raw: str
    task: Task


@dataclass(frozen=True, slots=True)
class Cursor:
    score: float
    task_id: str


@dataclass(frozen=True, slots=True)
class RawPage:
    docs: list[str]
    last: Cursor | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class EndedActivity:
    task: Task
    tokens: dict[str, str]


@dataclass(frozen=True, slots=True)
class PushContext:
    key_label: str
    live_activities: bool
    devices: list[Device]
    activities: dict[str, str]


def _pairs(flat: list[str]) -> dict[str, str]:
    return dict(zip(flat[::2], flat[1::2], strict=True))


def _score(value: float) -> str:
    return repr(value)


class Store:
    def __init__(self, redis: Redis, prefix: str) -> None:
        self._r = redis
        self.keys = KeySpace(prefix)
        self._p = prefix
        reg = redis.register_script
        self._auth = reg(scripts.AUTH)
        self._rate_hit = reg(scripts.RATE_HIT)
        self._hash_update = reg(scripts.HASH_UPDATE)
        self._hash_clear = reg(scripts.HASH_CLEAR)
        self._device_put = reg(scripts.DEVICE_PUT)
        self._device_delete = reg(scripts.DEVICE_DELETE)
        self._key_create = reg(scripts.KEY_CREATE)
        self._key_delete = reg(scripts.KEY_DELETE)
        self._account_delete = reg(scripts.ACCOUNT_DELETE)
        self._task_create = reg(scripts.TASK_CREATE)
        self._task_update = reg(scripts.TASK_UPDATE)
        self._task_delete = reg(scripts.TASK_DELETE)
        self._task_page = reg(scripts.TASK_PAGE)
        self._activity_put = reg(scripts.ACTIVITY_PUT)
        self._activity_delete = reg(scripts.ACTIVITY_DELETE)

    async def _call(self, *args: Any) -> Any:
        # redis-py gives no type for the reply of a raw command.
        return await self._r.execute_command(*args)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    async def ping(self) -> bool:
        return bool(await self._call("PING"))

    async def publish(self, account_id: str, frame: str) -> None:
        await self._call("PUBLISH", self.keys.channel(account_id), frame)

    # Credentials and rate limits

    async def authenticate(
        self, cred_hash: str, *, now: float, rate_limit: int, rate_bucket: str, rate_ttl: int
    ) -> AuthResult:
        reply = await self._auth(
            args=[
                self._p,
                cred_hash,
                _score(now),
                rate_limit,
                rate_bucket,
                rate_ttl,
                limits.LAST_USED_WRITE_INTERVAL_SECONDS,
            ]
        )
        code = int(reply[0])
        if code == 0:
            return AuthResult("unknown")
        kind, _, rest = str(reply[1]).partition(":")
        if kind == "a":
            account_id, key_id = rest, None
        else:
            account_id, _, key_id = rest.partition(":")
        return AuthResult("ok" if code == 1 else "limited", account_id, key_id)

    async def hit(self, bucket: str, ttl: int) -> int:
        return int(await self._rate_hit(keys=[self.keys.rate(bucket)], args=[ttl]))

    # Accounts

    async def create_account(self, account: Account, cred_hash: str) -> None:
        pipe = self._r.pipeline(transaction=True)
        pipe.hset(
            self.keys.account(account.id),
            mapping={
                "id": account.id,
                "created_at": _score(account.created_at),
                "live_activities": "1" if account.settings.live_activities else "0",
                "cred": cred_hash,
            },
        )
        pipe.set(self.keys.credential(cred_hash), f"a:{account.id}")
        await pipe.execute()

    async def get_account(self, account_id: str) -> Account | None:
        fields: dict[str, str] = await self._call("HGETALL", self.keys.account(account_id))
        return _account(fields) if fields else None

    async def set_live_activities(self, account_id: str, enabled: bool) -> Account | None:
        reply = await self._hash_update(
            keys=[self.keys.account(account_id)],
            args=["id", account_id, "live_activities", "1" if enabled else "0"],
        )
        return _account(_pairs(reply)) if reply else None

    async def delete_account(self, account_id: str, event: str) -> bool:
        return bool(await self._account_delete(args=[self._p, account_id, event]))

    # Devices

    async def put_device(self, account_id: str, device: Device) -> WriteStatus:
        fields = {
            "platform": device.platform,
            "model": device.model,
            "os_version": device.os_version,
            "app_version": device.app_version,
            "apns_environment": device.apns_environment,
            "apns_token": device.apns_token or "",
            "push_to_start_token": device.push_to_start_token or "",
            "updated_at": _score(device.updated_at),
        }
        flat = [item for pair in fields.items() for item in pair]
        return await self._device_put(args=[self._p, account_id, device.id, limits.MAX_DEVICES_PER_ACCOUNT, *flat])

    async def delete_device(self, account_id: str, device_id: str) -> bool:
        return bool(await self._device_delete(args=[self._p, account_id, device_id]))

    async def list_devices(self, account_id: str) -> list[Device]:
        ids: set[str] = await self._call("SMEMBERS", self.keys.account_devices(account_id))
        return await self._load_devices(account_id, sorted(ids))

    async def _load_devices(self, account_id: str, device_ids: list[str]) -> list[Device]:
        if not device_ids:
            return []
        pipe = self._r.pipeline(transaction=False)
        for device_id in device_ids:
            pipe.hgetall(self.keys.device(account_id, device_id))
        rows = await pipe.execute()
        return [_device(device_id, row) for device_id, row in zip(device_ids, rows, strict=True) if row]

    async def clear_device_token(
        self, account_id: str, device_id: str, field: Literal["apns_token", "push_to_start_token"], token: str
    ) -> None:
        keys = [self.keys.device(account_id, device_id)]
        if field == "apns_token":
            keys.append(self.keys.token(token))
        await self._hash_clear(keys=keys, args=[field, token, "empty"])

    # Keys

    async def create_key(self, account_id: str, key: Key, cred_hash: str, event: str) -> WriteStatus:
        return await self._key_create(
            args=[
                self._p,
                account_id,
                key.id,
                cred_hash,
                limits.MAX_KEYS_PER_ACCOUNT,
                _score(key.created_at),
                key.label,
                key.prefix,
                key.last4,
                event,
            ]
        )

    async def get_key(self, account_id: str, key_id: str) -> Key | None:
        fields: dict[str, str] = await self._call("HGETALL", self.keys.key(key_id))
        if not fields or fields.get("account_id") != account_id:
            return None
        return _key(key_id, fields)

    async def list_keys(self, account_id: str) -> list[Key]:
        ids: list[str] = await self._call("ZREVRANGE", self.keys.account_keys(account_id), 0, -1)
        if not ids:
            return []
        pipe = self._r.pipeline(transaction=False)
        for key_id in ids:
            pipe.hgetall(self.keys.key(key_id))
        rows = await pipe.execute()
        return [_key(key_id, row) for key_id, row in zip(ids, rows, strict=True) if row]

    async def set_key_label(self, account_id: str, key_id: str, label: str) -> Key | None:
        reply = await self._hash_update(keys=[self.keys.key(key_id)], args=["account_id", account_id, "label", label])
        return _key(key_id, _pairs(reply)) if reply else None

    async def set_key_client(self, account_id: str, key_id: str, client: ClientInfo) -> Key | None:
        reply = await self._hash_update(
            keys=[self.keys.key(key_id)],
            args=["account_id", account_id, "client_name", client.name, "client_version", client.version],
        )
        return _key(key_id, _pairs(reply)) if reply else None

    async def delete_key(self, account_id: str, key_id: str, event: str) -> list[EndedActivity] | None:
        reply = await self._key_delete(args=[self._p, account_id, key_id, event])
        if reply is None:
            return None
        return [EndedActivity(Task.model_validate_json(raw), _pairs(pairs)) for raw, pairs in reply if raw]

    # Tasks

    async def create_task(self, account_id: str, task: Task, event: str) -> WriteStatus:
        return await self._task_create(
            args=[
                self._p,
                account_id,
                task.key_id,
                task.id,
                task.model_dump_json(),
                task.status,
                _score(task.updated_at),
                limits.MAX_OPEN_TASKS_PER_KEY,
                event,
            ]
        )

    async def get_task(self, index: str, task_id: str) -> StoredTask | None:
        """Read a task only if it is a member of `index` (the tasks of one key or of one account)."""
        pipe = self._r.pipeline(transaction=False)
        pipe.zscore(index, task_id)
        pipe.get(self.keys.task(task_id))
        score, raw = await pipe.execute()
        if score is None or raw is None:
            return None
        return StoredTask(raw, Task.model_validate_json(raw))

    async def update_task(self, account_id: str, old: StoredTask, new: Task, event: str) -> WriteStatus:
        return await self._task_update(
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
                limits.MAX_OPEN_TASKS_PER_KEY,
                event,
            ]
        )

    async def delete_task(self, account_id: str, stored: StoredTask, event: str) -> dict[str, str] | None:
        """Delete a task. Returns its Live Activity tokens, or None on a concurrent write."""
        task = stored.task
        reply = await self._task_delete(
            args=[self._p, account_id, task.key_id, task.id, stored.raw, task.status, event]
        )
        return None if reply is None else _pairs(reply)

    async def page_tasks(self, index: str, statuses: list[Status], count: int, cursor: Cursor | None) -> RawPage:
        ordered = [s for s in STATUSES if s in statuses]
        reply = await self._task_page(
            args=[
                index,
                self.keys.task_prefix,
                count,
                _score(cursor.score) if cursor else "",
                cursor.task_id if cursor else "",
                *ordered,
            ]
        )
        last = Cursor(float(reply[1]), reply[2]) if reply[2] else None
        return RawPage(docs=reply[3:], last=last, has_more=bool(reply[0]))

    async def closed_before(self, cutoff: float, count: int) -> list[str]:
        return await self._call("ZRANGEBYSCORE", self.keys.closed, "-inf", _score(cutoff), "LIMIT", 0, count)

    async def get_task_unscoped(self, task_id: str) -> StoredTask | None:
        raw: str | None = await self._call("GET", self.keys.task(task_id))
        return StoredTask(raw, Task.model_validate_json(raw)) if raw else None

    async def key_account(self, key_id: str) -> str | None:
        return await self._call("HGET", self.keys.key(key_id), "account_id")

    async def forget_closed(self, task_id: str) -> None:
        await self._call("ZREM", self.keys.closed, task_id)

    async def acquire_lock(self, name: str, ttl: int) -> bool:
        return bool(await self._call("SET", self.keys.lock(name), "1", "NX", "EX", ttl))

    # Live Activities

    async def put_activity(self, account_id: str, task_id: str, device_id: str, token: str) -> WriteStatus:
        return await self._activity_put(args=[self._p, account_id, task_id, device_id, token])

    async def delete_activity(self, account_id: str, task_id: str, device_id: str) -> bool:
        return bool(await self._activity_delete(args=[self._p, account_id, task_id, device_id]))

    async def clear_activities(self, task_id: str) -> None:
        await self._call("DEL", self.keys.activities(task_id))

    async def forget_activity(self, task_id: str, device_id: str, token: str | None = None) -> None:
        key = self.keys.activities(task_id)
        if token is None:
            await self._call("HDEL", key, device_id)
        else:
            await self._hash_clear(keys=[key], args=[device_id, token, "del"])

    # Push

    async def push_context(self, account_id: str, key_id: str, task_id: str | None) -> PushContext:
        pipe = self._r.pipeline(transaction=False)
        pipe.hget(self.keys.key(key_id), "label")
        pipe.hget(self.keys.account(account_id), "live_activities")
        pipe.smembers(self.keys.account_devices(account_id))
        if task_id:
            pipe.hgetall(self.keys.activities(task_id))
        reply: list[Any] = await pipe.execute()
        devices = await self._load_devices(account_id, sorted(reply[2]))
        return PushContext(
            key_label=reply[0] or "",
            live_activities=reply[1] == "1",
            devices=devices,
            activities=reply[3] if task_id else {},
        )


def _account(fields: dict[str, str]) -> Account:
    return Account(
        id=fields["id"],
        created_at=float(fields["created_at"]),
        settings=AccountSettings(live_activities=fields.get("live_activities", "1") == "1"),
    )


def _device(device_id: str, fields: dict[str, str]) -> Device:
    return Device.model_validate(
        {
            **fields,
            "id": device_id,
            "apns_token": fields.get("apns_token") or None,
            "push_to_start_token": fields.get("push_to_start_token") or None,
            "updated_at": float(fields["updated_at"]),
        }
    )


def _key(key_id: str, fields: dict[str, str]) -> Key:
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
        counts=KeyCounts(**{status: max(0, int(fields.get(f"n:{status}", 0))) for status in STATUSES}),
    )
