from typing import Annotated

from fastapi import APIRouter, Query, Response

from mcp_client_server import limits
from mcp_client_server.api.deps import AccountAuth, AnyAuth, ServicesDep
from mcp_client_server.api.query import choices
from mcp_client_server.ids import KeyId, TaskId
from mcp_client_server.models import (
    PRIORITIES,
    STATUSES,
    LiveActivityInput,
    LiveActivityRemoval,
    Task,
    TaskCreate,
    TaskPatch,
)

router = APIRouter(prefix="/v1/tasks")


@router.get("", response_model=None)
async def list_tasks(
    principal: AnyAuth,
    services: ServicesDep,
    key_id: KeyId | None = None,
    status: str | None = None,
    priority: str | None = None,
    limit: Annotated[int, Query(ge=1, le=limits.LIST_LIMIT_MAX)] = limits.LIST_LIMIT_DEFAULT,
    cursor: str | None = None,
) -> Response:
    page = await services.tasks.page(
        principal,
        key_id=key_id,
        statuses=choices("status", status, STATUSES),
        priorities=choices("priority", priority, PRIORITIES),
        limit=limit,
        cursor=cursor,
    )
    return Response(page.to_json(), media_type="application/json")


@router.post("", status_code=201)
async def create_task(data: TaskCreate, principal: AnyAuth, services: ServicesDep) -> Task:
    return await services.tasks.create(principal, data)


@router.get("/{task_id}")
async def get_task(task_id: TaskId, principal: AnyAuth, services: ServicesDep) -> Task:
    return await services.tasks.get(principal, task_id)


@router.patch("/{task_id}")
async def update_task(task_id: TaskId, patch: TaskPatch, principal: AnyAuth, services: ServicesDep) -> Task:
    return await services.tasks.update(principal, task_id, patch)


@router.delete("/{task_id}", status_code=204, response_class=Response)
async def delete_task(task_id: TaskId, principal: AnyAuth, services: ServicesDep) -> None:
    await services.tasks.delete(principal, task_id)


@router.put("/{task_id}/live-activity", status_code=204, response_class=Response)
async def set_live_activity(
    task_id: TaskId, data: LiveActivityInput, principal: AccountAuth, services: ServicesDep
) -> None:
    await services.tasks.set_live_activity(principal, task_id, data)


@router.delete("/{task_id}/live-activity", status_code=204, response_class=Response)
async def clear_live_activity(
    task_id: TaskId, data: LiveActivityRemoval, principal: AccountAuth, services: ServicesDep
) -> None:
    await services.tasks.clear_live_activity(principal, task_id, data)
