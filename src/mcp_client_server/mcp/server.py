"""The MCP surface. Each tool calls the same service functions that the REST routes call."""

import functools
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, cast

from mcp.server import CacheHint, MCPServer, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CLIENT_INFO_META_KEY, CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, BeforeValidator, Field, ValidationError
from starlette.routing import Route

from mcp_client_server import __version__, limits
from mcp_client_server.errors import AppError, NotFound, RateLimited, ValidationFailed
from mcp_client_server.mcp import views
from mcp_client_server.mcp.endpoint import McpEndpoint, current_principal, optional_principal
from mcp_client_server.mcp.views import DeleteOut, TaskListOut, TaskOut
from mcp_client_server.models import (
    CLOSED_STATUSES,
    Description,
    NotifyBody,
    NotifyInput,
    NotifyResult,
    NotifyTitle,
    Priority,
    Status,
    StatusNote,
    TaskCreate,
    TaskPatch,
    Title,
)
from mcp_client_server.services import Services
from mcp_client_server.services.keys import KeyService
from mcp_client_server.services.tasks import TaskService

logger = logging.getLogger(__name__)

_LIST_TTL_MS = 5 * 60 * 1000


def instructions(app_name: str) -> str:
    return (
        f"{app_name} shows your tasks to the person on their iPhone, iPad, and Apple Watch as you work. "
        "Each API key sees only its own tasks.\n\n"
        "1. Create a task before you start a unit of work that takes more than a few minutes.\n"
        "2. Set `status` to `in_progress` when you start, and keep `status_note` and `progress` current.\n"
        "3. Set `status` to `done` when the work is complete and verified.\n"
        "4. Use `notify_user` only when the person must act or must know now."
    )


def _split(value: Any) -> Any:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


TaskId = Annotated[str, Field(description="The `id` of a task, for example from create_task or list_tasks.")]
StatusFilter = Annotated[
    list[Status] | None, BeforeValidator(_split), Field(description="Only tasks with one of these statuses.")
]
PriorityFilter = Annotated[
    list[Priority] | None, BeforeValidator(_split), Field(description="Only tasks with one of these priorities.")
]


def _result(text: str, structured: BaseModel) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=text)], structured_content=structured.model_dump(mode="json")
    )


def _error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def _explain(exc: AppError) -> str:
    """A message that tells the model how to correct the call."""
    if isinstance(exc, NotFound):
        return f"{exc.message} This API key sees only its own tasks. Call list_tasks to get the valid task IDs."
    if isinstance(exc, RateLimited):
        minutes = -(-exc.retry_after // 60)
        return f"{exc.message} Try again in {minutes} min. Do not call again before then."
    if isinstance(exc, ValidationFailed):
        return "Invalid arguments. " + " ".join(f"{name}: {message}" for name, message in exc.fields.items())
    return exc.message


def corrective[**P](
    tool: Callable[P, Awaitable[CallToolResult]],
) -> Callable[P, Awaitable[CallToolResult]]:
    @functools.wraps(tool)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> CallToolResult:
        try:
            return await tool(*args, **kwargs)
        except AppError as exc:
            return _error(_explain(exc))
        except ValidationError as exc:
            problems = (f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}" for e in exc.errors())
            return _error("Invalid arguments. " + " ".join(problems))

    return wrapper


def _sent_arguments(ctx: Context) -> Mapping[str, Any]:
    params = ctx.request_context.params or {}
    arguments: Any = params.get("arguments")
    return cast(Mapping[str, Any], arguments) if isinstance(arguments, Mapping) else {}


class Tools:
    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks

    @corrective
    async def create_task(
        self,
        title: Annotated[Title, Field(description="Short name of the result, 1 to 200 characters, one line.")],
        description: Annotated[Description, Field(description="Details in Markdown.")] = "",
        priority: Annotated[
            Priority, Field(description="An `urgent` task alerts the person as time-sensitive.")
        ] = "medium",
        status: Annotated[Status, Field(description="Use `in_progress` if you start the work now.")] = "todo",
        notify: Annotated[
            bool,
            Field(description="If true, changes to this task alert the person. If false, the devices update silently."),
        ] = True,
    ) -> Annotated[CallToolResult, TaskOut]:
        data = TaskCreate(title=title, description=description, priority=priority, status=status, notify=notify)
        task = await self._tasks.create(current_principal(), data)
        return _result(f"Created task {views.line(task)}", TaskOut.of(task))

    @corrective
    async def list_tasks(
        self,
        status: StatusFilter = None,
        priority: PriorityFilter = None,
        limit: Annotated[int, Field(ge=1, le=limits.LIST_LIMIT_MAX, description="Maximum number of tasks.")] = (
            limits.MCP_LIST_LIMIT_DEFAULT
        ),
        cursor: Annotated[str | None, Field(description="The `next_cursor` of the previous page.")] = None,
    ) -> Annotated[CallToolResult, TaskListOut]:
        page = await self._tasks.page(
            current_principal(), statuses=status or (), priorities=priority or (), limit=limit, cursor=cursor
        )
        out = TaskListOut(tasks=[TaskOut.of(task) for task in page.tasks], next_cursor=page.next_cursor)
        return _result(views.listing(page.tasks, page.next_cursor), out)

    @corrective
    async def get_task(self, task_id: TaskId) -> Annotated[CallToolResult, TaskOut]:
        task = await self._tasks.get(current_principal(), task_id)
        return _result(views.detail(task), TaskOut.of(task))

    @corrective
    async def update_task(
        self,
        task_id: TaskId,
        title: Annotated[Title | None, Field(description="New title.")] = None,
        description: Annotated[Description | None, Field(description="New details in Markdown.")] = None,
        status: Annotated[
            Status | None,
            Field(description="`in_progress` when you start, `done` when the work is complete and verified."),
        ] = None,
        priority: Annotated[Priority | None, Field(description="New priority.")] = None,
        notify: Annotated[bool | None, Field(description="If true, changes to this task alert the person.")] = None,
        progress: Annotated[
            Annotated[int, Field(ge=0, le=100)] | None,
            Field(description="Percent complete, 0 to 100. Send null to clear it. `done` sets it to 100."),
        ] = None,
        status_note: Annotated[
            StatusNote | None,
            Field(
                description="One short line that says what you do now, 140 characters at most. Send null to clear it."
            ),
        ] = None,
        *,
        ctx: Context,
    ) -> Annotated[CallToolResult, TaskOut]:
        given: dict[str, Any] = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "notify": notify,
            "progress": progress,
            "status_note": status_note,
        }
        fields = {name: value for name, value in given.items() if value is not None}
        sent = _sent_arguments(ctx)
        for name in ("progress", "status_note"):
            if name in sent and sent[name] is None:
                fields[name] = None
        if not fields:
            return _error("No field to change. Send one or more of: " + ", ".join(given) + ".")
        task = await self._tasks.update(current_principal(), task_id, TaskPatch.model_validate(fields))
        return _result(f"Updated task {views.line(task)}", TaskOut.of(task))

    @corrective
    async def delete_task(self, task_id: TaskId) -> Annotated[CallToolResult, DeleteOut]:
        task = await self._tasks.delete(current_principal(), task_id)
        return _result(f'Deleted task {task.id} "{task.title}".', DeleteOut(deleted=True, task_id=task.id))

    @corrective
    async def notify_user(
        self,
        title: Annotated[NotifyTitle, Field(description="Headline of the notification, 100 characters at most.")],
        body: Annotated[NotifyBody, Field(description="What the person must do or know, 1,000 characters at most.")],
        task_id: Annotated[str | None, Field(description="The task that the notification is about.")] = None,
    ) -> Annotated[CallToolResult, NotifyResult]:
        result = await self._tasks.notify_user(
            current_principal(), NotifyInput(title=title, body=body, task_id=task_id)
        )
        if result.sent:
            devices = "1 device" if result.devices == 1 else f"{result.devices} devices"
            return _result(f"Notification sent to {devices}.", result)
        return _result("No notification was sent: the person has no device that can get notifications.", result)


class Resources:
    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks

    async def open_tasks(self) -> str:
        tasks = await self._tasks.open_tasks(current_principal())
        return json.dumps({"tasks": [TaskOut.of(task).model_dump(mode="json") for task in tasks]})

    async def task(self, task_id: str) -> str:
        try:
            task = await self._tasks.get(current_principal(), task_id)
        except NotFound as exc:
            raise ResourceNotFoundError(f"Task {task_id} was not found for this API key.") from exc
        return TaskOut.of(task).model_dump_json()


class Prompts:
    def __init__(self, tasks: TaskService) -> None:
        self._tasks = tasks

    def plan_work(self, goal: Annotated[str, Field(description="What the person wants done.")]) -> str:
        return (
            f"Goal: {goal}\n\n"
            "Divide this goal into tasks and track them for me.\n\n"
            "1. Find the units of work that the goal needs. Each unit is one clear result that takes more than a "
            "few minutes. Use 3 to 10 units.\n"
            "2. Call create_task one time for each unit, in the order of the work. Give each task a short title "
            "that names the result, a description that says how to verify the result, and a priority.\n"
            "3. Show me the tasks with their IDs.\n"
            "4. When you start a task, call update_task with status in_progress. Keep status_note and progress "
            "current. Set status to done only after you verify the result."
        )

    async def status_report(self) -> str:
        principal = current_principal()
        open_tasks = await self._tasks.open_tasks(principal)
        closed = await self._tasks.page(principal, statuses=sorted(CLOSED_STATUSES), limit=20)
        open_lines = "\n".join(views.line(task) for task in open_tasks) or "None."
        closed_lines = "\n".join(views.line(task) for task in closed.tasks) or "None."
        return (
            "Write a short status report for me from the tasks below. Use only these tasks.\n\n"
            "Give three parts: in progress (with the progress and the current note), to do (highest priority "
            "first), and recently closed. Put an urgent task first.\n\n"
            f"Open tasks:\n{open_lines}\n\nRecently closed tasks:\n{closed_lines}"
        )


class ClientRecorder:
    """SDK middleware that keeps `last_client` of the key from the client info of each request."""

    def __init__(self, keys: KeyService) -> None:
        self._keys = keys

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        principal = optional_principal()
        client = _client_info(ctx)
        if principal and client:
            try:
                await self._keys.record_client(principal, *client)
            except Exception:
                logger.warning("could not record the MCP client", exc_info=True)
        return await call_next(ctx)


def _client_info(ctx: ServerRequestContext[Any, Any]) -> tuple[str, str] | None:
    """Client info from the request `_meta` (2026-07-28) or from `initialize` (earlier revisions)."""
    params = ctx.params or {}
    raw: Any = None
    if ctx.method == "initialize":
        raw = params.get("clientInfo")
    else:
        meta: Any = params.get("_meta")
        if isinstance(meta, Mapping):
            raw = cast(Mapping[str, Any], meta).get(CLIENT_INFO_META_KEY)
    if not isinstance(raw, Mapping):
        return None
    info = cast(Mapping[str, Any], raw)
    name, version = info.get("name"), info.get("version")
    if not isinstance(name, str) or not name:
        return None
    return name, version if isinstance(version, str) else ""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "tasks"


def build_mcp_server(services: Services) -> MCPServer:
    settings = services.settings
    listing = CacheHint(ttl_ms=_LIST_TTL_MS, scope="public")
    server = MCPServer(
        name=_slug(settings.app_name),
        title=settings.app_name,
        description="Tasks that AI agents create and that the person follows on Apple devices.",
        instructions=instructions(settings.app_name),
        website_url=settings.base_url,
        version=__version__,
        cache_hints={
            "server/discover": listing,
            "tools/list": listing,
            "prompts/list": listing,
            "resources/list": listing,
            "resources/templates/list": listing,
            "resources/read": CacheHint(ttl_ms=0, scope="private"),
        },
        # The server never changes its lists, and a listen stream would hold a connection for each client.
        subscriptions=False,
        middleware=[ClientRecorder(services.keys)],
    )
    _add_tools(server, Tools(services.tasks))
    _add_resources(server, Resources(services.tasks))
    _add_prompts(server, Prompts(services.tasks))
    return server


_DESCRIPTIONS = {
    "create_task": (
        "Create a task that the person sees on their devices. Create one before you start a unit of work "
        "that takes more than a few minutes. The result has the task `id` for later calls."
    ),
    "list_tasks": (
        "List the tasks of this API key, newest update first. If `next_cursor` is not null, call again "
        "with it as `cursor` to get the next page."
    ),
    "get_task": "Get one task with its full description.",
    "update_task": (
        "Change a task. Only the fields that you send change. Keep `progress` and `status_note` current while you work."
    ),
    "delete_task": (
        "Delete a task permanently. To keep the task in the history, use update_task with status `cancelled` instead."
    ),
    "notify_user": (
        "Send a push notification to the person's devices now. Use it only when the person must act or "
        "must know now. A key can send 30 in one hour. Task changes already notify the person."
    ),
}


def _hints(
    title: str, *, read_only: bool = False, destructive: bool = False, idempotent: bool = False
) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=False,
    )


def _add_tools(server: MCPServer, tools: Tools) -> None:
    # tools/list returns the tools in this order.
    catalog: list[tuple[Callable[..., Any], ToolAnnotations]] = [
        (tools.create_task, _hints("Create task")),
        (tools.list_tasks, _hints("List tasks", read_only=True, idempotent=True)),
        (tools.get_task, _hints("Get task", read_only=True, idempotent=True)),
        (tools.update_task, _hints("Update task", idempotent=True)),
        (tools.delete_task, _hints("Delete task", destructive=True, idempotent=True)),
        (tools.notify_user, _hints("Notify user")),
    ]
    for tool, annotations in catalog:
        server.add_tool(
            tool,
            title=annotations.title,
            description=_DESCRIPTIONS[tool.__name__],
            annotations=annotations,
            structured_output=True,
        )


def _add_resources(server: MCPServer, resources: Resources) -> None:
    server.resource(
        "tasks://open",
        name="open_tasks",
        title="Open tasks",
        description="The todo and in_progress tasks of this API key, as JSON.",
        mime_type="application/json",
    )(resources.open_tasks)
    server.resource(
        "tasks://task/{task_id}",
        name="task",
        title="Task",
        description="One task of this API key, as JSON.",
        mime_type="application/json",
    )(resources.task)


def _add_prompts(server: MCPServer, prompts: Prompts) -> None:
    server.prompt(name="plan_work", title="Plan work", description="Divide a goal into tasks and create them.")(
        prompts.plan_work
    )
    server.prompt(
        name="status_report",
        title="Status report",
        description="Summarize the open and recently closed tasks.",
    )(prompts.status_report)


def mcp_route(services: Services) -> tuple[Route, StreamableHTTPSessionManager]:
    """The `/mcp` route and the session manager that the application lifespan must run."""
    server = build_mcp_server(services)
    # Called for its side effect: it builds the stateless session manager. The service
    # is public and each request has an API key, so the SDK's localhost-only Host check
    # is off; McpEndpoint checks the Origin header.
    server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = server.session_manager
    endpoint = McpEndpoint(manager.handle_request, services.auth, services.settings.allowed_origins)
    return Route("/mcp", endpoint=endpoint), manager
