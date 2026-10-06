#!/usr/bin/env python3
"""pathorbit_adapter.py — PathOrbit 知识库看板的 AI Usage 适配器。

适配原则（硬约束）：
    只读 usage-board.json 出口，**不碰账本 SQLite、不写知识库正文**。
    数据链路：usage-ledger → usage-board.json → 本适配器 → AI Usage 模块 → 看板

本适配器会拒绝：
    - 含本机完整路径的数据（privacy.paths_included 必须为 false）
    - 含 event id 的数据（privacy.event_ids_included 必须为 false）
    - 未声明 schema 版本、或版本不是 1 的数据
    - 目标 HTML 里没有明确插槽的「盲插」

用法：
    python pathorbit_adapter.py usage-board.json --module-out ai-usage-module.json
    python pathorbit_adapter.py usage-board.json --board-html 模板/board.html --out-html /tmp/board-with-usage.html
    python pathorbit_adapter.py usage-board.json --check          # 只校验，不产出
"""
from __future__ import annotations

import argparse
import json
import os
import sys

MODULE_SCHEMA = 1
MARKER = '<!-- PATHORBIT_AI_USAGE -->'
REQUIRED_TOP = ('schema_version', 'generated_at', 'privacy', 'summary',
                'window_days', 'daily', 'clients', 'activity', 'models', 'sources')
EXIT_OK, EXIT_BAD_INPUT, EXIT_NO_SLOT = 0, 2, 3


class AdapterError(Exception):
    pass


# ---------------------------------------------------------------- 校验

def validate(data):
    """校验 Usage Board Schema v1。失败抛 AdapterError（含可读原因）。"""
    if not isinstance(data, dict):
        raise AdapterError('usage-board.json 顶层必须是对象')
    missing = [k for k in REQUIRED_TOP if k not in data]
    if missing:
        raise AdapterError('usage-board.json 缺少必需字段：%s' % ', '.join(missing))
    if data['schema_version'] != 1:
        raise AdapterError('不支持的 schema_version=%r（本适配器只认 1）'
                           % data['schema_version'])
    priv = data.get('privacy') or {}
    if priv.get('paths_included') is not False:
        raise AdapterError('拒绝：数据声明包含本机完整路径（paths_included != false）')
    if priv.get('event_ids_included') is not False:
        raise AdapterError('拒绝：数据声明包含 event id（event_ids_included != false）')
    if not isinstance(data['window_days'], int) or data['window_days'] < 1:
        raise AdapterError('window_days 必须是 >=1 的整数')
    for key in ('daily', 'clients', 'activity', 'models', 'sources'):
        if not isinstance(data[key], list):
            raise AdapterError('字段 %s 必须是数组' % key)
    if not isinstance(data['summary'], dict):
        raise AdapterError('字段 summary 必须是对象')

    # 兜底：即使声明为 false，也扫一遍有没有明显泄漏
    blob = json.dumps(data, ensure_ascii=False)
    for pat in ('C:\\', 'D:\\', '/Users/', '/home/'):
        if pat in blob:
            raise AdapterError('拒绝：数据里出现了疑似本机路径片段 %r' % pat)
    return True


# ---------------------------------------------------------------- 映射

def _num(v):
    return v if isinstance(v, (int, float)) else 0


def to_module(data):
    """映射成看板模块所需的稳定结构。金额缺价时给 None，绝不折算成 0。"""
    s = data['summary']
    sources = data.get('sources') or []

    def count_mode(m):
        return sum(1 for x in sources if x.get('mode') == m)

    # 数据覆盖优先用 Core 给出的 data_grade（权威结论）。
    # 只有在旧版 board 没有 grades 时才退回按 mode 计数。
    grades = data.get('grades') or {}
    counts = grades.get('counts')
    if isinstance(counts, dict) and counts:
        coverage = {
            'token_visible': _num(counts.get('TOKEN')),
            'activity_only': _num(counts.get('ACTIVITY')),
            'hidden': _num(counts.get('UNKNOWN')),
        }
    else:
        coverage = {
            'token_visible': count_mode('usage'),
            'activity_only': count_mode('activity'),
            'hidden': count_mode('unknown'),
        }

    opaque = data.get('opaque_stores') or []

    clients = []
    for row in data.get('clients') or []:
        clients.append({
            'client': row.get('client'),
            'label': row.get('label') or row.get('client'),
            'mode': row.get('mode'),
            'data_grade': row.get('data_grade') or (
                'TOKEN' if row.get('mode') == 'usage' else 'ACTIVITY'),
            'events': _num(row.get('events')),
            'input': _num(row.get('input')),
            'output': _num(row.get('output')),
            'cache_read': _num(row.get('cache_read')),
        })

    cost = s.get('estimated_cost_usd_top50_models')
    if not s.get('priced_models'):
        cost = None

    activity = []
    for row in data.get('activity') or []:
        activity.append({
            'client': row.get('client'),
            'label': row.get('label') or row.get('client'),
            'records': _num(row.get('records')),
            'sessions': _num(row.get('sessions')),
            'days': _num(row.get('days')),
        })

    retention = []
    for x in sources:
        retention.append({
            'client': x.get('client'),
            'label': x.get('label'),
            'data_grade': x.get('data_grade'),
            'miss': _num(x.get('files_missing')),
            'files': _num(x.get('files_alive')),
            'records': _num(x.get('records')),
            'state': x.get('state'),
        })

    automation = data.get('automation') or {}

    return {
        'module_schema': MODULE_SCHEMA,
        'title': 'AI 使用',
        'subtitle': '本地 AI 用量账本',
        'generated_at': data.get('generated_at'),
        'window_days': data.get('window_days'),
        'range': [s.get('usage_first_day'), s.get('usage_last_day')],
        'kpi': {
            'tokens': _num(s.get('input_tokens')) + _num(s.get('output_tokens')),
            'cache_read_tokens': _num(s.get('cache_read_tokens')),
            'requests': _num(s.get('usage_events')),
            'cost_usd': cost,
            'cost_configured': bool(s.get('priced_models')),
            'unpriced_models': _num(s.get('unpriced_models')),
        },
        'coverage': coverage,
        'grades': grades,
        'opaque_stores': opaque,
        'clients': clients,
        'activity': activity,
        'models': (data.get('models') or [])[:10],
        'retention': retention,
        'sources': sources,
        'automation': {
            'status': automation.get('status'),
            'exit_code': automation.get('exit_code'),
            'last_run_at': automation.get('last_run_at'),
            'sources_found': automation.get('sources_found'),
            'sources_scanned': automation.get('sources_scanned'),
            'pricing_status': automation.get('pricing_status'),
        } if automation else None,
        'source': 'usage-ledger',
    }


# ---------------------------------------------------------------- 渲染 / 注入

def _fmt_cost(module):
    return ('未配置' if not module['kpi']['cost_configured']
            else '$%.2f' % (module['kpi']['cost_usd'] or 0.0))


def render_static_slot(module):
    """生成自包含的模块片段（无外部资源、无 CDN）。"""
    k = module['kpi']
    cv = module['coverage']
    c = module['clients']
    rows = ''.join(
        '<div class="po-ai-row"><span>%s</span><span>%s</span><span>%s</span></div>'
        % (x['label'], x['mode'] == 'activity' and '仅活动量' or '含 Token',
           f"{x['input']:,}")
        for x in c) or '<div class="po-ai-row po-ai-empty">没有产生 Token 的客户端</div>'
    ret = module['retention'] or []
    miss = sum(x['miss'] for x in ret)
    ret_note = ('已清退源文件 %d 个，记录仍留在账本里' % miss) if miss else '所有源文件都在'
    auto = module.get('automation') or {}
    auto_txt = ('自动采集：%s（%s）' % (auto.get('status'), auto.get('last_run_at'))
                if auto.get('status') else '自动采集：尚未运行 autopilot')
    return (
        '<section class="po-ai-usage" data-module="ai-usage" data-schema="%d">\n'
        '  <div class="po-ai-head">\n'
        '    <div class="po-ai-eyebrow">AI USAGE</div>\n'
        '    <div class="po-ai-title">%s</div>\n'
        '    <div class="po-ai-sub">%s · 最近 %s 天</div>\n'
        '  </div>\n'
        '  <div class="po-ai-kpis">\n'
        '    <div class="po-ai-kpi"><b>%s</b><span>Requests</span></div>\n'
        '    <div class="po-ai-kpi"><b>%s</b><span>Tokens</span></div>\n'
        '    <div class="po-ai-kpi"><b>%s</b><span>Cost 估算</span></div>\n'
        '    <div class="po-ai-kpi"><b>%s</b><span>Cache Read</span></div>\n'
        '  </div>\n'
        '  <div class="po-ai-rows">%s</div>\n'
        '  <div class="po-ai-foot">\n'
        '    <span>Token 可见 %d · 仅活动量 %d · 不可见 %d</span>\n'
        '    <span>%s</span>\n'
        '    <span>%s</span>\n'
        '  </div>\n'
        '</section>'
        % (module['module_schema'], module['title'], module['subtitle'],
           module['window_days'],
           f"{k['requests']:,}", f"{k['tokens']:,}", _fmt_cost(module),
           f"{k['cache_read_tokens']:,}",
           rows, cv['token_visible'], cv['activity_only'], cv['hidden'],
           ret_note, auto_txt))


def inject_slot(board_html, out_html, module):
    """只在 board.html 里存在明确插槽时注入；否则报错，绝不盲目插入。"""
    with open(board_html, encoding='utf-8') as f:
        text = f.read()
    if MARKER not in text:
        raise AdapterError(
            '目标 HTML 没有明确插槽 %s，拒绝盲目插入。'
            '请在模板里显式加入该 marker 后再注入。' % MARKER)
    out = text.replace(MARKER, render_static_slot(module))
    tmp = str(out_html) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(out)
    os.replace(tmp, out_html)
    return True


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description='PathOrbit AI Usage 适配器（只读账本出口）')
    ap.add_argument('usage_board', help='usage-board.json 路径')
    ap.add_argument('--module-out', help='输出模块 JSON')
    ap.add_argument('--board-html', help='输入看板 HTML（含插槽）')
    ap.add_argument('--out-html', help='输出看板 HTML')
    ap.add_argument('--check', action='store_true', help='只校验，不产出')
    args = ap.parse_args()

    try:
        with open(args.usage_board, encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print('无法读取 %s：%s' % (args.usage_board, e), file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        validate(data)
    except AdapterError as e:
        print('校验失败：%s' % e, file=sys.stderr)
        return EXIT_BAD_INPUT

    module = to_module(data)
    if args.check:
        print('校验通过：schema_version=%d，模块可生成（%d 个客户端）'
              % (data['schema_version'], len(module['clients'])))
        return EXIT_OK

    if args.module_out:
        tmp = args.module_out + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(module, f, ensure_ascii=False, indent=2)
        os.replace(tmp, args.module_out)
        print('已写入模块：%s' % args.module_out)

    if bool(args.board_html) != bool(args.out_html):
        ap.error('--board-html 与 --out-html 必须成对出现')
    if args.board_html:
        try:
            inject_slot(args.board_html, args.out_html, module)
        except AdapterError as e:
            print('注入失败：%s' % e, file=sys.stderr)
            return EXIT_NO_SLOT
        print('已注入插槽：%s' % args.out_html)

    if not args.module_out and not args.board_html:
        print(json.dumps(module, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == '__main__':
    sys.exit(main())
