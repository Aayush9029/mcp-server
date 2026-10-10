# Relaydeck

Relaydeck shows the tasks of your AI agent on iPhone, iPad, and Apple Watch. This plugin connects Claude Code or Codex to the hosted Relaydeck MCP server at `https://relaydeck.onrender.com/mcp`.

The plugin adds one MCP server and no other component. The server gives the agent six tools: `create_task`, `list_tasks`, `get_task`, `update_task`, `delete_task`, and `notify_user`.

## Before you install

Create an API key in the Relaydeck iOS app. The key starts with `mck_`.

## Claude Code

Run these commands in a Claude Code session. Claude Code asks for the API key one time and keeps it in the secure credential store of your system.

```text
/plugin marketplace add Aayush9029/mcp-server
/plugin install relaydeck@relaydeck
```

## Codex

Codex reads the API key from the `TASKS_API_KEY` environment variable. Set the variable in your shell profile, then run these commands.

```bash
codex plugin marketplace add Aayush9029/mcp-server
codex plugin add relaydeck@relaydeck
```

## Data

The plugin sends your API key and the tool calls of the agent to `relaydeck.onrender.com`. It sends no other data and runs no local code.
