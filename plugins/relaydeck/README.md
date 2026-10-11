# Relaydeck

Relaydeck shows the tasks of your AI agent on iPhone, iPad, and Apple Watch. This plugin connects Claude Code or Codex to the hosted Relaydeck MCP server at `https://relaydeck.onrender.com/mcp`.

The connection goes two ways. The agent reports its work to the app. The agent also gets what you send from the app: a new task, a change to a task, a cancelled task, an answer, or a message. Agents of the same account can send messages to each other.

The plugin has three parts:

- One MCP server with these tools: `create_task`, `list_tasks`, `get_task`, `update_task`, `delete_task`, `notify_user`, `ask_user`, `get_answer`, `check_inbox`, `send_message`, and `list_agents`.
- Hooks that bring the inbox to the agent. You do not type a prompt for this.
- Two skills: `inbox` shows the inbox, and `message` sends a message to a different agent.

## Before you install

Create an API key in the Relaydeck iOS app. The key starts with `mck_`. Use one key for each agent, because each key has its own inbox.

## Claude Code

Run these commands in a Claude Code session. Claude Code asks for the API key one time and keeps it in the secure credential store of your system.

```text
/plugin marketplace add Aayush9029/mcp-server
/plugin install relaydeck@relaydeck
```

The hooks start with the next session. The plugin was tested with Claude Code 2.1.296. The stop hook needs version 2.1.163 or later.

## Codex

Codex reads the API key from the `TASKS_API_KEY` environment variable. Set the variable in your shell profile, then run these commands. If you also set `RELAYDECK_HOST`, for example to the output of `hostname -s`, the app shows that name for the agent.

```bash
codex plugin marketplace add Aayush9029/mcp-server
codex plugin add relaydeck@relaydeck
```

Codex does not run the hooks of a plugin until you approve them. Start Codex, type `/hooks`, and trust the three Relaydeck hooks. Do this again after an update of the plugin. The plugin was tested with Codex CLI 0.162.0.

## How the inbox comes to the agent

| Moment | What the hook does | Claude Code | Codex |
|--------|--------------------|-------------|-------|
| A session starts | Puts the unread items into the context | `SessionStart` | `SessionStart` |
| You send a prompt | Puts the unread items into the context | `UserPromptSubmit` | `UserPromptSubmit` |
| The agent is about to stop | Gives the unread items to the agent, and the agent continues | `Stop` | `Stop` |
| The agent waits for you | Listens for a new item and wakes the agent | `SessionStart` and `Stop`, in the background | Not available |

Each hook runs `hooks/inbox.sh`. The script calls `GET /v1/inbox` with your API key. The items that the agent gets become read. The script needs `sh`, `curl`, and `awk`, and it has no other dependency.

- **No loop.** A stop hook does not continue the agent two times in a row, because it obeys `stop_hook_active`. It also remembers the items that a session got, so one item does not continue or wake the agent two times.
- **No delay when you are offline.** A request stops after 5 seconds. If the server does not answer, the hook prints nothing and the agent continues.
- **The wake in Claude Code.** When a session waits for you, one request stays open to the server. A new item wakes the agent, and the agent starts to work without a prompt. The plugin listens for 480 minutes after the last stop. To change this, run `/plugin configure relaydeck@relaydeck` and set the minutes. The value 0 turns the wake off. A run with `claude -p` has no wake.

## Other agents

The [installer](https://relaydeck.onrender.com/install) sets up the same script for Cursor and Gemini CLI, because they have hooks and no plugin from this repository.

| Agent | Hooks | Limits |
|-------|-------|--------|
| Cursor | `sessionStart` and `stop` in `~/.cursor/hooks.json` | Cursor has no hook that adds context to a prompt |
| Gemini CLI | `SessionStart`, `BeforeAgent`, and `AfterAgent` in `~/.gemini/settings.json` | None |

The Gemini CLI hooks were tested with Gemini CLI 0.29.7. The Cursor hooks follow the Cursor documentation, and they were not run in Cursor.

The other agents that the installer supports have no hook that can run this script. For them, the server gives the reminder: when the inbox has unread items, each tool result ends with the line `Inbox: 2 unread. Call check_inbox.`, and the server instructions tell the agent to call `check_inbox` when it starts and after each unit of work.

## Settings

| Name | Where | Meaning |
|------|-------|---------|
| `api_key` | Claude Code plugin option | The API key |
| `idle_watch_minutes` | Claude Code plugin option | Minutes to listen while the agent waits. Default 480. The value 0 turns the wake off. |
| `host_name` | Claude Code plugin option | The name of this machine in the app. The installer sets it to the short host name. |
| `TASKS_API_KEY` | Environment, Codex | The API key |
| `RELAYDECK_HOST` | Environment | The name of this machine in the app, for Codex and for the hooks. Without it, the hooks send the short host name. |
| `RELAYDECK_URL` | Environment | A different server for the hooks, for example `http://localhost:8000` |

## Data

The plugin sends your API key, the name of this machine, and the tool calls of the agent to `relaydeck.onrender.com`. The hook script sends your API key and the name of this machine to the same server and reads your inbox. It sends no other data.

The name of the machine goes in the header `X-Relaydeck-Host`. The app shows it, so you see where an agent runs. The name has only letters, digits, `.`, `_`, and `-`.

The script gives the key to `curl` through standard input. It does not print the key, and it does not write the key to a file. It keeps the IDs of the items that each session got in the data folder of the plugin, and it removes them after 7 days.
