from collections.abc import Sequence
from dataclasses import dataclass

from cryptography import x509

from mcp_client_server.clock import Clock
from mcp_client_server.config import Settings
from mcp_client_server.events import EventHub
from mcp_client_server.ids import IdSource
from mcp_client_server.limits import Limits
from mcp_client_server.push import PushDispatcher
from mcp_client_server.services.accounts import AccountService, DeviceService
from mcp_client_server.services.activity import ActivityLog
from mcp_client_server.services.auth import AuthService
from mcp_client_server.services.inbox import InboxService
from mcp_client_server.services.integrity import IntegrityService
from mcp_client_server.services.keys import KeyService
from mcp_client_server.services.maintenance import Maintenance
from mcp_client_server.services.purge import KeyPurger
from mcp_client_server.services.questions import QuestionService
from mcp_client_server.services.seen import SeenMarker
from mcp_client_server.services.system import SystemService
from mcp_client_server.services.tasks import TaskService
from mcp_client_server.services.waits import WaitSlots
from mcp_client_server.store import Store


@dataclass(frozen=True, slots=True)
class Services:
    """The one service layer. The REST routes and the MCP tools both call these objects."""

    settings: Settings
    limits: Limits
    events: EventHub
    push: PushDispatcher
    system: SystemService
    auth: AuthService
    integrity: IntegrityService
    accounts: AccountService
    devices: DeviceService
    keys: KeyService
    tasks: TaskService
    questions: QuestionService
    inbox: InboxService
    activity: ActivityLog
    maintenance: Maintenance


def build_services(
    settings: Settings,
    store: Store,
    events: EventHub,
    push: PushDispatcher,
    clock: Clock,
    ids: IdSource,
    limits: Limits,
    attest_roots: Sequence[x509.Certificate],
) -> Services:
    seen = SeenMarker(store, clock)
    waits = WaitSlots(limits.max_waits_per_key)
    purger = KeyPurger(store, push, limits)
    inbox = InboxService(store, events, seen, waits, clock, ids, limits)
    questions = QuestionService(store, events, push, inbox, waits, clock, ids, limits)
    return Services(
        settings=settings,
        limits=limits,
        events=events,
        push=push,
        system=SystemService(store),
        auth=AuthService(store, clock, settings.rate_limit_per_minute, settings.rate_limit_accounts_per_hour),
        integrity=IntegrityService(store, clock, settings, attest_roots),
        accounts=AccountService(store, purger, clock, ids),
        devices=DeviceService(store, clock, limits),
        keys=KeyService(store, push, purger, clock, ids, limits),
        tasks=TaskService(store, push, questions, inbox, seen, clock, ids, limits),
        inbox=inbox,
        activity=ActivityLog(store, clock, ids),
        questions=questions,
        maintenance=Maintenance(store, questions, purger, clock, limits),
    )
