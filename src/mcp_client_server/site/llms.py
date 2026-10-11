"""`llms.txt` and `llms-full.txt`: the docs as plain Markdown for language models."""

from collections.abc import Sequence

from mcp_client_server.config import Settings
from mcp_client_server.site.connect import install_command
from mcp_client_server.site.docs import DocPage


def summary(settings: Settings) -> str:
    return (
        f"{settings.app_name} shows the work of AI coding agents on iPhone, iPad, and Apple Watch. "
        "An agent connects to the MCP server with an API key. It creates tasks, reports status and progress, "
        "sends notifications, and asks the person questions."
    )


def _intro(settings: Settings) -> list[str]:
    return [
        f"# {settings.app_name}",
        "",
        f"> {summary(settings)}",
        "",
        f"- MCP endpoint: `{settings.mcp_url}` (Streamable HTTP, header `Authorization: Bearer <API key>`).",
        f"- One-line setup for an agent: `{install_command(settings)}`.",
        "- The person creates each API key in the app. The account is anonymous: no email and no password.",
        "",
    ]


def llms_txt(settings: Settings, docs: Sequence[DocPage], repository_url: str | None) -> str:
    base = settings.base_url
    lines = [*_intro(settings), "## Docs", ""]
    for page in docs:
        note = f": {page.summary}" if page.summary else ""
        lines.append(f"- [{page.title}]({base}{page.path}.md){note}")
    lines += [
        "",
        "## Optional",
        "",
        f"- [Install script]({base}/install.sh): The shell script that the one-line setup runs.",
        f"- [All docs in one file]({base}/llms-full.txt): Every page above, joined.",
        f"- [Privacy]({base}/privacy): What the server stores and how to delete it.",
    ]
    if repository_url:
        lines.append(f"- [Source code]({repository_url}): The open source server.")
    return "\n".join(lines) + "\n"


def llms_full_txt(settings: Settings, docs: Sequence[DocPage]) -> str:
    parts = ["\n".join(_intro(settings)).rstrip()]
    parts += [f"Source: {settings.base_url}{page.path}.md\n\n{page.markdown.strip()}" for page in docs]
    return "\n\n---\n\n".join(parts) + "\n"
