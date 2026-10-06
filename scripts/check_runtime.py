#!/usr/bin/env python3
"""check_runtime.py — Usage Ledger 正式 Runtime 检查（只读，零依赖）。

验证当前解释器与环境满足产品运行要求。任何一项失败都以非零码退出，
**绝不悄悄退回系统 Python** —— 正式启动器（bat）都以本脚本为第一道闸。

检查项：
    1. 解释器必须位于本仓库的 .venv 内（python.exe / pythonw.exe）
    2. Python 版本 >= 3.10
    3. zstandard 可导入（Core 解析 dsh 会话的唯一第三方依赖）
    4. 仓库关键文件在位：ledger.py / autopilot.py / serve.py /
       usage-board.schema.json / web/v1（Functional V1 正式 Runtime UI）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

REQUIRED_FILES = ('ledger.py', 'autopilot.py', 'serve.py',
                  'usage-board.schema.json',
                  os.path.join('web', 'v1', 'shell.html'),
                  os.path.join('web', 'v1', 'app.js'),
                  os.path.join('web', 'v1', 'app.css'))


def fail(msg):
    print('  ✗ %s' % msg)
    print()
    print('  正式 Runtime 要求：本仓库 .venv（见 docs/RUNTIME.md）。')
    print('  不允许悄悄退回系统 Python。')
    return 1


def main():
    print()
    print('  智账 · PathOrbit AI Ledger Runtime 检查')
    print('  解释器：%s' % sys.executable)
    print('  版本　：%s' % sys.version.split()[0])
    exe = sys.executable.replace('\\', '/').lower()
    venv_ok = '/.venv/scripts/' in exe or '/.venv/bin/' in exe
    if not venv_ok:
        return fail('当前不是项目 .venv 解释器 —— 正式运行禁止使用系统 Python。')
    print('  .venv ✓')
    try:
        major, minor = sys.version_info[:2]
        if (major, minor) < (3, 10):
            return fail('Python 版本过低（需 >= 3.10）')
        print('  版本要求 ✓')
    except Exception:
        return fail('无法解析 Python 版本')
    try:
        import zstandard
        print('  zstandard %s ✓' % getattr(zstandard, '__version__', '?'))
    except ImportError:
        return fail('zstandard 未安装 —— 请执行 '
                    '.venv\\Scripts\\python -m pip install -r requirements-core.txt')
    missing = []
    for rel in REQUIRED_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            missing.append(rel)
    if missing:
        return fail('仓库关键文件缺失：%s' % ', '.join(missing))
    print('  仓库文件（%d 项）✓' % len(REQUIRED_FILES))
    print()
    print('  ✓ Runtime 就绪（%s）' % os.path.basename(sys.executable))
    return 0


if __name__ == '__main__':
    sys.exit(main())
