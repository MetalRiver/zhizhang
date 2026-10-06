# -*- coding: utf-8 -*-
"""智账 · PathOrbit AI Ledger — PyInstaller Windows version resource。

由两个 spec（UsageLedgerBackend.spec / UsageLedger.spec）通过 exec 共享。
FileVersion 数值映射：0.11.0-rc.2 → (0, 11, 0, 2)（rc 序号进第 4 段）；
字符串 ProductVersion 保持正式版本 0.11.0-rc.2。
"""

V_STRING = '0.11.0-rc.2'
V_NUMERIC = (0, 11, 0, 2)
COMPANY = 'PathOrbit'

from PyInstaller.utils.win32.versioninfo import VSVersionInfo  # noqa: E402
from PyInstaller.utils.win32.versioninfo import FixedFileInfo  # noqa: E402
from PyInstaller.utils.win32.versioninfo import StringFileInfo  # noqa: E402
from PyInstaller.utils.win32.versioninfo import StringTable  # noqa: E402
from PyInstaller.utils.win32.versioninfo import StringStruct  # noqa: E402
from PyInstaller.utils.win32.versioninfo import VarFileInfo  # noqa: E402
from PyInstaller.utils.win32.versioninfo import VarStruct  # noqa: E402


def make_version_info(original_filename, internal_name, file_description):
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=V_NUMERIC,
            prodvers=V_NUMERIC,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,          # VOS_NT_WINDOWS32
            fileType=0x1,        # VFT_APP
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable('080404B0', [   # 简体中文 / Unicode
                    StringStruct('CompanyName', COMPANY),
                    StringStruct('FileDescription', file_description),
                    StringStruct('FileVersion', V_STRING),
                    StringStruct('InternalName', internal_name),
                    StringStruct('LegalCopyright', COMPANY),
                    StringStruct('OriginalFilename', original_filename),
                    StringStruct('ProductName', '智账'),
                    StringStruct('ProductVersion', V_STRING),
                ])
            ]),
            VarFileInfo([VarStruct('Translation', [2052, 1200])]),
        ],
    )
