# Task Management MCP Server

[![PyPI version](https://badge.fury.io/py/task-management-mcp.svg)](https://badge.fury.io/py/task-management-mcp)
[![Python](https://img.shields.io/pypi/pyversions/task-management-mcp.svg)](https://pypi.org/project/task-management-mcp/)
[![Test](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml/badge.svg)](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml)
[![Publish](https://github.com/Aayush9029/mcp-server/actions/workflows/publish.yml/badge.svg)](https://github.com/Aayush9029/mcp-server/actions/workflows/publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub](https://img.shields.io/github/stars/Aayush9029/mcp-server?style=social)](https://github.com/Aayush9029/mcp-server)

A Model Context Protocol (MCP) server that enables Large Language Models (LLMs) to interact with task management systems through a standardized protocol.

## Overview

This MCP server provides a bridge between LLMs and task management APIs, allowing AI assistants to:
- Create, read, update, and delete tasks
- Manage task priorities and statuses
- Handle task notifications
- Maintain secure, user-specific task lists through API key authentication

## Features

- **Full CRUD Operations**: Complete task lifecycle management
- **Rich Task Attributes**: Status, priority, descriptions, and notification settings
- **Secure Multi-tenancy**: API key-based authentication ensures data isolation
- **MCP Protocol Compliance**: Follows the Model Context Protocol specification
- **Async Architecture**: Built with Python async/await for optimal performance
- **Type Safety**: Comprehensive Pydantic models for data validation

## Installation

### Via pip

```bash
pip install task-management-mcp
```

### Via uv (recommended)

```bash
uv add task-management-mcp
```

### Via uvx (no installation needed)

Run the server directly without installing:

```bash
# Run with environment variable
TASK_API_KEY=your_api_key uvx task-management-mcp

# Or run interactively (will prompt for API key if not set)
uvx task-management-mcp
```

### Via pipx

```bash
pipx install task-management-mcp
```

### From source

```bash
git clone https://github.com/Aayush9029/mcp-server
cd mcp-server
uv sync
```

## Configuration

The MCP server connects to the Task Management API at `https://mcpclient.lovedoingthings.com`. The API key must be provided through your MCP client configuration.

### MCP Client Setup

Add this server to your MCP client configuration:

#### For Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "task-management": {
      "command": "uv",
      "args": ["run", "task-management-mcp"],
      "env": {
        "TASK_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

#### For other MCP clients

```json
{
  "mcpServers": {
    "task-management": {
      "command": "python",
      "args": ["-m", "task_management_mcp"],
      "env": {
        "TASK_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

## Usage

Once configured, the MCP server exposes the following tools to LLMs:

### Available Tools

#### `create_task`
Create a new task with specified details.

```typescript
{
  "title": "Complete project documentation",
  "description": "Write comprehensive README and API docs",
  "status": "TODO",  // TODO, IN_PROGRESS, DONE, CANCELLED
  "priority": "HIGH", // LOW, MEDIUM, HIGH, URGENT
  "notify": true
}
```

#### `list_tasks`
Retrieve all tasks for the authenticated user.

```typescript
// No parameters required
// Returns array of Task objects
```

#### `get_task`
Get details of a specific task by ID.

```typescript
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

#### `update_task`
Update an existing task (partial updates supported).

```typescript
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000",
  "title": "Updated title",
  "status": "IN_PROGRESS",
  "priority": "URGENT"
}
```

#### `delete_task`
Delete a task by ID.

```typescript
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

### Example Interactions

Here are some example prompts you can use with an LLM that has access to this MCP server:

```
"Create a high-priority task to review pull requests"
"Show me all my pending tasks"
"Mark task 123 as completed"
"Update the project planning task to urgent priority"
"Delete all cancelled tasks"
```

## Development

### Setting up the development environment

```bash
# Clone the repository
git clone https://github.com/Aayush9029/mcp-server
cd mcp-server

# Install dependencies with uv
uv install

# Run the development server
uv run task-mcp

# Or run the module directly
uv run python -m task_management_mcp
```

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=. --cov-report=html

# Run specific test file
uv run pytest tests/test_server.py
```

### Project Structure

```
mcp-server/
├── server.py          # Main MCP server implementation
├── models.py          # Pydantic models for data validation
├── __init__.py        # Package initialization
├── pyproject.toml     # Project configuration
├── tests/             # Test suite
│   ├── test_server.py
│   └── test_models.py
└── .github/
    └── workflows/     # CI/CD pipelines
```

### Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m '✨ Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

### Code Style

This project uses:
- Black for code formatting
- isort for import sorting
- mypy for type checking
- ruff for linting

Run all checks:
```bash
uv run black .
uv run isort .
uv run mypy .
uv run ruff check .
```

## API Documentation

### Task Model

```python
class Task:
    id: str                    # UUID
    api_key_id: str           # User identifier
    title: str                # Task title
    description: str | None   # Optional description
    status: TaskStatus        # TODO, IN_PROGRESS, DONE, CANCELLED
    priority: TaskPriority    # LOW, MEDIUM, HIGH, URGENT
    notify: bool              # Notification preference
    created_at: datetime      # Creation timestamp
    updated_at: datetime      # Last update timestamp
```

### Error Handling

The server implements comprehensive error handling:

- `400 Bad Request`: Invalid input data
- `401 Unauthorized`: Missing or invalid API key
- `404 Not Found`: Task not found
- `500 Internal Server Error`: Server-side errors

All errors return descriptive messages to help LLMs provide better user feedback.

## Security

- **API Key Authentication**: All requests require a valid API key
- **Data Isolation**: Tasks are scoped to individual API keys
- **Input Validation**: Comprehensive validation using Pydantic
- **Error Sanitization**: Error messages don't leak sensitive information

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Author

Created by [Aayush Pokharel](https://aayush.art)

- GitHub: [@Aayush9029](https://github.com/Aayush9029)
- Twitter: [@aayushbuilds](https://x.com/aayushbuilds)
- Email: [developer@lovedoingthings.com](mailto:developer@lovedoingthings.com)

## Acknowledgments

- Built on the [Model Context Protocol](https://modelcontextprotocol.io/)
- Uses [httpx](https://www.python-httpx.org/) for async HTTP
- Data validation by [Pydantic](https://pydantic-docs.helpmanual.io/)

## Support

For issues, questions, or contributions, please visit the [GitHub repository](https://github.com/Aayush9029/mcp-server).