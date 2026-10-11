# Quickstart

Connect an AI agent to Relaydeck. After that, the tasks of the agent show on your iPhone, iPad, and Apple Watch.

## Get an API key

1. Open the Relaydeck app.
2. Make an API key and give it a label, for example `Claude Code`. Use one key for each agent.
3. Copy the key. It starts with `mck_`.

## Install the plugin

The plugin adds the Relaydeck MCP server to the agent.

### Claude Code

Type these commands in Claude Code.

```bash
/plugin marketplace add Aayush9029/mcp-server
/plugin install relaydeck@relaydeck
```

Claude Code asks for the API key one time and keeps it in the Keychain.

### Codex

```bash
export TASKS_API_KEY=YOUR_API_KEY
codex plugin marketplace add Aayush9029/mcp-server
codex plugin add relaydeck@relaydeck
```

## Configure MCP manually

Use this if your agent has no plugin, or if you run your own server. The transport is Streamable HTTP. Replace `YOUR_API_KEY` with the key from the app.

### Claude Code

```bash
claude mcp add --transport http tasks https://relaydeck.onrender.com/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
```

### Codex

Put this in `~/.codex/config.toml`, then set `TASKS_API_KEY` in your shell.

```toml
[mcp_servers.tasks]
url = "https://relaydeck.onrender.com/mcp"
bearer_token_env_var = "TASKS_API_KEY"
```

### Cursor and other clients

```json
{
  "mcpServers": {
    "tasks": {
      "url": "https://relaydeck.onrender.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

A client that cannot send headers can use the URL `https://relaydeck.onrender.com/mcp?key=YOUR_API_KEY`.

## Try it

Ask the agent: "Create a task for the database migration, keep its progress current, and ask me before you deploy."

The agent uses the tools in [MCP](mcp.md). You see the task in the app, and you answer the question from the notification.

## Use the REST API from a script

An API key also works with the REST API.

```bash
curl https://relaydeck.onrender.com/v1/tasks \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title": "Write the release notes", "priority": "high"}'
```

The response is the new [Task](rest-api.md#task). See [REST API](rest-api.md) for all routes.
