# API Contract

This document is the single source of truth for the server and for the Apple clients. If the server and this document disagree, fix one of them in the same change.

One service gives all of these surfaces:

- A REST API for the Apple clients and for scripts (`/v1/...`).
- An MCP endpoint for AI agents (`/mcp`), on the MCP `2026-07-28` Streamable HTTP transport.
- A server-sent event stream for live updates (`/v1/events`).
- Static pages (`/`, `/privacy`, `/terms`, `/support`).

All surfaces use the same service layer and the same store. The MCP tools do not call the REST API over HTTP.

## Concepts

| Entity | Meaning |
|--------|---------|
| Account | One person. The first app launch creates it. No email and no password. |
| Device | One iPhone, iPad, or Apple Watch that belongs to an account. Holds the push tokens. |
| Key | One API key that an AI agent uses. Each key has a label, for example `Claude Code`. |
| Task | One unit of work. Each task belongs to exactly one key. |

An agent sees only the tasks of its own key. The account sees the tasks of all its keys.

## Conventions

- Requests and responses are JSON with `snake_case` fields.
- Timestamps are Unix seconds as a number with a fraction, for example `1760112000.412`.
- IDs are opaque strings with a prefix: `acc_`, `dev_` (the client makes this one), `key_`, `tsk_`. Clients must not parse them.
- Unknown request fields cause `422`. Unknown response fields must be ignored by clients.

### Authentication

Send one credential in the `Authorization` header:

| Credential | Format | Who has it | Scope |
|------------|--------|-----------|-------|
| Account token | `Bearer mca_<secret>` | The Apple clients | The full account |
| API key | `Bearer mck_<secret>` | AI agents and scripts | The tasks of that key |

`X-API-Key: mck_<secret>` is also accepted for API keys. The MCP endpoint also accepts `?key=mck_<secret>` for MCP clients that cannot send headers. The server never writes a credential to a log.

The server keeps only the SHA-256 hash of each secret. A secret is returned exactly one time, in the response that creates it.

### Errors

Every error uses this body, with a matching HTTP status:

```json
{ "error": { "code": "not_found", "message": "Task not found." } }
```

| Status | `code` |
|--------|--------|
| 400 | `bad_request` |
| 401 | `unauthorized` |
| 403 | `forbidden` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 409 | `conflict` |
| 422 | `validation_failed` (adds `error.fields`: a map of field name to message) |
| 429 | `rate_limited` (adds the `Retry-After` header) |
| 500 | `internal` |

A body that is not valid JSON gives `400`. A resource that belongs to a different key or account gives `404`, the same as a resource that does not exist.

Each credential has a request rate limit (default: 600 for each minute). `POST /v1/accounts` has a limit for each IP address (default: 20 for each hour). The operator can change the two values.

### Limits

| Item | Limit |
|------|-------|
| Keys for each account | 20 |
| Devices for each account | 20 |
| Open tasks (`todo` or `in_progress`) for each key | 1,000 |
| `title` | 1 to 200 characters |
| `description` | 0 to 20,000 characters, Markdown |
| `status_note` | 0 to 140 characters |
| `label` | 1 to 40 characters |
| `notify_user` calls for each key | 30 for each hour |
| `notify_user` `title` | 1 to 100 characters |
| `notify_user` `body` | 1 to 1,000 characters |

A request that goes above a count limit (keys, devices, open tasks) gets `409`.

The server makes `title`, `label`, and `status_note` one line: it removes space at the two ends and changes each run of white space to one space. An empty `status_note` is stored as `null`.

Closed tasks (`done` or `cancelled`) expire 90 days after their last update. The event stream sends `task.deleted` for an expired task.

## Objects

### Task

```json
{
  "id": "tsk_01jb2m9x4e7q",
  "key_id": "key_01jb2k3v9d",
  "title": "Add dark mode support",
  "description": "Implement a dark theme in all views.",
  "status": "in_progress",
  "priority": "high",
  "notify": true,
  "progress": 40,
  "status_note": "Updating the color tokens",
  "source": "agent",
  "created_at": 1760112000.412,
  "updated_at": 1760112300.9,
  "completed_at": null
}
```

- `status`: `todo`, `in_progress`, `done`, `cancelled`.
- `priority`: `low`, `medium`, `high`, `urgent`.
- `notify`: if `true`, a change by an agent sends a visible notification. If `false`, the devices update silently.
- `progress`: an integer from 0 to 100, or `null`.
- `status_note`: one short line that says what the agent does now, or `null`.
- `source`: `agent` if an API key created the task, `app` if the account created it.
- `completed_at`: set when `status` becomes `done`, cleared when it changes back.

### Key

```json
{
  "id": "key_01jb2k3v9d",
  "label": "Claude Code",
  "prefix": "mck_a1b2",
  "last4": "x9Qz",
  "created_at": 1760000000.0,
  "last_used_at": 1760112300.9,
  "last_client": { "name": "claude-code", "version": "2.1.290" },
  "counts": { "todo": 3, "in_progress": 1, "done": 12, "cancelled": 0 }
}
```

`last_client` comes from the MCP `clientInfo` of the most recent MCP request. It is `null` until an MCP client uses the key.

### Account

```json
{
  "id": "acc_01jb2j0p8c",
  "created_at": 1760000000.0,
  "settings": { "live_activities": true }
}
```

## REST API

### Health

`GET /health` returns `200 {"status":"ok","store":"ok"}`, or `503 {"status":"unavailable","store":"unavailable"}` if the store does not answer. No authentication.

### Accounts

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `POST` | `/v1/accounts` | none | `{}` | `201 {"account": Account, "token": "mca_..."}` |
| `GET` | `/v1/account` | account | | `200 Account` |
| `PATCH` | `/v1/account` | account | `{"settings": {"live_activities": false}}` | `200 Account` |
| `DELETE` | `/v1/account` | account | | `204`. Deletes the account, its devices, keys, and tasks. |

`POST /v1/accounts` has a strict rate limit for each IP address.

### Devices

| Method | Path | Auth | Result |
|--------|------|------|--------|
| `PUT` | `/v1/devices/{device_id}` | account | `200 Device`. Creates or replaces the device. |
| `DELETE` | `/v1/devices/{device_id}` | account | `204` |

`device_id` is a client-made string: `dev_` plus a lowercase UUID. Body of `PUT`:

```json
{
  "platform": "ios",
  "model": "iPhone18,1",
  "os_version": "27.0",
  "app_version": "1.0 (1)",
  "apns_environment": "production",
  "apns_token": "a1b2...",
  "push_to_start_token": "c3d4..."
}
```

- `platform`: `ios`, `ipados`, `watchos`.
- `apns_environment`: `production` or `sandbox`.
- `apns_token` and `push_to_start_token` are hexadecimal strings, or `null` if the device does not have them. A token that is not in the body is `null`.

The response repeats the stored fields and adds `id` and `updated_at`.

One APNs token belongs to one device registration. If a `PUT` has an `apns_token` that an older registration has (for example after the app is installed again), the server sets the two tokens of the older registration to `null`. If APNs reports that a token is not valid (`410`, or `BadDeviceToken`), the server sets that token to `null`.

### Keys

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `GET` | `/v1/keys` | account | | `200 {"keys": [Key]}`, newest first |
| `POST` | `/v1/keys` | account | `{"label": "Cursor"}` | `201 {"key": Key, "secret": "mck_..."}` |
| `PATCH` | `/v1/keys/{key_id}` | account | `{"label": "Cursor (work)"}` | `200 Key` |
| `DELETE` | `/v1/keys/{key_id}` | account | | `204`. Deletes the key and all its tasks. |

### Tasks

Both credential types can use these routes. An API key is limited to its own tasks.

| Method | Path | Result |
|--------|------|--------|
| `GET` | `/v1/tasks` | `200 {"tasks": [Task], "next_cursor": null}` |
| `POST` | `/v1/tasks` | `201 Task` |
| `GET` | `/v1/tasks/{task_id}` | `200 Task` |
| `PATCH` | `/v1/tasks/{task_id}` | `200 Task` |
| `DELETE` | `/v1/tasks/{task_id}` | `204` |

`GET /v1/tasks` query parameters:

| Name | Meaning |
|------|---------|
| `key_id` | Only this key. Account credential only. |
| `status` | One status, or several separated by commas. |
| `priority` | One priority, or several separated by commas. |
| `limit` | 1 to 500. Default 100. |
| `cursor` | The `next_cursor` of the previous page. |

Tasks are sorted by `updated_at`, newest first.

Continue to the next page while `next_cursor` is not `null`. If `priority` is set, a page can have fewer tasks than `limit`, or no tasks, and can still have a `next_cursor`.

`POST /v1/tasks` body. Only `title` is necessary. `key_id` is necessary with an account credential and not permitted with an API key. A `key_id` that breaks this rule gives `422` with `fields.key_id`. A `key_id` that the account does not have gives `404`.

```json
{
  "key_id": "key_01jb2k3v9d",
  "title": "Write unit tests",
  "description": "",
  "status": "todo",
  "priority": "medium",
  "notify": true
}
```

`PATCH /v1/tasks/{task_id}` accepts any subset of `title`, `description`, `status`, `priority`, `notify`, `progress`, `status_note`. A field that is not in the body does not change. `progress` and `status_note` accept `null`. If `status` becomes `done`, the server sets `progress` to `100`.

A `PATCH` that changes no field returns the task as it is. `updated_at` stays the same, and the server sends no event and no push.

### Live Activities

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `PUT` | `/v1/tasks/{task_id}/live-activity` | account | `{"device_id": "dev_...", "push_token": "e5f6..."}` | `204` |
| `DELETE` | `/v1/tasks/{task_id}/live-activity` | account | `{"device_id": "dev_..."}` | `204` |

The app calls `PUT` each time ActivityKit gives a new update token for the activity of that task. The task and the device must belong to the account, else the result is `404`. `DELETE` gives `204` also when the device has no registered activity. The server removes the registrations of a task after it sends `end`.

## Event stream

`GET /v1/events` with an account credential returns `text/event-stream`. Each event has a name and one line of JSON data.

| Event | Data |
|-------|------|
| `ready` | `{"account_id": "acc_..."}`. Sent one time when the stream opens. |
| `task.created` | `{"task": Task}` |
| `task.updated` | `{"task": Task}` |
| `task.deleted` | `{"task_id": "tsk_...", "key_id": "key_..."}` |
| `key.created` | `{"key": Key}` |
| `key.updated` | `{"key": Key}` |
| `key.deleted` | `{"key_id": "key_..."}` |
| `account.deleted` | `{}` |

The server sends a comment line (`: ping`) every 20 seconds. The stream does not replay old events. After each connect, the client gets the full lists again with `GET /v1/keys` and `GET /v1/tasks`.

`key.updated` is sent when the label changes and when `last_client` changes. It is not sent for each change of `last_used_at` or `counts`. The server closes the stream after `account.deleted`, and also when a client reads too slowly or the server loses its connection to the store. The client then connects again.

## Push notifications

The server sends pushes to every device of the account when an agent changes data. A change that a device makes does not send a visible notification. It sends the silent notification, so that the other devices of the account update.

`event` is `task.created`, `task.updated`, or `task.deleted`.

### Visible notification

Sent when the task has `notify: true`, and for each `notify_user` call.

A change of only `progress` or `status_note` is a progress tick. It sends the silent notification, also when `notify` is `true`, because an agent sends many of them.

```json
{
  "aps": {
    "alert": { "title": "Claude Code", "subtitle": "Task done", "body": "Add dark mode support" },
    "sound": "default",
    "thread-id": "key_01jb2k3v9d",
    "category": "TASK",
    "interruption-level": "active"
  },
  "event": "task.updated",
  "task_id": "tsk_01jb2m9x4e7q",
  "key_id": "key_01jb2k3v9d"
}
```

- `alert.title` is the key label.
- `alert.subtitle` is one of `New task`, `Task started`, `Task done`, `Task cancelled`, `Task updated`, `Task deleted`. A `notify_user` call uses the title that the agent gives.
- `interruption-level` is `time-sensitive` if the task priority is `urgent`.
- `category` is `TASK` for task events and `MESSAGE` for `notify_user`.
- `apns-collapse-id` is the task ID, thus a newer notification for the same task replaces the older one. A `notify_user` notification has no collapse ID.
- A `notify_user` notification has `"event": "message"` and the `key_id`. It has a `task_id` only if the agent gave one, and it is `time-sensitive` if that task is `urgent`.

### Silent notification

Sent when the task has `notify: false`.

```json
{ "aps": { "content-available": 1 }, "event": "task.updated", "task_id": "tsk_...", "key_id": "key_..." }
```

### Live Activity

The ActivityKit attributes type name is `TaskActivityAttributes`. The JSON keys below are exact, because ActivityKit decodes them with the default `JSONDecoder`.

Attributes:

```json
{ "taskID": "tsk_01jb2m9x4e7q", "keyLabel": "Claude Code" }
```

Content state:

```json
{ "title": "Add dark mode support", "status": "in_progress", "progress": 40, "note": "Updating the color tokens", "updatedAt": 1760112300.9 }
```

`progress` and `note` can be `null`.

Rules, when the account setting `live_activities` is `true`:

1. If an agent moves a task to `in_progress`, the server sends a push-to-start to each device that has a `push_to_start_token` and no registered activity for that task. This includes a task that an agent creates with the status `in_progress`.
2. If a task with a registered activity changes `title`, `status`, `progress`, or `status_note`, the server sends an `update`.
3. If the task becomes `done`, `cancelled`, or is deleted, the server sends `end` with the final content state and a dismissal date 15 minutes later. A deleted key ends the activities of its tasks.

Live Activity pushes use the topic `<bundle ID>.push-type.liveactivity`. The push-to-start payload also has an `alert` with the key label as `title` and the task title as `body`, because ActivityKit needs one.

## MCP endpoint

`POST /mcp` implements MCP `2026-07-28` over Streamable HTTP and also serves earlier protocol revisions. The server is stateless: it has no sessions and each request carries its own credential.

- A missing or wrong API key gives `401` with the error body of the REST API. An account token gives `403`.
- `GET` and `DELETE` give `405`. The server has no `subscriptions/listen` stream, because its lists do not change.
- A request with an `Origin` header that is not the origin of the server gives `403`.

Add the server to an MCP client:

```json
{
  "mcpServers": {
    "tasks": {
      "url": "https://HOST/mcp",
      "headers": { "Authorization": "Bearer mck_..." }
    }
  }
}
```

### Tools

`tools/list` returns the tools in this order. Each tool has an `outputSchema` and returns `structuredContent` together with a short text summary.

| Tool | Arguments | Structured result | Annotations |
|------|-----------|-------------------|-------------|
| `create_task` | `title`, `description?`, `priority?`, `status?`, `notify?` | `Task` | not read-only |
| `list_tasks` | `status?`, `priority?`, `limit?` (default 50), `cursor?` | `{"tasks": [Task], "next_cursor"}` | read-only |
| `get_task` | `task_id` | `Task` | read-only |
| `update_task` | `task_id`, `title?`, `description?`, `status?`, `priority?`, `notify?`, `progress?`, `status_note?` | `Task` | idempotent |
| `delete_task` | `task_id` | `{"deleted": true, "task_id"}` | destructive |
| `notify_user` | `title`, `body`, `task_id?` | `{"sent": true, "devices": 2}` | not read-only |

Tool results add `created_at_iso`, `updated_at_iso`, and `completed_at_iso` to each task, because a model reads ISO 8601 text better than a number.

A tool error returns `isError: true` with a message that tells the model how to correct the call.

- `list_tasks`: `status` and `priority` are arrays. One string with commas between the values is also accepted. `limit` is 1 to 500.
- `update_task`: an argument that is `null` or not sent does not change the field. For `progress` and `status_note`, a `null` that the client sends clears the field.
- `notify_user`: the result is `{"sent": false, "devices": 0}` if no device of the account has a push token, or if push is off on the server. The notification goes out after the tool returns, so `sent: true` means that the server queued it. A call above the hourly limit gives a tool error.

### Resources

| URI | Content |
|-----|---------|
| `tasks://open` | The `todo` and `in_progress` tasks of the key, as JSON |
| `tasks://task/{task_id}` | One task, as JSON |

### Prompts

| Name | Arguments | Purpose |
|------|-----------|---------|
| `plan_work` | `goal` | Divide a goal into tasks and create them |
| `status_report` | none | Summarize the open and recently closed tasks |

### Server instructions

The server tells each agent these rules:

1. Create a task before you start a unit of work that takes more than a few minutes.
2. Set `status` to `in_progress` when you start, and keep `status_note` and `progress` current.
3. Set `status` to `done` when the work is complete and verified.
4. Use `notify_user` only when the person must act or must know now.
