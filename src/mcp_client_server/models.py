from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
)

from mcp_client_server import limits

Status = Literal["todo", "in_progress", "done", "cancelled"]
Priority = Literal["low", "medium", "high", "urgent"]
Source = Literal["agent", "app"]
Platform = Literal["ios", "ipados", "watchos"]
ApnsEnvironment = Literal["production", "sandbox"]

STATUSES: tuple[Status, ...] = get_args(Status)
PRIORITIES: tuple[Priority, ...] = get_args(Priority)
OPEN_STATUSES: frozenset[Status] = frozenset({"todo", "in_progress"})
CLOSED_STATUSES: frozenset[Status] = frozenset({"done", "cancelled"})


def _one_line(value: str) -> str:
    text = " ".join(value.split())
    if not text:
        raise ValueError("Must not be empty.")
    return text


def _collapse(value: str) -> str:
    return " ".join(value.split())


Title = Annotated[str, StringConstraints(min_length=1, max_length=limits.TITLE_MAX), AfterValidator(_one_line)]
Description = Annotated[str, StringConstraints(max_length=limits.DESCRIPTION_MAX)]
StatusNote = Annotated[str, StringConstraints(max_length=limits.STATUS_NOTE_MAX), AfterValidator(_collapse)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=limits.LABEL_MAX), AfterValidator(_one_line)]
Progress = Annotated[StrictInt, Field(ge=0, le=100)]
NotifyTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=limits.NOTIFY_TITLE_MAX), AfterValidator(_one_line)
]
NotifyBody = Annotated[str, StringConstraints(min_length=1, max_length=limits.NOTIFY_BODY_MAX)]

DeviceId = Annotated[
    str, StringConstraints(pattern=r"^dev_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
HexToken = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-fA-F]+$", min_length=8, max_length=512), AfterValidator(str.lower)
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class Task(BaseModel):
    id: str
    key_id: str
    title: str
    description: str
    status: Status
    priority: Priority
    notify: bool
    progress: int | None
    status_note: str | None
    source: Source
    created_at: float
    updated_at: float
    completed_at: float | None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class ClientInfo(BaseModel):
    name: str
    version: str


class KeyCounts(BaseModel):
    todo: int = 0
    in_progress: int = 0
    done: int = 0
    cancelled: int = 0


class Key(BaseModel):
    id: str
    label: str
    prefix: str
    last4: str
    created_at: float
    last_used_at: float | None = None
    last_client: ClientInfo | None = None
    counts: KeyCounts = Field(default_factory=KeyCounts)


class AccountSettings(BaseModel):
    live_activities: bool = True


class Account(BaseModel):
    id: str
    created_at: float
    settings: AccountSettings = Field(default_factory=AccountSettings)


class Device(BaseModel):
    id: str
    platform: Platform
    model: str
    os_version: str
    app_version: str
    apns_environment: ApnsEnvironment
    apns_token: str | None
    push_to_start_token: str | None
    updated_at: float


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyBody(_Request):
    pass


class AccountSettingsPatch(_Request):
    live_activities: StrictBool


class AccountPatch(_Request):
    settings: AccountSettingsPatch


class DeviceInput(_Request):
    platform: Platform
    model: ShortText
    os_version: ShortText
    app_version: ShortText
    apns_environment: ApnsEnvironment
    apns_token: HexToken | None = None
    push_to_start_token: HexToken | None = None


class KeyCreate(_Request):
    label: Label


class KeyPatch(_Request):
    label: Label


class TaskCreate(_Request):
    key_id: str | None = None
    title: Title
    description: Description = ""
    status: Status = "todo"
    priority: Priority = "medium"
    notify: StrictBool = True


class TaskPatch(_Request):
    title: Title | None = None
    description: Description | None = None
    status: Status | None = None
    priority: Priority | None = None
    notify: StrictBool | None = None
    progress: Progress | None = None
    status_note: StatusNote | None = None

    @field_validator("title", "description", "status", "priority", "notify", mode="before")
    @classmethod
    def _not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Must not be null.")
        return value


class NotifyInput(_Request):
    title: NotifyTitle
    body: NotifyBody
    task_id: str | None = None


class LiveActivityInput(_Request):
    device_id: DeviceId
    push_token: HexToken


class LiveActivityRemoval(_Request):
    device_id: DeviceId


class AccountCreated(BaseModel):
    account: Account
    token: str


class KeyCreated(BaseModel):
    key: Key
    secret: str


class KeyList(BaseModel):
    keys: list[Key]


class NotifyResult(BaseModel):
    sent: bool
    devices: int
