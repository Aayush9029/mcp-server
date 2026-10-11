"""The public site: the product page, the docs, the legal pages, the install script, and the files for crawlers.

Most tests build the site from a small repository in a temporary folder, so they do not depend on the real docs.
"""

import base64
import json
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi import FastAPI

from mcp_client_server.config import Settings
from mcp_client_server.limits import Limits
from mcp_client_server.site import site_router
from mcp_client_server.site.docs import ORDER

type Serve = Callable[..., AbstractAsyncContextManager[httpx2.AsyncClient]]

BASE = "https://tasks.example.com"
MCP_URL = f"{BASE}/mcp"
ONE_LINE = f"curl -fsSL {BASE}/install | sh"
SHORT = "public, max-age=300"
DAY = "public, max-age=86400"
IMMUTABLE = "public, max-age=31536000, immutable"
REAL_DOCS = Path(__file__).resolve().parents[1] / "docs"

OVERVIEW = """# Overview <of> the service

The first paragraph says what this is. It has two sentences.

## Getting started

See [Quickstart](quickstart.md#install), [the same page](./quickstart.md), [the license](../LICENSE),
[another site](https://example.org/guide.md), [this part](#getting-started), and [a lost page](missing.md).

| Field | Meaning |
|-------|---------|
| `id` | The ID of the task |

```json
{"id": "tsk_1", "done": true}
```

```
plain <text>
```

### Getting started

Two headings have the same text.
"""
QUICKSTART = """# Quick start

Run one command.

## Install

```bash
export KEY=1  # a comment
```
"""
INSTALL = """#!/bin/sh
set -eu

DEFAULT_URL="https://old.example/mcp"
DEFAULT_NAME="tasks"
PLUGIN_URL="https://old.example/mcp"

echo "$DEFAULT_URL"
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_plugins(root: Path, server_url: str) -> None:
    """The marketplace and plugin manifests, as the real repository has them."""
    plugin = {"repository": "https://github.com/example/relay-server"}
    write(
        root / ".claude-plugin" / "marketplace.json",
        json.dumps({"name": "market", "plugins": [{"name": "relay", "source": "./plugins/relay"}]}),
    )
    write(
        root / "plugins" / "relay" / ".claude-plugin" / "plugin.json",
        json.dumps(
            {
                **plugin,
                "userConfig": {"api_key": {"type": "string", "sensitive": True}},
                "mcpServers": {"tasks": {"type": "http", "url": server_url}},
            }
        ),
    )
    write(
        root / ".agents" / "plugins" / "marketplace.json",
        json.dumps(
            {"name": "market", "plugins": [{"name": "relay", "source": {"source": "local", "path": "./plugins/relay"}}]}
        ),
    )
    write(
        root / "plugins" / "relay" / ".codex-plugin" / "plugin.json",
        json.dumps({**plugin, "mcpServers": {"tasks": {"url": server_url, "bearer_token_env_var": "RELAY_KEY"}}}),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository: four docs, an install script, and the plugin manifests."""
    write(tmp_path / "docs" / "overview.md", OVERVIEW)
    write(tmp_path / "docs" / "quickstart.md", QUICKSTART)
    write(tmp_path / "docs" / "zebra.md", "# Zebra\n\nThe last page.\n")
    write(tmp_path / "docs" / "apple-notes.md", "No heading at the top.\n")
    write(tmp_path / "docs" / "ignored.txt", "This is not a docs page.\n")
    write(tmp_path / "install.sh", INSTALL)
    write(tmp_path / "server.json", json.dumps({"repository": {"url": "https://github.com/example/relay-server"}}))
    write_plugins(tmp_path, MCP_URL)
    return tmp_path


@pytest.fixture
def serve(settings: Settings, repo: Path) -> Serve:
    """Start the site alone. Keyword arguments replace fields of the settings."""

    @asynccontextmanager
    async def start(
        *, limits: Limits | None = None, root: Path | None = None, **changes: object
    ) -> AsyncGenerator[httpx2.AsyncClient]:
        # As in the application: the generated API pages are off, so `/docs` belongs to the site.
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(site_router(settings.model_copy(update=changes), limits or Limits(), root=root or repo))
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://test") as http:
            yield http

    return start


@pytest.fixture
async def site(serve: Serve) -> AsyncGenerator[httpx2.AsyncClient]:
    async with serve() as http:
        yield http


async def page(http: httpx2.AsyncClient, path: str) -> str:
    response = await http.get(path)
    assert response.status_code == 200, path
    return response.text


def text_of(html: str) -> str:
    """The visible text of a piece of HTML, with one space between words."""
    return " ".join(unescape(re.sub(r"<[^>]+>", "", html)).split())


def part(html: str, tag: str) -> str:
    match = re.search(rf"<{tag}\b.*?</{tag}>", html, re.DOTALL)
    assert match, tag
    return match.group(0)


def tab_panel(html: str, name: str) -> str:
    match = re.search(
        rf'<div class="tab-panel" role="tabpanel" id="panel-{name}".*?(?=<div class="tab-panel"|</section>)',
        html,
        re.DOTALL,
    )
    assert match, name
    return text_of(match.group(0))


@pytest.mark.parametrize(
    ("path", "media_type", "cache"),
    [
        ("/", "text/html; charset=utf-8", SHORT),
        ("/docs", "text/html; charset=utf-8", SHORT),
        ("/docs/overview", "text/html; charset=utf-8", SHORT),
        ("/docs/overview.md", "text/markdown; charset=utf-8", SHORT),
        ("/privacy", "text/html; charset=utf-8", SHORT),
        ("/terms", "text/html; charset=utf-8", SHORT),
        ("/support", "text/html; charset=utf-8", SHORT),
        ("/llms.txt", "text/plain; charset=utf-8", SHORT),
        ("/llms-full.txt", "text/plain; charset=utf-8", SHORT),
        ("/install", "text/x-shellscript; charset=utf-8", SHORT),
        ("/install.sh", "text/x-shellscript; charset=utf-8", SHORT),
        ("/robots.txt", "text/plain; charset=utf-8", DAY),
        ("/sitemap.xml", "application/xml; charset=utf-8", DAY),
        ("/favicon.ico", "image/x-icon", DAY),
    ],
)
async def test_each_route_has_its_content_type_and_cache_lifetime(
    site: httpx2.AsyncClient, path: str, media_type: str, cache: str
) -> None:
    response = await site.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["cache-control"] == cache
    assert response.content
    head = await site.head(path)
    assert head.status_code == 200
    assert head.headers["etag"] == response.headers["etag"]


async def test_assets_have_a_content_hash_in_the_url_and_never_expire(site: httpx2.AsyncClient) -> None:
    home = await page(site, "/")
    urls = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', home))
    kinds = {url.rsplit(".", 1)[-1] for url in urls}
    assert {"css", "js", "avif", "png"} <= kinds
    for url in urls:
        assert re.search(r"\.[0-9a-f]{12}\.\w+$", url), url
        response = await site.get(url)
        assert response.status_code == 200, url
        assert response.headers["cache-control"] == IMMUTABLE
    stylesheet = next(url for url in urls if url.endswith(".css"))
    assert (await site.get(stylesheet)).headers["content-type"] == "text/css; charset=utf-8"
    assert (await site.get("/static/site.css")).status_code == 404
    assert (await site.get("/static/site.000000000000.css")).status_code == 404


@pytest.mark.parametrize("path", ["/", "/docs", "/docs/overview", "/privacy", "/terms", "/support"])
async def test_a_page_loads_nothing_from_another_host(site: httpx2.AsyncClient, path: str) -> None:
    html = await page(site, path)
    assert html.startswith("<!doctype html>")
    loaded = re.findall(r'<(?:img|script|source|iframe|video)\b[^>]*\bsrc="([^"]*)"', html)
    loaded += re.findall(r'<link\b[^>]*\brel="(?:stylesheet|icon|apple-touch-icon)"[^>]*\bhref="([^"]*)"', html)
    assert loaded
    for url in loaded:
        assert url.startswith("/"), url
        assert not url.startswith("//"), url
    assert "@import" not in html
    assert "<style" not in html


async def test_images_are_lazy_and_have_a_size_and_a_text(site: httpx2.AsyncClient) -> None:
    images = re.findall(r"<img\b[^>]*>", await page(site, "/"))
    assert len(images) > 8
    eager = [image for image in images if 'loading="lazy"' not in image]
    # Only the mark in the navigation bar and the three phones at the top load at once.
    assert len(eager) == 4
    assert sum('fetchpriority="high"' in image for image in images) == 1
    for image in images:
        assert re.search(r'\bwidth="\d+" height="\d+"', image), image
        assert re.search(r'\balt="[^"]*"', image), image


async def test_a_docs_page_has_highlighted_code_tables_anchors_and_copy_buttons(site: httpx2.AsyncClient) -> None:
    html = await page(site, "/docs/overview")
    article = part(html, "article")
    assert '<h1 id="overview-of-the-service">Overview &lt;of&gt; the service</h1>' in article
    assert "<title>Overview &lt;of&gt; the service - MCP Client docs</title>" in html
    assert '<meta name="description" content="The first paragraph says what this is. It has two sentences.">' in html

    assert '<h2 id="getting-started">Getting started<a class="anchor" href="#getting-started"' in article
    assert '<h3 id="getting-started-1">Getting started<a class="anchor" href="#getting-started-1"' in article
    toc = re.search(r'<nav class="docs-toc".*?</nav>', html, re.DOTALL)
    assert toc
    assert re.findall(r'href="(#[^"]+)"', toc.group(0)) == ["#getting-started", "#getting-started-1"]

    assert '<div class="table-wrap" tabindex="0" role="region" aria-label="Table"><table>' in article
    assert "<td><code>id</code></td>" in article

    blocks = re.findall(r'<div class="code">.*?</div>\n', article, re.DOTALL)
    assert len(blocks) == 2
    highlighted, plain = blocks
    assert '<span class="code-title">JSON</span>' in highlighted
    assert '<span class="t-nt">"id"</span>' in highlighted
    assert '<span class="t-kc">true</span>' in highlighted
    assert highlighted.endswith('<span class="t-p">}</span></code></pre></div>\n')
    assert text_of(part(highlighted, "pre")) == '{"id": "tsk_1", "done": true}'
    assert '<pre tabindex="0"><code>plain &lt;text&gt;</code></pre>' in plain
    for block in blocks:
        assert block.count('<button type="button" class="copy" data-copy>') == 1


async def test_relative_links_in_the_docs_go_to_the_site_or_to_the_repository(site: httpx2.AsyncClient) -> None:
    article = part(await page(site, "/docs/overview"), "article")
    links = dict(re.findall(r'<a href="([^"]+)">([^<]+)</a>', article))
    assert links == {
        "/docs/quickstart#install": "Quickstart",
        "/docs/quickstart": "the same page",
        "https://github.com/example/relay-server/blob/HEAD/LICENSE": "the license",
        "https://example.org/guide.md": "another site",
        "#getting-started": "this part",
        "https://github.com/example/relay-server/blob/HEAD/docs/missing.md": "a lost page",
    }


async def test_docs_come_in_the_listed_order_then_by_name(site: httpx2.AsyncClient) -> None:
    sidebar = re.search(r'<nav class="docs-side".*?</nav>', await page(site, "/docs/zebra"), re.DOTALL)
    assert sidebar
    pages = re.findall(r'<li><a href="([^"]+)"( aria-current="page")?>([^<]+)</a></li>', sidebar.group(0))
    assert pages == [
        ("/docs/overview", "", "Overview &lt;of&gt; the service"),
        ("/docs/quickstart", "", "Quick start"),
        ("/docs/apple-notes", "", "Apple notes"),
        ("/docs/zebra", ' aria-current="page"', "Zebra"),
    ]
    pager = re.findall(r'<a class="pager-link[^"]*" href="([^"]+)" rel="(\w+)">', await page(site, "/docs/quickstart"))
    assert pager == [("/docs/overview", "prev"), ("/docs/apple-notes", "next")]


async def test_the_markdown_of_a_docs_page_is_served_as_it_is(site: httpx2.AsyncClient) -> None:
    assert await page(site, "/docs/overview.md") == OVERVIEW
    assert '<link rel="alternate" type="text/markdown" href="/docs/overview.md">' in await page(site, "/docs/overview")


@pytest.mark.parametrize("path", ["/docs/nothing", "/docs/nothing.md", "/docs/ignored", "/docs/ignored.txt"])
async def test_a_docs_page_that_does_not_exist_gives_404(site: httpx2.AsyncClient, path: str) -> None:
    response = await site.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert "Page not found" in response.text


async def test_llms_txt_lists_every_docs_page_and_llms_full_txt_has_their_text(site: httpx2.AsyncClient) -> None:
    index = await page(site, "/llms.txt")
    assert index.startswith("# MCP Client\n\n> MCP Client shows the work of AI coding agents")
    assert re.findall(
        r"^- \[([^\]]+)\]\(([^)]+)\)", index.split("## Docs")[1].split("## Optional")[0], re.MULTILINE
    ) == [
        ("Overview <of> the service", f"{BASE}/docs/overview.md"),
        ("Quick start", f"{BASE}/docs/quickstart.md"),
        ("Apple notes", f"{BASE}/docs/apple-notes.md"),
        ("Zebra", f"{BASE}/docs/zebra.md"),
    ]
    assert f"]({BASE}/docs/overview.md): The first paragraph says what this is. It has two sentences.\n" in index
    assert f"`{ONE_LINE}`" in index
    assert f"`{MCP_URL}`" in index
    assert f"[Install script]({BASE}/install.sh)" in index
    assert f"[All docs in one file]({BASE}/llms-full.txt)" in index

    full = await page(site, "/llms-full.txt")
    for source in (OVERVIEW, QUICKSTART, "# Zebra\n\nThe last page.", "No heading at the top."):
        assert source.strip() in full
    assert full.index("# Overview") < full.index("# Quick start") < full.index("# Zebra")
    assert f"Source: {BASE}/docs/quickstart.md" in full
    assert ONE_LINE in full


async def test_the_real_docs_are_all_on_the_site_of_the_application(client: httpx2.AsyncClient) -> None:
    slugs = sorted(path.stem for path in REAL_DOCS.glob("*.md"))
    assert slugs
    assert set(slugs) <= set(ORDER), "A new docs file needs a place in the reading order."
    index = await page(client, "/llms.txt")
    listed = re.findall(rf"\]\({re.escape(BASE)}/docs/([\w-]+)\.md\)", index)
    assert listed == [slug for slug in ORDER if slug in slugs]
    for slug in slugs:
        html = await page(client, f"/docs/{slug}")
        assert '<article class="docs-article">' in html
        assert await page(client, f"/docs/{slug}.md") == (REAL_DOCS / f"{slug}.md").read_text(encoding="utf-8")
    assert (await client.get("/health")).json() == {"status": "ok", "store": "ok"}
    assert "<h1" in await page(client, "/")


async def test_product_strings_come_from_the_settings_and_are_escaped(serve: Serve) -> None:
    changes = {
        "app_name": "Agent <Pulse>",
        "support_email": "hi@pulse.example",
        "public_base_url": "https://pulse.example/",
    }
    paths = (
        "/",
        "/docs",
        "/docs/overview",
        "/privacy",
        "/terms",
        "/support",
        "/llms.txt",
        "/sitemap.xml",
        "/robots.txt",
    )
    async with serve(**changes) as http:
        pages = {path: await page(http, path) for path in paths}
    for path, body in pages.items():
        assert "Relaydeck" not in body, path
        assert "MCP Client" not in body, path
        assert "onrender" not in body, path
        assert "<Pulse>" not in body.replace("# Agent <Pulse>", "").replace("> Agent <Pulse>", ""), path
    home = pages["/"]
    assert "<title>Agent &lt;Pulse&gt;: see your agents work</title>" in home
    assert '<meta property="og:site_name" content="Agent &lt;Pulse&gt;">' in home
    assert "https://pulse.example/mcp" in home
    assert "curl -fsSL https://pulse.example<wbr>/install | sh" in home
    assert '<a href="mailto:hi@pulse.example">hi@pulse.example</a>' in pages["/support"]
    assert '<a href="mailto:hi@pulse.example">hi@pulse.example</a>' in pages["/privacy"]
    assert "<code>https://pulse.example/mcp</code>" in pages["/support"]
    assert pages["/llms.txt"].startswith("# Agent <Pulse>\n")
    assert "<loc>https://pulse.example/docs/overview</loc>" in pages["/sitemap.xml"]
    assert "Sitemap: https://pulse.example/sitemap.xml" in pages["/robots.txt"]


async def test_the_app_store_action_follows_the_setting(serve: Serve) -> None:
    async with serve() as http:
        soon = await page(http, "/")
    assert soon.count("Coming soon to the App&nbsp;Store") == 3
    assert "Get the app" not in soon
    assert not re.search(r"<a\b[^>]*>\s*Coming soon", soon)

    async with serve(app_store_url="https://apps.example/id1?a=1&b=2") as http:
        live = await page(http, "/")
    assert "Coming soon" not in live
    links = re.findall(r'<a class="([^"]+)" href="https://apps.example/id1\?a=1&amp;b=2">Get the app</a>', live)
    assert links == ["pill pill-accent", "button button-accent", "button button-accent button-large"]


async def test_the_one_line_setup_is_on_the_start_page_and_in_the_docs(site: httpx2.AsyncClient) -> None:
    home = await page(site, "/")
    commands = re.findall(r"<span data-copy-source>(.*?)</span>", home)
    assert [text_of(command) for command in commands] == [ONE_LINE, ONE_LINE]
    hero, connect = part(home, "section"), home.split('id="connect"')[1]
    assert 'class="hero"' in hero
    assert "data-copy-source" in hero
    assert "data-copy-source" in connect.split("data-tabs")[0]
    assert connect.index("data-copy-source") < connect.index('role="tablist"')
    assert 'href="/install.sh"' in connect

    docs = await page(site, "/docs")
    assert '<section class="quick"' in docs
    assert text_of(part(docs, "pre")) == ONE_LINE


async def test_the_docs_do_not_repeat_a_quick_start_that_they_have_already(serve: Serve, repo: Path) -> None:
    write(repo / "docs" / "quickstart.md", f"# Quick start\n\n```bash\n{ONE_LINE}\n```\n")
    async with serve() as http:
        docs = await page(http, "/docs")
    assert 'class="quick"' not in docs
    assert "Quick start" in docs


async def test_the_install_script_gets_the_url_of_this_server(serve: Serve, repo: Path) -> None:
    async with serve() as http:
        script = await page(http, "/install")
        assert await page(http, "/install.sh") == script
    assert script == INSTALL.replace('DEFAULT_URL="https://old.example/mcp"', f'DEFAULT_URL="{MCP_URL}"')
    assert 'PLUGIN_URL="https://old.example/mcp"' in script

    write(
        repo / "install.sh", "#!/bin/sh\nreadonly RELAY_DEFAULT_URL='https://old.example'\nURL=https://keep.example\n"
    )
    async with serve() as http:
        script = await page(http, "/install")
    assert script == f'#!/bin/sh\nreadonly RELAY_DEFAULT_URL="{BASE}"\nURL=https://keep.example\n'

    write(repo / "install.sh", "#!/bin/sh\necho no default\n")
    async with serve() as http:
        assert await page(http, "/install") == "#!/bin/sh\necho no default\n"


async def test_a_server_without_the_install_script_says_so(serve: Serve, repo: Path) -> None:
    (repo / "install.sh").unlink()
    async with serve() as http:
        for path in ("/install", "/install.sh"):
            response = await http.get(path)
            assert response.status_code == 503
            assert response.headers["content-type"] == "text/plain; charset=utf-8"
            assert response.headers["cache-control"] == "no-store"
            assert response.text == "The install script is not available on this server.\n"
        assert (await http.get("/")).status_code == 200


async def test_the_cursor_button_carries_the_server_and_a_key_placeholder(site: httpx2.AsyncClient) -> None:
    home = await page(site, "/")
    match = re.search(r'<a class="button button-accent" href="(cursor://[^"]+)">Add to Cursor</a>', home)
    assert match
    link = urlsplit(unescape(match.group(1)))
    assert (link.scheme, link.netloc, link.path) == ("cursor", "anysphere.cursor-deeplink", "/mcp/install")
    query = parse_qs(link.query, strict_parsing=True)
    assert set(query) == {"name", "config"}
    assert query["name"] == ["tasks"]
    assert json.loads(base64.b64decode(query["config"][0], validate=True)) == {
        "url": MCP_URL,
        "headers": {"Authorization": "Bearer YOUR_API_KEY"},
    }
    assert "+" not in link.query
    assert '"url": "https://tasks.example.com/mcp"' in tab_panel(home, "cursor")


async def test_each_agent_has_its_setup_commands(site: httpx2.AsyncClient) -> None:
    home = await page(site, "/")
    tab = r'<button type="button" class="tab" role="tab" id="tab-([\w-]+)" aria-controls="panel-\1"'
    tabs = re.findall(tab + r' aria-selected="(\w+)">([^<]+)</button>', home)
    assert tabs == [
        ("claude-code", "true", "Claude Code"),
        ("codex", "false", "Codex"),
        ("cursor", "false", "Cursor"),
        ("other", "false", "Other clients"),
    ]
    claude, codex, other = tab_panel(home, "claude-code"), tab_panel(home, "codex"), tab_panel(home, "other")
    assert "/plugin marketplace add example/relay-server /plugin install relay@market" in claude
    assert "claude plugin install relay --marketplace example/relay-server --config api_key=YOUR_API_KEY" in claude
    assert f'claude mcp add --transport http tasks {MCP_URL} --header "Authorization: Bearer YOUR_API_KEY"' in claude
    assert "export RELAY_KEY=YOUR_API_KEY codex plugin marketplace add example/relay-server" in codex
    assert "codex plugin add relay@market" in codex
    assert f"codex mcp add tasks --url {MCP_URL} --bearer-token-env-var TASKS_API_KEY" in codex
    assert MCP_URL in other
    assert "Authorization: Bearer YOUR_API_KEY" in other
    assert home.count('class="copy" data-copy') >= 9


async def test_a_plugin_for_a_different_server_is_not_offered(serve: Serve, repo: Path) -> None:
    write_plugins(repo, "https://hosted.example/mcp")
    async with serve() as http:
        home = await page(http, "/")
    claude, codex = tab_panel(home, "claude-code"), tab_panel(home, "codex")
    assert "plugin" not in claude
    assert "plugin" not in codex
    assert f"claude mcp add --transport http tasks {MCP_URL}" in claude
    assert f"codex mcp add tasks --url {MCP_URL}" in codex
    assert "hosted.example" not in home


async def test_a_repository_without_docs_and_manifests_still_has_a_site(serve: Serve, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    async with serve(root=empty) as http:
        home = await page(http, "/")
        docs = await page(http, "/docs")
        index = await page(http, "/llms.txt")
        assert (await http.get("/install")).status_code == 503
    assert "View the source" not in home
    assert "Source code" not in home
    assert "This server has no documentation pages." in docs
    assert index.split("## Docs")[1].split("## Optional")[0].strip() == ""


async def test_the_privacy_page_says_what_the_server_stores_and_for_how_long(serve: Serve) -> None:
    limits = Limits(closed_task_ttl_seconds=30 * 86400, closed_question_ttl_seconds=7 * 86400)
    async with serve(limits=limits) as http:
        text = text_of(part(await page(http, "/privacy"), "article"))
    for fact in (
        "An anonymous account ID.",
        "the device model, the OS version, the app version, and the push tokens",
        "a SHA-256 hash of the secret, your label",
        "the name and version of the last MCP client",
        "Your tasks, questions, and answers.",
        "No name, no email address",
        "no tracking, no ads, and no third-party analytics",
        "is deleted 30 days after its last change",
        "is deleted 7 days after it closed",
        "Delete a task, a key, or the full account in the app.",
        "DELETE /v1/account",
        "help@example.com",
    ):
        assert fact in text, fact
    assert "App Attest" not in text

    async with serve(app_attest_app_ids="TEAM123.com.example.app") as http:
        text = text_of(part(await page(http, "/privacy"), "article"))
    assert "Apple's App Attest service" in text
    assert "is deleted 90 days after its last change" in text


async def test_the_terms_point_to_the_apple_agreement_and_support_shows_the_address(site: httpx2.AsyncClient) -> None:
    terms = part(await page(site, "/terms"), "article")
    assert '<a href="https://www.apple.com/legal/internet-services/itunes/dev/stdeula/">' in terms
    for heading in ("Acceptable use", "No warranty for the hosted server", "Contact"):
        assert f">{heading}</h" in terms, heading
    assert '<a href="mailto:help@example.com">help@example.com</a>' in terms
    support = part(await page(site, "/support"), "article")
    assert 'Write to <a href="mailto:help@example.com">help@example.com</a>' in support


@pytest.mark.parametrize("path", ["/", "/docs", "/docs/overview", "/privacy"])
async def test_the_legal_pages_are_in_the_footer_and_not_in_the_navigation(site: httpx2.AsyncClient, path: str) -> None:
    html = await page(site, path)
    navigation = re.findall(r'href="([^"]+)"', part(html, "header"))
    assert [link for link in navigation if not link.startswith("/static/")] == ["/", "/docs", "/#connect"]
    footer = re.findall(r'<a href="([^"]+)">([^<]+)</a>', part(html, "footer"))
    assert footer == [
        ("/docs", "Docs"),
        ("/privacy", "Privacy"),
        ("/terms", "Terms"),
        ("/support", "Support"),
        ("https://github.com/example/relay-server", "Source code"),
        ("/llms.txt", "llms.txt"),
    ]


async def test_share_tags_the_icon_and_the_files_for_crawlers(site: httpx2.AsyncClient) -> None:
    home = await page(site, "/")
    meta = dict(re.findall(r'<meta (?:property|name)="([^"]+)" content="([^"]*)">', home))
    assert meta["og:title"] == meta["twitter:title"] == "MCP Client: see your agents work"
    assert meta["og:description"] == meta["twitter:description"] == meta["description"]
    assert meta["og:url"] == f"{BASE}/"
    assert meta["og:type"] == "website"
    assert meta["twitter:card"] == "summary_large_image"
    assert (meta["og:image:width"], meta["og:image:height"]) == ("1200", "630")
    assert meta["og:image"] == meta["twitter:image"]
    assert meta["og:image"].startswith(f"{BASE}/static/img/og.")
    image = await site.get(meta["og:image"].removeprefix(BASE))
    assert image.headers["content-type"] == "image/jpeg"
    assert image.content.startswith(b"\xff\xd8")
    assert f'<link rel="canonical" href="{BASE}/">' in home
    assert '<link rel="icon" href="/favicon.ico" sizes="48x48">' in home
    assert (await site.get("/favicon.ico")).content.startswith(b"\x00\x00\x01\x00")
    assert f'<link rel="canonical" href="{BASE}/docs/zebra">' in await page(site, "/docs/zebra")

    sitemap = await page(site, "/sitemap.xml")
    assert re.findall(r"<loc>([^<]+)</loc>", sitemap) == [
        f"{BASE}{path}"
        for path in ("/", "/docs", "/privacy", "/terms", "/support", "/docs/overview", "/docs/quickstart")
    ] + [f"{BASE}/docs/apple-notes", f"{BASE}/docs/zebra"]
    assert await page(site, "/robots.txt") == (
        f"User-agent: *\nAllow: /\nDisallow: /v1/\nDisallow: /mcp\n\nSitemap: {BASE}/sitemap.xml\n"
    )


async def test_a_page_is_sent_compressed_and_is_not_sent_again_when_it_did_not_change(site: httpx2.AsyncClient) -> None:
    plain = await site.get("/", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers
    assert plain.headers["vary"] == "Accept-Encoding"
    packed = await site.get("/", headers={"Accept-Encoding": "gzip"})
    assert packed.headers["content-encoding"] == "gzip"
    assert packed.text == plain.text
    assert int(packed.headers["content-length"]) < len(plain.content) // 3

    again = await site.get("/", headers={"If-None-Match": plain.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert (await site.get("/", headers={"If-None-Match": '"other"'})).status_code == 200

    image = await site.get("/favicon.ico", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in image.headers
