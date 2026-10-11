# MCP

`POST /mcp` is the endpoint for AI agents. It implements the Model Context Protocol, revision `2026-07-28`, on the Streamable HTTP transport. It also serves clients that start with the `initialize` handshake of an earlier revision.

The server is stateless. It has no sessions, and each request carries its own API key. The server keeps the `clientInfo` of the most recent request as `last_client` of the key.

A client can also send the header `X-Relaydeck-Host` with the short host name of its machine. See [Host name](overview.md#host-name). Each request has one entry in the [activity log](rest-api.md#activity-log) of its key.

## Connect

See [Quickstart](quickstart.md) for the configuration of each agent.

| Request | Result |
|---------|--------|
| No API key, or a wrong one | `401` with the error body of the [REST API](overview.md#errors) |
| An account token | `403` |
| `GET`, `DELETE`, or `PUT` | `405`. The server has no `subscriptions/listen` stream, because its lists do not change. |
| An `Origin` header that is not the origin of the server | `403` |

## Server instructions

The server gives each agent these rules:

1. Create a task before you start a unit of work that takes more than a few minutes.
2. Set `status` to `in_progress` when you start, and keep `status_note` and `progress` current.
3. Set `status` to `done` when the work is complete and verified.
4. Use `notify_user` only when the person must act or must know now.
5. Use `ask_user` when a decision belongs to the person, and do not guess the answer.
6. Call `check_inbox` when you start and after each unit of work. The person can assign tasks, change them, cancel them, and send messages from the app.
7. A `task_changed` item with a `cancelled` status means: stop that work.

## Tools

`tools/list` returns the tools in this order. Each tool has an `outputSchema`. Each result has `structuredContent` and a short text summary.

| Tool | Arguments | Structured result | Annotations |
|------|-----------|-------------------|-------------|
| `create_task` | `title`, `description?`, `priority?`, `status?`, `notify?` | Task | not read-only |
| `list_tasks` | `status?`, `priority?`, `limit?` (default 50), `cursor?` | `{"tasks": [Task], "next_cursor"}` | read-only |
| `get_task` | `task_id` | Task | read-only |
| `update_task` | `task_id`, `title?`, `description?`, `status?`, `priority?`, `notify?`, `progress?`, `status_note?` | Task | idempotent |
| `delete_task` | `task_id` | `{"deleted": true, "task_id"}` | destructive |
| `notify_user` | `title`, `body`, `task_id?` | `{"sent": true, "devices": 2}` | not read-only |
| `ask_user` | `prompt`, `kind?` (default `confirm`), `options?`, `task_id?`, `expires_in?` (default 3600), `wait_seconds?` (default 30) | Question | not read-only |
| `get_answer` | `question_id`, `wait_seconds?` (default 30) | Question | read-only |
| `check_inbox` | `wait_seconds?` (default 0), `limit?` (default 20), `mark_read?` (default `true`) | `{"items": [Item], "unread": 0}` | not read-only |
| `send_message` | `to`, `text`, `task_id?` | Item | not read-only |
| `list_agents` | none | `{"agents": [Agent]}` | read-only |

The Task and the Question are the objects of the [REST API](rest-api.md#objects). A Task in a tool result has three more fields: `created_at_iso`, `updated_at_iso`, and `completed_at_iso`. They are ISO 8601 text, because a model reads that better than a number.

A tool error returns `isError: true` with a message that tells the model how to correct the call.

### Task tools

- `list_tasks`: `status` and `priority` are arrays. One string with commas between the values is also accepted. `limit` is 1 to 500. If `next_cursor` is not `null`, call again with it as `cursor`.
- `update_task`: an argument that is `null` or not sent does not change the field. For `progress` and `status_note`, a `null` that the client sends clears the field.
- `update_task`: `status` `done` sets `progress` to 100.

### Notify the person

`notify_user` sends one notification to each device of the account.

- The result is `{"sent": false, "devices": 0}` if no device of the account has a push token, or if push is off on the server.
- The notification goes out after the tool returns. Thus `sent: true` means that the server queued it.
- A call above the hourly limit gives a tool error.

### Ask the person

`ask_user` makes a question and holds the call until the person answers. `get_answer` reads a question and can hold the call again. The arguments of `ask_user` are the fields of [Ask a question](rest-api.md#ask-a-question).

Both tools return as soon as the `status` of the question is not `pending`, or after `wait_seconds` (0 to 50).

| `status` in the result | What the agent does |
|------------------------|---------------------|
| `answered` | Use `answer`. |
| `pending` | Call `get_answer` again, or continue with other work. Do not guess the answer. |
| `expired` | The person did not answer in time. Do not guess the answer. |
| `cancelled` | The question was cancelled, or its task was deleted. |

A key can have 20 pending questions. One more `ask_user` gives a tool error.

A key can have 32 calls that wait at the same time on one server process: `ask_user`, `get_answer`, `check_inbox`, and the REST requests that wait. One more `get_answer` or `check_inbox` with a `wait_seconds` above 0 gives a tool error. `ask_user` still asks the question and returns it as `pending`.

### Read the inbox

`check_inbox` gives the agent what the person and other agents sent to its key: tasks that the person assigned, changed, or deleted in the app, answers, and messages. `send_message` puts a message into the inbox of a different agent of the same account. `list_agents` gives the keys of the account. See [Inbox](inbox.md) for the Item, the Agent, and the rules.

- `check_inbox` holds the call for up to `wait_seconds` (0 to 50) and returns as soon as the inbox has an unread item.
- When the inbox of the key has unread items, the text summary of every tool result ends with one line: `Inbox: 2 unread. Call check_inbox.` The structured result does not change.
- A tool result that gives a task to the agent sets the `seen_at` of that task.

## Resources

| URI | Content |
|-----|---------|
| `tasks://open` | The `todo` and `in_progress` tasks of the key, as JSON: `{"tasks": [Task]}` |
| `tasks://task/{task_id}` | One task, as JSON |

## Prompts

| Name | Arguments | Purpose |
|------|-----------|---------|
| `plan_work` | `goal` | Divide a goal into tasks and create them |
| `status_report` | none | Summarize the open tasks and the recently closed tasks |
