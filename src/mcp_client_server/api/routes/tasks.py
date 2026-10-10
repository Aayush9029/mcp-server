from typing import Annotated

from fastapi import APIRouter, Query, Response

from mcp_client_server import limits
from mcp_client_server.api.deps import AccountPrincipal, AnyPrincipal, ServicesDep
from mcp_client_server.errors import ValidationFailed
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


def _choices[T: str](name: str, raw: str | None, allowed: tuple[T, ...]) -> list[T]:
    if raw is None:
        return []
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    if not wanted <= set(allowed):
        raise ValidationFailed({name: f"Use one or more of: {', '.join(allowed)}."})
    return [value for value in allowed if value in wanted]


@router.get("")
async def list_tasks(
    principal: AnyPrincipal,
    services: ServicesDep,
    key_id: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    limit: Annotated[int, Query(ge=1, le=limits.LIST_LIMIT_MAX)] = limits.LIST_LIMIT_DEFAULT,
    cursor: str | None = None,
) -> Response:
    page = await services.tasks.page(
        principal,
        key_id=key_id,
        statuses=_choices("status", status, STATUSES),
        priorities=_choices("priority", priority, PRIORITIES),
        limit=limit,
        cursor=cursor,
    )
    # The store keeps each task as the JSON that the API returns, so the page goes out without a parse.
    return Response(page.to_json(), media_type="application/json")


@router.post("", status_code=201)
async def create_task(data: TaskCreate, principal: AnyPrincipal, services: ServicesDep) -> Task:
    return await services.tasks.create(principal, data)


@router.get("/{task_id}")
async def get_task(task_id: str, principal: AnyPrincipal, services: ServicesDep) -> Task:
    return await services.tasks.get(principal, task_id)


@router.patch("/{task_id}")
async def update_task(task_id: str, patch: TaskPatch, principal: AnyPrincipal, services: ServicesDep) -> Task:
    return await services.tasks.update(principal, task_id, patch)


@router.delete("/{task_id}", status_code=204, response_class=Response)
async def delete_task(task_id: str, principal: AnyPrincipal, services: ServicesDep) -> None:
    await services.tasks.delete(principal, task_id)


@router.put("/{task_id}/live-activity", status_code=204, response_class=Response)
async def set_live_activity(
    task_id: str, data: LiveActivityInput, principal: AccountPrincipal, services: ServicesDep
) -> None:
    await services.tasks.set_live_activity(principal, task_id, data)


@router.delete("/{task_id}/live-activity", status_code=204, response_class=Response)
async def clear_live_activity(
    task_id: str, data: LiveActivityRemoval, principal: AccountPrincipal, services: ServicesDep
) -> None:
    await services.tasks.clear_live_activity(principal, task_id, data)
