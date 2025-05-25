# Task Management MCP Server

[![PyPI version](https://badge.fury.io/py/task-mcp.svg)](https://badge.fury.io/py/task-mcp)
[![Python](https://img.shields.io/pypi/pyversions/task-mcp.svg)](https://pypi.org/project/task-mcp/)
[![Test](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml/badge.svg)](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub](https://img.shields.io/github/stars/Aayush9029/mcp-server?style=social)](https://github.com/Aayush9029/mcp-server)

A Model Context Protocol (MCP) server that provides LLMs with tools to manage tasks through a secure REST API. Built with FastMCP for seamless integration with Claude Desktop, Cursor, and other MCP clients.

## What is this?

This MCP server acts as a bridge between AI assistants and a task management API, enabling them to:
- ✅ Create, read, update, and delete tasks
- 🎯 Set priorities (LOW, MEDIUM, HIGH, URGENT) and track status (TODO, IN_PROGRESS, DONE, CANCELLED)
- 🔔 Manage task notifications
- 🔐 Maintain secure, isolated task lists via API key authentication

The server wraps around a FastAPI backend at `https://mcpclient.lovedoingthings.com` and exposes task management capabilities through the MCP protocol.

## Installation

### Quick Start with uvx (Recommended - No installation needed!)

```bash
# Run directly with API key from environment
TASK_API_KEY=your_api_key uvx task-mcp

# Or pass API key as argument
uvx task-mcp --api-key YOUR_API_KEY

# View help
uvx task-mcp -h
```

### Other Installation Methods

<details>
<summary>Via pip</summary>

```bash
pip install task-mcp
```
</details>

<details>
<summary>Via uv</summary>

```bash
uv add task-mcp
```
</details>

<details>
<summary>Via pipx</summary>

```bash
pipx install task-mcp
```
</details>

<details>
<summary>From source</summary>

```bash
git clone https://github.com/Aayush9029/mcp-server
cd mcp-server
uv sync
```
</details>

## Getting Started

### 1. Get an API Key

Contact the API provider to get your `TASK_API_KEY` for the task management service.

### 2. Configure Your MCP Client

#### For Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "task-manager": {
      "command": "uvx",
      "args": ["task-mcp"],
      "env": {
        "TASK_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

#### For Cursor or other MCP clients

```json
{
  "mcpServers": {
    "task-manager": {
      "command": "python",
      "args": ["-m", "task_mcp"],
      "env": {
        "TASK_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

### 3. Start Using It!

Once configured, you can ask your AI assistant to:
- "Create a high-priority task to review the pull request"
- "Show me all my pending tasks"
- "Mark task 123 as completed"
- "Update the project planning task to urgent priority"
- "Delete all cancelled tasks"

## Available Tools

### 📝 `create_task`
Create a new task with title, description, priority, and notification settings.

**Parameters:**
- `title` (required): Task title
- `description`: Detailed description (optional)
- `priority`: LOW, MEDIUM, HIGH, or URGENT (default: MEDIUM)
- `notify`: Enable notifications (default: true)

### 📋 `list_tasks`
List all your tasks with optional filtering.

**Parameters:**
- `status`: Filter by TODO, IN_PROGRESS, DONE, or CANCELLED
- `priority`: Filter by LOW, MEDIUM, HIGH, or URGENT
- `limit`: Max results 1-100 (default: 20)
- `offset`: Skip tasks for pagination (default: 0)

### 🔍 `get_task`
Get detailed information about a specific task.

**Parameters:**
- `task_id` (required): The task's unique identifier

### ✏️ `update_task`
Update any property of an existing task.

**Parameters:**
- `task_id` (required): The task to update
- `title`: New title
- `description`: New description
- `status`: New status
- `priority`: New priority
- `notify`: Update notification preference

### 🗑️ `delete_task`
Permanently delete a task.

**Parameters:**
- `task_id` (required): The task to delete

## Development

### Setting up for development

```bash
# Clone the repo
git clone https://github.com/Aayush9029/mcp-server
cd mcp-server

# Install dependencies
uv sync

# Run the server
uv run task-mcp --api-key YOUR_API_KEY
```

### Running tests

```bash
# Run all tests
uv run pytest

# With coverage
uv run pytest --cov=. --cov-report=html

# Run specific test
uv run pytest tests/test_server.py
```

### Code quality

```bash
# Format code
uv run black .

# Sort imports
uv run isort .

# Type checking
uv run mypy .

# Linting
uv run ruff check .
```

## Building Standalone Binaries

You can create platform-specific executables:

```bash
# Install PyInstaller
uv pip install pyinstaller

# Build binary
uv run python build_binary.py

# Run the binary
./dist/task-mcp-darwin-x86_64 --api-key YOUR_API_KEY
```

## API Details

### Base URL
The server connects to: `https://mcpclient.lovedoingthings.com/api`

### Authentication
All requests require an API key passed via the `X-API-Key` header. Tasks are isolated per API key - you can only see and modify your own tasks.

### Task Structure
```python
{
    "id": "uuid",                  # Unique identifier
    "title": "string",             # Task title
    "description": "string",       # Task description
    "status": "TODO",              # TODO, IN_PROGRESS, DONE, CANCELLED
    "priority": "MEDIUM",          # LOW, MEDIUM, HIGH, URGENT
    "notify": true,                # Notification preference
    "created_by": "api_key_hash",  # Owner identifier
    "created_at": 1234567890.0,    # Unix timestamp
    "last_updated_at": 1234567890.0 # Unix timestamp
}
```

## Security

- 🔐 **API Key Authentication**: All operations require a valid API key
- 🔒 **Data Isolation**: Tasks are strictly scoped to their creating API key
- ✅ **Input Validation**: Comprehensive validation using Pydantic models
- 🛡️ **Error Handling**: Safe error messages that don't leak sensitive data

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Author

Created by [Aayush Pokharel](https://aayush.art)
- GitHub: [@Aayush9029](https://github.com/Aayush9029)
- Twitter: [@aayushbuilds](https://x.com/aayushbuilds)

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m '✨ Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## Support

For issues, questions, or contributions, please visit the [GitHub repository](https://github.com/Aayush9029/mcp-server).