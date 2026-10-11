from typing import Annotated

from fastapi import APIRouter, Query

from mcp_client_server import activity, limits
from mcp_client_server.api.deps import AccountAuth, AgentAuth, AnyAuth, ServicesDep
from mcp_client_server.ids import KeyId
from mcp_client_server.models import ActivityList, AgentList, InboxItem, InboxList, MessageCreate

router = APIRouter(prefix="/v1")


@router.get("/inbox")
async def take_inbox(
    principal: AgentAuth,
    services: ServicesDep,
    wait: Annotated[int, Query(ge=0, le=limits.WAIT_MAX_SECONDS)] = 0,
    limit: Annotated[int, Query(ge=1, le=limits.INBOX_LIMIT_MAX)] = limits.INBOX_LIMIT_DEFAULT,
    mark_read: Annotated[bool, Query()] = True,  # noqa: FBT002  a query parameter comes by name
) -> InboxList:
    page = await services.inbox.take(principal, wait=wait, limit=limit, mark_read=mark_read)
    if wait > 0 and not page.items:
        activity.waited_for_nothing()
    return InboxList(items=page.items, unread=page.unread)


@router.post("/messages", status_code=201)
async def send_message(data: MessageCreate, principal: AnyAuth, services: ServicesDep) -> InboxItem:
    return await services.inbox.send(principal, data)


@router.get("/keys/{key_id}/inbox")
async def view_inbox(key_id: KeyId, principal: AccountAuth, services: ServicesDep) -> InboxList:
    page = await services.inbox.view(principal, key_id)
    return InboxList(items=page.items, unread=page.unread)


@router.get("/keys/{key_id}/activity")
async def view_activity(
    key_id: KeyId,
    principal: AccountAuth,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=limits.ACTIVITY_LIMIT_MAX)] = limits.ACTIVITY_LIMIT_DEFAULT,
    cursor: str | None = None,
) -> ActivityList:
    page = await services.activity.page(principal, key_id, limit=limit, cursor=cursor)
    return ActivityList(items=page.items, next_cursor=page.next_cursor)


@router.get("/agents")
async def list_agents(principal: AgentAuth, services: ServicesDep) -> AgentList:
    return AgentList(agents=await services.keys.agents(principal))
