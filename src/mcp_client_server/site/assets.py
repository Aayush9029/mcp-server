"""Static files, held in memory and served under URLs that contain a hash of the content."""

import gzip
import hashlib
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from fastapi import Request, Response

IMMUTABLE = "public, max-age=31536000, immutable"
# On each response of the site. The site has no frame and no cross-site script, so a browser can be strict.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
_MEDIA_TYPES = {
    ".avif": "image/avif",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".xml": "application/xml; charset=utf-8",
}
_COMPRESSIBLE = ("text/", "application/xml", "image/svg+xml")


@dataclass(frozen=True, slots=True)
class Cached:
    """One response body that never changes while the process runs."""

    body: bytes
    media_type: str
    cache_control: str
    etag: str
    compressed: bytes | None
    status: int = HTTPStatus.OK

    def response(self, request: Request) -> Response:
        headers = {**SECURITY_HEADERS, "Cache-Control": self.cache_control, "ETag": self.etag}
        if self.compressed is not None:
            headers["Vary"] = "Accept-Encoding"
        if self.status == HTTPStatus.OK and self.etag in request.headers.get("if-none-match", ""):
            return Response(status_code=HTTPStatus.NOT_MODIFIED, headers=headers)
        if self.compressed is not None and "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(self.compressed, self.status, headers, self.media_type)
        return Response(self.body, self.status, headers, self.media_type)


def cached(body: bytes | str, media_type: str, cache_control: str, *, status: int = HTTPStatus.OK) -> Cached:
    data = body.encode() if isinstance(body, str) else body
    etag = f'"{hashlib.sha256(data).hexdigest()[:20]}"'
    compressed = gzip.compress(data, 9, mtime=0) if media_type.startswith(_COMPRESSIBLE) else None
    if compressed is not None and len(compressed) >= len(data):
        compressed = None
    return Cached(data, media_type, cache_control, etag, compressed, status)


def media_type(name: str) -> str:
    return _MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


class Assets:
    """The files of one folder. `url("site.css")` gives `/static/site.<hash>.css`."""

    def __init__(self, directory: Path, prefix: str = "/static") -> None:
        self._prefix = prefix
        self._urls: dict[str, str] = {}
        self._files: dict[str, Cached] = {}
        self._sizes: dict[str, int] = {}
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            name = path.relative_to(directory).as_posix()
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()[:12]
            hashed = f"{name.removesuffix(path.suffix)}.{digest}{path.suffix}"
            self._urls[name] = f"{prefix}/{hashed}"
            self._files[hashed] = cached(data, media_type(name), IMMUTABLE)
            self._sizes[name] = len(data)

    def url(self, name: str) -> str:
        return self._urls[name]

    def has(self, name: str) -> bool:
        return name in self._urls

    def names(self) -> list[str]:
        return list(self._urls)

    def size(self, name: str) -> int:
        return self._sizes[name]

    def get(self, hashed: str) -> Cached | None:
        return self._files.get(hashed)

    def by_name(self, name: str) -> Cached:
        return self._files[self._urls[name].removeprefix(f"{self._prefix}/")]
