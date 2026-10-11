"""The Markdown files in `docs/`, rendered to HTML one time at startup."""

import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from pygments import highlight as pygments_highlight
from pygments import lexers
from pygments.formatters import HtmlFormatter
from pygments.util import ClassNotFound

# Pages in this list come first, in this order. All other pages follow in alphabetical order.
ORDER = ("overview", "quickstart", "mcp", "rest-api", "events", "push", "app-integrity", "self-hosting")

_SUMMARY_MAX = 180
_PLAIN_LANGUAGES = frozenset({"", "text", "txt", "plain", "plaintext", "none"})
_LANGUAGE_NAMES = {
    "bash": "Terminal",
    "sh": "Terminal",
    "shell": "Terminal",
    "zsh": "Terminal",
    "console": "Terminal",
    "json": "JSON",
    "jsonc": "JSON",
    "toml": "TOML",
    "yaml": "YAML",
    "yml": "YAML",
    "http": "HTTP",
    "python": "Python",
    "py": "Python",
    "swift": "Swift",
}
_ICON = (
    '<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round">'
)
COPY_BUTTON = (
    '<button type="button" class="copy" data-copy>'
    '<span class="copy-icons" aria-hidden="true">'
    f'{_ICON}<rect x="5.5" y="5.5" width="8" height="8" rx="2"/>'
    '<path d="M10.5 5.5v-1a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h1"/></svg>'
    f'{_ICON}<path d="m3.5 8.5 3 3 6-7"/></svg>'
    "</span>"
    '<span class="copy-label" aria-live="polite">Copy</span>'
    "</button>"
)
_FORMATTER: HtmlFormatter[str] = HtmlFormatter(nowrap=True, classprefix="t-")


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    anchor: str
    text: str


@dataclass(frozen=True, slots=True)
class DocPage:
    slug: str
    title: str
    summary: str
    markdown: str
    html: str
    headings: tuple[Heading, ...]

    @property
    def path(self) -> str:
        return f"/docs/{self.slug}"


def highlight(code: str, language: str) -> str:
    """Code as HTML with one span for each token. An unknown language gives escaped text."""
    name = language.lower()
    if name in _PLAIN_LANGUAGES:
        return escape(code)
    try:
        lexer = lexers.get_lexer_by_name(name, stripnl=False, ensurenl=False)  # pyright: ignore[reportUnknownMemberType]  Pygments does not annotate the lexer options
    except ClassNotFound:
        return escape(code)
    # The formatter ends the last line with a line break. A copied command must not have one.
    return pygments_highlight(code, lexer, _FORMATTER).removesuffix("\n")


def code_block(code: str, language: str = "", *, title: str | None = None) -> str:
    """A code block with a title bar and a copy button. The docs and the setup tabs use the same markup."""
    name = _LANGUAGE_NAMES.get(language.lower(), language) if title is None else title
    return (
        '<div class="code">'
        f'<div class="code-bar"><span class="code-title">{escape(name)}</span>{COPY_BUTTON}</div>'
        f'<pre tabindex="0"><code>{highlight(code.rstrip("\n"), language)}</code></pre>'
        "</div>\n"
    )


def _fence(_: RendererHTML, tokens: Sequence[Token], idx: int, _options: OptionsDict, _env: EnvType) -> str:
    token = tokens[idx]
    words = token.info.split()
    return code_block(token.content, words[0] if words else "")


def _code_indented(_: RendererHTML, tokens: Sequence[Token], idx: int, _options: OptionsDict, _env: EnvType) -> str:
    return code_block(tokens[idx].content)


def _heading_close(_: RendererHTML, tokens: Sequence[Token], idx: int, _options: OptionsDict, _env: EnvType) -> str:
    token = tokens[idx]
    anchor, text = token.meta.get("anchor"), token.meta.get("text", "")
    if not anchor:
        return f"</{token.tag}>\n"
    label = escape(f"Link to {text}")
    return f'<a class="anchor" href="#{escape(anchor)}" aria-label="{label}">#</a></{token.tag}>\n'


def _table_open(_: RendererHTML, _tokens: Sequence[Token], _idx: int, _options: OptionsDict, _env: EnvType) -> str:
    # A wide table scrolls inside its own region, and a keyboard can reach that region.
    return '<div class="table-wrap" tabindex="0" role="region" aria-label="Table"><table>\n'


def _table_close(_: RendererHTML, _tokens: Sequence[Token], _idx: int, _options: OptionsDict, _env: EnvType) -> str:
    return "</table></div>\n"


def _markdown() -> MarkdownIt:
    markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    markdown.add_render_rule("fence", _fence)
    markdown.add_render_rule("code_block", _code_indented)
    markdown.add_render_rule("heading_close", _heading_close)
    markdown.add_render_rule("table_open", _table_open)
    markdown.add_render_rule("table_close", _table_close)
    return markdown


_MARKDOWN = _markdown()


def _plain(inline: Token) -> str:
    parts = [
        " " if child.type in ("softbreak", "hardbreak") else child.content
        for child in inline.children or ()
        if child.type in ("text", "code_inline", "softbreak", "hardbreak")
    ]
    return " ".join("".join(parts).split())


def _anchor(text: str, seen: dict[str, int]) -> str:
    """The anchor that GitHub makes for a heading, so that links written for GitHub also work here."""
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-") or "section"
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def _shorten(text: str) -> str:
    if len(text) <= _SUMMARY_MAX:
        return text
    sentence = text[: _SUMMARY_MAX + 1].rsplit(". ", 1)[0]
    if len(sentence) < len(text) and len(sentence) >= _SUMMARY_MAX // 3:
        return sentence + "."
    return text[:_SUMMARY_MAX].rsplit(" ", 1)[0] + "…"


def slug_of(path: Path) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", path.stem.lower()).strip("-") or "page"


def _rewrite(href: str, slugs: frozenset[str], repository_url: str | None) -> str:
    """A relative link to a docs file becomes a site URL. Other repository files go to the repository."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return href
    target = posixpath.normpath(posixpath.join("docs", parts.path))
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    folder, name = posixpath.split(target)
    slug = slug_of(Path(name))
    if folder == "docs" and name.lower().endswith(".md") and slug in slugs:
        return f"/docs/{slug}{fragment}"
    if repository_url and not target.startswith(".."):
        return f"{repository_url}/blob/HEAD/{target}{fragment}"
    return href


def _render(slug: str, source: str, slugs: frozenset[str], repository_url: str | None) -> DocPage:
    tokens = _MARKDOWN.parse(source)
    seen: dict[str, int] = {}
    title: str | None = None
    summary = ""
    headings: list[Heading] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            text = _plain(tokens[index + 1])
            level = int(token.tag[1:])
            anchor = _anchor(text, seen)
            token.attrSet("id", anchor)
            if level == 1 and title is None:
                title = text
                continue
            tokens[index + 2].meta = {"anchor": anchor, "text": text}
            if level in (2, 3):
                headings.append(Heading(level, anchor, text))
        elif token.type == "paragraph_open" and token.level == 0 and not summary and not headings:
            summary = _shorten(_plain(tokens[index + 1]))
        elif token.type == "inline":
            for child in token.children or ():
                href = child.attrGet("href") if child.type == "link_open" else None
                if isinstance(href, str):
                    child.attrSet("href", _rewrite(href, slugs, repository_url))
    html = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {})
    return DocPage(slug, title or slug.replace("-", " ").capitalize(), summary, source, html, tuple(headings))


def _position(page: DocPage) -> tuple[int, str]:
    return (ORDER.index(page.slug) if page.slug in ORDER else len(ORDER), page.slug)


def load_docs(directory: Path, repository_url: str | None = None) -> tuple[DocPage, ...]:
    files = sorted(directory.glob("*.md")) if directory.is_dir() else []
    sources = {slug_of(path): path.read_text(encoding="utf-8") for path in files}
    slugs = frozenset(sources)
    pages = [_render(slug, source, slugs, repository_url) for slug, source in sources.items()]
    return tuple(sorted(pages, key=_position))
