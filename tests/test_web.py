"""The static pages, the settings, and the rule that no credential reaches a log."""

import logging
import re
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from mcp_client_server.logsetup import RedactKeyParam, configure_logging, redact
from tests.conftest import Launch, TestSettings
from tests.support import Api, mcp_client, secret_of

PAGES = ["/", "/privacy", "/terms", "/support"]


@pytest.mark.parametrize("path", PAGES)
async def test_pages_are_self_contained_html(client: httpx2.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "public, max-age=300"
    html = response.text
    assert html.startswith("<!doctype html>")
    assert "MCP Client" in html
    for external in ("<script", "<link", "<img", "<iframe", "@import", "url(", "src="):
        assert external not in html, external
    links = set(re.findall(r'href="([^"]+)"', html))
    assert links <= {"/", "/privacy", "/terms", "/support", "mailto:help@example.com"}


async def test_home_page_has_the_setup_for_each_agent(client: httpx2.AsyncClient) -> None:
    html = (await client.get("/")).text
    for text in (
        "claude mcp add --transport http tasks https://tasks.example.com/mcp",
        "[mcp_servers.tasks]",
        "mcpServers",
        "Authorization: Bearer mck_YOUR_KEY",
        "Other MCP clients",
    ):
        assert text in html, text


async def test_privacy_page_says_what_the_server_stores_and_for_how_long(client: httpx2.AsyncClient) -> None:
    html = (await client.get("/privacy")).text
    for fact in ("anonymous account ID", "push tokens", "SHA-256 hash", "DELETE /v1/account", "90 days"):
        assert fact in html, fact


async def test_product_strings_come_from_the_settings_and_are_escaped(launch: Launch) -> None:
    changes = {
        "app_name": "Agent <Pulse>",
        "support_email": "hi@pulse.example",
        "app_store_url": "https://apps.example/id1",
    }
    async with launch(**changes) as running:
        home = (await running.http.get("/")).text
        support = (await running.http.get("/support")).text
        _, key, _ = await running.api.agent()
        discover = (await running.api.rpc(key, "server/discover")).json()["result"]
    assert "Agent &lt;Pulse&gt;" in home
    assert "<Pulse>" not in home
    assert "MCP Client" not in home
    assert 'href="https://apps.example/id1"' in home
    assert "hi@pulse.example" in support
    assert discover["_meta"]["io.modelcontextprotocol/serverInfo"]["title"] == "Agent <Pulse>"
    assert discover["instructions"].startswith("Agent <Pulse> shows your tasks")


def test_push_needs_the_key_and_all_three_identifiers(tmp_path: Path) -> None:
    key_file = tmp_path / "key.p8"
    key_file.write_text("FROM A FILE")
    full = {"apns_key_p8": "LINE1\\nLINE2", "apns_key_id": "KEY", "apns_team_id": "TEAM", "apns_bundle_id": "app"}

    config = TestSettings.model_validate(full).apns()
    assert config is not None
    assert (config.private_key, config.key_id, config.team_id) == ("LINE1\nLINE2", "KEY", "TEAM")
    from_file = TestSettings.model_validate({**full, "apns_key_p8": None, "apns_key_path": key_file}).apns()
    assert from_file is not None
    assert from_file.private_key == "FROM A FILE"
    for missing in full:
        assert TestSettings.model_validate({**full, missing: None}).apns() is None, missing


def test_settings_come_from_the_environment_and_bad_values_stop_the_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://relay.example/")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://a.example/, https://b.example")
    monkeypatch.setenv("APP_STORE_URL", "")
    monkeypatch.setenv("REDIS_URL", "redis://:secret@store:6379/0")
    settings = TestSettings()

    assert (settings.log_level, settings.mcp_url, settings.app_store_url) == (
        "WARNING",
        "https://relay.example/mcp",
        None,
    )
    assert settings.allowed_origins == {"https://relay.example", "https://a.example", "https://b.example"}
    assert "secret" not in repr(settings)
    for name, value in (("LOG_LEVEL", "loud"), ("WEB_CONCURRENCY", "0"), ("RATE_LIMIT_PER_MINUTE", "-1")):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValidationError):
            TestSettings()
        monkeypatch.delenv(name)


def test_the_key_query_parameter_is_removed_from_log_records() -> None:
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
    app: FastAPI, client: httpx2.AsyncClient, api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    account, key, _ = await api.agent()
    token, secret = secret_of(account), secret_of(key)

    await api.device(account)
    await api.task(key)
    await client.get("/v1/tasks", headers={"X-API-Key": secret})
    await client.get("/v1/account", headers=key)
    await client.get("/v1/tasks", headers={"Authorization": "Bearer mck_" + "x" * 43})
    await api.rpc(key, "tools/list")
    await client.post(f"/mcp?key={secret}", json={"jsonrpc": "2.0", "id": 1, "method": "nope"})
    async with mcp_client(app, key) as mcp:
        await mcp.call_tool("create_task", {"title": "Logged"})
        await mcp.call_tool("get_task", {"task_id": "tsk_missing"})
    await client.delete("/v1/account", headers=account)

    assert caplog.records
    for record in caplog.records:
        message = record.getMessage()
        assert secret not in message, message
        assert token not in message, message
