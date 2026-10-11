import argparse
import asyncio
import sys
from collections.abc import Sequence

import uvicorn

from mcp_client_server import admin
from mcp_client_server.config import get_settings
from mcp_client_server.errors import StoreUnavailable
from mcp_client_server.main import build_runtime

STORE_DOWN = 2


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mcp-client-server", description="Run the server, or an operator command.")
    commands = parser.add_subparsers(dest="command")
    operator = commands.add_parser("admin", help="Operator commands. Each one runs inside the service environment.")
    operator_commands = operator.add_subparsers(dest="admin_command", required=True)
    delete = operator_commands.add_parser(
        "delete-account", help="Delete an account and all its data, as DELETE /v1/account does."
    )
    target = delete.add_mutually_exclusive_group(required=True)
    target.add_argument("--account-id", metavar="acc_...", help="The ID of the account.")
    target.add_argument("--key-id", metavar="key_...", help="The ID of one API key of the account.")
    return parser.parse_args(argv)


def serve() -> None:
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


async def run_admin(args: argparse.Namespace) -> int:
    runtime = build_runtime(get_settings())
    try:
        return await admin.delete_account(
            runtime.services, account_id=args.account_id, key_id=args.key_id, out=sys.stdout
        )
    except StoreUnavailable:
        sys.stderr.write("The store does not answer. Check REDIS_URL and run the command again.\n")
        return STORE_DOWN
    finally:
        await runtime.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command is None:
        serve()
        return
    sys.exit(asyncio.run(run_admin(args)))


if __name__ == "__main__":
    main()
