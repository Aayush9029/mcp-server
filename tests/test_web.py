import logging
import re

import httpx
import pytest
from fastapi import FastAPI

from mcp_client_server.config import Settings
from mcp_client_server.logsetup import RedactKeyParam, configure_logging, redact
from mcp_client_server.main import create_app
from tests.conftest import Api, Connect, FakePush
from tests.test_mcp import mcp_client, rpc

PAGES = ["/", "/privacy", "/terms", "/support"]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_are_self_contained_html(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert html.startswith("<!doctype html>") and "MCP Client" in html
    for external in ("<script", "<link", "<img", "<iframe", "@import", "url(", "src="):
        assert external not in html, external
    links = set(re.findall(r'href="([^"]+)"', html))
    assert links <= {"/", "/privacy", "/terms", "/support", "mailto:help@example.com"}


async def test_home_page_has_the_setup_for_each_agent(client: httpx.AsyncClient) -> None:
    html = (await client.get("/")).text
    assert "https://tasks.example.com/mcp" in html
    for agent in ("Claude Code", "Cursor", "Codex", "Other MCP clients"):
        assert agent in html
    assert "claude mcp add --transport http tasks https://tasks.example.com/mcp" in html
    assert "[mcp_servers.tasks]" in html and "mcpServers" in html
    assert "Authorization: Bearer mck_YOUR_KEY" in html


async def test_privacy_page_says_what_the_server_stores(client: httpx.AsyncClient) -> None:
    html = (await client.get("/privacy")).text
    for fact in (
        "anonymous account ID",
        "push tokens",
        "SHA-256 hash",
        "label",
        "titles, descriptions",
        "No name, no email address",
        "no ads",
        "DELETE /v1/account",
        "90 days",
        "help@example.com",
    ):
        assert fact in html, fact


async def test_product_strings_come_from_configuration(settings: Settings, connect: Connect, push: FakePush) -> None:
    custom = settings.model_copy(
        update={
            "app_name": "Agent <Pulse>",
            "support_email": "hi@pulse.example",
            "app_store_url": "https://apps.example/id1",
        }
    )
    app = create_app(custom, redis=connect(), event_redis=connect(), push_sender=push)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        home = (await http.get("/")).text
        support = (await http.get("/support")).text
    assert "Agent &lt;Pulse&gt;" in home and "<Pulse>" not in home and "MCP Client" not in home
    assert 'href="https://apps.example/id1"' in home
    assert "hi@pulse.example" in support


def test_default_page_has_no_app_store_link(settings: Settings) -> None:
    from mcp_client_server.web.pages import pages_router

    assert settings.app_store_url is None
    assert pages_router(settings).routes


def test_key_query_parameter_is_redacted() -> None:
    assert redact("/mcp?key=mck_SECRET") == "/mcp?key=[redacted]"
    assert redact("/mcp?a=1&key=mck_SECRET&b=2") == "/mcp?a=1&key=[redacted]&b=2"
    assert redact("/v1/tasks?status=todo") == "/v1/tasks?status=todo"
    assert redact(200) == 200

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "POST", "/mcp?key=mck_SECRET", "1.1", 200),
        None,
    )
    assert RedactKeyParam().filter(record) is True
    assert record.getMessage() == '127.0.0.1:1 - "POST /mcp?key=[redacted] HTTP/1.1" 200'

    configure_logging("INFO")
    configure_logging("INFO")
    filters = [item for item in logging.getLogger("uvicorn.access").filters if isinstance(item, RedactKeyParam)]
    assert len(filters) == 1


async def test_no_credential_reaches_a_log(
    app: FastAPI, client: httpx.AsyncClient, api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    account = await api.account()
    key, _ = await api.key(account)
    token = account["Authorization"].removeprefix("Bearer ")
    secret = key["Authorization"].removeprefix("Bearer ")

    await api.task(key)
    await client.get("/v1/tasks", headers={"X-API-Key": secret})
    await client.get("/v1/account", headers={"Authorization": f"Bearer {secret}"})
    await client.get("/v1/tasks", headers={"Authorization": "Bearer mck_" + "x" * 43})
    await rpc(client, key, "tools/list")
    await client.post(f"/mcp?key={secret}", json={"jsonrpc": "2.0", "id": 1, "method": "nope"})
    async with mcp_client(app, key) as mcp:
        await mcp.call_tool("create_task", {"title": "Logged"})
        await mcp.call_tool("get_task", {"task_id": "tsk_missing"})
    await client.delete("/v1/account", headers=account)

    assert caplog.records
    for record in caplog.records:
        message = record.getMessage()
        assert secret not in message and token not in message, message
