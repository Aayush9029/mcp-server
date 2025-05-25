#!/usr/bin/env python
"""
Task Management MCP Server using FastMCP
----------------------------------------
A Model Context Protocol server for managing tasks via API.

Features:
- Create, read, update, and delete tasks
- Filter tasks by status and priority  
- Secure API key authentication
- Task notifications support

To use this server, set the TASK_API_KEY environment variable:
export TASK_API_KEY=your_api_key_here

Then run:
python task_mcp_server.py
"""

import os
from datetime import datetime
from enum import Enum
from typing import Optional

import httpx
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp.server.fastmcp import FastMCP


# Enums
class TaskStatus(str, Enum):
    """Task status enumeration."""
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskPriority(str, Enum):
    """Task priority enumeration."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


# Pydantic Models
class TaskCreate(BaseModel):
    """Model for creating a new task."""
    title: str = Field(..., min_length=1, description="Task title cannot be empty")
    description: str = Field(default="", description="Task description")
    status: TaskStatus = Field(default=TaskStatus.TODO, description="Task status")
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM, description="Task priority")
    notify: bool = Field(default=True, description="Whether to send notifications")


class TaskUpdate(BaseModel):
    """Model for updating an existing task."""
    title: Optional[str] = Field(None, min_length=1, description="Task title cannot be empty if provided")
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    notify: Optional[bool] = None


class TaskResponse(BaseModel):
    """Response model for task API endpoints."""
    id: str
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    created_by: str
    created_at: float  # Unix timestamp
    last_updated_at: float  # Unix timestamp
    notify: bool


class TaskListResponse(BaseModel):
    """Response model for listing tasks."""
    tasks: list[TaskResponse]
    total: int


# Settings
class TaskSettings(BaseSettings):
    """Settings for Task MCP Server."""
    
    model_config: SettingsConfigDict = SettingsConfigDict(
        env_prefix="TASK_", 
        env_file=".env",
        extra="ignore"  # Ignore extra fields from .env
    )
    
    api_key: str = Field(..., description="API key for task management service")
    api_base_url: str = Field(
        default="https://mcpclient.lovedoingthings.com/api",
        description="Base URL for the task management API"
    )


# Create FastMCP server
mcp = FastMCP(
    "Task Manager",
    description="Manage tasks with create, read, update, and delete operations. All tasks are secured by API key authentication.",
    dependencies=["httpx", "pydantic", "pydantic-settings"]
)

# Initialize settings
try:
    task_settings = TaskSettings()
except Exception as e:
    print(f"Error: Failed to load settings. Make sure TASK_API_KEY is set in environment variables or .env file")
    print(f"Details: {e}")
    raise


def get_headers() -> dict[str, str]:
    """Get headers for API requests with authentication."""
    return {
        "Content-Type": "application/json",
        "X-API-Key": task_settings.api_key
    }


def format_task_display(task: TaskResponse) -> str:
    """Format a task for display with emojis and readable timestamps."""
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
    
    created = datetime.fromtimestamp(task.created_at).strftime("%Y-%m-%d %H:%M")
    updated = datetime.fromtimestamp(task.last_updated_at).strftime("%Y-%m-%d %H:%M")
    
    lines = [
        f"{status_emoji} **{task.title}** {priority_emoji}",
        f"ID: `{task.id}`",
        f"Status: {task.status} | Priority: {task.priority}"
    ]
    
    if task.description:
        lines.append(f"Description: {task.description}")
    
    if task.notify:
        lines.append("🔔 Notifications enabled")
    
    lines.extend([
        f"Created: {created}",
        f"Updated: {updated}"
    ])
    
    return "\n".join(lines)


@mcp.tool(
    name="create_task",
    description="Create a new task with title, description, priority, and notification settings"
)
def create_task(
    title: str = Field(..., description="Task title (required)", min_length=1),
    description: str = Field(default="", description="Detailed description of the task"),
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM, description="Task priority level"),
    notify: bool = Field(default=True, description="Enable notifications for this task")
) -> str:
    """
    Create a new task in the task management system.
    
    The task will be created with TODO status and associated with your API key.
    Only you can view, update, or delete tasks created with your API key.
    """
    task_data = TaskCreate(
        title=title,
        description=description,
        status=TaskStatus.TODO,
        priority=priority,
        notify=notify
    )
    
    with httpx.Client() as client:
        response = client.post(
            f"{task_settings.api_base_url}/tasks/",
            json=task_data.model_dump(exclude_none=True),
            headers=get_headers()
        )
        response.raise_for_status()
        
        task = TaskResponse(**response.json())
        return f"✅ Created task '{task.title}' with ID: {task.id}\n\n{format_task_display(task)}"


@mcp.tool(
    name="list_tasks", 
    description="List all your tasks with optional filtering by status and priority"
)
def list_tasks(
    status: Optional[TaskStatus] = Field(None, description="Filter by task status"),
    priority: Optional[TaskPriority] = Field(None, description="Filter by priority level"),
    limit: int = Field(default=20, description="Maximum tasks to return (1-100)", ge=1, le=100),
    offset: int = Field(default=0, description="Number of tasks to skip", ge=0)
) -> str:
    """
    List all tasks associated with your API key.
    
    You can filter tasks by status and priority, and control pagination with limit and offset.
    """
    params = {}
    if status:
        params["status"] = status
    if priority:
        params["priority"] = priority
    params["limit"] = limit
    params["offset"] = offset
    
    with httpx.Client() as client:
        response = client.get(
            f"{task_settings.api_base_url}/tasks/",
            params=params,
            headers=get_headers()
        )
        response.raise_for_status()
        
        task_list = TaskListResponse(**response.json())
        
        if not task_list.tasks:
            return "📭 No tasks found."
        
        result = [f"📋 Found {task_list.total} task(s):"]
        result.append("=" * 50)
        
        for i, task in enumerate(task_list.tasks, 1):
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
            
            result.append(f"\n{i}. {status_emoji} [{task.id}] **{task.title}** {priority_emoji}")
            
            if task.description:
                result.append(f"   {task.description}")
            
            if task.notify:
                result.append("   🔔 Notifications enabled")
        
        return "\n".join(result)


@mcp.tool(
    name="get_task",
    description="Get detailed information about a specific task by its ID"
)
def get_task(
    task_id: str = Field(..., description="Unique identifier of the task")
) -> str:
    """
    Retrieve detailed information about a specific task.
    
    You can only access tasks created with your API key.
    """
    with httpx.Client() as client:
        response = client.get(
            f"{task_settings.api_base_url}/tasks/{task_id}",
            headers=get_headers()
        )
        response.raise_for_status()
        
        task = TaskResponse(**response.json())
        return format_task_display(task)


@mcp.tool(
    name="update_task",
    description="Update an existing task's title, description, status, priority, or notification settings"
)
def update_task(
    task_id: str = Field(..., description="Unique identifier of the task to update"),
    title: Optional[str] = Field(None, description="New task title", min_length=1),
    description: Optional[str] = Field(None, description="New task description"),
    status: Optional[TaskStatus] = Field(None, description="New task status"),
    priority: Optional[TaskPriority] = Field(None, description="New priority level"),
    notify: Optional[bool] = Field(None, description="Update notification settings")
) -> str:
    """
    Update an existing task's properties.
    
    Only provide the fields you want to update. You can only update tasks created with your API key.
    """
    update_data = TaskUpdate()
    
    if title is not None:
        update_data.title = title
    if description is not None:
        update_data.description = description
    if status is not None:
        update_data.status = status
    if priority is not None:
        update_data.priority = priority
    if notify is not None:
        update_data.notify = notify
    
    with httpx.Client() as client:
        response = client.put(
            f"{task_settings.api_base_url}/tasks/{task_id}",
            json=update_data.model_dump(exclude_none=True),
            headers=get_headers()
        )
        response.raise_for_status()
        
        task = TaskResponse(**response.json())
        return f"✅ Updated task '{task.title}' (ID: {task.id})\n\n{format_task_display(task)}"


@mcp.tool(
    name="delete_task",
    description="Delete a task permanently by its ID"
)
def delete_task(
    task_id: str = Field(..., description="Unique identifier of the task to delete")
) -> str:
    """
    Permanently delete a task from the system.
    
    This action cannot be undone. You can only delete tasks created with your API key.
    """
    with httpx.Client() as client:
        response = client.delete(
            f"{task_settings.api_base_url}/tasks/{task_id}",
            headers=get_headers()
        )
        response.raise_for_status()
        
        return f"🗑️ Successfully deleted task with ID: {task_id}"


if __name__ == "__main__":
    # Run the FastMCP server
    mcp.run()