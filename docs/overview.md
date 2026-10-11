# Overview

Relaydeck is one service for AI agents and for the Apple app. An agent reports its work as tasks and asks the person questions. The app shows each change live, and sends answers, changes, and messages back to the agent.

These files are the contract between the server and its clients:

1. [Overview](overview.md): the rules that all surfaces share.
2. [Quickstart](quickstart.md): connect an agent.
3. [MCP](mcp.md): the tools of an agent.
4. [REST API](rest-api.md): the objects and the routes.
5. [Events](events.md): the live event stream.
6. [Inbox](inbox.md): what the person and other agents send to an agent.
7. [Push](push.md): notifications, Live Activities, and widget reloads.
8. [App integrity](app-integrity.md): App Attest and the app code.
9. [Self-hosting](self-hosting.md): run your own server.

## Surfaces

| Surface | Path | Client |
|---------|------|--------|
| MCP endpoint | `POST /mcp` | AI agents |
| REST API | `/v1/...` | The Apple app, and scripts |
| Event stream | `GET /v1/events` | The Apple app |
| Health | `GET /health` | Monitors |

A change through one surface shows on all the others.

## Concepts

| Entity | ID prefix | Meaning |
|--------|-----------|---------|
| Account | `acc_` | One person. The first launch of the app makes it. It has no email and no password. |
| Device | `dev_` | One iPhone, iPad, or Apple Watch of an account. It holds the push tokens. The client makes this ID. |
| Key | `key_` | One API key that an agent uses. Each key has a label, for example `Claude Code`. |
| Task | `tsk_` | One unit of work. Each task belongs to one key. |
| Question | `qst_` | One question from an agent to the person. Each question belongs to one key. |
| Inbox item | `inb_` | One thing that the person or a different agent sends to a key. See [Inbox](inbox.md). |

An agent sees only the tasks and the questions of its own key. The account sees those of all its keys.

## Conventions

- Requests and responses are JSON with `snake_case` fields.
- A timestamp is Unix seconds as a number with a fraction, for example `1760112000.412`.
- An ID is an opaque string. Do not parse it.
- An unknown field in a request gives `422`. Ignore unknown fields in a response, because new fields can come.

## Authentication

Send one credential in the `Authorization` header.

| Credential | Format | Who has it | Scope |
|------------|--------|------------|-------|
| Account token | `Bearer mca_<secret>` | The Apple app | The full account |
| API key | `Bearer mck_<secret>` | Agents and scripts | The tasks and the questions of that key |

- An API key is also accepted in the header `X-API-Key: mck_<secret>`.
- The MCP endpoint also accepts `?key=mck_<secret>`, for a client that cannot send headers. A URL can appear in logs, so use a header if you can.
- The server keeps only the SHA-256 hash of a secret, and does not log it. It returns a secret one time, in the response that makes it.
- A server can also ask the app for an app code. See [App integrity](app-integrity.md).

## Errors

Each error has this body and a matching HTTP status.

```json
{ "error": { "code": "not_found", "message": "Task not found." } }
```

| Status | `code` | Meaning |
|--------|--------|---------|
| 400 | `bad_request` | The body is not valid JSON. |
| 401 | `unauthorized` | The credential is missing or wrong. |
| 401 | `app_code_invalid`, `attestation_required`, `attestation_invalid` | See [App integrity](app-integrity.md). |
| 403 | `forbidden` | The credential type cannot use this route. |
| 404 | `not_found` | The resource does not exist, or it belongs to a different key or account. |
| 405 | `method_not_allowed` | |
| 409 | `conflict` | A count limit is full, or the state of the resource does not permit the change. |
| 422 | `validation_failed` | `error.fields` maps each field name to a message. |
| 429 | `rate_limited` | Wait for the seconds in the `Retry-After` header. |
| 500 | `internal` | A defect in the server. |
| 503 | `unavailable` | The server cannot reach its store. Wait for the seconds in `Retry-After`, then send the request again. |

## Rate limits

| Limit | Default |
|-------|---------|
| Requests for each credential | 600 in one minute |
| `POST /v1/accounts` for each IP address | 20 in one hour |
| `POST /v1/attest/challenge` for each IP address | 20 in one hour |
| `notify_user` calls for each key | 30 in one hour |
| Messages from one sender | 60 in one hour |

The operator of a server can change the first three values.

## Limits

| Item | Limit |
|------|-------|
| Keys for each account | 20 |
| Devices for each account | 20 |
| Open tasks (`todo` or `in_progress`) for each key | 1,000 |
| Pending questions for each key | 20 |
| Task `title` | 1 to 200 characters |
| Task `description` | 0 to 20,000 characters, Markdown |
| Task `status_note` | 0 to 140 characters |
| Key `label` | 1 to 40 characters |
| `notify_user` `title` | 1 to 100 characters |
| `notify_user` `body` | 1 to 1,000 characters |
| Question `prompt` | 1 to 300 characters |
| One question option | 1 to 30 characters |
| Typed answer | 1 to 1,000 characters |
| Question `expires_in` | 60 to 86,400 seconds |
| Message text | 1 to 2,000 characters |
| Unread inbox items for each key | 200. The oldest item is removed first. |

A request above a count limit gets `409`.

The server makes `title`, `label`, `status_note`, `prompt`, and each option one line: it removes the space at the two ends and changes each run of white space to one space. An empty `status_note` is stored as `null`. A typed answer loses only the space at its two ends.

## Retention

| Data | Deleted |
|------|---------|
| A task with the status `done` or `cancelled` | 90 days after its last update. The event stream sends `task.deleted`. |
| A question that is not `pending` | 90 days after it closed |
| An inbox item | 7 days if it is unread, 24 hours after it was read |
| All other data | When the person deletes it |

## Health

`GET /health` needs no credential. It returns `200 {"status":"ok","store":"ok"}`, or `503 {"status":"unavailable","store":"unavailable"}` if the store does not answer.
