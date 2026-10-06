#!/usr/bin/env python3
"""apply_pricing_proposal.py — 把已获人工批准的拟价写入 pricing.json（R8-C）。

安全设计：
    1. 先备份 pricing.json → pricing.json.bak-r8（字节级复制，gitignore）
    2. 只写入本脚本内 WHITELIST 中人工批准的条目（默认仅 GLM-5.3-Flash）
    3. 每条目必须携带 currency / unit / evidence_id / verified_at —— 禁止隐含 USD
    4. 幂等：重复运行结果一致
    5. 写入后自动重出 board，使估算成本立即生效

用法：
    .venv/Scripts/python scripts/apply_pricing_proposal.py            # 正式（仓库根）
    .venv/Scripts/python scripts/apply_pricing_proposal.py --show     # 只预览
    .venv/Scripts/python scripts/apply_pricing_proposal.py --root X   # 沙盒（测试）
运行前请先阅读 docs/verification/R8_PRICING_PROPOSAL.md 并获得人工批准。
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---- 人工批准白名单（R8-B 拟价表 APPROVE CANDIDATE 项）----
# 修改本表 = 修改人工批准范围；未列出的模型一律不写。
# currency 必填：USD 是 USD，CNY 是 CNY；unit 固定 per_1m_tokens。
WHITELIST = {
    'GLM-5.3-Flash': {
        'input': 0.8, 'output': 2.8, 'cache_read': 0.23, 'cache_write': None,
        'currency': 'CNY', 'unit': 'per_1m_tokens',
        'evidence_id': 'glm-5.3-flash-cn-2026-09-26',
        'verified_at': '2026-09-26',
    },
}
CURRENCY_NOTE = {
    'GLM-5.3-Flash': ('CNY', 'Tier B：晚点 LatePost / 新浪财经多源转述智谱官方定价；'
                      'Reference API-equivalent rate — source usage may '
                      'originate from subscription/Coding Plan channel'),
}


def apply(root, show=False):
    path = os.path.join(root, 'pricing.json')
    with open(path, encoding='utf-8') as f:
        current = json.load(f)

    print('  拟写入条目（白名单 %d 项）：' % len(WHITELIST))
    for m, v in WHITELIST.items():
        cur = current.get(m) or {}
        note = CURRENCY_NOTE.get(m, ('', ''))
        print('    %-18s currency=%s rates=%s' % (m, note[0],
              {k: val for k, val in v.items() if k in ('input', 'output',
                                                       'cache_read',
                                                       'cache_write')}))
        print('      当前：%s' % cur)
    if show:
        return 0

    bak = os.path.join(root, 'pricing.json.bak-r8')
    shutil.copy2(path, bak)
    print('  备份：%s（sha256 %s）'
          % (bak, hashlib.sha256(open(bak, 'rb').read()).hexdigest()[:16]))

    for m, v in WHITELIST.items():
        cur = current.setdefault(m, {'input': None, 'output': None,
                                     'cache_read': None, 'cache_write': None})
        for k, val in v.items():
            cur[k] = val
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    print('  已写入 pricing.json（幂等；重复运行无副作用）')

    # 子进程刷新 board：fresh interpreter 必然加载 root 下的 ledger/autopilot，
    # 杜绝模块缓存把写入引向别的账本（R7 已在 serve 上采用同一模式）
    r = subprocess.run([sys.executable, os.path.join(root, 'ledger.py'), 'board'],
                       cwd=root, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=300)
    print((r.stdout or '') + (r.stderr or ''))
    return 0 if r.returncode == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--show', action='store_true', help='只预览将写入的内容')
    ap.add_argument('--root', default=ROOT,
                    help='仓库根（默认脚本所在仓库；测试用沙盒路径）')
    args = ap.parse_args()
    return apply(os.path.abspath(args.root), show=args.show)


if __name__ == '__main__':
    sys.exit(main())
