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

# Windows-specific configuration
is_windows = sys.platform.startswith('win')

a = Analysis(
    ['task_management_mcp/server.py'],
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
        'task_management_mcp',
        'task_management_mcp.models',
        'task_management_mcp.server',
        'asyncio',
        'json',
        'logging',
        'typing',
        'datetime',
        'uuid',
        'enum',
        # Windows-specific imports
        'encodings',
        'encodings.utf_8',
        'encodings.ascii',
        'encodings.latin_1',
        'encodings.cp1252',
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
    optimize=1 if is_windows else 2,  # Less optimization for Windows
)

pyz = PYZ(a.pure)

# Different configuration for Windows vs Unix
if is_windows:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=binary_name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,  # Don't strip on Windows
        upx=False,  # Disable UPX on Windows to avoid DLL issues
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
else:
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