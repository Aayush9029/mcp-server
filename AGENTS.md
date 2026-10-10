# AGENTS.md

This file gives guidance to coding agents (Claude Code, Codex) that work in this repository.

## What this is

One Python service for the Relaydeck iOS app. AI agents reach it over MCP with an API key and report tasks. Apple devices reach it over REST and a server-sent event stream, and get pushes through APNs. The only database is a Redis-compatible Key Value store (Valkey).

## Commands

```bash
uv sync
valkey-server --save "" &                      # local store
uv run mcp-client-server                       # http://localhost:8000

uv run pytest                                  # uses the local store
TEST_STORE=fakeredis uv run pytest             # no store necessary
uv run pytest tests/test_mcp.py -k discover    # one test
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

For a load test, start the server with `RATE_LIMIT_PER_MINUTE=0 RATE_LIMIT_ACCOUNTS_PER_HOUR=0`, then run the script in `scripts/`.

## Architecture

- **Layers:** surfaces (REST routes, MCP tools, event stream, pages), then services, then the store. A surface does not touch the store and does not call a different surface. REST and MCP call the same service functions.
- **Store:** the only code that talks to Key Value. A write that touches more than one key is one Lua script. No request path uses `KEYS` or `SCAN`. The key schema is written at the top of the schema module.
- **Stateless processes:** no session state in memory. Several workers serve the same account. Events go through pub/sub, and each process has one subscriber that fans out to its own streams.
- **MCP:** the official `mcp` SDK, version 2, protocol revision `2026-07-28`. It also serves the earlier `initialize` handshake. Do not write protocol messages by hand.
- **Push:** sent after the response, not in the request path. One dispatcher decides who gets a visible, a silent, or a Live Activity push.
- **Credentials:** `mca_` account tokens and `mck_` API keys. The server stores only their SHA-256 hashes and does not log them.

## Rules

- `docs/api.md` is the contract with the Apple clients. Change the contract and the code in the same commit. Do not change a REST shape, a push payload, or the Live Activity JSON without the matching app change.
- Product strings come from settings such as `APP_NAME`. Do not hardcode the app name.
- All configuration is environment variables, listed in `.env.example`. No secret goes in the repository.
- A push to `main` deploys to production on Render. Run the tests, ruff, and pyright first.
- Tests use the fakes in the test support code for the store, APNs, and time. No test calls a real network service.
- Keep `README.md` short and in its current style. Documentation for agents goes in this file.
