"""Task MCP Server - Command line interface."""

import asyncio
import logging
import os
import sys

import click

from .server import create_server


@click.command()
@click.option(
    "--api-key",
    envvar="TASK_API_KEY",
    help="API key for authentication (can also be set via TASK_API_KEY env var)",
)
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]),
    default="INFO",
    help="Set the logging level",
)
@click.help_option("-h", "--help")
def main(api_key: str | None, log_level: str):
    """Task Management MCP Server.
    
    This server provides task management capabilities through the Model Context Protocol.
    It wraps the task API at https://mcpclient.lovedoingthings.com and exposes
    tools for creating, listing, updating, and deleting tasks.
    
    Usage:
        task-mcp [OPTIONS]
        
    The server communicates via stdin/stdout and is designed to be used with
    MCP clients like Claude Desktop or Cursor.
    
    You can install this server using:
        uvx task-mcp
        pipx install task-mcp
        pip install task-mcp
    """
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,  # Important: logs must go to stderr, not stdout
    )
    
    logger = logging.getLogger(__name__)
    
    # Set API key if provided via CLI
    if api_key:
        os.environ["TASK_API_KEY"] = api_key
        logger.info("API key set from command line")
    elif os.getenv("TASK_API_KEY"):
        logger.info("Using API key from environment variable")
    else:
        logger.warning("No API key provided. Some operations may fail.")
    
    # Create and run the server
    server = create_server()
    
    try:
        logger.info("Starting Task MCP Server via stdio transport...")
        asyncio.run(server.run())
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()