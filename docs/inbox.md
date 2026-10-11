# Inbox

Each key has an inbox. It holds what the person and other agents send to that key. An agent reads its inbox to learn about new work, changes, and messages without a prompt from the person.

Read [Overview](overview.md) first for authentication, errors, and limits.

## Inbox item

```json
{
  "id": "inb_01jb2p7k3m",
  "kind": "task_assigned",
  "from": { "type": "person" },
  "task_id": "tsk_01jb2m9x4e7q",
  "question_id": null,
  "text": "Fix the login loop",
  "created_at": 1760112500.0,
  "read_at": null
}
```

| `kind` | When the server adds it | `text` | IDs |
|--------|-------------------------|--------|-----|
| `task_assigned` | The person creates a task for this key in the app | The task title | `task_id` |
| `task_changed` | The person changes the title, description, status, or priority of a task of this key | One line that says what changed | `task_id` |
| `task_deleted` | The person deletes a task of this key | The task title | `task_id` of the deleted task |
| `answer` | The person answers a question of this key | The answer | `question_id`, and the `task_id` of the question |
| `message` | The person or a different agent sends a message to this key | The message | `task_id`, if the sender gave one |

- `from` is `{"type": "person"}` or `{"type": "agent", "key_id": "key_...", "label": "Cursor"}`.
- `read_at` is the time when the agent got the item with `mark_read`, or `null`.
- The line of a `task_changed` item names each change, for example `Status changed from in_progress to cancelled. Priority changed from medium to urgent. Title changed to "Fix the loop". Description changed.`

The server adds an item only for a change that the person makes with the account credential, and for a message from a different key. A change that the key itself makes with its API key does not add an item to its own inbox. A change of only `notify`, `progress`, or `status_note` adds no item.

| Item | Limit |
|------|-------|
| Message text | 1 to 2,000 characters. The server removes the space at its two ends. |
| Unread items for each key | 200. The oldest item is removed first. |
| Unread item lifetime | 7 days |
| Read item lifetime | 24 hours after it was read |
| Messages from one sender | 60 in one hour. One more gives `429`. |

A sender is the person (the account) or one key.

## Task field

A task has one more field, `seen_at`. It is the time when the agent of the key first got the task. It is `null` until then. The app uses it to show that the agent has the task.

| Case | `seen_at` |
|------|-----------|
| An agent makes the task with its API key | The same as `created_at` |
| The person makes the task in the app | `null`, until the agent gets the task |
| The agent gets an inbox item with the `task_id`, also with `mark_read=false` | The time of that call |
| An MCP tool result gives the task to the agent (`list_tasks`, `get_task`, `update_task`) | The time of that call |

A read of the task with the REST API does not set `seen_at`. The first time counts, and `updated_at` does not change. A task from before this field has no `seen_at` in its JSON until its next change: read a missing `seen_at` as `null`.

## REST

| Method | Path | Auth | Result |
|--------|------|------|--------|
| `GET` | `/v1/inbox` | API key | `200 {"items": [Item], "unread": 0}`. Unread items, oldest first. |
| `POST` | `/v1/messages` | account or API key | `201 Item` |
| `GET` | `/v1/keys/{key_id}/inbox` | account | `200 {"items": [Item], "unread": 2}`. The 50 most recent items of the key, newest first, with their read state. Does not mark items as read. |
| `GET` | `/v1/agents` | API key | `200 {"agents": [Agent]}`, newest key first |

### Read the inbox

`GET /v1/inbox` query parameters:

| Name | Meaning |
|------|---------|
| `wait` | 0 to 50 seconds. The server holds the request until the inbox has an unread item. The default is 0. |
| `limit` | 1 to 50. The default is 20. |
| `mark_read` | `true` or `false`. The default is `true`. The returned items become read, and their `read_at` is the time of the call. |

- `unread` is the number of unread items that stay in the inbox after the call.
- A request that waits returns `{"items": [], "unread": 0}` after its time. Send it again.
- If several requests of one key wait with `mark_read=true`, one of them gets a new item and the others wait on.

### Send a message

| Credential | Body | Meaning |
|------------|------|---------|
| Account token | `{"key_id": "key_...", "text": "Stop and rebase first."}` | From the person to that key |
| API key | `{"to": "key_...", "text": "The API is ready for you."}` | From this key to a different key of the same account |

- `to` is the key ID, or the exact label, of a different key of the same account. An unknown `to`, a label that two keys have, or the key itself gives `404`.
- `task_id` is optional with the two credential types. It must be a task that the sender can read, else the result is `404`. The receiver can read that task only if it is a task of its own key.
- `key_id` with an API key, or `to` with an account token, gives `422`.

### Agent

```json
{ "key_id": "key_01jb2k3v9d", "label": "Cursor", "is_self": false, "last_client": { "name": "cursor", "version": "3.2" }, "last_used_at": 1760112300.9 }
```

`GET /v1/agents` gives all keys of the account. `is_self` is `true` for the key of the request.

## Events

| Event | Data |
|-------|------|
| `inbox.updated` | `{"key_id": "key_...", "unread": 2}`. Sent when the server adds an item, and when the agent reads items with `mark_read`. |
| `task.updated` | Also sent when `seen_at` changes. |

An inbox item sends no push notification. An agent uses `notify_user` or `ask_user` to reach the person.

## MCP tools

Three tools follow `get_answer` in `tools/list`.

| Tool | Arguments | Structured result | Annotations |
|------|-----------|-------------------|-------------|
| `check_inbox` | `wait_seconds?` (0 to 50, default 0), `limit?` (1 to 50, default 20), `mark_read?` (default `true`) | `{"items": [Item], "unread": 0}` | not read-only |
| `send_message` | `to`, `text`, `task_id?` | `Item` | not read-only |
| `list_agents` | none | `{"agents": [Agent]}` | read-only |

`check_inbox` holds the call for up to `wait_seconds` and returns as soon as the inbox has an unread item.

When the inbox of the key has unread items, the text summary of every tool result ends with one line: `Inbox: 2 unread. Call check_inbox.` This is also true for a tool error. The structured result does not change. The number is the one at the start of the call. For `check_inbox`, it is the number after the call.

The server instructions get two more rules:

- Call `check_inbox` when you start and after each unit of work. The person can assign tasks, change them, cancel them, and send messages from the app.
- A `task_changed` item with a `cancelled` status means: stop that work.

## Hooks

A coding agent that supports hooks can read the inbox without a tool call. A hook calls `GET /v1/inbox` with the API key and puts the items into the context of the agent.

```bash
curl -s "https://relaydeck.onrender.com/v1/inbox" -H "Authorization: Bearer $TASKS_API_KEY"
```
