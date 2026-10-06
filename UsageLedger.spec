# -*- mode: python ; coding: utf-8 -*-
"""Usage Ledger PyInstaller spec — onefdir 模式。

构建命令（在仓库根目录、.venv 激活状态下）：
    pyinstaller UsageLedger.spec --noconfirm

产物：dist/UsageLedger/ 目录（含 UsageLedger.exe + 全部依赖 + web/）
"""
import os
import sys

block_cipher = None
ROOT = os.path.abspath('.')

a = Analysis(
    ['serve.py'],
    pathex=[ROOT],
    binaries=[],
    datas=[
        # 静态 Web UI
        ('web', 'web'),
        # 出口协议
        ('usage-board.schema.json', '.'),
        # Universal Discovery catalog（V1.1）
        ('source-catalog.json', '.'),
        # 版本信息
        ('VERSION', '.'),
        # 默认定价参考（如果 pricing.json 有已验证条目则打包为 seed）
        ('pricing.json', '.'),
        # 用户文档
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
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc_data', 'setuptools', 'pkg_resources',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)


# ---- Windows version resource（智账 · PathOrbit AI Ledger）----
exec(open(os.path.join(ROOT, 'scripts', 'pyinstaller_version_info.py'),
          encoding='utf-8').read())
version_info = make_version_info(
    original_filename='UsageLedger.exe',
    internal_name='UsageLedger',
    file_description='智账 · PathOrbit AI Ledger')

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    version=version_info,
    name='UsageLedger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台窗口便于诊断
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='UsageLedger',
)
