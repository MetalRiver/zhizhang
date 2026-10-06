# -*- mode: python ; coding: utf-8 -*-
"""Usage Ledger Backend Sidecar spec — R10-D Desktop Shell。

与 UsageLedger.spec（便携版）同源，仅差三处：
    1. name='usage-ledger-backend'（与 Tauri 壳 UsageLedger.exe 严格区分）
    2. onedir 产物直接落位 src-tauri/binaries/usage-ledger-backend/
    3. console=True 由 Tauri 以 CREATE_NO_WINDOW 拉起，不显示控制台

构建（仓库根目录）：
    .venv\\Scripts\\pyinstaller.exe UsageLedgerBackend.spec --noconfirm --distpath src-tauri/binaries
"""
import os

block_cipher = None
ROOT = os.path.abspath('.')

a = Analysis(
    ['serve.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        ('web', 'web'),
        ('usage-board.schema.json', '.'),
        ('source-catalog.json', '.'),
        ('VERSION', '.'),
        ('pricing.json', '.'),
        ('docs/PRODUCT_WALKTHROUGH.md', 'docs'),
        ('docs/RUNTIME.md', 'docs'),
        ('docs/INSTALL.md', 'docs'),
        ('README.md', '.'),
    ],
    hiddenimports=[
        'zstandard',
        'ledger',
        'autopilot',
        'discovery',
        'paths',
    ],
    excludes=[
        'tkinter', 'unittest', 'pydoc_data', 'setuptools', 'pkg_resources',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# ---- Windows version resource（智账 · PathOrbit AI Ledger）----
exec(open(os.path.join(ROOT, 'scripts', 'pyinstaller_version_info.py'),
          encoding='utf-8').read())
version_info = make_version_info(
    original_filename='usage-ledger-backend.exe',
    internal_name='usage-ledger-backend',
    file_description='智账 · PathOrbit AI Ledger 本地服务核心')

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    version=version_info,
    name='usage-ledger-backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='usage-ledger-backend',
)
