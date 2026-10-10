import uvicorn

from mcp_client_server.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "mcp_client_server.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        workers=settings.web_concurrency,
        log_level=settings.log_level.lower(),
        # Proxy headers are read in api/clientip.py, and only on Render.
        proxy_headers=False,
        server_header=False,
        # Longer than the idle timeout of a usual load balancer, so the proxy closes idle connections first.
        timeout_keep_alive=75,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
