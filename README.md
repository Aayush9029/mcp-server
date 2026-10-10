# MCP Client server

One service that lets AI agents report their work to a person's Apple devices.

AI agents (Claude Code, Cursor, Codex, and other MCP clients) connect over the Model Context Protocol with an API key. They create tasks and keep them current. The app on iPhone, iPad, and Apple Watch shows those tasks live, gets push notifications, and shows a Live Activity for work in progress. The app makes the API keys.

The app name is configuration (`APP_NAME`). This document says "the app".

## What is in the service

One ASGI application gives all of these surfaces:

| Surface | Path | For |
|---------|------|-----|
| REST API | `/v1/...` | The app, and scripts |
| MCP endpoint | `/mcp` | AI agents. MCP `2026-07-28` Streamable HTTP, stateless. Earlier revisions also work. |
| Event stream | `/v1/events` | The app. Server-sent events for live updates. |
| Pages | `/`, `/privacy`, `/terms`, `/support` | People |
| Health | `/health` | The platform |

The contract for all of them is [docs/api.md](docs/api.md).

## Architecture

```
 app / scripts ──REST──┐                              ┌── APNs (HTTP/2, JWT)
                       ├─► services ─► store ─► Key Value (Valkey)
 AI agents ─────MCP────┘      │                              │
                              └─► push dispatcher            └─ pub/sub ─► event hub ─► /v1/events
```

- **One service layer.** The REST routes and the MCP tools call the same functions in `services/`. The MCP tools never call the REST API.
- **One store.** `store/` is the only code that talks to the Key Value instance. Each write that touches more than one key is one Lua script, so the indexes cannot go out of step. No request uses `KEYS` or `SCAN`. The key schema is at the top of [`store/schema.py`](src/mcp_client_server/store/schema.py).
- **Stateless processes.** A process keeps no session. Several workers and several instances can serve the same account. Events go through pub/sub, and each process has one subscriber connection that fans out to its own streams.
- **Push off the request path.** A request returns before the server talks to APNs.

```
src/mcp_client_server/
  config.py       settings from environment variables
  models.py       tasks, keys, accounts, devices, and the request bodies
  credentials.py  secrets, hashes, the authenticated principal
  store/          key schema, Lua scripts, the repository
  services/       authentication, accounts, devices, keys, tasks, task expiry
  api/            REST routes, error bodies, client IP
  mcp/            MCP server: tools, resources, prompts, API key check
  events/         SSE frames and the pub/sub hub
  push/           APNs client, payloads, and the rules for who gets which push
  web/            the HTML pages
  main.py         application factory
  cli.py          `mcp-client-server` command
```

## Quick start

You need [uv](https://docs.astral.sh/uv/) and a Valkey or Redis server.

```bash
brew install valkey && valkey-server --save "" &   # or: docker run -p 6379:6379 valkey/valkey

uv sync
cp .env.example .env
uv run mcp-client-server
```

The server listens on `http://localhost:8000`. Without the APNs variables, push is off and all other functions work.

Try the API:

```bash
# 1. The app makes an account. The token comes back one time.
curl -s -X POST localhost:8000/v1/accounts -H 'Content-Type: application/json' -d '{}'

# 2. The app makes an API key for an agent. The secret comes back one time.
curl -s -X POST localhost:8000/v1/keys -H "Authorization: Bearer $ACCOUNT_TOKEN" \
  -H 'Content-Type: application/json' -d '{"label": "Claude Code"}'

# 3. The agent (or a script) uses the key.
curl -s -X POST localhost:8000/v1/tasks -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{"title": "Write unit tests"}'

# 4. The app follows the changes.
curl -N localhost:8000/v1/events -H "Authorization: Bearer $ACCOUNT_TOKEN"
```

## Connect an agent

Use a key from the app in place of `mck_YOUR_KEY`, and your host in place of `HOST`. The start page of a running server shows the same text with its own URL.

**Claude Code**

```bash
claude mcp add --transport http tasks https://HOST/mcp --header "Authorization: Bearer mck_YOUR_KEY"
```

**Cursor** (`~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "tasks": {
      "url": "https://HOST/mcp",
      "headers": { "Authorization": "Bearer mck_YOUR_KEY" }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`, with `TASKS_API_KEY` set in the shell)

```toml
[mcp_servers.tasks]
url = "https://HOST/mcp"
bearer_token_env_var = "TASKS_API_KEY"
```

**Other MCP clients**

Use the Streamable HTTP transport with the URL `https://HOST/mcp` and the header `Authorization: Bearer mck_YOUR_KEY`. The header `X-API-Key: mck_YOUR_KEY` also works. For a client that cannot send headers, use `https://HOST/mcp?key=mck_YOUR_KEY`. A URL can appear in the logs of a proxy, so use a header if you can.

The agent gets six tools (`create_task`, `list_tasks`, `get_task`, `update_task`, `delete_task`, `notify_user`), two resources, two prompts, and server instructions that tell it when to use them.

## Configuration

All configuration comes from environment variables. A `.env` file is read in local work. The repository has no secrets.

| Variable | Default | Meaning |
|----------|---------|---------|
| `APP_NAME` | `MCP Client` | Product name in the pages and in the MCP server info |
| `SUPPORT_EMAIL` | `support@example.com` | Contact address in the pages |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Public URL of the service, in the setup text and for the `Origin` check on `/mcp` |
| `APP_STORE_URL` | none | If set, the start page links to it |
| `REDIS_URL` | `redis://localhost:6379/0` | Key Value connection string |
| `REDIS_KEY_PREFIX` | `mc` | Prefix of all keys and channels |
| `REDIS_MAX_CONNECTIONS` | `20` | Connection pool size for each process |
| `APNS_KEY_P8` | none | Contents of the APNs `.p8` key (`\n` is accepted for a new line) |
| `APNS_KEY_PATH` | none | Path of the `.p8` key, as an alternative to `APNS_KEY_P8` |
| `APNS_KEY_ID` | none | Key ID of the APNs key |
| `APNS_TEAM_ID` | none | Apple developer team ID |
| `APNS_BUNDLE_ID` | none | Bundle ID of the app. Live Activity pushes use `<bundle ID>.push-type.liveactivity`. |
| `APNS_WATCH_BUNDLE_ID` | `APNS_BUNDLE_ID` | Bundle ID of the watch app, if its topic is different |
| `RATE_LIMIT_PER_MINUTE` | `600` | Requests for each credential in one minute. `0` turns the limit off. |
| `RATE_LIMIT_ACCOUNTS_PER_HOUR` | `20` | New accounts for each IP address in one hour. `0` turns the limit off. |
| `MCP_ALLOWED_ORIGINS` | none | More browser origins that can call `/mcp`, with commas between them |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Listen address |
| `WEB_CONCURRENCY` | `1` | Number of worker processes |
| `LOG_LEVEL` | `INFO` | Log level |

Push is on only if a key (`APNS_KEY_P8` or `APNS_KEY_PATH`), `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_BUNDLE_ID` are all set. If not, the server writes one log line at startup and sends no pushes.

## Deploy to Render

[`render.yaml`](render.yaml) is a Blueprint with one web service (native Python runtime with uv, health check on `/health`) and one Key Value instance (`maxmemoryPolicy: noeviction`, with persistence).

1. Put the repository on GitHub or GitLab.
2. In the Render Dashboard, select **New > Blueprint** and select the repository.
3. Give the values that the Blueprint asks for: `PUBLIC_BASE_URL`, `SUPPORT_EMAIL`, and the `APNS_*` values. `APP_STORE_URL` is optional.
4. After the first deploy, set `PUBLIC_BASE_URL` to the URL of the service, or to your own domain.

Notes for production:

- The Key Value instance is the only database. Keep `noeviction` and a plan that has persistence.
- The store must be one node. The Lua scripts build key names at run time, which a cluster does not permit.
- On Render, the server takes the client address for the per-IP limit from the `CF-Connecting-IP` header that the Render edge sets. It reads proxy headers only if the `RENDER` environment variable is set, which Render does. On a different platform, put the server behind a proxy that you control and adapt `api/clientip.py`.

## Security

- A credential (`mca_` account token, `mck_` API key) has 256 random bits. The server stores only its SHA-256 hash and returns the secret one time.
- The server does not write credentials to its logs. It removes `?key=` from the access log.
- An API key reaches only its own tasks. An account reaches only its own keys, devices, and tasks. A resource of a different owner gives `404`.
- Rate limits are counters in the Key Value store, so they hold across workers.

## Development

```bash
uv run pytest             # tests
uv run ruff check .       # lint
uv run ruff format .      # format
uv run pyright            # types
```

The tests start a private `valkey-server` or `redis-server` if one is installed. If not, they use `fakeredis`. Set `TEST_STORE=fakeredis` to use `fakeredis` always, or `TEST_REDIS_URL` to use a server that runs.

The MCP tests connect the official SDK client to the application, one time with protocol `2026-07-28` and one time with the `initialize` handshake of the earlier revisions.

### Load test

```bash
RATE_LIMIT_PER_MINUTE=0 RATE_LIMIT_ACCOUNTS_PER_HOUR=0 LOG_LEVEL=WARNING uv run mcp-client-server
uv run python scripts/load.py --url http://127.0.0.1:8000
```

The script measures `create_task`, `list_tasks` with 500 tasks, and an MCP `tools/call`, and prints requests for each second with the p50, p95, and p99 latency.

One measurement, as a reference: one worker process, 32 connections, Valkey 9.1 on the same machine (an Apple silicon laptop that also did other work), push off.

| Operation | Requests/s | p50 | p95 | p99 |
|-----------|-----------:|----:|----:|----:|
| `create_task` (REST) | 4,410 | 7.1 ms | 8.3 ms | 9.1 ms |
| `list_tasks`, 500 tasks in one page (REST) | 2,126 | 14.3 ms | 19.4 ms | 21.8 ms |
| `tools/call update_task` (MCP `2026-07-28`) | 2,753 | 11.5 ms | 14.3 ms | 17.4 ms |

With 32 connections on one worker, most of each latency is time in the queue.

## License

[MIT](LICENSE)
