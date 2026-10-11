import secrets
import time
from typing import NewType, Protocol

AccountId = NewType("AccountId", str)
KeyId = NewType("KeyId", str)
TaskId = NewType("TaskId", str)
DeviceId = NewType("DeviceId", str)
QuestionId = NewType("QuestionId", str)
InboxItemId = NewType("InboxItemId", str)

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


class IdSource(Protocol):
    def account_id(self) -> AccountId: ...

    def key_id(self) -> KeyId: ...

    def task_id(self) -> TaskId: ...

    def question_id(self) -> QuestionId: ...

    def inbox_id(self) -> InboxItemId: ...


def ulid() -> str:
    # Lowercase ULID: the time comes first, so IDs made later sort later.
    value = ((time.time_ns() // 1_000_000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(value >> shift) & 31] for shift in range(125, -1, -5))


class UlidSource:
    def account_id(self) -> AccountId:
        return AccountId(f"acc_{ulid()}")

    def key_id(self) -> KeyId:
        return KeyId(f"key_{ulid()}")

    def task_id(self) -> TaskId:
        return TaskId(f"tsk_{ulid()}")

    def question_id(self) -> QuestionId:
        return QuestionId(f"qst_{ulid()}")

    def inbox_id(self) -> InboxItemId:
        return InboxItemId(f"inb_{ulid()}")
