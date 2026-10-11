# Events

`GET /v1/events` with an account token returns a stream of server-sent events (`text/event-stream`). The app uses it to show each change at once.

## Connect

```http
GET /v1/events HTTP/1.1
Host: relaydeck.onrender.com
Authorization: Bearer mca_...
Accept: text/event-stream
```

Each event has a name and one line of JSON data.

```http
event: task.updated
data: {"task":{"id":"tsk_01jb2m9x4e7q","key_id":"key_01jb2k3v9d","title":"Add dark mode support","description":"","status":"in_progress","priority":"high","notify":true,"progress":40,"status_note":"Updating the color tokens","source":"agent","created_at":1760112000.412,"updated_at":1760112300.9,"completed_at":null}}
```

The server sends a comment line (`: ping`) after 20 seconds with no event, so that a proxy does not close the connection.

## Event types

| Event | Data | Sent when |
|-------|------|-----------|
| `ready` | `{"account_id": "acc_..."}` | One time, when the stream opens |
| `task.created` | `{"task": Task}` | A task is made |
| `task.updated` | `{"task": Task}` | A field of a task changes |
| `task.deleted` | `{"task_id": "tsk_...", "key_id": "key_..."}` | A task is deleted, or it expires |
| `key.created` | `{"key": Key}` | A key is made |
| `key.updated` | `{"key": Key}` | The label changes, or `last_client` changes |
| `key.deleted` | `{"key_id": "key_..."}` | A key is deleted. No event comes for its tasks and its questions. |
| `question.created` | `{"question": Question}` | An agent asks a question |
| `question.updated` | `{"question": Question}` | The status of a question changes |
| `account.deleted` | `{}` | The account is deleted |

The objects are those of the [REST API](rest-api.md#objects).

`key.updated` is not sent for each change of `last_used_at` or `counts`. Get the keys again when you need current counts.

## Reconnect

The stream does not replay old events. After each connect, wait for `ready`, then get the full lists again with `GET /v1/keys`, `GET /v1/tasks`, and `GET /v1/questions`. An event that arrives after `ready` is newer than those lists.

The server closes the stream in these cases. Connect again after each of them, except the first.

| Case | What the client sees |
|------|----------------------|
| The account is deleted | `account.deleted`, then the end of the stream |
| The client reads too slowly | The end of the stream |
| The server loses its connection to the store | The end of the stream |
| The server starts again | The end of the stream |

If the store is not available at the time of the connect, the response is `503`. Wait for the seconds in `Retry-After`, then connect again.
