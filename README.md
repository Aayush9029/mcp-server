# Task Management MCP Server

<a href="https://glama.ai/mcp/servers/vktfj0m5y0"><img width="380" height="200" src="https://glama.ai/mcp/servers/vktfj0m5y0/badge" alt="Task Management API Server MCP server" /></a>

[![PyPI version](https://badge.fury.io/py/task-mcp.svg)](https://badge.fury.io/py/task-mcp)
[![Python](https://img.shields.io/pypi/pyversions/task-mcp.svg)](https://pypi.org/project/task-mcp/)
[![Test](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml/badge.svg)](https://github.com/Aayush9029/mcp-server/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub](https://img.shields.io/github/stars/Aayush9029/mcp-server?style=social)](https://github.com/Aayush9029/mcp-server)

A Model Context Protocol (MCP) server that enables Large Language Models (LLMs) to interact with task management systems through a standardized protocol. This server uses stdio transport for communication, making it compatible with Claude Desktop, Cursor, and other MCP clients.

## Overview

This MCP server provides a bridge between LLMs and task management APIs, allowing AI assistants to:
- Create, read, update, and delete tasks
- Manage task priorities and statuses
- Handle task notifications
- Maintain secure, user-specific task lists through API key authentication

## Features

- **Full CRUD Operations**: Complete task lifecycle management
- **Rich Task Attributes**: Status (TODO, IN_PROGRESS, DONE, CANCELLED), priority levels (LOW, MEDIUM, HIGH, URGENT)
- **Notification Support**: Toggle notifications for individual tasks
- **Secure Multi-tenancy**: API key-based authentication ensures data isolation
- **MCP Protocol Compliance**: Follows the Model Context Protocol specification with stdio transport
- **Async Architecture**: Built with Python async/await for optimal performance
- **Type Safety**: Comprehensive Pydantic models for data validation
- **Filtering & Pagination**: List tasks with status/priority filtering and pagination support

## Installation

### Via pip

```bash
pip install task-mcp
```

### Via uv (recommended)

```bash
uv add task-mcp
```

### Via uvx (recommended - no installation needed)

Run the server directly without installing:

```bash
# Run with API key from environment
TASK_API_KEY=your_api_key uvx task-mcp

# Or pass API key as argument
uvx task-mcp --api-key YOUR_API_KEY

# View help and options
uvx task-mcp -h
```

### Via pipx

```bash
pipx install task-mcp
```

### From source

```bash
git clone https://github.com/Aayush9029/mcp-server
cd mcp-server
uv sync
```

## Configuration

The MCP server connects to the Task Management API at `https://mcpclient.lovedoingthings.com`. The server communicates via stdin/stdout using the MCP protocol.

### Command Line Options

```bash
task-mcp [OPTIONS]

Options:
  --api-key TEXT                  API key for authentication (can also be set via TASK_API_KEY env var)
  --log-level [DEBUG|INFO|WARNING|ERROR|CRITICAL]
                                  Set the logging level (default: INFO)
  -h, --help                      Show this message and exit
```

### Environment Variables

- `TASK_API_KEY`: API key for authentication (alternative to --api-key flag)

### MCP Client Setup

Add this server to your MCP client configuration:

#### For Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "task-management": {
      "command": "uvx",
      "args": ["task-mcp"],
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
      "args": ["-m", "task_mcp"],
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

**Parameters:**
- `title` (required): Task title
- `description`: Task description (optional)
- `status`: Task status (TODO, IN_PROGRESS, DONE, CANCELLED) - defaults to TODO
- `priority`: Task priority (LOW, MEDIUM, HIGH, URGENT) - defaults to MEDIUM
- `notify`: Whether to send notifications (boolean) - defaults to true

**Example:**
```json
{
  "title": "Complete project documentation",
  "description": "Write comprehensive README and API docs",
  "status": "TODO",
  "priority": "HIGH",
  "notify": true
}
```

#### `list_tasks`
List all tasks with optional filtering and pagination.

**Parameters:**
- `status`: Filter by status (TODO, IN_PROGRESS, DONE, CANCELLED)
- `priority`: Filter by priority (LOW, MEDIUM, HIGH, URGENT)
- `limit`: Maximum number of tasks to return (1-100, default 20)
- `offset`: Number of tasks to skip (default 0)

**Example:**
```json
{
  "status": "TODO",
  "priority": "HIGH",
  "limit": 10
}
```

#### `get_task`
Get details of a specific task by ID.

**Parameters:**
- `task_id` (required): Task ID

**Example:**
```json
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

#### `update_task`
Update an existing task's properties.

**Parameters:**
- `task_id` (required): Task ID
- `title`: New task title
- `description`: New task description
- `status`: New task status (TODO, IN_PROGRESS, DONE, CANCELLED)
- `priority`: New task priority (LOW, MEDIUM, HIGH, URGENT)
- `notify`: Whether to send notifications (boolean)

**Example:**
```json
{
  "task_id": "123e4567-e89b-12d3-a456-426614174000",
  "title": "Updated title",
  "status": "IN_PROGRESS",
  "priority": "HIGH",
  "notify": false
}
```

#### `delete_task`
Delete a task by ID.

**Parameters:**
- `task_id` (required): Task ID to delete

**Example:**
```json
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
uv sync

# Run the development server
uv run task-mcp --api-key YOUR_API_KEY

# Or run the module directly
uv run python -m task_mcp --api-key YOUR_API_KEY
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
├── task_mcp/          # Main package directory
│   ├── __init__.py    # Package initialization
│   ├── __main__.py    # CLI entry point
│   ├── server.py      # MCP server implementation (stdio transport)
│   └── models.py      # Pydantic models for data validation
├── tests/             # Test suite
│   ├── __init__.py
│   └── test_basic.py
├── pyproject.toml     # Project configuration
├── README.md          # Project documentation
├── LICENSE            # MIT License
├── __main__.py        # Package entry point
└── build_binary.py    # Script for building standalone binaries
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
    title: str                # Task title
    description: str          # Task description
    status: TaskStatus        # TODO, IN_PROGRESS, DONE, CANCELLED
    priority: TaskPriority    # LOW, MEDIUM, HIGH, URGENT
    notify: bool              # Notification preference
    created_by: str           # API key identifier
    created_at: float         # Creation timestamp (Unix timestamp)
    last_updated_at: float    # Last update timestamp (Unix timestamp)
```

### Building Binaries

You can build standalone executables for distribution:

```bash
# Install PyInstaller
uv pip install pyinstaller

# Build the binary
uv run python build_binary.py

# The binary will be created in dist/
# For macOS: dist/task-mcp-darwin-x86_64
# For Linux: dist/task-mcp-linux-x86_64
# For Windows: dist/task-mcp-windows-x86_64.exe
```

The binary can be run directly:
```bash
./dist/task-mcp-darwin-x86_64 --api-key YOUR_API_KEY
```

### Error Handling

The server implements comprehensive error handling:

- **Invalid input**: Returns error messages for malformed requests
- **Authentication errors**: Clear messages when API key is missing or invalid
- **API errors**: Descriptive error messages from the backend API
- **Network errors**: Handles connection issues gracefully

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

## Acknowledgments

- Built on the [Model Context Protocol](https://modelcontextprotocol.io/)
- Uses [httpx](https://www.python-httpx.org/) for async HTTP
- Data validation by [Pydantic](https://pydantic-docs.helpmanual.io/)

## Support

For issues, questions, or contributions, please visit the [GitHub repository](https://github.com/Aayush9029/mcp-server).
