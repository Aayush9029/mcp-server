"""The MCP endpoint through the SDK's own client (2026-07-28 and the legacy handshake) and over raw HTTP."""

import json

import httpx2
import pytest
from fastapi import FastAPI
from mcp_types import TextResourceContents

from mcp_client_server.limits import Limits
from tests.conftest import Launch
from tests.support import MCP_META, MCP_VERSION, Api, Json, error, mcp_client, secret_of, structured, tool_text

TOOL_ORDER = [
    "create_task",
    "list_tasks",
    "get_task",
    "update_task",
    "delete_task",
    "notify_user",
    "ask_user",
    "get_answer",
    "check_inbox",
    "send_message",
    "list_agents",
]
BOTH_HANDSHAKES = pytest.mark.parametrize("mode", ["auto", "legacy"])


@pytest.mark.parametrize(("mode", "version"), [("auto", MCP_VERSION), ("legacy", "2025-11-25")])
async def test_full_task_flow_through_the_sdk_client(app: FastAPI, api: Api, mode: str, version: str) -> None:
    account, key, key_info = await api.agent()
    async with mcp_client(app, key, mode) as client:
        assert client.protocol_version == version
        assert client.server_info is not None
        assert client.server_info.name == "mcp-client"
        assert client.instructions is not None
        assert "Create a task before you start" in client.instructions

        created = await client.call_tool("create_task", {"title": "Add dark mode", "priority": "high"})
        made = structured(created)
        assert (made["title"], made["status"], made["priority"], made["source"], made["key_id"]) == (
            "Add dark mode",
            "todo",
            "high",
            "agent",
            key_info["id"],
        )
        assert made["created_at_iso"] == "2025-10-09T08:53:20Z"
        assert made["completed_at_iso"] is None
        assert made["id"] in tool_text(created)
        task_id: str = made["id"]

        updated = structured(
            await client.call_tool(
                "update_task", {"task_id": task_id, "status": "in_progress", "progress": 40, "status_note": "Colors"}
            )
        )
        assert (updated["progress"], updated["status_note"]) == (40, "Colors")

        # A null that the client sends clears the field. An argument that is not sent does not change it.
        cleared = structured(
            await client.call_tool("update_task", {"task_id": task_id, "status_note": None, "priority": "low"})
        )
        assert (cleared["status_note"], cleared["progress"], cleared["priority"]) == (None, 40, "low")
        assert structured(await client.call_tool("get_task", {"task_id": task_id})) == cleared

        await client.call_tool("create_task", {"title": "Second", "status": "done"})
        listed = await client.call_tool("list_tasks", {"status": ["in_progress", "todo"]})
        assert [item["id"] for item in structured(listed)["tasks"]] == [task_id]
        assert structured(listed)["next_cursor"] is None
        assert task_id in tool_text(listed)

        paged = await client.call_tool("list_tasks", {"limit": 1})
        cursor: str = structured(paged)["next_cursor"]
        assert cursor in tool_text(paged)
        rest = await client.call_tool("list_tasks", {"limit": 1, "cursor": cursor, "status": "todo,in_progress,done"})
        assert [item["id"] for item in structured(rest)["tasks"]] == [task_id]

        done = structured(await client.call_tool("update_task", {"task_id": task_id, "status": "done"}))
        assert (done["progress"], done["status"]) == (100, "done")
        assert done["completed_at_iso"] is not None

        deleted = await client.call_tool("delete_task", {"task_id": task_id})
        assert structured(deleted) == {"deleted": True, "task_id": task_id}

    assert [item["title"] for item in (await api.http.get("/v1/tasks", headers=account)).json()["tasks"]] == ["Second"]
    stored_key = (await api.http.get("/v1/keys", headers=account)).json()["keys"][0]
    assert stored_key["last_client"] == {"name": "claude-code", "version": "2.1.290"}
    assert stored_key["counts"]["done"] == 1


@BOTH_HANDSHAKES
async def test_tools_list_has_the_order_schemas_and_annotations_of_the_contract(
    app: FastAPI, api: Api, mode: str
) -> None:
    _, key, _ = await api.agent()
    async with mcp_client(app, key, mode) as client:
        tools = (await client.list_tools()).tools
    assert [tool.name for tool in tools] == TOOL_ORDER
    by_name = {tool.name: tool for tool in tools}

    hints = {
        name: (notes.read_only_hint, notes.destructive_hint, notes.idempotent_hint)
        for name, tool in by_name.items()
        if (notes := tool.annotations) is not None
    }
    assert hints == {
        "create_task": (False, False, False),
        "list_tasks": (True, False, True),
        "get_task": (True, False, True),
        "update_task": (False, False, True),
        "delete_task": (False, True, True),
        "notify_user": (False, False, False),
        "ask_user": (False, False, False),
        "get_answer": (True, False, True),
        "check_inbox": (False, False, False),
        "send_message": (False, False, False),
        "list_agents": (True, False, True),
    }
    for tool in tools:
        assert tool.title
        assert tool.description
        assert tool.annotations is not None
        assert tool.annotations.title == tool.title
        assert tool.output_schema is not None
        assert (tool.input_schema["type"], tool.output_schema["type"]) == ("object", "object")

    def arguments(name: str) -> tuple[set[str], list[str]]:
        schema = by_name[name].input_schema
        return set(schema["properties"]), schema.get("required", [])

    assert arguments("create_task") == ({"title", "description", "priority", "status", "notify"}, ["title"])
    assert arguments("list_tasks") == ({"status", "priority", "limit", "cursor"}, [])
    assert arguments("get_task") == ({"task_id"}, ["task_id"])
    assert arguments("update_task") == (
        {"task_id", "title", "description", "status", "priority", "notify", "progress", "status_note"},
        ["task_id"],
    )
    assert arguments("delete_task") == ({"task_id"}, ["task_id"])
    assert arguments("notify_user") == ({"title", "body", "task_id"}, ["title", "body"])
    assert arguments("ask_user") == (
        {"prompt", "kind", "options", "task_id", "expires_in", "wait_seconds"},
        ["prompt"],
    )
    assert arguments("get_answer") == ({"question_id", "wait_seconds"}, ["question_id"])
    assert arguments("check_inbox") == ({"wait_seconds", "limit", "mark_read"}, [])
    assert arguments("send_message") == ({"to", "text", "task_id"}, ["to", "text"])
    assert arguments("list_agents") == (set(), [])
    inbox = by_name["check_inbox"].input_schema["properties"]
    assert (inbox["wait_seconds"]["default"], inbox["limit"]["default"], inbox["mark_read"]["default"]) == (0, 20, True)
    assert (inbox["limit"]["minimum"], inbox["limit"]["maximum"]) == (1, 50)
    wait = by_name["ask_user"].input_schema["properties"]["wait_seconds"]
    assert (wait["default"], wait["minimum"], wait["maximum"]) == (30, 0, 50)
    assert by_name["ask_user"].input_schema["properties"]["kind"]["default"] == "confirm"

    create = by_name["create_task"].input_schema["properties"]
    assert (create["title"]["maxLength"], create["title"]["minLength"]) == (200, 1)
    assert create["status"]["enum"] == ["todo", "in_progress", "done", "cancelled"]
    limit = by_name["list_tasks"].input_schema["properties"]["limit"]
    assert (limit["default"], limit["minimum"], limit["maximum"]) == (50, 1, 500)

    def result(name: str) -> set[str]:
        return set((by_name[name].output_schema or {})["properties"])

    task_fields = {
        "id",
        "key_id",
        "title",
        "description",
        "status",
        "priority",
        "notify",
        "progress",
        "status_note",
        "source",
        "created_at",
        "updated_at",
        "completed_at",
        "seen_at",
        "host",
        "created_at_iso",
        "updated_at_iso",
        "completed_at_iso",
    }
    assert result("create_task") == result("get_task") == result("update_task") == task_fields
    assert result("list_tasks") == {"tasks", "next_cursor"}
    assert result("delete_task") == {"deleted", "task_id"}
    assert result("notify_user") == {"sent", "devices"}
    question_fields = {
        "id",
        "key_id",
        "task_id",
        "prompt",
        "kind",
        "options",
        "status",
        "answer",
        "created_at",
        "answered_at",
        "expires_at",
    }
    assert result("ask_user") == result("get_answer") == question_fields
    assert result("check_inbox") == {"items", "unread"}
    assert result("send_message") == {"id", "kind", "from", "task_id", "question_id", "text", "created_at", "read_at"}
    assert result("list_agents") == {"agents"}


@BOTH_HANDSHAKES
async def test_a_tool_error_tells_the_model_how_to_correct_the_call(app: FastAPI, api: Api, mode: str) -> None:
    _, key, _ = await api.agent()
    _, other_key, _ = await api.agent("Other")
    foreign = await api.task(other_key, title="Not yours")
    not_found: list[tuple[str, Json]] = [
        ("get_task", {"task_id": "tsk_missing"}),
        ("get_task", {"task_id": foreign["id"]}),
        ("update_task", {"task_id": foreign["id"], "title": "Mine"}),
        ("delete_task", {"task_id": foreign["id"]}),
        ("notify_user", {"title": "Hi", "body": "There", "task_id": foreign["id"]}),
    ]
    invalid: list[tuple[str, Json, str]] = [
        ("create_task", {"title": ""}, "title"),
        ("create_task", {"title": "ok", "priority": "critical"}, "priority"),
        ("create_task", {}, "title"),
        ("update_task", {"task_id": "tsk_x", "progress": 150}, "progress"),
        ("list_tasks", {"status": ["open"]}, "status"),
        ("list_tasks", {"cursor": "!!"}, "cursor"),
        ("list_tasks", {"limit": 0}, "limit"),
        ("notify_user", {"title": "x" * 101, "body": "ok"}, "title"),
    ]
    async with mcp_client(app, key, mode) as client:
        for name, arguments in not_found:
            result = await client.call_tool(name, arguments)
            assert result.is_error is True, name
            assert "not found" in tool_text(result).lower()
            assert "list_tasks" in tool_text(result)

        empty = await client.call_tool("update_task", {"task_id": foreign["id"]})
        assert empty.is_error is True
        assert tool_text(empty).startswith("No field to change. Send one or more of: title,")

        for name, arguments, word in invalid:
            result = await client.call_tool(name, arguments)
            assert result.is_error is True, (name, arguments)
            assert word in tool_text(result), (name, arguments, tool_text(result))
    assert (await api.http.get(f"/v1/tasks/{foreign['id']}", headers=other_key)).json()["title"] == "Not yours"


async def test_notify_user_tool_counts_the_devices_and_has_an_hourly_limit(launch: Launch) -> None:
    async with launch(limits=Limits(notify_user_per_hour=2)) as running:
        api = running.api
        account, key, _ = await api.agent("Codex")
        async with mcp_client(running.app, key) as client:
            nobody = await client.call_tool("notify_user", {"title": "Hello", "body": "Is anyone there?"})
            assert structured(nobody) == {"sent": False, "devices": 0}
            assert "no device" in tool_text(nobody)

            await api.device(account)
            sent = await client.call_tool("notify_user", {"title": "Approve the deploy", "body": "The build is ready."})
            assert structured(sent) == {"sent": True, "devices": 1}
            assert tool_text(sent) == "Notification sent to 1 device."

            limited = await client.call_tool("notify_user", {"title": "Again", "body": "And again."})
            assert limited.is_error is True
            assert "2 notifications in one hour" in tool_text(limited)
            assert "Try again in" in tool_text(limited)
        await running.pushes_done()
        assert [payload["aps"]["alert"]["subtitle"] for payload in running.push.payloads("alert")] == [
            "Approve the deploy"
        ]


@BOTH_HANDSHAKES
async def test_resources_and_prompts_show_only_the_tasks_of_the_key(app: FastAPI, api: Api, mode: str) -> None:
    _, key, _ = await api.agent()
    _, other_key, _ = await api.agent()
    foreign = await api.task(other_key, title="Foreign one")
    open_task = await api.task(key, title="Open one", status="in_progress")
    await api.task(key, title="Closed one", status="done")
    async with mcp_client(app, key, mode) as client:
        assert [str(resource.uri) for resource in (await client.list_resources()).resources] == ["tasks://open"]
        templates = (await client.list_resource_templates()).resource_templates
        assert [template.uri_template for template in templates] == ["tasks://task/{task_id}"]

        listing = (await client.read_resource("tasks://open")).contents[0]
        assert isinstance(listing, TextResourceContents)
        assert listing.mime_type == "application/json"
        assert [item["title"] for item in json.loads(listing.text)["tasks"]] == ["Open one"]

        single = (await client.read_resource(f"tasks://task/{open_task['id']}")).contents[0]
        assert isinstance(single, TextResourceContents)
        assert json.loads(single.text) == {
            **open_task,
            "created_at_iso": "2025-10-09T08:53:20Z",
            "updated_at_iso": "2025-10-09T08:53:20Z",
            "completed_at_iso": None,
        }
        with pytest.raises(Exception, match="not found"):
            await client.read_resource(f"tasks://task/{foreign['id']}")

        prompts = {prompt.name: prompt for prompt in (await client.list_prompts()).prompts}
        assert set(prompts) == {"plan_work", "status_report"}
        assert [(a.name, a.required) for a in prompts["plan_work"].arguments or []] == [("goal", True)]
        assert not prompts["status_report"].arguments

        plan = await client.get_prompt("plan_work", {"goal": "Ship the dark theme"})
        plan_text: str = getattr(plan.messages[0].content, "text", "")
        assert plan.messages[0].role == "user"
        assert "Ship the dark theme" in plan_text
        assert "create_task" in plan_text

        report = await client.get_prompt("status_report")
        report_text: str = getattr(report.messages[0].content, "text", "")
        assert "Open one" in report_text
        assert "Closed one" in report_text
        assert "Foreign one" not in report_text


async def test_server_discover(api: Api) -> None:
    _, key, _ = await api.agent()
    response = await api.rpc(key, "server/discover")
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["supportedVersions"] == [MCP_VERSION]
    assert result["resultType"] == "complete"
    assert set(result["capabilities"]) >= {"tools", "resources", "prompts"}
    assert "Create a task before you start" in result["instructions"]
    assert (result["ttlMs"], result["cacheScope"]) == (300_000, "public")
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "mcp-client"
    assert "mcp-session-id" not in response.headers


async def test_modern_results_have_the_result_type_and_the_cache_fields(api: Api) -> None:
    _, key, _ = await api.agent()
    for method in ("tools/list", "prompts/list", "resources/list", "resources/templates/list"):
        result = (await api.rpc(key, method)).json()["result"]
        assert (result["resultType"], result["ttlMs"], result["cacheScope"]) == ("complete", 300_000, "public"), method

    read = await api.rpc(key, "resources/read", {"uri": "tasks://open"}, headers={"Mcp-Name": "tasks://open"})
    assert (read.json()["result"]["ttlMs"], read.json()["result"]["cacheScope"]) == (0, "private")

    call = {"name": "create_task", "arguments": {"title": "Raw"}}
    called = await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "create_task"})
    assert called.status_code == 200
    assert called.headers["content-type"].startswith("application/json")
    result = called.json()["result"]
    assert (result["resultType"], result["isError"], result["structuredContent"]["title"]) == ("complete", False, "Raw")
    assert result["content"][0]["type"] == "text"


async def test_protocol_errors_of_the_modern_transport(api: Api) -> None:
    _, key, _ = await api.agent()
    unknown = await api.rpc(key, "tasks/unknown")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, -32601)

    call = {"name": "create_task", "arguments": {"title": "Raw"}}
    mismatch = await api.rpc(key, "tools/call", call, headers={"Mcp-Name": "get_task"})
    assert (mismatch.status_code, mismatch.json()["error"]["code"]) == (400, -32020)

    unsupported = await api.rpc(key, "tools/list", version="2099-01-01")
    assert (unsupported.status_code, unsupported.json()["error"]["code"]) == (400, -32022)
    assert MCP_VERSION in unsupported.json()["error"]["data"]["supported"]

    # The server has no listen stream, because its lists do not change.
    assert (await api.rpc(key, "subscriptions/listen", {"notifications": {}})).status_code in (400, 404)


async def test_2025_06_18_handshake_over_raw_http(client: httpx2.AsyncClient, api: Api) -> None:
    account, key, _ = await api.agent()
    headers = {**key, "Accept": "application/json, text/event-stream"}

    async def post(body: Json) -> httpx2.Response:
        return await client.post("/mcp", json={"jsonrpc": "2.0", **body}, headers=headers)

    info = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old-client", "version": "0.9"}}
    response = await post({"id": 1, "method": "initialize", "params": info})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert (result["protocolVersion"], result["serverInfo"]["name"]) == ("2025-06-18", "mcp-client")
    assert "Create a task before you start" in result["instructions"]
    assert "mcp-session-id" not in response.headers

    headers["MCP-Protocol-Version"] = "2025-06-18"
    assert (await post({"method": "notifications/initialized"})).status_code == 202

    listed = (await post({"id": 2, "method": "tools/list"})).json()["result"]
    assert [tool["name"] for tool in listed["tools"]] == TOOL_ORDER
    assert "resultType" not in listed

    call = {"name": "create_task", "arguments": {"title": "From an old client"}}
    called = await post({"id": 3, "method": "tools/call", "params": call})
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"]["title"] == "From an old client"
    assert called.json()["result"]["isError"] is False

    stored = (await client.get("/v1/keys", headers=account)).json()["keys"][0]
    assert stored["last_client"] == {"name": "old-client", "version": "0.9"}


async def test_a_missing_wrong_or_deleted_key_is_a_401_and_an_account_token_is_a_403(api: Api) -> None:
    account, key, _ = await api.agent()
    for auth in (
        {},
        {"Authorization": "Bearer mck_wrong"},
        {"Authorization": "Bearer nope"},
        {"X-API-Key": "mck_wrong"},
    ):
        response = await api.rpc(auth, "tools/list")
        assert error(response, 401, "unauthorized")["message"] == "Missing or invalid credential."
        assert response.headers["www-authenticate"] == "Bearer"
    assert (await api.http.post("/mcp?key=mck_wrong", json={})).status_code == 401
    error(await api.rpc(account, "tools/list"), 403, "forbidden")

    await api.http.delete("/v1/account", headers=account)
    error(await api.rpc(key, "tools/list"), 401, "unauthorized")


async def test_the_key_can_come_in_a_header_or_in_the_query(api: Api) -> None:
    _, key, _ = await api.agent()
    secret = secret_of(key)
    assert (await api.rpc(key, "tools/list")).status_code == 200
    assert (await api.rpc({"X-API-Key": secret}, "tools/list")).status_code == 200
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": MCP_META}}
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_VERSION,
        "Mcp-Method": "tools/list",
    }
    assert (await api.http.post(f"/mcp?key={secret}", json=body, headers=headers)).status_code == 200


async def test_only_post_and_only_allowed_origins(launch: Launch) -> None:
    async with launch(mcp_allowed_origins="https://app.example.org/ , https://other.example") as running:
        api = running.api
        _, key, _ = await api.agent()
        for method in ("GET", "DELETE", "PUT"):
            response = await api.http.request(method, "/mcp", headers=key)
            error(response, 405, "method_not_allowed")
            assert response.headers["allow"] == "POST"

        error(await api.rpc(key, "tools/list", headers={"Origin": "https://evil.example"}), 403, "forbidden")
        for origin in ("https://tasks.example.com", "https://app.example.org", "https://other.example"):
            assert (await api.rpc(key, "tools/list", headers={"Origin": origin})).status_code == 200, origin


async def test_each_key_sees_only_its_own_tasks_over_mcp(app: FastAPI, api: Api) -> None:
    account = await api.account()
    key_a, _ = await api.key(account, "A")
    key_b, _ = await api.key(account, "B")
    async with mcp_client(app, key_a, name="agent-a") as a, mcp_client(app, key_b, "legacy", name="agent-b") as b:
        made = structured(await a.call_tool("create_task", {"title": "Task of A"}))
        await b.call_tool("create_task", {"title": "Task of B"})

        for client, title in ((a, "Task of A"), (b, "Task of B")):
            listed = structured(await client.call_tool("list_tasks", {}))
            assert [item["title"] for item in listed["tasks"]] == [title]
        assert (await b.call_tool("get_task", {"task_id": made["id"]})).is_error is True
        assert (await b.call_tool("delete_task", {"task_id": made["id"]})).is_error is True
        assert structured(await a.call_tool("get_task", {"task_id": made["id"]}))["title"] == "Task of A"

    keys = (await api.http.get("/v1/keys", headers=account)).json()["keys"]
    assert {key["label"]: key["last_client"]["name"] for key in keys} == {"A": "agent-a", "B": "agent-b"}
