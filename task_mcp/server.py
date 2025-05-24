"""MCP Server for Task Management API."""

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from task_mcp.models import (TaskCreate, TaskListResponse, TaskResponse,
                             TaskUpdate)


class TaskManagementMCP:
    """MCP Server wrapper for Task Management API."""

    def __init__(self):
        self.server = Server("task-management-mcp")
        self.base_url = "https://mcpclient.lovedoingthings.com"
        self.api_key = os.getenv("TASK_API_KEY", "")

        # Store tools
        self.tools = [
            self._create_task_tool(),
            self._list_tasks_tool(),
            self._get_task_tool(),
            self._update_task_tool(),
            self._delete_task_tool(),
        ]

        # Register handlers
        self._register_handlers()

    def _create_task_tool(self) -> Tool:
        """Define the create task tool."""
        return Tool(
            name="create_task",
            description="Create a new task",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Task title (required)"},
                    "description": {
                        "type": "string",
                        "description": "Task description",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["TODO", "IN_PROGRESS", "DONE", "CANCELLED"],
                        "description": "Task status",
                        "default": "TODO",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                        "description": "Task priority",
                        "default": "MEDIUM",
                    },
                    "notify": {
                        "type": "boolean",
                        "description": "Whether to send notifications",
                        "default": False,
                    },
                },
                "required": ["title"],
            },
        )

    def _list_tasks_tool(self) -> Tool:
        """Define the list tasks tool."""
        return Tool(
            name="list_tasks",
            description="List all tasks associated with the API key",
            inputSchema={"type": "object", "properties": {}},
        )

    def _get_task_tool(self) -> Tool:
        """Define the get task tool."""
        return Tool(
            name="get_task",
            description="Get a specific task by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique identifier of the task",
                    }
                },
                "required": ["task_id"],
            },
        )

    def _update_task_tool(self) -> Tool:
        """Define the update task tool."""
        return Tool(
            name="update_task",
            description="Update an existing task",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique identifier of the task",
                    },
                    "title": {"type": "string", "description": "New task title"},
                    "description": {
                        "type": "string",
                        "description": "New task description",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["TODO", "IN_PROGRESS", "DONE", "CANCELLED"],
                        "description": "New task status",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["LOW", "MEDIUM", "HIGH", "URGENT"],
                        "description": "New task priority",
                    },
                    "notify": {
                        "type": "boolean",
                        "description": "Whether to send notifications",
                    },
                },
                "required": ["task_id"],
            },
        )

    def _delete_task_tool(self) -> Tool:
        """Define the delete task tool."""
        return Tool(
            name="delete_task",
            description="Delete a task by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique identifier of the task",
                    }
                },
                "required": ["task_id"],
            },
        )

    def _register_handlers(self):
        """Register all handlers with the server."""

        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return self.tools

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
            return await self._handle_tool_call(name, arguments)

    async def _handle_tool_call(
        self, name: str, arguments: dict[str, Any]
    ) -> list[TextContent]:
        """Handle tool calls."""
        if not self.api_key:
            return [
                TextContent(
                    type="text",
                    text="Error: No API key configured. Please provide the API key in your MCP client configuration or download the MCP Client app from the Apple App Store to generate one.",
                )
            ]

        if not self.api_key.startswith("ldtmcp-"):
            return [
                TextContent(
                    type="text",
                    text="Error: Invalid API key format. API key must start with 'ldtmcp-' prefix.",
                )
            ]

        headers = {"X-API-Key": self.api_key}

        async with httpx.AsyncClient() as client:
            try:
                if name == "create_task":
                    task_data = TaskCreate(**arguments)
                    response = await client.post(
                        f"{self.base_url}/api/tasks/",
                        headers=headers,
                        json=task_data.model_dump(),
                    )
                    response.raise_for_status()
                    task = TaskResponse(**response.json())
                    return [
                        TextContent(
                            type="text",
                            text=f"Task created successfully:\n{json.dumps(task.model_dump(), indent=2)}",
                        )
                    ]

                elif name == "list_tasks":
                    response = await client.get(
                        f"{self.base_url}/api/tasks/", headers=headers
                    )
                    response.raise_for_status()
                    task_list = TaskListResponse(**response.json())
                    return [
                        TextContent(
                            type="text",
                            text=f"Found {task_list.total} tasks:\n{json.dumps([task.model_dump() for task in task_list.tasks], indent=2)}",
                        )
                    ]

                elif name == "get_task":
                    task_id = arguments["task_id"]
                    response = await client.get(
                        f"{self.base_url}/api/tasks/{task_id}", headers=headers
                    )
                    response.raise_for_status()
                    task = TaskResponse(**response.json())
                    return [
                        TextContent(
                            type="text",
                            text=f"Task details:\n{json.dumps(task.model_dump(), indent=2)}",
                        )
                    ]

                elif name == "update_task":
                    task_id = arguments.pop("task_id")
                    update_data = TaskUpdate(**arguments)
                    response = await client.put(
                        f"{self.base_url}/api/tasks/{task_id}",
                        headers=headers,
                        json=update_data.model_dump(exclude_none=True),
                    )
                    response.raise_for_status()
                    task = TaskResponse(**response.json())
                    return [
                        TextContent(
                            type="text",
                            text=f"Task updated successfully:\n{json.dumps(task.model_dump(), indent=2)}",
                        )
                    ]

                elif name == "delete_task":
                    task_id = arguments["task_id"]
                    response = await client.delete(
                        f"{self.base_url}/api/tasks/{task_id}", headers=headers
                    )
                    response.raise_for_status()
                    return [
                        TextContent(
                            type="text", text=f"Task {task_id} deleted successfully"
                        )
                    ]

                else:
                    return [TextContent(type="text", text=f"Unknown tool: {name}")]

            except httpx.HTTPStatusError as e:
                error_detail = ""
                try:
                    error_detail = e.response.json()
                except (ValueError, TypeError, AttributeError):
                    error_detail = e.response.text

                if e.response.status_code == 401:
                    return [
                        TextContent(
                            type="text",
                            text="Authentication error: Invalid or missing API key. Please download the MCP Client app from the Apple App Store to generate a valid API key.",
                        )
                    ]
                elif e.response.status_code == 404:
                    return [
                        TextContent(
                            type="text",
                            text="Task not found or does not belong to your API key",
                        )
                    ]
                else:
                    return [
                        TextContent(
                            type="text",
                            text=f"HTTP Error {e.response.status_code}: {error_detail}",
                        )
                    ]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {str(e)}")]

    async def run(self):
        """Run the MCP server."""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, None)


def main():
    """Main entry point."""
    import sys

    # Simple argument handling
    if len(sys.argv) > 1 and sys.argv[1] in ["--help", "-h"]:
        print("Task Management MCP Server")
        print("\nUsage: task-mcp [options]")
        print("\nOptions:")
        print("  --help, -h    Show this help message")
        print("\nEnvironment Variables:")
        print("  TASK_API_KEY  API key for the task management service")
        print("\nThis server implements the Model Context Protocol (MCP)")
        print("and provides tools for managing tasks via API.")
        return 0

    # Run the server
    asyncio.run(run_server())
    return 0


async def run_server():
    """Run the MCP server."""
    mcp = TaskManagementMCP()
    await mcp.run()


if __name__ == "__main__":
    sys.exit(main())
