"""Pydantic models for Task Management API."""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Task status enum."""

    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class TaskPriority(str, Enum):
    """Task priority enum."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class TaskCreate(BaseModel):
    """Model for creating a new task."""

    title: str = Field(..., min_length=1, description="Task title cannot be empty")
    description: str = Field(default="", description="Task description")
    status: TaskStatus = Field(default=TaskStatus.TODO, description="Task status")
    priority: TaskPriority = Field(
        default=TaskPriority.MEDIUM, description="Task priority"
    )
    notify: bool = Field(
        default=False, description="Whether to send notifications for this task"
    )


class TaskUpdate(BaseModel):
    """Model for updating an existing task."""

    title: Optional[str] = Field(
        None, min_length=1, description="Task title cannot be empty if provided"
    )
    description: Optional[str] = Field(None, description="Task description")
    status: Optional[TaskStatus] = Field(None, description="Task status")
    priority: Optional[TaskPriority] = Field(None, description="Task priority")
    notify: Optional[bool] = Field(
        None, description="Whether to send notifications for this task"
    )


class TaskResponse(BaseModel):
    """Response model for task API endpoints."""

    id: str
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    created_by: str
    created_at: float
    last_updated_at: float
    notify: bool


class TaskListResponse(BaseModel):
    """Response model for listing tasks."""

    tasks: list[TaskResponse]
    total: int
