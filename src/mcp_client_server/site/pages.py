"""Every response of the site, built one time at startup and kept in memory."""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from mcp_client_server.config import Settings
from mcp_client_server.limits import Limits
from mcp_client_server.site.assets import Assets, Cached, cached
from mcp_client_server.site.connect import KEY_PLACEHOLDER, agent_tabs, install_command
from mcp_client_server.site.docs import COPY_BUTTON, DocPage, code_block, load_docs
from mcp_client_server.site.llms import llms_full_txt, llms_txt
from mcp_client_server.site.project import find_root, load_project

logger = logging.getLogger(__name__)

PACKAGE = Path(__file__).parent
HTML = "text/html; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
MARKDOWN = "text/markdown; charset=utf-8"
SHELL = "text/x-shellscript; charset=utf-8"
XML = "application/xml; charset=utf-8"
# A page stays in a cache for a short time, because the URL of an asset changes when the asset changes.
SHORT = "public, max-age=300"
DAY = "public, max-age=86400"
UPDATED = "October 10, 2026"
APPLE_EULA = "https://www.apple.com/legal/internet-services/itunes/dev/stdeula/"

# A shell variable near the top of the install script that holds the default server URL.
_DEFAULT_URL = re.compile(
    r"""^(?P<name>\s*(?:readonly\s+|export\s+)?\w*DEFAULT\w*URL\w*=)(?P<quote>["']?)(?P<value>[^"'\s]*)(?P=quote)""",
    re.MULTILINE,
)
_SCRIPT_HEAD_LINES = 40
_DAY_SECONDS = 86400


@dataclass(frozen=True, slots=True)
class Meta:
    title: str
    description: str
    path: str


@dataclass(frozen=True, slots=True)
class Site:
    pages: dict[str, Cached]
    docs: dict[str, Cached]
    markdown: dict[str, Cached]
    not_found: Cached
    install: Cached
    assets: Assets


def install_script(path: Path, settings: Settings) -> Cached:
    """The install script, with its default server URL set to this server."""
    try:
        script = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("the install script is missing: %s", path)
        message = "The install script is not available on this server.\n"
        return cached(message, TEXT, "no-store", status=HTTPStatus.SERVICE_UNAVAILABLE)
    head = "\n".join(script.split("\n")[:_SCRIPT_HEAD_LINES])
    match = _DEFAULT_URL.search(head)
    if match:
        is_endpoint = urlsplit(match["value"]).path.rstrip("/").endswith("/mcp")
        url = settings.mcp_url if is_endpoint else settings.base_url
        script = f'{script[: match.start()]}{match["name"]}"{url}"{script[match.end() :]}'
    return cached(script, SHELL, SHORT)


def _sitemap(base_url: str, paths: list[str]) -> str:
    urls = "".join(f"<url><loc>{xml_escape(base_url + path)}</loc></url>\n" for path in paths)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n'
    )


def _robots(base_url: str) -> str:
    return f"User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /mcp\n\nSitemap: {base_url}/sitemap.xml\n"


def build_site(settings: Settings, limits: Limits, root: Path | None = None) -> Site:
    project = load_project(root or find_root())
    assets = Assets(PACKAGE / "static")
    docs = load_docs(project.docs_dir, project.repository_url)
    if not docs:
        logger.warning("no documentation pages found in %s", project.docs_dir)
    name = settings.app_name
    command = install_command(settings)
    environment = Environment(
        loader=FileSystemLoader(PACKAGE / "templates"),
        autoescape=select_autoescape(default=True),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    shared: dict[str, object] = {
        "app_name": name,
        "support_email": settings.support_email,
        "base_url": settings.base_url,
        "mcp_url": settings.mcp_url,
        "app_store_url": settings.app_store_url or "",
        "repository_url": project.repository_url or "",
        "install_command": command,
        "install_block": code_block(command, "bash"),
        "copy_button": COPY_BUTTON,
        "key_placeholder": KEY_PLACEHOLDER,
        "task_days": limits.closed_task_ttl_seconds // _DAY_SECONDS,
        "question_days": limits.closed_question_ttl_seconds // _DAY_SECONDS,
        "uses_app_attest": bool(settings.attest_app_ids),
        "asset": assets.url,
        "has_asset": assets.has,
        "docs": docs,
        "year": datetime.now(UTC).year,
    }

    def html(template: str, meta: Meta, status: HTTPStatus = HTTPStatus.OK, **context: object) -> Cached:
        body = environment.get_template(template).render(page=meta, **shared, **context)
        return cached(body, HTML, SHORT if status == HTTPStatus.OK else "no-store", status=status)

    description = (
        f"{name} shows the tasks, the progress, and the questions of your AI coding agents "
        "on iPhone, iPad, and Apple Watch."
    )
    pages = {
        "/": html(
            "home.html", Meta(f"{name}: see your agents work", description, "/"), tabs=agent_tabs(settings, project)
        ),
        "/docs": html(
            "docs_index.html",
            Meta(f"Documentation - {name}", f"How to connect an agent to {name}, and how the server works.", "/docs"),
            show_quick_start=not any("/install" in page.markdown for page in docs),
        ),
        "/privacy": html(
            "privacy.html",
            Meta(f"Privacy - {name}", f"What the {name} server stores and how to delete it.", "/privacy"),
            updated=UPDATED,
        ),
        "/terms": html(
            "terms.html",
            Meta(f"Terms - {name}", f"The terms for the {name} app and its hosted server.", "/terms"),
            updated=UPDATED,
            apple_eula=APPLE_EULA,
        ),
        "/support": html(
            "support.html", Meta(f"Support - {name}", f"Help for {name}, and how to reach us.", "/support")
        ),
    }
    doc_pages: dict[str, Cached] = {}
    markdown: dict[str, Cached] = {}
    for index, page in enumerate(docs):
        meta = Meta(f"{page.title} - {name} docs", page.summary or f"{page.title}: {name} documentation.", page.path)
        newer: DocPage | None = docs[index + 1] if index + 1 < len(docs) else None
        older: DocPage | None = docs[index - 1] if index else None
        doc_pages[page.slug] = html("docs_page.html", meta, doc=page, older=older, newer=newer)
        markdown[page.slug] = cached(page.markdown, MARKDOWN, SHORT)

    site_paths = [*pages, *(page.path for page in docs)]
    pages["/llms.txt"] = cached(llms_txt(settings, docs, project.repository_url), TEXT, SHORT)
    pages["/llms-full.txt"] = cached(llms_full_txt(settings, docs), TEXT, SHORT)
    pages["/robots.txt"] = cached(_robots(settings.base_url), TEXT, DAY)
    pages["/sitemap.xml"] = cached(_sitemap(settings.base_url, site_paths), XML, DAY)
    icon = assets.by_name("img/favicon.ico")
    pages["/favicon.ico"] = cached(icon.body, icon.media_type, DAY)

    missing = Meta(f"Page not found - {name}", "This page does not exist.", "/docs")
    not_found = html("not_found.html", missing, HTTPStatus.NOT_FOUND)
    return Site(pages, doc_pages, markdown, not_found, install_script(project.install_script, settings), assets)
