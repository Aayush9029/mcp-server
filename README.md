> [!NOTE]
> The new iOS app, **Relaydeck**, is on its way to the App Store. The download link goes here when it is live.

# MCP Client for iOS

[![CI](https://github.com/Aayush9029/mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/Aayush9029/mcp-server/actions/workflows/ci.yml)
![MCP](https://img.shields.io/badge/MCP-2026--07--28-8b5cf6.svg)
![Python](https://img.shields.io/badge/python-3.13-blue.svg)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)

<p>

![image](https://github.com/user-attachments/assets/a655c5ca-97e9-4ec6-af5b-95b8ebf07d5e)

</p>

### Verified with
<img src="https://github.com/user-attachments/assets/c05449f1-4da8-4228-9bba-6821f70a3ab8" width="40px" />
<img src="https://github.com/user-attachments/assets/caa206db-bf97-4376-81c1-20d542d5963a" width="40px" />
<img src="https://github.com/user-attachments/assets/c2cbc255-af9b-4683-ba12-c36a4b1e6268" width="40px" />
<img src="https://github.com/user-attachments/assets/ec6c1c3d-8209-406f-986d-4fb1d76b401b" width="40px" />
<img src="https://github.com/user-attachments/assets/220a04be-f6f2-4a3d-b161-b961311d6775" width="40px" />
<img src="https://github.com/user-attachments/assets/69781fbe-5888-4cd0-98d8-be71dbb14e8c" width="40px" />

## Task Management MCP Server

Bridge AI assistants with real-world task management

This MCP server connects Claude Code, Cursor, Codex, and other AI tools to your iPhone, iPad, and Apple Watch. Your agents create, update, and track tasks, and your devices show each change live.

### What You Can Do

- Ask Claude to create tasks during coding sessions
- Let Cursor update task status as it completes work
- Get notifications on your iPhone when AI assistants finish tasks
- Follow work in progress with a Live Activity on the Lock Screen
- Answer your agent's questions from the notification
- Track progress across multiple AI tools and automation scripts

Perfect for developers who want their AI assistants to collaborate on real projects with automatic task tracking and mobile notifications.

## Quick Start

### 1. Get Your API Key

Open the iOS app and create a key for each agent.

### 2. Choose Your Setup

#### For Claude Code

```
/plugin marketplace add Aayush9029/mcp-server
/plugin install relaydeck@relaydeck
```

Claude Code asks for your API key one time and keeps it in the Keychain.

#### For Codex

```bash
export TASKS_API_KEY=YOUR_API_KEY
codex plugin marketplace add Aayush9029/mcp-server
codex plugin add relaydeck@relaydeck
```

#### For Cursor or Other MCP Clients

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

Replace `YOUR_API_KEY` with your actual API key.

### 3. Start Using It

Once configured, your AI assistant can:

- "Create a high-priority task to review the pull request"
- "Show me all pending tasks"
- "Mark the deployment task as completed"
- "Ping my phone when the migration is done"
- "Ask me before you deploy to production"

## Available Tools

<details>
<summary>Eight tools for your agents</summary>

| Tool | What it does |
|------|--------------|
| `create_task` | Create a task with title, description, priority, and notification settings |
| `list_tasks` | List tasks, with optional filters for status and priority |
| `get_task` | Get the details of one task |
| `update_task` | Change status, priority, progress, and the live status note |
| `delete_task` | Delete a task |
| `notify_user` | Send a push notification to your devices |
| `ask_user` | Ask you a yes-or-no, multiple-choice, or free-text question |
| `get_answer` | Wait for your answer to a question |

</details>

## Security & Privacy

- **API Key Isolation**: Each key maintains completely separate task data
- **No Cross-Access**: Tasks are never shared between different API keys
- **Hashed Credentials**: The server stores only a hash of each key
- **No Account Details**: No email, no password, no tracking
- **Real-time Updates**: Changes sync instantly to your devices

---

GitHub: [@Aayush9029](https://github.com/Aayush9029/mcp-server) • Twitter: [@aayushbuilds](https://twitter.com/aayushbuilds)
