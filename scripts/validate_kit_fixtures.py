# -*- coding: utf-8 -*-
"""validate_kit_fixtures.py — 包时预检：rc7-desk-kit 模拟来源必须能被
当前构建的真实解析器识别（门禁二.5，替代"存在即可发现"的猜测）。

校验：
  1. sources-template.json 的键 ⊆ ledger.SOURCES 的合法客户端键；
  2. workbuddy/catpaw/codex/dsh 四种 fixtures 能被对应 parse_* 函数
     解析出至少一条记录（dsh 经 zstd 解压）；
  3. 无占位符残留（{{ }}）。

用法：python scripts/validate_kit_fixtures.py [kit_dir]（默认仓库旁
rc7-desk-kit）。任一失败 exit 1。
"""
import glob
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

KIT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(HERE), 'rc7-desk-kit')
FIX = os.path.join(KIT, 'fixtures')

failures = []


def check(cond, msg):
    if cond:
        print('OK', msg)
    else:
        failures.append(msg)
        print('FAIL', msg)


# 1) sources-template.json 键合法性
tpl_path = os.path.join(KIT, 'sources-template.json')
if os.path.isfile(tpl_path):
    tpl = json.load(io.open(tpl_path, encoding='utf-8'))
    import ledger
    bad_keys = [k for k in tpl
                if not k.startswith('_') and k not in ledger.SOURCES]
    check(not bad_keys, 'sources-template 键全部合法：%s'
          % [k for k in tpl if not k.startswith('_')])
    # 模板本身理应携带 {{LOCALAPPDATA}} 占位符（由沙盒 setup 运行时替换），
    # 正向断言六个客户端键全部使用该占位符——残留检查针对的是已物化文件。
    using = [k for k, v in tpl.items()
             if k != '_note' and isinstance(v, str)
             and '{{LOCALAPPDATA}}' in v]
    check(len(using) == 6, '六个客户端键均含 {{LOCALAPPDATA}} 占位符：%s'
          % using)
else:
    failures.append('缺少 sources-template.json')

# 2) 四种 fixtures 被真实解析器识别
import zstandard

wb_files = sorted(glob.glob(os.path.join(FIX, 'wb', '**', '*.jsonl'),
                            recursive=True))
check(len(wb_files) >= 1, 'workbuddy fixture 存在')
n_wb = sum(len(list(ledger.parse_workbuddy_file(f))) for f in wb_files)
check(n_wb >= 4, 'workbuddy 解析出 %d 条用量（期望 ≥4）' % n_wb)

cp_files = sorted(glob.glob(os.path.join(FIX, 'cp', '**', '*.jsonl'),
                            recursive=True))
n_cp = sum(len(list(ledger.parse_catpaw_file(f))) for f in cp_files)
check(n_cp >= 4, 'catpaw 解析出 %d 条活动（期望 ≥4）' % n_cp)

cx_files = sorted(glob.glob(os.path.join(FIX, 'codex', '**', '*.jsonl'),
                            recursive=True))
n_cx = sum(len(list(ledger.parse_codex_file(f))) for f in cx_files)
check(n_cx >= 2, 'codex 解析出 %d 条用量（期望 ≥2）' % n_cx)

dsh_files = sorted(glob.glob(os.path.join(FIX, 'dsh-home', '**',
                                          'session.jsonl.zstd'),
                             recursive=True))
check(len(dsh_files) >= 1, 'dsh fixture 存在')
n_dsh = sum(len(list(ledger.parse_dsh_file(f))) for f in dsh_files)
check(n_dsh >= 2, 'dsh 解析出 %d 条用量（期望 ≥2）' % n_dsh)

# 3) 占位符残留
for f in glob.glob(os.path.join(KIT, '**', '*'), recursive=True):
    if os.path.isfile(f) and f.endswith(('.json', '.ps1', '.md')):
        t = io.open(f, encoding='utf-8', errors='replace').read()
        if '{{LOCALAPPDATA}}' in t and 'sources-template' not in f:
            failures.append('占位符残留：%s' % f)

print('=' * 50)
if failures:
    print('FIXTURE-PREFLIGHT-FAIL (%d)' % len(failures))
    for f in failures:
        print(' -', f)
    sys.exit(1)
print('FIXTURE-PREFLIGHT-OK: 四种模拟来源均可被当前构建的解析器识别')
