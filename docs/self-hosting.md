# Self-hosting

The server is one Python service and one Redis-compatible Key Value store (Valkey). You can run it on your own machine or on a host such as Render.

## Requirements

| Part | Version |
|------|---------|
| Python | 3.13 |
| [uv](https://docs.astral.sh/uv/) | A current release |
| Valkey or Redis | One node, no cluster |
| An Apple Developer account | Only for push notifications |

## Run it locally

```bash
git clone https://github.com/Aayush9029/mcp-server.git
cd mcp-server
uv sync
cp .env.example .env
valkey-server --save "" &
uv run mcp-client-server
```

The server listens on `http://localhost:8000`. Check it:

```bash
curl http://localhost:8000/health
```

```json
{ "status": "ok", "store": "ok" }
```

Make an account with `POST /v1/accounts` and a key with `POST /v1/keys`, or point the app at your server. Then see [Quickstart](quickstart.md#configure-mcp-manually), with your own URL.

## Settings

All configuration is environment variables. `.env.example` lists them. An empty value is the same as no value.

### Product

| Variable | Default | Meaning |
|----------|---------|---------|
| `APP_NAME` | `MCP Client` | The name in the pages, the MCP server info, and the server instructions |
| `SUPPORT_EMAIL` | `support@example.com` | The contact address in the pages |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | The public URL of the server. Its origin can call `/mcp` from a browser. |
| `APP_STORE_URL` | none | The link to the app in the pages |

### Store

| Variable | Default | Meaning |
|----------|---------|---------|
| `REDIS_URL` | `redis://localhost:6379/0` | The URL of the store |
| `REDIS_KEY_PREFIX` | `mc` | The prefix of each key in the store |
| `REDIS_MAX_CONNECTIONS` | `20` | The size of the connection pool of each worker |

Use a store that keeps its data on disk and does not evict keys (`maxmemory-policy noeviction`).

### Push notifications

Push is off until the key, `APNS_KEY_ID`, `APNS_TEAM_ID`, and `APNS_BUNDLE_ID` are all there. The server then logs one line at the start that says that push is off, and each route still works.

| Variable | Meaning |
|----------|---------|
| `APNS_KEY_P8` | The APNs authentication key (`.p8`) as text. `\n` is accepted for a new line. |
| `APNS_KEY_PATH` | The path of the `.p8` file. Use this or `APNS_KEY_P8`. |
| `APNS_KEY_ID` | The key ID of that key |
| `APNS_TEAM_ID` | The team ID of the Apple Developer account |
| `APNS_BUNDLE_ID` | The bundle ID of the app |
| `APNS_WATCH_BUNDLE_ID` | The bundle ID of the watch app. The default is `APNS_BUNDLE_ID`. |

Each device says in its registration if its tokens are for the `production` or the `sandbox` environment of APNs.

### App integrity

See [App integrity](app-integrity.md). Each check is off until its variable has a value.

| Variable | Default | Meaning |
|----------|---------|---------|
| `APP_ATTEST_APP_IDS` | none | The app IDs (`TEAMID.bundle.identifier`) that can make accounts, with commas between them |
| `APP_ATTEST_ENVIRONMENT` | `production` | `production` or `development` |
| `APP_TOTP_SECRET` | none | The base32 secret of the app code |

### Limits and access

| Variable | Default | Meaning |
|----------|---------|---------|
| `RATE_LIMIT_PER_MINUTE` | `600` | Requests for each credential in one minute. `0` turns the limit off. |
| `RATE_LIMIT_ACCOUNTS_PER_HOUR` | `20` | New accounts, and challenges, for each IP address in one hour. `0` turns the limit off. |
| `MCP_ALLOWED_ORIGINS` | none | More browser origins that can call `/mcp`, with commas between them |

### Process

| Variable | Default | Meaning |
|----------|---------|---------|
| `HOST` | `0.0.0.0` | The address that the server listens on |
| `PORT` | `8000` | The port |
| `WEB_CONCURRENCY` | `1` | The number of worker processes |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` |

## Deploy on Render

The repository has a `render.yaml` Blueprint. It makes one web service and one Key Value store.

1. In Render, make a new Blueprint from your fork of the repository.
2. Give the values that Render asks for: `PUBLIC_BASE_URL`, `SUPPORT_EMAIL`, and the `APNS_` values.
3. Wait for the health check on `/health`.

A push to the `main` branch deploys a new version.

## Operation

- The server keeps no state in memory. Several workers, and several instances, can serve the same account.
- The server starts also when the store is not there yet. Until the store answers, `/health` gives `503` and each API request gives `503` with the code `unavailable`. No restart is necessary after that.
- The server deletes old data by itself. See [Overview](overview.md#retention).
- If the server runs behind a proxy that is not Render, the limit for each IP address uses the address of the proxy. Put a rate limit in your proxy in that case.

### Delete an account for a person

A person who lost the device cannot delete the account in the app. Run this inside the service environment (on Render, as a one-off job of the web service), with the account ID or the ID of one of its keys from the support request:

```bash
uv run mcp-client-server admin delete-account --key-id key_01jb2k3v9d
```

It deletes the account with all its data, as `DELETE /v1/account` does, and prints only the account ID. The exit code is `1` if there is no such account, and `2` if the store does not answer. The data goes in small steps; if the command stops before the end, run it again, or wait for the maintenance loop of the server.

## Develop

```bash
uv run pytest
TEST_STORE=fakeredis uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

`uv run pytest` starts its own Valkey if `valkey-server` is installed. `TEST_STORE=fakeredis` needs no store.
