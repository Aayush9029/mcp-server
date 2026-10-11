"""The routes of the public site. Each handler returns a response that was built at startup."""

from pathlib import Path

from fastapi import APIRouter, Request, Response

from mcp_client_server.config import Settings
from mcp_client_server.limits import Limits
from mcp_client_server.site.assets import Cached
from mcp_client_server.site.pages import build_site

_METHODS = ["GET", "HEAD"]


def site_router(settings: Settings, limits: Limits, *, root: Path | None = None) -> APIRouter:
    """The site for these settings. `root` is the repository folder with `docs/` and `install.sh`."""
    site = build_site(settings, limits, root)
    router = APIRouter(include_in_schema=False)

    def fixed(path: str, content: Cached) -> None:
        async def handler(request: Request) -> Response:
            return content.response(request)

        router.add_api_route(path, handler, methods=_METHODS)

    for path, content in site.pages.items():
        fixed(path, content)
    fixed("/install", site.install)
    fixed("/install.sh", site.install)

    async def doc_markdown(slug: str, request: Request) -> Response:
        return (site.markdown.get(slug) or site.not_found).response(request)

    async def doc_page(slug: str, request: Request) -> Response:
        return (site.docs.get(slug) or site.not_found).response(request)

    async def static(path: str, request: Request) -> Response:
        return (site.assets.get(path) or site.not_found).response(request)

    router.add_api_route("/docs/{slug}.md", doc_markdown, methods=_METHODS)
    router.add_api_route("/docs/{slug}", doc_page, methods=_METHODS)
    router.add_api_route("/static/{path:path}", static, methods=_METHODS)
    return router
