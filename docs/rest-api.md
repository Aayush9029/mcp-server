# REST API

The Apple app and scripts use the routes under `/v1`. Read [Overview](overview.md) first for authentication, errors, and limits.

## Objects

### Account

```json
{
  "id": "acc_01jb2j0p8c",
  "created_at": 1760000000.0,
  "settings": { "live_activities": true }
}
```

### Device

```json
{
  "id": "dev_3f2b8c1e-5a4d-4e6f-9b7a-1c2d3e4f5a6b",
  "platform": "ios",
  "model": "iPhone18,1",
  "os_version": "27.0",
  "app_version": "1.0 (1)",
  "apns_environment": "production",
  "apns_token": "a1b2...",
  "push_to_start_token": "c3d4...",
  "widget_push_token": "e5f6...",
  "updated_at": 1760000000.0
}
```

| Field | Values |
|-------|--------|
| `id` | `dev_` plus a lowercase UUID. The client makes it. |
| `platform` | `ios`, `ipados`, `watchos` |
| `apns_environment` | `production`, `sandbox` |
| `apns_token` | The APNs device token, as hexadecimal text, or `null` |
| `push_to_start_token` | The ActivityKit push-to-start token, or `null` |
| `widget_push_token` | The WidgetKit push token, or `null` |

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

- `last_client` is the MCP `clientInfo` of the most recent MCP request. It is `null` until an MCP client uses the key.
- `last_used_at` changes one time in one minute at most.

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
  "completed_at": null,
  "seen_at": 1760112010.2
}
```

| Field | Values |
|-------|--------|
| `status` | `todo`, `in_progress`, `done`, `cancelled` |
| `priority` | `low`, `medium`, `high`, `urgent` |
| `notify` | If `true`, a change by an agent sends a visible notification. If `false`, the devices update silently. |
| `progress` | An integer from 0 to 100, or `null` |
| `status_note` | One short line that says what the agent does now, or `null` |
| `source` | `agent` if an API key made the task, `app` if the account made it |
| `completed_at` | Set when `status` becomes `done`. `null` when the status is a different one. |
| `seen_at` | When the agent of the key first got the task, or `null`. See [Inbox](inbox.md#task-field). |

### Question

```json
{
  "id": "qst_01jb2n4r8t",
  "key_id": "key_01jb2k3v9d",
  "task_id": "tsk_01jb2m9x4e7q",
  "prompt": "Deploy to production now?",
  "kind": "confirm",
  "options": ["Yes", "No"],
  "status": "pending",
  "answer": null,
  "created_at": 1760112400.0,
  "answered_at": null,
  "expires_at": 1760116000.0
}
```

| Field | Values |
|-------|--------|
| `kind` | `confirm`, `choice`, `text` |
| `options` | `["Yes", "No"]` for `confirm`. 2 to 4 labels from the agent for `choice`. `[]` for `text`. |
| `status` | `pending`, `answered`, `expired`, `cancelled` |
| `answer` | The selected option label, or the typed text. `null` until `status` is `answered`. |
| `task_id` | The task that the question is about, or `null` |
| `expires_at` | `created_at` plus `expires_in` |

A question leaves `pending` one time, and does not change after that.

| Event | New `status` |
|-------|--------------|
| The person answers | `answered` |
| `expires_at` passes | `expired`, within 10 seconds, and at once when a client reads or answers the question |
| The agent cancels the question, or its task is deleted | `cancelled` |

## Accounts

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `POST` | `/v1/accounts` | none | `{}` | `201 {"account": Account, "token": "mca_..."}` |
| `GET` | `/v1/account` | account | | `200 Account` |
| `PATCH` | `/v1/account` | account | `{"settings": {"live_activities": false}}` | `200 Account` |
| `DELETE` | `/v1/account` | account | | `204`. Deletes the account, its devices, keys, tasks, and questions. |

`POST /v1/accounts` has a strict rate limit for each IP address. A server that uses App Attest needs an `attestation` in the body, from `POST /v1/attest/challenge`. See [App integrity](app-integrity.md).

## Devices

| Method | Path | Auth | Result |
|--------|------|------|--------|
| `PUT` | `/v1/devices/{device_id}` | account | `200 Device`. Makes or replaces the device. |
| `DELETE` | `/v1/devices/{device_id}` | account | `204` |

The body of `PUT` has the fields of the [Device](#device) without `id` and `updated_at`. The three tokens are optional.

- A token that is not in the body is `null`. Send all tokens that the device has in each `PUT`.
- The response repeats the stored fields and adds `id` and `updated_at`.
- One APNs token belongs to one device registration. If a `PUT` has an `apns_token` that an older registration has (for example after the app is installed again), the server sets all tokens of the older registration to `null`.
- If APNs reports that a token is not valid (`410`, or `BadDeviceToken`), the server sets that token to `null`.

## Keys

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `GET` | `/v1/keys` | account | | `200 {"keys": [Key]}`, newest first |
| `POST` | `/v1/keys` | account | `{"label": "Cursor"}` | `201 {"key": Key, "secret": "mck_..."}` |
| `PATCH` | `/v1/keys/{key_id}` | account | `{"label": "Cursor (work)"}` | `200 Key` |
| `DELETE` | `/v1/keys/{key_id}` | account | | `204`. Deletes the key, its tasks, its questions, and its inbox. |

A deleted key, or a deleted account, is unusable when the response comes: its credential is refused, and no route returns its data. The server removes the data itself in small steps after that, and finishes a deletion that stopped.

## Tasks

Both credential types can use these routes. An API key is limited to its own tasks.

| Method | Path | Result |
|--------|------|--------|
| `GET` | `/v1/tasks` | `200 {"tasks": [Task], "next_cursor": null}` |
| `POST` | `/v1/tasks` | `201 Task` |
| `GET` | `/v1/tasks/{task_id}` | `200 Task` |
| `PATCH` | `/v1/tasks/{task_id}` | `200 Task` |
| `DELETE` | `/v1/tasks/{task_id}` | `204`. The pending questions of the task become `cancelled`. |

### List tasks

| Query parameter | Meaning |
|-----------------|---------|
| `key_id` | Only this key. Account credential only. |
| `status` | One status, or several with commas between them |
| `priority` | One priority, or several with commas between them |
| `limit` | 1 to 500. The default is 100. |
| `cursor` | The `next_cursor` of the previous page |

Tasks are sorted by `updated_at`, newest first. Continue to the next page while `next_cursor` is not `null`. If `priority` is set, a page can have fewer tasks than `limit`, or no tasks, and can still have a `next_cursor`.

### Create a task

Only `title` is necessary.

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

| Credential | `key_id` |
|------------|----------|
| Account token | Necessary. `422` with `fields.key_id` if it is missing. `404` if the account does not have the key. |
| API key | Not permitted. `422` with `fields.key_id` if it is there. |

### Update a task

`PATCH` accepts any subset of `title`, `description`, `status`, `priority`, `notify`, `progress`, `status_note`.

- A field that is not in the body does not change.
- `progress` and `status_note` accept `null`. The other fields do not.
- If `status` becomes `done`, the server sets `progress` to `100`.
- A `PATCH` that changes no field returns the task as it is. `updated_at` stays the same, and the server sends no event and no push.

## Questions

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `GET` | `/v1/questions` | account or API key | | `200 {"questions": [Question]}` |
| `POST` | `/v1/questions` | API key | See below | `201 Question` |
| `GET` | `/v1/questions/{question_id}` | account or API key | | `200 Question` |
| `POST` | `/v1/questions/{question_id}/answer` | account | `{"answer": "Yes"}` | `200 Question` |
| `DELETE` | `/v1/questions/{question_id}` | API key | | `204`. Sets `cancelled`. |

### List questions

`GET /v1/questions` returns the newest questions first, 100 at most. The query parameter `status` has one status, or several with commas between them. The default is `pending`.

### Ask a question

Only `prompt` is necessary.

```json
{
  "prompt": "Which environment?",
  "kind": "choice",
  "options": ["Staging", "Production"],
  "task_id": "tsk_01jb2m9x4e7q",
  "expires_in": 3600
}
```

| Field | Rule |
|-------|------|
| `kind` | The default is `confirm`. |
| `options` | Necessary for `choice`: 2 to 4 different labels. Not permitted for `confirm` and `text`. |
| `task_id` | A task of the same key. `404` if the key does not have the task. |
| `expires_in` | 60 to 86,400 seconds. The default is 3,600. |

A key can have 20 pending questions. One more gives `409`.

### Wait for the answer

`GET /v1/questions/{question_id}?wait=30` holds the request until the status is not `pending`, for 30 seconds at most. `wait` is 0 to 50 seconds. The default is 0.

- The response is the Question in each case. Look at `status`.
- If `expires_at` comes before the end of the wait, the request returns at that time with the status `expired`.
- If the result is still `pending`, send the request again.
- A key can have 32 requests that wait at the same time on one server process, over this route and `GET /v1/inbox?wait=`. One more gets `429` with `Retry-After`. A request with `wait=0` is not counted.

### Answer a question

| Case | Result |
|------|--------|
| The question is `pending` and the answer is valid | `200` with the status `answered` |
| The question is not `pending` | `409` |
| The answer is not one of the options (`confirm`, `choice`), or is empty or too long (`text`) | `422` with `fields.answer` |

If two answers arrive at the same time, one gets `200` and the other gets `409`.

### Cancel a question

`DELETE` gives `204` for a question that is `pending` or already `cancelled`. It gives `409` for a question that is `answered` or `expired`.

## Inbox and messages

| Method | Path | Auth | Result |
|--------|------|------|--------|
| `GET` | `/v1/inbox` | API key | `200 {"items": [Item], "unread": 0}` |
| `POST` | `/v1/messages` | account or API key | `201 Item` |
| `GET` | `/v1/keys/{key_id}/inbox` | account | `200 {"items": [Item], "unread": 2}` |
| `GET` | `/v1/agents` | API key | `200 {"agents": [Agent]}` |

When the person creates, changes, or deletes a task of a key with the account token, or answers a question, the server adds an item to the inbox of that key. See [Inbox](inbox.md) for the objects, the query parameters, and the rules.

## Live Activities

| Method | Path | Auth | Body | Result |
|--------|------|------|------|--------|
| `PUT` | `/v1/tasks/{task_id}/live-activity` | account | `{"device_id": "dev_...", "push_token": "e5f6..."}` | `204` |
| `DELETE` | `/v1/tasks/{task_id}/live-activity` | account | `{"device_id": "dev_..."}` | `204` |

- The app calls `PUT` each time ActivityKit gives a new update token for the activity of that task.
- The task and the device must belong to the account, else the result is `404`.
- `DELETE` gives `204` also when the device has no registered activity.
- The server removes the registrations of a task after it sends `end`.

See [Push](push.md#live-activity) for the pushes that the server sends.
