#!/usr/bin/env python3
"""verify_baseline.py — 统一基线核对（零依赖，只读）。

核对本仓库的基线承诺是否仍然成立：
  1. Functional V1 正式 Runtime UI 在位（web/v1/shell.html、app.js、app.css）
  2. 运行态账本（usage.db）存在时哈希记录 / 计数与基线一致
  3. usage-board.json（若存在）与基线计数一致、privacy 承诺成立
  4. Core 出口协议文件（usage-board.schema.json）存在

历史：Round 2 曾冻结 V3.0-C UI（web/index.html）与五层场景资产的 SHA256；
V1.1 Legacy Retirement 后旧 UI/场景资产已退役（仅存于 Git 历史），对应
冻结哈希检查一并退出，改为 Functional V1 在位性检查。

与 VERSION、docs/architecture/UNIFIED_BASELINE.md 里的数字同源。
用法：python scripts/verify_baseline.py     （只读，绝不写任何文件）
"""
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---- 基线哈希 ----
# ---- Round 1 冒烟实测的账本基线计数 ----
BASELINE_COUNTS = {'usage': 34041, 'activity': 773, 'source_files': 173, 'scan_runs': 52}
V1_RUNTIME_FILES = (
    os.path.join('web', 'v1', 'shell.html'),
    os.path.join('web', 'v1', 'app.js'),
    os.path.join('web', 'v1', 'app.css'),
)


def main():
    errors = 0

    # ---- 1. Functional V1 Runtime UI 在位 ----
    for rel in V1_RUNTIME_FILES:
        p = os.path.join(ROOT, rel)
        ok = os.path.isfile(p)
        print('%s %s' % ('✓' if ok else '✗', rel))
        if not ok:
            errors += 1

    # ---- 3. 账本 ----
    db = os.path.join(ROOT, 'usage.db')
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect('file:%s?mode=ro' % db.replace('\\', '/'), uri=True)
            conn.row_factory = sqlite3.Row
            counts = {
                'usage': conn.execute('SELECT COUNT(*) n FROM usage_event').fetchone()['n'],
                'activity': conn.execute('SELECT COUNT(*) n FROM activity_event').fetchone()['n'],
                'source_files': conn.execute('SELECT COUNT(*) n FROM source_file').fetchone()['n'],
                'scan_runs': conn.execute('SELECT COUNT(*) n FROM scan_run').fetchone()['n'],
            }
            conn.close()
            for k, want in BASELINE_COUNTS.items():
                got = counts[k]
                ok = got >= want   # 账本只增不减；等于基线 = 未再扫描，大于 = 有新数据
                print('%s usage.db %-12s %6d（基线 %d，只增不减）'
                      % ('✓' if ok else '✗', k, got, want))
                if not ok:
                    errors += 1
        except Exception as e:
            print('✗ usage.db 只读核对失败：%s' % e)
            errors += 1
    else:
        print('· usage.db 不存在（本机尚未采集）——跳过账本核对')

    # ---- 4. board ----
    board = os.path.join(ROOT, 'usage-board.json')
    if os.path.isfile(board):
        try:
            with open(board, encoding='utf-8') as f:
                b = json.load(f)
            s = b.get('summary') or {}
            d = b.get('dedup') or {}
            # Round 5 起顶层 = EFFECTIVE；审计关系必须自洽：
            #   raw = replay + effective，且 raw 不少于历史基线（账本只增不减）
            ok = True
            if d:
                ok = (d.get('raw_events') == (d.get('replay_events') or 0)
                      + (d.get('effective_events') or 0))
                ok = ok and (d.get('raw_events') or 0) >= BASELINE_COUNTS['usage']
                ok = ok and (s.get('usage_events') == d.get('effective_events'))
                print('%s board 审计关系 raw(%s) = replay(%s) + effective(%s)'
                      % ('✓' if ok else '✗', d.get('raw_events'),
                         d.get('replay_events'), d.get('effective_events')))
            else:
                ok = (s.get('usage_events') or 0) >= BASELINE_COUNTS['usage']
                print('%s usage-board.json usage_events=%s（无 dedup 块）'
                      % ('✓' if ok else '✗', s.get('usage_events')))
            if not ok:
                errors += 1
            if (b.get('privacy') or {}).get('paths_included') is not False:
                print('✗ privacy.paths_included 必须 false')
                errors += 1
        except Exception as e:
            print('✗ usage-board.json 不可读：%s' % e)
            errors += 1
    else:
        print('· usage-board.json 不存在（运行产物，不入库）——跳过')

    # ---- 5. 出口协议 ----
    schema = os.path.join(ROOT, 'usage-board.schema.json')
    ok = os.path.isfile(schema)
    print('%s usage-board.schema.json %s' % ('✓' if ok else '✗', '存在' if ok else '缺失'))
    if not ok:
        errors += 1

    print()
    if errors:
        print('结果：✗ %d 项基线承诺被破坏' % errors)
        return 1
    print('结果：✓ 统一基线完好')
    return 0


if __name__ == '__main__':
    sys.exit(main())
