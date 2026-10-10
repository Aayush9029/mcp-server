from starlette.requests import HTTPConnection


def client_ip(conn: HTTPConnection, behind_render: bool) -> str:
    """The address for per-IP rate limits.

    On Render, the edge writes CF-Connecting-IP and replaces any value that the caller
    sent. X-Forwarded-For is only appended to, so its first entries are caller data;
    the last entry is the one that Render's own proxy added. Off Render, no proxy
    header is trusted.
    """
    if behind_render:
        edge = conn.headers.get("cf-connecting-ip")
        if edge:
            return edge.strip()
        forwarded = conn.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip()
    return conn.client.host if conn.client else "unknown"
