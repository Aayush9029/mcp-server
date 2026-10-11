"""The contract objects of docs/api.md. REST and MCP share these definitions."""

import base64
import binascii
from typing import Annotated, Literal, get_args

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from mcp_client_server import limits
from mcp_client_server.credentials import Secret
from mcp_client_server.errors import ErrorCode
from mcp_client_server.ids import AccountId, DeviceId, InboxItemId, KeyId, QuestionId, TaskId

Status = Literal["todo", "in_progress", "done", "cancelled"]
Priority = Literal["low", "medium", "high", "urgent"]
Source = Literal["agent", "app"]
Platform = Literal["ios", "ipados", "watchos"]
ApnsEnvironment = Literal["production", "sandbox"]
PatchField = Literal["title", "description", "status", "priority", "notify", "progress", "status_note"]
QuestionKind = Literal["confirm", "choice", "text"]
QuestionStatus = Literal["pending", "answered", "expired", "cancelled"]
InboxKind = Literal["task_assigned", "task_changed", "task_deleted", "answer", "message"]

STATUSES: tuple[Status, ...] = get_args(Status)
PRIORITIES: tuple[Priority, ...] = get_args(Priority)
OPEN_STATUSES: tuple[Status, ...] = ("todo", "in_progress")
CLOSED_STATUSES: tuple[Status, ...] = ("done", "cancelled")
QUESTION_STATUSES: tuple[QuestionStatus, ...] = get_args(QuestionStatus)
CONFIRM_OPTIONS = ("Yes", "No")


def _collapse(value: str) -> str:
    return " ".join(value.split())


def _one_line(value: str) -> str:
    text = _collapse(value)
    if not text:
        raise ValueError("Must not be empty.")
    return text


def _from_base64(value: object) -> bytes:
    if not isinstance(value, str):
        raise ValueError("Must be base64 text.")  # noqa: TRY004  pydantic reports a ValueError as a field problem
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error as exc:
        raise ValueError("Must be base64 text.") from exc


def split_commas(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


Title = Annotated[str, StringConstraints(min_length=1, max_length=limits.TITLE_MAX), AfterValidator(_one_line)]
Description = Annotated[str, StringConstraints(max_length=limits.DESCRIPTION_MAX)]
StatusNote = Annotated[str, StringConstraints(max_length=limits.STATUS_NOTE_MAX), AfterValidator(_collapse)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=limits.LABEL_MAX), AfterValidator(_one_line)]
Progress = Annotated[int, Field(ge=0, le=100)]
NotifyTitle = Annotated[
    str, StringConstraints(min_length=1, max_length=limits.NOTIFY_TITLE_MAX), AfterValidator(_one_line)
]
NotifyBody = Annotated[str, StringConstraints(min_length=1, max_length=limits.NOTIFY_BODY_MAX)]
PageLimit = Annotated[int, Field(ge=1, le=limits.LIST_LIMIT_MAX)]
Prompt = Annotated[str, StringConstraints(min_length=1, max_length=limits.PROMPT_MAX), AfterValidator(_one_line)]
Option = Annotated[str, StringConstraints(min_length=1, max_length=limits.OPTION_MAX), AfterValidator(_one_line)]
Answer = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=limits.ANSWER_MAX)]
ExpiresIn = Annotated[int, Field(ge=limits.EXPIRES_IN_MIN, le=limits.EXPIRES_IN_MAX)]
WaitSeconds = Annotated[int, Field(ge=0, le=limits.WAIT_MAX_SECONDS)]
MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=limits.MESSAGE_MAX)]
InboxLimit = Annotated[int, Field(ge=1, le=limits.INBOX_LIMIT_MAX)]

ClientDeviceId = Annotated[
    DeviceId, StringConstraints(pattern=r"^dev_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
]
HexToken = Annotated[
    str, StringConstraints(pattern=r"^[0-9a-fA-F]+$", min_length=8, max_length=512), AfterValidator(str.lower)
]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class _Object(BaseModel):
    model_config = ConfigDict(frozen=True)


class Task(_Object):
    id: TaskId
    key_id: KeyId
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
    # When the agent of the key first got the task. A task from before this field has no value.
    seen_at: float | None = None

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES


class ClientInfo(_Object):
    name: str
    version: str


class KeyCounts(_Object):
    todo: int = 0
    in_progress: int = 0
    done: int = 0
    cancelled: int = 0


class Key(_Object):
    id: KeyId
    label: str
    prefix: str
    last4: str
    created_at: float
    last_used_at: float | None = None
    last_client: ClientInfo | None = None
    counts: KeyCounts = Field(default_factory=KeyCounts)


class AccountSettings(_Object):
    live_activities: bool = True


class Account(_Object):
    id: AccountId
    created_at: float
    settings: AccountSettings = Field(default_factory=AccountSettings)


class Device(_Object):
    id: DeviceId
    platform: Platform
    model: str
    os_version: str
    app_version: str
    apns_environment: ApnsEnvironment
    apns_token: str | None
    push_to_start_token: str | None
    widget_push_token: str | None = None
    updated_at: float


class Question(_Object):
    id: QuestionId
    key_id: KeyId
    task_id: TaskId | None
    prompt: str
    kind: QuestionKind
    options: list[str]
    status: QuestionStatus
    answer: str | None
    created_at: float
    answered_at: float | None
    expires_at: float


class PersonSender(_Object):
    type: Literal["person"] = "person"


class AgentSender(_Object):
    type: Literal["agent"] = "agent"
    key_id: KeyId
    label: str


class InboxItem(_Object):
    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    id: InboxItemId
    kind: InboxKind
    # `from` is a Python keyword, so the field has a different name in the code.
    sender: PersonSender | AgentSender = Field(
        discriminator="type", serialization_alias="from", validation_alias=AliasChoices("from", "sender")
    )
    task_id: TaskId | None
    question_id: QuestionId | None
    text: str
    created_at: float
    read_at: float | None = None


class Agent(_Object):
    key_id: KeyId
    label: str
    is_self: bool
    last_client: ClientInfo | None
    last_used_at: float | None


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AttestationInput(_Request):
    # The key identifier of App Attest: the SHA-256 hash of the public key.
    key_id: Annotated[bytes, BeforeValidator(_from_base64), Field(min_length=32, max_length=32)]
    challenge: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    attestation_object: Annotated[
        bytes, BeforeValidator(_from_base64), Field(alias="object", min_length=1, max_length=16_384)
    ]


class AccountCreate(_Request):
    attestation: AttestationInput | None = None


class AccountSettingsPatch(_Request):
    live_activities: bool


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
    widget_push_token: HexToken | None = None


class KeyCreate(_Request):
    label: Label


class KeyPatch(_Request):
    label: Label


class TaskCreate(_Request):
    key_id: KeyId | None = None
    title: Title
    description: Description = ""
    status: Status = "todo"
    priority: Priority = "medium"
    notify: bool = True


class TaskPatch(_Request):
    """A field that is not in `model_fields_set` does not change. Only `progress` and `status_note` accept null."""

    title: Title | None = None
    description: Description | None = None
    status: Status | None = None
    priority: Priority | None = None
    notify: bool | None = None
    progress: Progress | None = None
    status_note: StatusNote | None = None

    @field_validator("title", "description", "status", "priority", "notify", mode="before")
    @classmethod
    def _not_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Must not be null.")
        return value


class NotifyInput(_Request):
    title: NotifyTitle
    body: NotifyBody
    task_id: TaskId | None = None


class QuestionCreate(_Request):
    prompt: Prompt
    kind: QuestionKind = "confirm"
    options: list[Option] | None = Field(default=None, validate_default=True)
    task_id: TaskId | None = None
    expires_in: ExpiresIn = limits.EXPIRES_IN_DEFAULT

    @field_validator("options")
    @classmethod
    def _options_fit_the_kind(cls, options: list[str] | None, info: ValidationInfo) -> list[str] | None:
        kind: object = info.data.get("kind")
        if kind != "choice":
            if options is not None:
                raise ValueError("Only a question of the kind choice has options.")
            return None
        if options is None or not limits.OPTIONS_MIN <= len(options) <= limits.OPTIONS_MAX:
            raise ValueError(f"A choice needs {limits.OPTIONS_MIN} to {limits.OPTIONS_MAX} options.")
        if len(set(options)) != len(options):
            raise ValueError("Each option must be different.")
        return options


class QuestionAnswer(_Request):
    answer: Answer


class MessageCreate(_Request):
    """`key_id` with an account credential, `to` with an API key. The service applies that rule."""

    key_id: KeyId | None = None
    to: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    text: MessageText
    task_id: TaskId | None = None


class LiveActivityInput(_Request):
    device_id: ClientDeviceId
    push_token: HexToken


class LiveActivityRemoval(_Request):
    device_id: ClientDeviceId


class AccountCreated(_Object):
    account: Account
    token: Secret


class KeyCreated(_Object):
    key: Key
    secret: Secret


class KeyList(_Object):
    keys: list[Key]


class QuestionList(_Object):
    questions: list[Question]


class InboxList(_Object):
    items: list[InboxItem]
    unread: int


class AgentList(_Object):
    agents: list[Agent]


class TaskList(_Object):
    tasks: list[Task]
    next_cursor: str | None


class AttestChallenge(_Object):
    challenge: str
    expires_in: int


class NotifyResult(_Object):
    sent: bool
    devices: int


class Health(_Object):
    status: Literal["ok", "unavailable"]
    store: Literal["ok", "unavailable"]


class ErrorBody(_Object):
    code: ErrorCode
    message: str
    fields: dict[str, str] | None = None


class ErrorResponse(_Object):
    error: ErrorBody
