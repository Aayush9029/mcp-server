#!/usr/bin/env python
"""Test script for FastMCP server."""

import os
import sys

# Set a test API key if not already set
if "TASK_API_KEY" not in os.environ:
    print("Setting test API key...")
    os.environ["TASK_API_KEY"] = "test-api-key-123"

from task_mcp.fastmcp_server import mcp

if __name__ == "__main__":
    # Run the FastMCP server
    mcp.run()