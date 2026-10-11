"""The setup commands for each agent. Every URL comes from the settings, and every plugin name from the manifests."""

import base64
import json
from dataclasses import dataclass
from urllib.parse import quote

from mcp_client_server.config import Settings
from mcp_client_server.site.docs import code_block
from mcp_client_server.site.project import Plugin, Project

KEY_PLACEHOLDER = "YOUR_API_KEY"
SERVER_NAME = "tasks"
KEY_ENV = "TASKS_API_KEY"


@dataclass(frozen=True, slots=True)
class Step:
    heading: str
    html: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Action:
    label: str
    url: str
    note: str


@dataclass(frozen=True, slots=True)
class AgentTab:
    id: str
    name: str
    steps: tuple[Step, ...]
    action: Action | None = None


def install_command(settings: Settings) -> str:
    return f"curl -fsSL {settings.base_url}/install | sh"


def server_config(mcp_url: str) -> dict[str, object]:
    return {"url": mcp_url, "headers": {"Authorization": f"Bearer {KEY_PLACEHOLDER}"}}


def cursor_install_link(mcp_url: str) -> str:
    """The Cursor MCP install deeplink: the server name, and the server config as base64 JSON."""
    config = json.dumps(server_config(mcp_url), separators=(",", ":"))
    encoded = base64.b64encode(config.encode()).decode()
    query = f"name={quote(SERVER_NAME, safe='')}&config={quote(encoded, safe='')}"
    return f"cursor://anysphere.cursor-deeplink/mcp/install?{query}"


def _mcp_json(mcp_url: str) -> str:
    return json.dumps({"mcpServers": {SERVER_NAME: server_config(mcp_url)}}, indent=2)


def _usable(plugin: Plugin | None, mcp_url: str) -> Plugin | None:
    # A plugin connects to the URL in its manifest. Offer it only on the server that it connects to.
    return plugin if plugin and plugin.server_url == mcp_url else None


def _claude_code(mcp_url: str, plugin: Plugin | None) -> AgentTab:
    direct = (
        f'claude mcp add --transport http {SERVER_NAME} {mcp_url} --header "Authorization: Bearer {KEY_PLACEHOLDER}"'
    )
    if not plugin:
        return AgentTab("claude-code", "Claude Code", (Step("Run this in a terminal", code_block(direct, "bash")),))
    session = f"/plugin marketplace add {plugin.source}\n/plugin install {plugin.selector}"
    shell = f"claude plugin install {plugin.name} --marketplace {plugin.source}"
    if plugin.config_key:
        shell += f" --config {plugin.config_key}={KEY_PLACEHOLDER}"
    return AgentTab(
        "claude-code",
        "Claude Code",
        (
            Step(
                "Install the plugin in a session",
                code_block(session, "text", title="Claude Code"),
                "Claude Code asks for your API key one time and keeps it in the secure storage of your system.",
            ),
            Step(
                "Or install it from a terminal",
                code_block(shell, "bash"),
                "One command. It needs Claude Code 2.1.292 or later.",
            ),
            Step("Or add the server without the plugin", code_block(direct, "bash")),
        ),
    )


def _codex(mcp_url: str, plugin: Plugin | None) -> AgentTab:
    export = f"export {KEY_ENV}={KEY_PLACEHOLDER}"
    direct = f"{export}\ncodex mcp add {SERVER_NAME} --url {mcp_url} --bearer-token-env-var {KEY_ENV}"
    note = f"Codex reads the key from {KEY_ENV}. Put the export line in your shell profile."
    if not plugin:
        return AgentTab("codex", "Codex", (Step("Run this in a terminal", code_block(direct, "bash"), note),))
    env = plugin.key_env or KEY_ENV
    install = (
        f"export {env}={KEY_PLACEHOLDER}\n"
        f"codex plugin marketplace add {plugin.source}\n"
        f"codex plugin add {plugin.selector}"
    )
    return AgentTab(
        "codex",
        "Codex",
        (
            Step(
                "Install the plugin",
                code_block(install, "bash"),
                f"Codex reads the key from {env}. Put the export line in your shell profile.",
            ),
            Step("Or add the server without the plugin", code_block(direct, "bash")),
        ),
    )


def _cursor(mcp_url: str) -> AgentTab:
    return AgentTab(
        "cursor",
        "Cursor",
        (Step("Or edit the file yourself", code_block(_mcp_json(mcp_url), "json", title="~/.cursor/mcp.json")),),
        Action(
            "Add to Cursor",
            cursor_install_link(mcp_url),
            f"Cursor opens and shows the server. Replace {KEY_PLACEHOLDER} with your key, then install.",
        ),
    )


def _other(mcp_url: str) -> AgentTab:
    return AgentTab(
        "other",
        "Other clients",
        (
            Step(
                "Use the Streamable HTTP transport",
                code_block(mcp_url, "text", title="Server URL")
                + code_block(f"Authorization: Bearer {KEY_PLACEHOLDER}", "text", title="Header"),
            ),
            Step(
                "Or use the common JSON format",
                code_block(_mcp_json(mcp_url), "json", title="mcp.json"),
                "Many clients read this format. The documentation of your client gives the file location.",
            ),
        ),
    )


def agent_tabs(settings: Settings, project: Project) -> tuple[AgentTab, ...]:
    url = settings.mcp_url
    return (
        _claude_code(url, _usable(project.claude_plugin, url)),
        _codex(url, _usable(project.codex_plugin, url)),
        _cursor(url),
        _other(url),
    )
