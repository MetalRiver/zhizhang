# -*- coding: utf-8 -*-
"""validate_portable_zip.py — 便携包结构门禁（可重复运行，只读）。

校验契约（与 src-tauri/src/main.rs::backend_exe 的运行时解析一致：
resource_dir = exe 所在目录，backend 必须位于
  <exe旁>/binaries/usage-ledger-backend/usage-ledger-backend.exe
另需 _internal 运行资源齐全）：

  1. 恰有一个顶层目录，其下存在 UsageLedger.exe（size>0）；
  2. 同一顶层目录下存在 binaries/usage-ledger-backend/usage-ledger-backend.exe
     （size>0）；
  3. _internal/VERSION 内 version 行 = 期望版本（--expect，默认 0.11.0-rc.7）；
  4. _internal/web/v1/shell.html、_internal/source-catalog.json、
     _internal/usage-board.schema.json、_internal/README.md 在位；
  5. sidecar onedir 文件数 ≥ 40（_internal 依赖完整性粗检）；
  6. 条目名可解码且不含盘符/穿越（../）。

任一缺失即 FAIL（exit 1）。静态结构校验 ≠ 真实桌面启动验收。

用法：
    python scripts/validate_portable_zip.py <portable.zip> [--expect 0.11.0-rc.7]
"""
import argparse
import io
import sys
import zipfile

REQUIRED_INTERNAL = (
    'web/v1/shell.html',
    'source-catalog.json',
    'usage-board.schema.json',
    'README.md',
)
MIN_SIDECAR_FILES = 40


def fail(msg):
    print('FAIL:', msg)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('zip_path')
    ap.add_argument('--expect', default='0.11.0-rc.7')
    args = ap.parse_args()

    try:
        z = zipfile.ZipFile(args.zip_path)
    except Exception as e:
        fail('无法打开 zip：%s' % e)

    names = []
    for n in z.namelist():
        fn = n.replace('\\', '/')
        if fn.startswith('/') or '..' in fn.split('/') or \
                (len(fn) > 1 and fn[1] == ':'):
            fail('条目名疑似路径穿越或带盘符：%r' % n)
        names.append(fn)
    print('条目总数: %d' % len(names))

    tops = {n.split('/')[0] for n in names}
    if len(tops) != 1:
        fail('顶层目录应恰有一个，实际：%s' % sorted(tops))
    top = next(iter(tops))
    print('顶层目录: %s' % top)

    def info(rel):
        hit = z.getinfo(top + '/' + rel) if (top + '/' + rel) in \
            {n for n in names} else None
        return hit

    # 1) 主程序
    exe = info('UsageLedger.exe')
    if exe is None or exe.file_size <= 0:
        fail('顶层目录下缺少 UsageLedger.exe（或为空）')
    print('OK UsageLedger.exe: %d B' % exe.file_size)

    # 2) sidecar 主程序：必须与 UsageLedger.exe 同一顶层目录
    side = info('binaries/usage-ledger-backend/usage-ledger-backend.exe')
    if side is None or side.file_size <= 0:
        fail('缺少 binaries/usage-ledger-backend/usage-ledger-backend.exe'
             '（backend_exe 运行时契约路径）')
    print('OK sidecar exe: %d B' % side.file_size)

    # 3) 版本一致性
    ver = info('binaries/usage-ledger-backend/_internal/VERSION')
    if ver is None:
        fail('缺少 _internal/VERSION')
    text = z.read(top + '/binaries/usage-ledger-backend/_internal/VERSION') \
        .decode('utf-8', 'replace')
    ver_line = next((l for l in text.splitlines() if l.startswith('version')),
                    '')
    got = ver_line.replace('version', '').strip()
    if got != args.expect:
        fail('_internal/VERSION = %r，期望 %r' % (got, args.expect))
    print('OK _internal/VERSION = %s' % got)

    # 4) 运行资源
    for rel in REQUIRED_INTERNAL:
        it = info('binaries/usage-ledger-backend/_internal/' + rel)
        if it is None or it.file_size <= 0:
            fail('缺少 _internal/%s' % rel)
        print('OK _internal/%s (%d B)' % (rel, it.file_size))

    # 5) sidecar onedir 完整性粗检
    n_side = sum(1 for n in names
                 if n.startswith(top + '/binaries/usage-ledger-backend/'))
    if n_side < MIN_SIDECAR_FILES:
        fail('sidecar 文件数 %d < %d（onedir 依赖疑似不完整）'
             % (n_side, MIN_SIDECAR_FILES))
    print('OK sidecar 文件数: %d (>= %d)' % (n_side, MIN_SIDECAR_FILES))

    print('VALIDATION-OK: 便携包结构与 %s 契约一致（backend_exe 路径契约、'
          '版本、运行资源、sidecar 完整性）' % args.expect)


if __name__ == '__main__':
    main()
