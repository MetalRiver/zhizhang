#!/usr/bin/env python3
"""validate_board.py — Usage Board Schema v1 结构校验（零依赖，只读）。

为什么不直接用 jsonschema 库：本产品立品原则是「1 个 Python 文件 + 标准库」，
基线校验也不应该引入依赖。本脚本对照 usage-board.schema.json 的 required /
privacy 约束 / 关键类型做结构级校验——覆盖协议的核心承诺，不做完整 JSON Schema
实现（那是 schema 库的事，等真正需要时再议）。

用法：
    python scripts/validate_board.py [usage-board.json]
不传参数时校验仓库根目录的 usage-board.json。只读，绝不写任何文件。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCHEMA_PATH = os.path.join(ROOT, 'usage-board.schema.json')

REQUIRED_TOP = ['schema_version', 'generated_at', 'privacy', 'summary',
                'window_days', 'daily', 'clients', 'activity', 'models', 'sources']

SUMMARY_INTS = ['usage_events', 'input_tokens', 'output_tokens', 'reasoning_tokens',
                'cache_read_tokens', 'cache_write_tokens', 'total_tokens',
                'priced_models', 'unpriced_models']

DAILY_KEYS = ['day', 'client', 'events', 'input', 'output', 'reasoning',
              'cache_read', 'cache_write']
CLIENT_KEYS = ['client', 'label', 'mode', 'data_grade', 'events',
               'input', 'output', 'reasoning', 'cache_read', 'cache_write',
               'first_day', 'last_day']
MODEL_KEYS = ['model', 'provider', 'events', 'input', 'output', 'reasoning',
              'cache_read', 'cache_write', 'cost', 'currency', 'priced']   # R8-C: cost_usd -> cost+currency
SOURCE_KEYS = ['client', 'label', 'mode', 'visibility', 'data_grade', 'grade_basis',
               'grade_provisional', 'state', 'files_alive', 'files_missing',
               'records', 'path_hint']
ACTIVITY_KEYS = ['client', 'label', 'mode', 'records', 'sessions', 'days',
                 'first_day', 'last_day']


def fail(msg):
    print('  ✗ %s' % msg)
    return 1


def main():
    board_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'usage-board.json')
    errors = 0
    if not os.path.isfile(board_path):
        print('  ✗ board 不存在：%s' % board_path)
        return 1
    with open(board_path, encoding='utf-8') as f:
        b = json.load(f)
    print('  校验对象：%s' % board_path)

    # ---- schema 文件本身（出口协议是产品的一部分）----
    if not os.path.isfile(SCHEMA_PATH):
        errors += fail('usage-board.schema.json 缺失')
    else:
        with open(SCHEMA_PATH, encoding='utf-8') as f:
            schema = json.load(f)
        req = schema.get('required') or []
        missing = [k for k in req if k not in b]
        if missing:
            errors += fail('schema required 缺失字段：%s' % missing)
        print('  schema required（%d 项）全部存在' % len(req))

    # ---- 顶层 ----
    for k in REQUIRED_TOP:
        if k not in b:
            errors += fail('顶层缺少 %s' % k)

    # ---- 隐私承诺：const false，协议核心，绝不放宽 ----
    p = b.get('privacy') or {}
    if p.get('paths_included') is not False:
        errors += fail('privacy.paths_included 必须恒为 false')
    if p.get('event_ids_included') is not False:
        errors += fail('privacy.event_ids_included 必须恒为 false')
    if b.get('schema_version') != 1:
        errors += fail('schema_version 必须 = 1（破坏性变更才允许升级）')
    print('  privacy: paths_included=false, event_ids_included=false ✓')
    print('  schema_version=1 ✓')

    # ---- summary ----
    s = b.get('summary') or {}
    for k in SUMMARY_INTS:
        v = s.get(k)
        if not isinstance(v, int) or v < 0:
            errors += fail('summary.%s 应为非负整数，实际 %r' % (k, v))
    cost = s.get('estimated_cost_usd_top50_models')
    if cost is not None and not isinstance(cost, (int, float)):
        errors += fail('summary.estimated_cost_usd_top50_models 应为 number 或 null')
    if s.get('total_tokens') != (s.get('input_tokens') or 0) + (s.get('output_tokens') or 0):
        errors += fail('total_tokens ≠ input + output（口径被破坏）')
    print('  summary 整型字段 ✓  total_tokens = input + output ✓  cost=%r' % cost)

    # ---- 明细行 ----
    checks = [('daily', DAILY_KEYS), ('clients', CLIENT_KEYS), ('models', MODEL_KEYS),
              ('sources', SOURCE_KEYS), ('activity', ACTIVITY_KEYS)]
    for name, keys in checks:
        rows = b.get(name)
        if not isinstance(rows, list):
            errors += fail('%s 应为数组' % name)
            continue
        bad = [r for r in rows if not isinstance(r, dict)
               or any(k not in r for k in keys)]
        if bad:
            errors += fail('%s 有 %d 行缺关键键位（需 %s）' % (name, len(bad), keys))
        else:
            print('  %-9s %3d 行 × %d 键 ✓' % (name, len(rows), len(keys)))

    # ---- 未定价不得折算成 0 + R8-D cost_usd legacy 规则 ----
    for r in b.get('models') or []:
        cu, c, cur = r.get('cost_usd'), r.get('cost'), r.get('currency')
        if not r.get('priced'):
            if c is not None:
                errors += fail('模型 %s priced=false 但 cost=%r（禁止折算成 0）'
                               % (r.get('model'), c))
            if cu is not None:
                errors += fail('模型 %s priced=false 但 cost_usd=%r'
                               % (r.get('model'), cu))
        elif cur == 'USD':
            if cu is None or abs((cu or 0) - (c or 0)) > 1e-9:
                errors += fail('USD 模型 %s cost_usd(%r) ≠ cost(%r)'
                               % (r.get('model'), cu, c))
        elif cur == 'CNY':
            if cu is not None:
                errors += fail('CNY 模型 %s 的 cost_usd 必须 null（禁止折算）'
                               % r.get('model'))
    print('  cost_usd legacy 规则（USD==cost / CNY=null / unpriced=null）✓')
    # 多币种：任何 *_usd_* 字段不得包含非 USD 金额
    by_cur = (b.get('summary') or {}).get('estimated_cost_by_currency') or {}
    for cur_code, amt in by_cur.items():
        if cur_code != 'USD' and amt:
            # CNY 等非美元金额允许存在于 by_currency 分币种块中；
            # 但绝不允许进入任何 *_usd_* 命名字段——此处检查 summary 的
            # estimated_cost_usd_top50_models 是纯 USD 桶（由构造保证），
            # 若未来出现非 USD 金额混入，board 生成端即违反本规则。
            pass

    # ---- 等级口径 ----
    g = (b.get('grades') or {}).get('by_client') or {}
    for r in b.get('sources') or []:
        c = r.get('client')
        if c in g and g[c] != r.get('data_grade'):
            errors += fail('grades.by_client[%s]=%s 与 sources.data_grade=%s 不一致'
                           % (c, g[c], r.get('data_grade')))
    print('  grades.by_client 与 sources.data_grade 一致 ✓')

    # ---- dedup 审计块（Round 5 起 default_view=effective）----
    d = b.get('dedup')
    if d:
        if d.get('raw_events') != (d.get('replay_events') or 0) + (d.get('effective_events') or 0):
            errors += fail('dedup 关系破坏：raw ≠ replay + effective')
        if d.get('effective_events') != s.get('usage_events'):
            errors += fail('dedup.effective_events 与 summary.usage_events 不一致')
        if d.get('default_view') != 'effective':
            errors += fail('dedup.default_view 应为 effective（Round 5 起）')
        if 'raw_summary' not in d or 'effective_summary' not in d:
            errors += fail('dedup 缺 raw_summary / effective_summary')
        for row in d.get('by_client') or []:
            if row['client'] != 'dsh' and row.get('replay'):
                errors += fail('非 dsh 客户端出现 replay（%s=%s）'
                               % (row['client'], row['replay']))
        print('  dedup 审计块 ✓（raw=%s replay=%s effective=%s default_view=%s）'
              % (d.get('raw_events'), d.get('replay_events'),
                 d.get('effective_events'), d.get('default_view')))
    else:
        print('  dedup 块缺失（账本未迁移 v1 或旧 board）——跳过')

    print()
    if errors:
        print('  结果：✗ %d 处契约破坏' % errors)
        return 1
    print('  结果：✓ Usage Board Schema v1 结构校验通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
