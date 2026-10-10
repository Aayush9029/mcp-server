"""Key names. The Lua scripts in scripts.py build the same names from the prefix.

{p}:cred:{sha256}            string  "a:{account}" or "k:{account}:{key}"
{p}:acc:{account}            hash    id, created_at, live_activities, cred
{p}:acc:{account}:keys       zset    key id by created_at
{p}:acc:{account}:devices    set     device id
{p}:acc:{account}:tasks      zset    task id by updated_at
{p}:acc:{account}:tasks:{s}  zset    the same, one for each status
{p}:key:{key}                hash    account_id, label, prefix, last4, created_at, cred,
                                     last_used_at, client_name, client_version, n:{status}
{p}:key:{key}:tasks          zset    task id by updated_at
{p}:key:{key}:tasks:{s}      zset    the same, one for each status
{p}:tsk:{task}               string  task JSON, the same shape that the API returns
{p}:la:{task}                hash    device id -> Live Activity update token
{p}:dev:{account}:{device}   hash    device fields
{p}:tok:{apns token}         string  "{account}:{device}" that owns the token
{p}:closed                   zset    closed task id by updated_at, for expiry
{p}:rl:{bucket}              string  rate limit counter
{p}:touch:{key}              string  last_used_at write throttle
{p}:lock:{name}              string  maintenance lock
{p}:ev:{account}             pub/sub channel, one SSE frame for each message
"""


class KeySpace:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def credential(self, cred_hash: str) -> str:
        return f"{self.prefix}:cred:{cred_hash}"

    def account(self, account_id: str) -> str:
        return f"{self.prefix}:acc:{account_id}"

    def account_keys(self, account_id: str) -> str:
        return f"{self.prefix}:acc:{account_id}:keys"

    def account_devices(self, account_id: str) -> str:
        return f"{self.prefix}:acc:{account_id}:devices"

    def account_tasks(self, account_id: str) -> str:
        return f"{self.prefix}:acc:{account_id}:tasks"

    def key(self, key_id: str) -> str:
        return f"{self.prefix}:key:{key_id}"

    def key_tasks(self, key_id: str) -> str:
        return f"{self.prefix}:key:{key_id}:tasks"

    def task(self, task_id: str) -> str:
        return f"{self.prefix}:tsk:{task_id}"

    @property
    def task_prefix(self) -> str:
        return f"{self.prefix}:tsk:"

    def activities(self, task_id: str) -> str:
        return f"{self.prefix}:la:{task_id}"

    def device(self, account_id: str, device_id: str) -> str:
        return f"{self.prefix}:dev:{account_id}:{device_id}"

    def token(self, apns_token: str) -> str:
        return f"{self.prefix}:tok:{apns_token}"

    @property
    def closed(self) -> str:
        return f"{self.prefix}:closed"

    def rate(self, bucket: str) -> str:
        return f"{self.prefix}:rl:{bucket}"

    def lock(self, name: str) -> str:
        return f"{self.prefix}:lock:{name}"

    def channel(self, account_id: str) -> str:
        return f"{self.prefix}:ev:{account_id}"

    @property
    def channel_prefix(self) -> str:
        return f"{self.prefix}:ev:"
