import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp_types import CallToolResult, Implementation, TextContent, TextResourceContents

from mcp_client_server.services import Services
from tests.conftest import DEVICE_A, Api, FakePush, Headers

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "raw-client", "version": "0.1"},
}
TOOL_ORDER = ["create_task", "list_tasks", "get_task", "update_task", "delete_task", "notify_user"]


@asynccontextmanager
async def mcp_client(
    app: FastAPI, auth: Headers, mode: str = "auto", name: str = "claude-code"
) -> AsyncIterator[Client]:
    """The SDK's own client, connected to the ASGI app."""
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=auth)
    transport = streamable_http_client("http://test/mcp", http_client=http)
    info = Implementation(name=name, version="2.1.290")
    async with http, Client(transport, mode=mode, client_info=info) as client:
        yield client


def text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


async def agent(api: Api, label: str = "Claude Code") -> tuple[Headers, Headers, dict[str, Any]]:
    account = await api.account()
    key, info = await api.key(account, label)
    return account, key, info


async def rpc(
    client: httpx.AsyncClient,
    auth: Headers,
    method: str,
    params: dict[str, Any] | None = None,
    version: str = MODERN,
    **headers: str,
) -> httpx.Response:
    meta = {**META, "io.modelcontextprotocol/protocolVersion": version}
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**(params or {}), "_meta": meta}}
    sent = {
        **auth,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
        **headers,
    }
    return await client.post("/mcp", json=body, headers=sent)


@pytest.mark.parametrize(("mode", "version"), [("auto", MODERN), ("legacy", "2025-11-25")])
async def test_full_task_flow_through_the_sdk_client(app: FastAPI, api: Api, mode: str, version: str) -> None:
    account, key, key_info = await agent(api)
    async with mcp_client(app, key, mode) as client:
        assert client.protocol_version == version
        assert client.server_info is not None and client.server_info.name == "mcp-client"
        assert client.instructions is not None and "Create a task before you start" in client.instructions

        created = await client.call_tool("create_task", {"title": "Add dark mode", "priority": "high"})
        assert created.is_error is not True
        made = created.structured_content
        assert isinstance(made, dict)
        assert (made["title"], made["status"], made["priority"], made["source"]) == (
            "Add dark mode",
            "todo",
            "high",
            "agent",
        )
        assert made["key_id"] == key_info["id"]
        assert made["created_at_iso"].endswith("Z") and made["completed_at_iso"] is None
        assert made["id"] in text(created) and "Add dark mode" in text(created)
        task_id = made["id"]

        updated = await client.call_tool(
            "update_task", {"task_id": task_id, "status": "in_progress", "progress": 40, "status_note": "Color tokens"}
        )
        assert isinstance(updated.structured_content, dict)
        assert (updated.structured_content["progress"], updated.structured_content["status_note"]) == (
            40,
            "Color tokens",
        )

        cleared = await client.call_tool("update_task", {"task_id": task_id, "status_note": None, "priority": "low"})
        assert isinstance(cleared.structured_content, dict)
        assert (cleared.structured_content["status_note"], cleared.structured_content["progress"]) == (None, 40)
        assert cleared.structured_content["priority"] == "low"

        fetched = await client.call_tool("get_task", {"task_id": task_id})
        assert fetched.structured_content == cleared.structured_content

        await client.call_tool("create_task", {"title": "Second", "status": "done"})
        listed = await client.call_tool("list_tasks", {"status": ["in_progress", "todo"]})
        assert isinstance(listed.structured_content, dict)
        assert [item["id"] for item in listed.structured_content["tasks"]] == [task_id]
        assert listed.structured_content["next_cursor"] is None
        assert task_id in text(listed)

        paged = await client.call_tool("list_tasks", {"limit": 1})
        assert isinstance(paged.structured_content, dict)
        cursor = paged.structured_content["next_cursor"]
        assert cursor and cursor in text(paged)
        rest = await client.call_tool("list_tasks", {"limit": 1, "cursor": cursor, "status": "todo,in_progress,done"})
        assert isinstance(rest.structured_content, dict)
        assert [item["id"] for item in rest.structured_content["tasks"]] == [task_id]

        done = await client.call_tool("update_task", {"task_id": task_id, "status": "done"})
        assert isinstance(done.structured_content, dict)
        assert (done.structured_content["progress"], done.structured_content["status"]) == (100, "done")
        assert done.structured_content["completed_at_iso"] is not None

        deleted = await client.call_tool("delete_task", {"task_id": task_id})
        assert deleted.structured_content == {"deleted": True, "task_id": task_id}

    seen = (await api.http.get("/v1/tasks", headers=account)).json()["tasks"]
    assert [item["title"] for item in seen] == ["Second"]
    stored_key = (await api.http.get("/v1/keys", headers=account)).json()["keys"][0]
    assert stored_key["last_client"] == {"name": "claude-code", "version": "2.1.290"}
    assert stored_key["counts"]["done"] == 1


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_tools_list_order_schemas_and_annotations(app: FastAPI, api: Api, mode: str) -> None:
    _, key, _ = await agent(api)
    async with mcp_client(app, key, mode) as client:
        tools = (await client.list_tools()).tools
        assert [tool.name for tool in tools] == TOOL_ORDER
        assert [tool.name for tool in (await client.list_tools(cache_mode="bypass")).tools] == TOOL_ORDER
        by_name = {tool.name: tool for tool in tools}

        for tool in tools:
            assert tool.title and tool.description and tool.annotations is not None
            assert tool.annotations.title == tool.title
            assert tool.output_schema is not None and tool.output_schema["type"] == "object"
            assert tool.input_schema["type"] == "object"

        def hints(name: str) -> tuple[bool | None, bool | None, bool | None]:
            notes = by_name[name].annotations
            assert notes is not None
            return notes.read_only_hint, notes.destructive_hint, notes.idempotent_hint

        assert hints("create_task") == (False, False, False)
        assert hints("list_tasks")[0] is True and hints("get_task")[0] is True
        assert hints("update_task") == (False, False, True)
        assert hints("delete_task")[:2] == (False, True)
        assert hints("notify_user") == (False, False, False)

        create = by_name["create_task"].input_schema
        assert create["required"] == ["title"]
        assert set(create["properties"]) == {"title", "description", "priority", "status", "notify"}
        assert create["properties"]["title"]["maxLength"] == 200
        assert create["properties"]["status"]["enum"] == ["todo", "in_progress", "done", "cancelled"]
        update = by_name["update_task"].input_schema
        assert update["required"] == ["task_id"]
        assert set(update["properties"]) == {
            "task_id",
            "title",
            "description",
            "status",
            "priority",
            "notify",
            "progress",
            "status_note",
        }
        assert set(by_name["list_tasks"].input_schema["properties"]) == {"status", "priority", "limit", "cursor"}
        assert by_name["list_tasks"].input_schema["properties"]["limit"]["default"] == 50
        assert set(by_name["notify_user"].input_schema["properties"]) == {"title", "body", "task_id"}

        task_schema = by_name["get_task"].output_schema
        assert task_schema is not None
        assert {"id", "key_id", "title", "status", "created_at_iso", "updated_at_iso", "completed_at_iso"} <= set(
            task_schema["properties"]
        )
        assert set((by_name["delete_task"].output_schema or {})["properties"]) == {"deleted", "task_id"}
        assert set((by_name["notify_user"].output_schema or {})["properties"]) == {"sent", "devices"}
        assert set((by_name["list_tasks"].output_schema or {})["properties"]) == {"tasks", "next_cursor"}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_tool_errors_tell_the_model_how_to_correct_the_call(app: FastAPI, api: Api, mode: str) -> None:
    _, key, _ = await agent(api)
    _, other_key, _ = await agent(api, "Other")
    foreign = await api.task(other_key, title="Not yours")
    async with mcp_client(app, key, mode) as client:
        for name, arguments in [
            ("get_task", {"task_id": "tsk_missing"}),
            ("get_task", {"task_id": foreign["id"]}),
            ("update_task", {"task_id": foreign["id"], "title": "Mine"}),
            ("delete_task", {"task_id": foreign["id"]}),
            ("notify_user", {"title": "Hi", "body": "There", "task_id": foreign["id"]}),
        ]:
            result = await client.call_tool(name, arguments)
            assert result.is_error is True, name
            assert "list_tasks" in text(result) and "not found" in text(result).lower()

        empty = await client.call_tool("update_task", {"task_id": foreign["id"]})
        assert empty.is_error is True and "No field to change" in text(empty)

        for name, arguments, word in [
            ("create_task", {"title": ""}, "title"),
            ("create_task", {"title": "ok", "priority": "critical"}, "priority"),
            ("create_task", {}, "title"),
            ("update_task", {"task_id": "tsk_x", "progress": 150}, "progress"),
            ("list_tasks", {"status": ["open"]}, "status"),
            ("list_tasks", {"cursor": "!!"}, "cursor"),
            ("list_tasks", {"limit": 0}, "limit"),
            ("notify_user", {"title": "x" * 101, "body": "ok"}, "title"),
        ]:
            result = await client.call_tool(name, arguments)
            assert result.is_error is True, (name, arguments)
            assert word in text(result), (name, arguments, text(result))
    assert (await api.http.get(f"/v1/tasks/{foreign['id']}", headers=other_key)).json()["title"] == "Not yours"


async def test_notify_user_tool(
    app: FastAPI, api: Api, services: Services, push: FakePush, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_client_server import limits

    monkeypatch.setattr(limits, "NOTIFY_USER_PER_HOUR", 2)
    account, key, _ = await agent(api, "Codex")
    async with mcp_client(app, key) as client:
        nobody = await client.call_tool("notify_user", {"title": "Hello", "body": "Is anyone there?"})
        assert nobody.structured_content == {"sent": False, "devices": 0}
        assert nobody.is_error is not True and "no device" in text(nobody)

        await api.device(account, DEVICE_A)
        sent = await client.call_tool("notify_user", {"title": "Approve the deploy", "body": "The build is ready."})
        assert sent.structured_content == {"sent": True, "devices": 1}
        assert text(sent) == "Notification sent to 1 device."

        limited = await client.call_tool("notify_user", {"title": "Again", "body": "And again."})
        assert limited.is_error is True
        assert "2 notifications in one hour" in text(limited) and "Try again in" in text(limited)
    await services.push.drain()
    assert [request.payload["aps"]["alert"]["subtitle"] for request in push.sent] == ["Approve the deploy"]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_resources_and_prompts(app: FastAPI, api: Api, mode: str) -> None:
    _, key, _ = await agent(api)
    _, other_key, _ = await agent(api)
    foreign = await api.task(other_key)
    open_task = await api.task(key, title="Open one", status="in_progress")
    await api.task(key, title="Closed one", status="done")
    async with mcp_client(app, key, mode) as client:
        resources = (await client.list_resources()).resources
        assert [str(resource.uri) for resource in resources] == ["tasks://open"]
        templates = (await client.list_resource_templates()).resource_templates
        assert [template.uri_template for template in templates] == ["tasks://task/{task_id}"]

        listing = (await client.read_resource("tasks://open")).contents[0]
        assert isinstance(listing, TextResourceContents) and listing.mime_type == "application/json"
        assert [item["title"] for item in json.loads(listing.text)["tasks"]] == ["Open one"]

        single = (await client.read_resource(f"tasks://task/{open_task['id']}")).contents[0]
        assert isinstance(single, TextResourceContents)
        assert json.loads(single.text)["id"] == open_task["id"]
        assert "updated_at_iso" in json.loads(single.text)
        with pytest.raises(Exception, match="not found"):
            await client.read_resource(f"tasks://task/{foreign['id']}")

        prompts = {prompt.name: prompt for prompt in (await client.list_prompts()).prompts}
        assert set(prompts) == {"plan_work", "status_report"}
        arguments = prompts["plan_work"].arguments or []
        assert [(argument.name, argument.required) for argument in arguments] == [("goal", True)]
        assert not prompts["status_report"].arguments

        plan = await client.get_prompt("plan_work", {"goal": "Ship the dark theme"})
        plan_text = getattr(plan.messages[0].content, "text", "")
        assert plan.messages[0].role == "user" and "Ship the dark theme" in plan_text and "create_task" in plan_text

        report = await client.get_prompt("status_report")
        report_text = getattr(report.messages[0].content, "text", "")
        assert "Open one" in report_text and "Closed one" in report_text
        assert foreign["id"] not in report_text


async def test_server_discover(client: httpx.AsyncClient, api: Api) -> None:
    _, key, _ = await agent(api)
    response = await rpc(client, key, "server/discover")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["supportedVersions"] == [MODERN]
    assert result["resultType"] == "complete"
    assert set(result["capabilities"]) >= {"tools", "resources", "prompts"}
    assert "Create a task before you start" in result["instructions"]
    assert isinstance(result["ttlMs"], int) and result["cacheScope"] in ("public", "private")
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "mcp-client"
    assert "mcp-session-id" not in response.headers


async def test_modern_results_have_result_type_and_cache_fields(client: httpx.AsyncClient, api: Api) -> None:
    _, key, _ = await agent(api)
    for method in ("tools/list", "prompts/list", "resources/list", "resources/templates/list"):
        result = (await rpc(client, key, method)).json()["result"]
        assert result["resultType"] == "complete", method
        assert (result["ttlMs"], result["cacheScope"]) == (300_000, "public"), method
    tools = (await rpc(client, key, "tools/list")).json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == TOOL_ORDER

    read = await rpc(client, key, "resources/read", {"uri": "tasks://open"}, **{"Mcp-Name": "tasks://open"})
    assert (read.json()["result"]["ttlMs"], read.json()["result"]["cacheScope"]) == (0, "private")

    called = await rpc(
        client, key, "tools/call", {"name": "create_task", "arguments": {"title": "Raw"}}, **{"Mcp-Name": "create_task"}
    )
    assert called.status_code == 200 and called.headers["content-type"].startswith("application/json")
    result = called.json()["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    assert result["structuredContent"]["title"] == "Raw"
    assert result["content"][0]["type"] == "text"

    unknown = await rpc(client, key, "tasks/unknown")
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == -32601
    mismatch = await rpc(
        client, key, "tools/call", {"name": "create_task", "arguments": {"title": "Raw"}}, **{"Mcp-Name": "get_task"}
    )
    assert mismatch.status_code == 400 and mismatch.json()["error"]["code"] == -32020
    unsupported = await rpc(client, key, "tools/list", version="2099-01-01")
    assert unsupported.status_code == 400 and unsupported.json()["error"]["code"] == -32022
    assert MODERN in unsupported.json()["error"]["data"]["supported"]
    subscribe = await rpc(client, key, "subscriptions/listen", {"notifications": {}})
    assert subscribe.status_code in (400, 404)


async def test_2025_06_18_handshake_over_raw_http(client: httpx.AsyncClient, api: Api) -> None:
    account, key, _ = await agent(api)
    headers = {**key, "Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "old-client", "version": "0.9"},
        },
    }
    response = await client.post("/mcp", json=initialize, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "mcp-client"
    assert "Create a task before you start" in result["instructions"]
    assert "mcp-session-id" not in response.headers

    headers["MCP-Protocol-Version"] = "2025-06-18"
    ack = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    assert ack.status_code == 202

    listed = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == TOOL_ORDER
    assert "resultType" not in listed.json()["result"]

    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "create_task", "arguments": {"title": "From an old client"}},
    }
    called = await client.post("/mcp", json=call, headers=headers)
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["title"] == "From an old client"
    assert called.json()["result"]["isError"] is False

    stored = (await client.get("/v1/keys", headers=account)).json()["keys"][0]
    assert stored["last_client"] == {"name": "old-client", "version": "0.9"}


async def test_bad_or_missing_key_is_a_401(client: httpx.AsyncClient, api: Api, app: FastAPI) -> None:
    account, key, _ = await agent(api)
    for auth in (
        {},
        {"Authorization": "Bearer mck_wrong"},
        {"Authorization": "Bearer nope"},
        {"X-API-Key": "mck_wrong"},
    ):
        response = await rpc(client, auth, "tools/list")
        assert response.status_code == 401, auth
        assert response.json() == {"error": {"code": "unauthorized", "message": "Missing or invalid credential."}}
        assert response.headers["www-authenticate"] == "Bearer"
    assert (await client.post("/mcp?key=mck_wrong", json={})).status_code == 401
    forbidden = await rpc(client, account, "tools/list")
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "forbidden"

    with pytest.raises(Exception):  # noqa: B017, PT011
        async with mcp_client(app, {"Authorization": "Bearer mck_wrong"}):
            pass

    deleted_key = key
    await client.delete("/v1/account", headers=account)
    assert (await rpc(client, deleted_key, "tools/list")).status_code == 401


async def test_key_in_header_x_api_key_or_query(client: httpx.AsyncClient, api: Api) -> None:
    _, key, _ = await agent(api)
    secret = key["Authorization"].removeprefix("Bearer ")
    assert (await rpc(client, key, "tools/list")).status_code == 200
    assert (await rpc(client, {"X-API-Key": secret}, "tools/list")).status_code == 200
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}}
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": "tools/list",
    }
    assert (await client.post(f"/mcp?key={secret}", json=body, headers=headers)).status_code == 200


async def test_only_post_and_only_allowed_origins(client: httpx.AsyncClient, api: Api) -> None:
    _, key, _ = await agent(api)
    for method in ("GET", "DELETE", "PUT"):
        response = await client.request(method, "/mcp", headers=key)
        assert response.status_code == 405 and response.headers["allow"] == "POST"
    evil = await rpc(client, key, "tools/list", Origin="https://evil.example")
    assert evil.status_code == 403 and evil.json()["error"]["code"] == "forbidden"
    assert (await rpc(client, key, "tools/list", Origin="https://tasks.example.com")).status_code == 200


async def test_each_key_sees_only_its_own_tasks_over_mcp(app: FastAPI, api: Api) -> None:
    account = await api.account()
    key_a, _ = await api.key(account, "A")
    key_b, _ = await api.key(account, "B")
    async with mcp_client(app, key_a, name="agent-a") as a, mcp_client(app, key_b, "legacy", name="agent-b") as b:
        made = (await a.call_tool("create_task", {"title": "Task of A"})).structured_content
        assert isinstance(made, dict)
        await b.call_tool("create_task", {"title": "Task of B"})

        for client, title in ((a, "Task of A"), (b, "Task of B")):
            listed = (await client.call_tool("list_tasks", {})).structured_content
            assert isinstance(listed, dict)
            assert [item["title"] for item in listed["tasks"]] == [title]
        assert (await b.call_tool("get_task", {"task_id": made["id"]})).is_error is True
        assert (await b.call_tool("delete_task", {"task_id": made["id"]})).is_error is True
        assert (await a.call_tool("get_task", {"task_id": made["id"]})).is_error is not True

    clients = {
        key["label"]: key["last_client"]["name"]
        for key in (await api.http.get("/v1/keys", headers=account)).json()["keys"]
    }
    assert clients == {"A": "agent-a", "B": "agent-b"}
