from dataclasses import dataclass

from mcp_client_server.config import Settings
from mcp_client_server.events import EventHub
from mcp_client_server.push import PushDispatcher
from mcp_client_server.services.accounts import AccountService, DeviceService
from mcp_client_server.services.auth import AuthService
from mcp_client_server.services.keys import KeyService
from mcp_client_server.services.maintenance import Maintenance
from mcp_client_server.services.tasks import TaskService
from mcp_client_server.store import Store


@dataclass(frozen=True, slots=True)
class Services:
    """The one service layer. The REST routes and the MCP tools both call these objects."""

    settings: Settings
    store: Store
    events: EventHub
    push: PushDispatcher
    auth: AuthService
    accounts: AccountService
    devices: DeviceService
    keys: KeyService
    tasks: TaskService
    maintenance: Maintenance


def build_services(settings: Settings, store: Store, events: EventHub, push: PushDispatcher) -> Services:
    return Services(
        settings=settings,
        store=store,
        events=events,
        push=push,
        auth=AuthService(store, settings),
        accounts=AccountService(store),
        devices=DeviceService(store),
        keys=KeyService(store, push),
        tasks=TaskService(store, push),
        maintenance=Maintenance(store),
    )
