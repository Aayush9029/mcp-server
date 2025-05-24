# -*- mode: python ; coding: utf-8 -*-

import sys
import os
from pathlib import Path

# Get the platform-specific binary name
platform = sys.platform
if platform == 'darwin':
    platform = 'darwin'
elif platform.startswith('linux'):
    platform = 'linux'
elif platform.startswith('win'):
    platform = 'windows'

arch = os.environ.get('TARGET_ARCH', 'x86_64')
binary_name = f'task-mcp-{platform}-{arch}'

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('README.md', '.'),
        ('LICENSE', '.'),
    ],
    hiddenimports=[
        'mcp',
        'mcp.server',
        'mcp.server.models',
        'mcp.server.stdio',
        'mcp.types',
        'httpx',
        'httpx._transports',
        'httpx._transports.default',
        'pydantic',
        'pydantic.main',
        'pydantic.fields',
        'uvicorn',
        'uvicorn.config',
        'uvicorn.main',
        'starlette',
        'starlette.applications',
        'starlette.routing',
        'starlette.responses',
        'models',
        'asyncio',
        'json',
        'logging',
        'typing',
        'datetime',
        'uuid',
        'enum',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
    ],
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=binary_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)