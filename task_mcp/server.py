"""Task Management MCP Server - stdio transport implementation."""

import logging
import os
from typing import Any, Optional

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from .models import TaskCreate, TaskListResponse, TaskResponse, TaskUpdate

logger = logging.getLogger(__name__)

API_BASE_URL = "https://mcpclient.lovedoingthings.com"


class TaskMCPServer:
    """MCP Server for task management via API wrapper."""

    def __init__(self):
        """Initialize the MCP server."""
        self.server = Server("task-mcp")
        self.api_key = os.getenv("TASK_API_KEY")
        self._setup_handlers()

    def _setup_handlers(self):
        """Set up all MCP protocol handlers."""
        
        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            """List all available tools."""
            return [
                types.Tool(
                    name="create_task",
                    description="Create a new task",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "description": "Task title"
                            },
                            "description": {
                                "type": "string",
                                "description": "Task description"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                                "description": "Task priority",
                                "default": "MEDIUM"
                            },
                            "notify": {
                                "type": "boolean",
                                "description": "Whether to send notifications",
                                "default": true
                            }
                        },
                        "required": ["title"]
                    }
                ),
                types.Tool(
                    name="list_tasks",
                    description="List all tasks with optional filtering",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["TODO", "IN_PROGRESS", "DONE", "CANCELLED"],
                                "description": "Filter by status"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                                "description": "Filter by priority"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of tasks to return",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 20
                            },
                            "offset": {
                                "type": "integer",
                                "description": "Number of tasks to skip",
                                "minimum": 0,
                                "default": 0
                            }
                        }
                    }
                ),
                types.Tool(
                    name="get_task",
                    description="Get a specific task by ID",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task ID"
                            }
                        },
                        "required": ["task_id"]
                    }
                ),
                types.Tool(
                    name="update_task",
                    description="Update an existing task",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task ID"
                            },
                            "title": {
                                "type": "string",
                                "description": "New task title"
                            },
                            "description": {
                                "type": "string",
                                "description": "New task description"
                            },
                            "status": {
                                "type": "string",
                                "enum": ["TODO", "IN_PROGRESS", "DONE", "CANCELLED"],
                                "description": "New task status"
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                                "description": "New task priority"
                            },
                            "notify": {
                                "type": "boolean",
                                "description": "Whether to send notifications"
                            }
                        },
                        "required": ["task_id"]
                    }
                ),
                types.Tool(
                    name="delete_task",
                    description="Delete a task",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task ID to delete"
                            }
                        },
                        "required": ["task_id"]
                    }
                )
            ]

        @self.server.call_tool()
        async def handle_call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
            """Handle tool calls."""
            
            # Get headers for API calls
            headers = self._get_headers()
            
            try:
                if name == "create_task":
                    return await self._create_task(arguments, headers)
                elif name == "list_tasks":
                    return await self._list_tasks(arguments, headers)
                elif name == "get_task":
                    return await self._get_task(arguments, headers)
                elif name == "update_task":
                    return await self._update_task(arguments, headers)
                elif name == "delete_task":
                    return await self._delete_task(arguments, headers)
                else:
                    raise ValueError(f"Unknown tool: {name}")
            except Exception as e:
                logger.error(f"Error in tool {name}: {str(e)}")
                return [types.TextContent(
                    type="text",
                    text=f"Error: {str(e)}"
                )]

    def _get_headers(self) -> dict[str, str]:
        """Get headers for API requests."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _create_task(
        self, arguments: dict[str, Any], headers: dict[str, str]
    ) -> list[types.TextContent]:
        """Create a new task."""
        task_data = TaskCreate(**arguments)
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{API_BASE_URL}/tasks",
                json=task_data.model_dump(exclude_none=True),
                headers=headers
            )
            response.raise_for_status()
            
            task = TaskResponse(**response.json())
            return [types.TextContent(
                type="text",
                text=f"Created task '{task.title}' with ID: {task.id}"
            )]

    async def _list_tasks(
        self, arguments: dict[str, Any], headers: dict[str, str]
    ) -> list[types.TextContent]:
        """List tasks with optional filtering."""
        params = {k: v for k, v in arguments.items() if v is not None}
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{API_BASE_URL}/tasks",
                params=params,
                headers=headers
            )
            response.raise_for_status()
            
            task_list = TaskListResponse(**response.json())
            
            if not task_list.tasks:
                return [types.TextContent(
                    type="text",
                    text="No tasks found."
                )]
            
            # Format task list
            lines = [f"Found {task_list.total} task(s):"]
            for task in task_list.tasks:
                status_emoji = {
                    "TODO": "📝",
                    "IN_PROGRESS": "🔄",
                    "DONE": "✅",
                    "CANCELLED": "❌"
                }.get(task.status, "")
                
                priority_emoji = {
                    "LOW": "🟢",
                    "MEDIUM": "🟡",
                    "HIGH": "🔴",
                    "URGENT": "🚨"
                }.get(task.priority, "")
                
                lines.append(
                    f"\n{status_emoji} [{task.id}] {task.title} {priority_emoji}"
                )
                if task.description:
                    lines.append(f"   {task.description}")
                if task.notify:
                    lines.append(f"   🔔 Notifications enabled")
            
            return [types.TextContent(
                type="text",
                text="\n".join(lines)
            )]

    async def _get_task(
        self, arguments: dict[str, Any], headers: dict[str, str]
    ) -> list[types.TextContent]:
        """Get a specific task by ID."""
        task_id = arguments["task_id"]
        
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{API_BASE_URL}/tasks/{task_id}",
                headers=headers
            )
            response.raise_for_status()
            
            task = TaskResponse(**response.json())
            
            status_emoji = {
                "TODO": "📝",
                "IN_PROGRESS": "🔄",
                "DONE": "✅",
                "CANCELLED": "❌"
            }.get(task.status, "")
            
            priority_emoji = {
                "LOW": "🟢",
                "MEDIUM": "🟡",
                "HIGH": "🔴",
                "URGENT": "🚨"
            }.get(task.priority, "")
            
            lines = [
                f"{status_emoji} Task: {task.title} {priority_emoji}",
                f"ID: {task.id}",
                f"Status: {task.status}",
                f"Priority: {task.priority}"
            ]
            
            if task.description:
                lines.append(f"Description: {task.description}")
            if task.notify:
                lines.append(f"Notifications: Enabled 🔔")
            
            # Convert timestamps to readable format
            from datetime import datetime
            created = datetime.fromtimestamp(task.created_at).strftime("%Y-%m-%d %H:%M:%S")
            updated = datetime.fromtimestamp(task.last_updated_at).strftime("%Y-%m-%d %H:%M:%S")
            
            lines.extend([
                f"Created: {created}",
                f"Updated: {updated}"
            ])
            
            return [types.TextContent(
                type="text",
                text="\n".join(lines)
            )]

    async def _update_task(
        self, arguments: dict[str, Any], headers: dict[str, str]
    ) -> list[types.TextContent]:
        """Update an existing task."""
        task_id = arguments.pop("task_id")
        update_data = TaskUpdate(**arguments)
        
        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{API_BASE_URL}/tasks/{task_id}",
                json=update_data.model_dump(exclude_none=True),
                headers=headers
            )
            response.raise_for_status()
            
            task = TaskResponse(**response.json())
            return [types.TextContent(
                type="text",
                text=f"Updated task '{task.title}' (ID: {task.id})"
            )]

    async def _delete_task(
        self, arguments: dict[str, Any], headers: dict[str, str]
    ) -> list[types.TextContent]:
        """Delete a task."""
        task_id = arguments["task_id"]
        
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{API_BASE_URL}/tasks/{task_id}",
                headers=headers
            )
            response.raise_for_status()
            
            return [types.TextContent(
                type="text",
                text=f"Successfully deleted task with ID: {task_id}"
            )]

    async def run(self):
        """Run the MCP server using stdio transport."""
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="task-mcp",
                    server_version="1.0.11",
                    capabilities=self.server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )


def create_server() -> TaskMCPServer:
    """Create and return a TaskMCPServer instance."""
    return TaskMCPServer()