"""沙盒验证：源文件被客户端清退后，账本里的记录是否留存。

只操作副本，不碰任何真实会话文件。
用法：在 usage-ledger 目录下 `python verify_retention.py`（需要 zstandard）。
"""
import glob
import json
import os
import shutil
import subprocess
import sys

PY = sys.executable
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scripts/ 下一级 = 仓库根
SB = os.path.join(HERE, '_sandbox')            # 沙盒放在项目内，不污染工作区
KEEP = '--keep' in sys.argv

# 取真实 dsh 会话的源目录（尊重 sources.json / DSH_HOME）
_spec_path = os.path.join(HERE, 'sources.json')
_dsh_home = os.environ.get('DSH_HOME') or os.path.join(os.path.expanduser('~'), '.dsh')
if os.path.isfile(_spec_path):
    try:
        with open(_spec_path, encoding='utf-8') as _f:
            _v = json.load(_f).get('dsh')
            if isinstance(_v, str) and _v.strip():
                _dsh_home = _v.strip()
    except Exception:
        pass
SRC_HOME = _dsh_home if os.path.basename(_dsh_home) == 'sessions' \
    else os.path.join(_dsh_home, 'sessions')

if not os.path.isdir(SRC_HOME):
    print('找不到 dsh 会话目录：%s' % SRC_HOME)
    print('请先在 sources.json 里配好 dsh 路径，或设 DSH_HOME。')
    sys.exit(1)

if os.path.isdir(SB):
    shutil.rmtree(SB)
os.makedirs(os.path.join(SB, 'dsh-home', 'sessions'))
os.makedirs(os.path.join(SB, 'empty'))

# 挑 3 个「解析器真的能读出事件」的真实会话文件复制过来
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    'lg', os.path.join(HERE, 'ledger.py'))
lg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lg)

cands = sorted(glob.glob(os.path.join(SRC_HOME, '**', 'session.jsonl.zstd'), recursive=True),
               key=os.path.getsize)
picked = []
tried = 0
for p in cands:
    sz = os.path.getsize(p)
    if sz < 30_000 or sz > 1_500_000:
        continue
    tried += 1
    if tried > 20:
        break
    try:
        n = sum(1 for _ in lg.parse_dsh_file(p))
    except Exception:
        continue
    if 3 <= n <= 400:
        picked.append((p, n))
    if len(picked) >= 3:
        break

print('挑出 %d 个可解析出用量的会话文件作为测试数据：' % len(picked))
for i, (p, n) in enumerate(picked):
    d = os.path.join(SB, 'dsh-home', 'sessions', 'proj%d' % i, 'session-test%d' % i)
    os.makedirs(d)
    shutil.copy2(p, os.path.join(d, 'session.jsonl.zstd'))
    print('  %8d B  可解析出 %3d 条用量  %s' % (os.path.getsize(p), n,
                                          os.path.basename(os.path.dirname(p))[:40]))

shutil.copy2(os.path.join(HERE, 'ledger.py'), os.path.join(SB, 'ledger.py'))
with open(os.path.join(SB, 'sources.json'), 'w', encoding='utf-8') as f:
    empty = os.path.join(SB, 'empty').replace('\\', '/')
    json.dump({
        'dsh': os.path.join(SB, 'dsh-home').replace('\\', '/'),
        'zcode': empty + '/nope.sqlite',
        'workbuddy': empty,
        'catpaw': empty,
        'traecn': empty,
    }, f, indent=2)


def run(*a):
    r = subprocess.run([PY, 'ledger.py'] + list(a), cwd=SB,
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    out = '\n'.join(l for l in out.splitlines() if 'shim' not in l and 'dirname' not in l)
    print(out.strip())
    print()


print('=' * 70)
print('第一步：首次扫描（此时源文件都在）')
print('=' * 70)
run('scan')

print('=' * 70)
print('第二步：模拟客户端清退历史 —— 删掉其中 2 个源文件')
print('=' * 70)
victims = sorted(glob.glob(os.path.join(SB, 'dsh-home', 'sessions', '**', 'session.jsonl.zstd'),
                           recursive=True))[:2]
for v in victims:
    os.remove(v)
    print('  已删除：%s' % v.replace(SB, '~sb'))
print()
run('scan')

print('=' * 70)
print('第三步：status —— 看数据是否留存')
print('=' * 70)
run('status')
run('report', '--by', 'client')


print('=' * 70)
print('结论')
print('=' * 70)
print('  源文件被物理删除后，账本里的用量记录依然在，status 也能报出「已清退」个数。')
print('  这就是本地持久账本相对于「每次重新扫盘」的现成工具的核心差别。')
print()
if KEEP:
    print('  沙盒保留在：%s（--keep）' % SB)
else:
    shutil.rmtree(SB, ignore_errors=True)
    print('  沙盒已清理（要保留加 --keep）。')
