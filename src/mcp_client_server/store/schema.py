"""Key names. The Lua scripts in scripts.py build the same names from the prefix.

{p}:cred:{sha256}            string  "a:{account}" or "k:{account}:{key}"
{p}:acc:{account}            hash    id, created_at, live_activities, cred,
                                     attest_key_id, attest_public_key, attest_counter
{p}:acc:{account}:keys       zset    key id by created_at
{p}:acc:{account}:devices    set     device id
{p}:acc:{account}:tasks      zset    task id by updated_at
{p}:acc:{account}:tasks:{s}  zset    the same, one for each status
{p}:key:{key}                hash    account_id, label, prefix, last4, created_at, cred,
                                     last_used_at, client_name, client_version, n:{status};
                                     a deleted key has gc_account in place of account_id and cred
{p}:gc:keys                  zset    deleted key id by the time of the deletion, until its data is gone
{p}:key:{key}:tasks          zset    task id by updated_at
{p}:key:{key}:tasks:{s}      zset    the same, one for each status
{p}:tsk:{task}               string  task JSON, the same shape that the API returns
{p}:la:{task}                hash    device id -> Live Activity update token
{p}:dev:{account}:{device}   hash    device fields
{p}:qst:{question}           string  question JSON, the same shape that the API returns
{p}:key:{key}:qst:{s}        zset    question id by created_at, one for each question status
{p}:acc:{account}:qst:{s}    zset    the same for the account
{p}:tq:{task}                zset    pending question id of the task by created_at
{p}:qexp                     zset    pending question id by expires_at
{p}:qclosed                  zset    closed question id by the time it closed, for expiry
{p}:inb:{item}               string  inbox item JSON, with no read time
{p}:key:{key}:inbox          zset    inbox item id by created_at
{p}:key:{key}:inbox:unread   zset    unread item id by created_at
{p}:key:{key}:inbox:read     zset    read item id by the time it was read
{p}:wdg:{account}:{device}   string  WidgetKit push throttle
{p}:wdgd:{account}:{device}  string  a change came while the throttle was on
{p}:lat:{task}               string  Live Activity update throttle
{p}:latd:{task}              string  a change came while that throttle was on
{p}:attc:{challenge}         string  App Attest challenge that is not used yet
{p}:attk:{key id}            string  account of an attested key
{p}:tok:{apns token}         string  "{account}:{device}" that owns the token
{p}:closed                   zset    closed task id by updated_at, for expiry
{p}:rl:{bucket}              string  rate limit counter
{p}:touch:{key}              string  last_used_at write throttle
{p}:lock:{name}              string  maintenance lock
{p}:ev:{account}             pub/sub channel, one SSE frame for each message
"""

from mcp_client_server.credentials import CredentialHash
from mcp_client_server.ids import AccountId, DeviceId, KeyId, QuestionId, TaskId


class KeySpace:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def credential(self, cred_hash: CredentialHash) -> str:
        return f"{self.prefix}:cred:{cred_hash}"

    def account(self, account_id: AccountId) -> str:
        return f"{self.prefix}:acc:{account_id}"

    def account_keys(self, account_id: AccountId) -> str:
        return f"{self.prefix}:acc:{account_id}:keys"

    def account_devices(self, account_id: AccountId) -> str:
        return f"{self.prefix}:acc:{account_id}:devices"

    def account_tasks(self, account_id: AccountId) -> str:
        return f"{self.prefix}:acc:{account_id}:tasks"

    def key(self, key_id: KeyId) -> str:
        return f"{self.prefix}:key:{key_id}"

    def key_tasks(self, key_id: KeyId) -> str:
        return f"{self.prefix}:key:{key_id}:tasks"

    def task(self, task_id: TaskId) -> str:
        return f"{self.prefix}:tsk:{task_id}"

    @property
    def task_prefix(self) -> str:
        return f"{self.prefix}:tsk:"

    def activities(self, task_id: TaskId) -> str:
        return f"{self.prefix}:la:{task_id}"

    def device(self, account_id: AccountId, device_id: DeviceId) -> str:
        return f"{self.prefix}:dev:{account_id}:{device_id}"

    def question(self, question_id: QuestionId) -> str:
        return f"{self.prefix}:qst:{question_id}"

    @property
    def question_prefix(self) -> str:
        return f"{self.prefix}:qst:"

    def key_questions(self, key_id: KeyId) -> str:
        """The prefix of the question indexes of a key. A status name follows."""
        return f"{self.prefix}:key:{key_id}:qst:"

    def account_questions(self, account_id: AccountId) -> str:
        return f"{self.prefix}:acc:{account_id}:qst:"

    def task_questions(self, task_id: TaskId) -> str:
        return f"{self.prefix}:tq:{task_id}"

    @property
    def questions_due(self) -> str:
        return f"{self.prefix}:qexp"

    @property
    def questions_closed(self) -> str:
        return f"{self.prefix}:qclosed"

    def widget_throttle(self, account_id: AccountId, device_id: DeviceId) -> str:
        return f"{self.prefix}:wdg:{account_id}:{device_id}"

    def widget_dirty(self, account_id: AccountId, device_id: DeviceId) -> str:
        return f"{self.prefix}:wdgd:{account_id}:{device_id}"

    def activity_throttle(self, task_id: TaskId) -> str:
        return f"{self.prefix}:lat:{task_id}"

    def activity_dirty(self, task_id: TaskId) -> str:
        return f"{self.prefix}:latd:{task_id}"

    def attest_challenge(self, challenge: str) -> str:
        return f"{self.prefix}:attc:{challenge}"

    def token(self, apns_token: str) -> str:
        return f"{self.prefix}:tok:{apns_token}"

    @property
    def closed(self) -> str:
        return f"{self.prefix}:closed"

    def rate(self, bucket: str) -> str:
        return f"{self.prefix}:rl:{bucket}"

    @property
    def keys_to_purge(self) -> str:
        return f"{self.prefix}:gc:keys"

    def lock(self, name: str) -> str:
        return f"{self.prefix}:lock:{name}"

    def channel(self, account_id: AccountId) -> str:
        return f"{self.prefix}:ev:{account_id}"

    @property
    def idle_channel(self) -> str:
        """A channel with no messages. The event subscriber holds it so that its connection is always in use."""
        return f"{self.prefix}:ev:_"
