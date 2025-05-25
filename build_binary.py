#!/usr/bin/env python3
"""Build script for creating platform-specific binaries."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def build_binary():
    """Build the binary for the current platform."""
    # Determine platform and architecture
    platform = sys.platform
    if platform == "darwin":
        platform_name = "darwin"
    elif platform.startswith("linux"):
        platform_name = "linux"
    elif platform.startswith("win"):
        platform_name = "windows"
    else:
        platform_name = platform

    # Get architecture from environment or default
    arch = os.environ.get("TARGET_ARCH", "x86_64")

    # Set environment variable for spec file
    os.environ["TARGET_ARCH"] = arch

    print(f"Building for {platform_name}-{arch}")

    # Clean previous builds
    if Path("build").exists():
        shutil.rmtree("build")
    if Path("dist").exists():
        shutil.rmtree("dist")

    # Run PyInstaller with spec file
    cmd = [sys.executable, "-m", "PyInstaller", "task-mcp-server.spec", "--clean"]

    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Build failed:\n{result.stderr}")
        sys.exit(1)

    print("Build successful! Binary created in dist/")

    # List files in dist
    dist_files = list(Path("dist").glob("*"))
    for f in dist_files:
        print(f"  - {f}")


if __name__ == "__main__":
    build_binary()
