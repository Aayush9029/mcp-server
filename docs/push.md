# Push

The server sends pushes through the Apple Push Notification service (APNs) to the devices of an account. A device registers its tokens with `PUT /v1/devices/{device_id}`. See [REST API](rest-api.md#devices).

## Overview of the pushes

| Push | `apns-push-type` | Token | Topic |
|------|------------------|-------|-------|
| Visible notification | `alert` | `apns_token` | The bundle ID |
| Silent notification | `background` | `apns_token` | The bundle ID |
| Live Activity start | `liveactivity` | `push_to_start_token` | The bundle ID plus `.push-type.liveactivity` |
| Live Activity update and end | `liveactivity` | The token from `PUT /v1/tasks/{task_id}/live-activity` | The bundle ID plus `.push-type.liveactivity` |
| Widget reload | `widgets` | `widget_push_token` | The bundle ID plus `.push-type.widgets` |

A device with the platform `watchos` gets the topic of the watch app, if the server has a bundle ID for it.

The server sends a push after it sends the HTTP response. A push does not make a request slower.

## Who gets which notification

| Change | Notification |
|--------|--------------|
| An agent creates, updates, or deletes a task with `notify: true` | Visible |
| An agent changes only `progress` or `status_note` | Silent. This is a progress tick, and an agent sends many of them. |
| An agent changes a task with `notify: false` | Silent |
| The app (an account token) changes a task | Silent, so that the other devices of the account update |
| An agent calls `notify_user` | Visible |
| An agent asks a question | Visible |
| The status of a question changes | Silent |

Each notification goes to each device of the account that has an `apns_token`.

## Task notifications

`event` is `task.created`, `task.updated`, or `task.deleted`.

### Visible

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

| Field | Value |
|-------|-------|
| `alert.title` | The label of the key |
| `alert.subtitle` | `New task`, `Task started`, `Task done`, `Task cancelled`, `Task updated`, or `Task deleted` |
| `alert.body` | The title of the task |
| `interruption-level` | `time-sensitive` if the priority of the task is `urgent`, else `active` |
| `apns-collapse-id` header | The task ID. A newer notification for the same task replaces the older one. |

### Silent

```json
{ "aps": { "content-available": 1 }, "event": "task.updated", "task_id": "tsk_...", "key_id": "key_..." }
```

## Message notifications

A `notify_user` call sends this notification. It has no collapse ID.

```json
{
  "aps": {
    "alert": { "title": "Claude Code", "subtitle": "Approve the deploy", "body": "The build is ready." },
    "sound": "default",
    "thread-id": "key_01jb2k3v9d",
    "category": "MESSAGE",
    "interruption-level": "active"
  },
  "event": "message",
  "key_id": "key_01jb2k3v9d",
  "task_id": "tsk_01jb2m9x4e7q"
}
```

- `alert.subtitle` is the title that the agent gives. `alert.body` is the body that the agent gives.
- `task_id` is there only if the agent gave one. If that task is `urgent`, `interruption-level` is `time-sensitive`.

## Question notifications

### New question

```json
{
  "aps": {
    "alert": { "title": "Claude Code", "subtitle": "Question", "body": "Deploy to production now?" },
    "sound": "default",
    "thread-id": "key_01jb2k3v9d",
    "category": "QUESTION_CONFIRM",
    "interruption-level": "time-sensitive",
    "mutable-content": 1
  },
  "event": "question.created",
  "question_id": "qst_01jb2n4r8t",
  "kind": "confirm",
  "options": ["Yes", "No"],
  "task_id": "tsk_01jb2m9x4e7q",
  "key_id": "key_01jb2k3v9d"
}
```

| Field | Value |
|-------|-------|
| `category` | `QUESTION_CONFIRM`, `QUESTION_CHOICE`, or `QUESTION_TEXT`, from the `kind` of the question |
| `options` | The options of the question. `[]` for the kind `text`. |
| `task_id` | The task of the question, or `null` |
| `apns-collapse-id` header | The question ID |
| `apns-expiration` header | The `expires_at` of the question. APNs does not deliver the notification after that time. |

The app answers with `POST /v1/questions/{question_id}/answer`. See [REST API](rest-api.md#answer-a-question).

### Status change

When a question is answered, expired, or cancelled, the server sends a silent notification with the same collapse ID. Each device then removes the delivered notification of that question.

```json
{ "aps": { "content-available": 1 }, "event": "question.updated", "question_id": "qst_...", "key_id": "key_..." }
```

## Live Activity

The ActivityKit attributes type name is `TaskActivityAttributes`. The JSON keys below are exact, because ActivityKit decodes them with the default `JSONDecoder`.

Attributes:

```json
{ "taskID": "tsk_01jb2m9x4e7q", "keyLabel": "Claude Code" }
```

Content state:

```json
{
  "title": "Add dark mode support",
  "status": "in_progress",
  "progress": 40,
  "note": "Waiting for you",
  "updatedAt": 1760112300.9,
  "question": { "id": "qst_01jb2n4r8t", "prompt": "Deploy to production now?", "kind": "confirm", "options": ["Yes", "No"] }
}
```

- `progress` and `note` can be `null`.
- `question` is the oldest pending question of the task. It is `null` when the task has no pending question.

The server sends these pushes when the account setting `live_activities` is `true`.

| Case | Push |
|------|------|
| An agent moves a task to `in_progress`, or creates it with that status | `start` to each device that has a `push_to_start_token` and no registered activity for that task |
| A task with a registered activity changes `title`, `status`, `progress`, or `status_note` | `update` |
| A question for a task with a registered activity is created, answered, expired, or cancelled | `update` |
| The task becomes `done` or `cancelled`, or the task or its key is deleted | `end`, with the final content state and a dismissal date 15 minutes later |

Each payload has `aps.timestamp`, `aps.event` (`start`, `update`, or `end`), and `aps.content-state`. A `start` also has `attributes-type`, `attributes`, and an `alert` with the key label as `title` and the task title as `body`, because ActivityKit needs one. An `end` also has `dismissal-date`.

## Widget reload

A device that registers a `widget_push_token` gets a WidgetKit push when a task or a question of the account changes. WidgetKit then reloads the widget timelines. The push carries no task data.

```json
{ "aps": { "content-changed": true } }
```

- A device gets one widget push in 15 seconds at most.
- If more changes come in those 15 seconds, the device gets one more push when the 15 seconds end. Thus the last change is not lost.
- The system also limits how often it delivers these pushes.

## Tokens that are not valid

If APNs answers `410`, or `400` with the reason `BadDeviceToken`, the server sets that token of the device to `null`, or removes that Live Activity registration. The app registers a new token with the next `PUT`.
