#!/usr/bin/env python3
"""test_effective_default.py — Round 5 默认口径切换测试（全部合成 fixture）。

覆盖：
    1  report 默认 = effective
    2  --view raw 仍可用（审计口径）
    3  board summary = effective
    4  board daily 不含 replay
    5  board clients 不含 replay
    6  board models 不含 replay（含 replay-only 模型区分）
    7  dedup 关系：raw = replay + effective；default_view=effective；raw_summary
    8  成本基于 effective（含价 fixture）；未定价 cost_usd=null
    9  activity 不受口径切换影响
    10 opaque_stores 保留
    12 UI LIVE 模式状态（headless Chrome，skip 无 chrome 环境）
    13 UI SNAPSHOT 模式状态（headless Chrome）
    14 UI window_days 真实性（90 天档标注 over-window）

禁止依赖生产固定绝对数字；全部关系断言 + 合成夹具。
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_replay_v1 import Base as V1Base, msg, run


class EffBase(V1Base):
    """标准夹具：父(m-a ×2) + 子 fork（种子区重放 ×2 + 自有 m-b ×1）+ 定价。
    时间戳取近期（board daily 有 45 天窗口，1970 年代数据会落窗外）。"""

    def setUp(self):
        super().setUp()
        import time as _t
        now = int(_t.time() * 1000)
        t1, t2, t3 = now - 7200_000, now - 3600_000, now - 60_000
        self.write_dsh_session('session-p', [msg(1, t1, 10, 2, 30, 'm-a'),
                                             msg(2, t2, 20, 4, 60, 'm-a')])
        self.write_dsh_session('session-c',
                               [msg(1, t1, 10, 2, 30, 'm-a'),
                                msg(2, t2, 20, 4, 60, 'm-a'),
                                msg(9, t3, 7, 1, 0, 'm-b')],
                               parent='session-p', seed=5)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w', encoding='utf-8') as f:
            json.dump({'m-a': {'input': 1.0, 'output': 2.0, 'cache_read': None,
                               'cache_write': None}}, f)
        self.scan()
        code, out = run(['ledger.py', 'board'], self.tmp)
        assert code == 0, out


class TestDefaultEffective(EffBase):
    def test_report_default_is_effective(self):
        code, out = run(['ledger.py', 'report', '--by', 'client'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('已剔除', out)
        self.assertIn('已定价成本合计', out)

    def test_report_view_raw_explicit(self):
        code, out = run(['ledger.py', 'report', '--by', 'client', '--view', 'raw'],
                        self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('完整账本', out)

    def board(self):
        with open(os.path.join(self.tmp, 'usage-board.json'), encoding='utf-8') as f:
            return json.load(f)

    def test_board_summary_is_effective(self):
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.board()
        self.assertEqual(b['summary']['usage_events'], 3)
        self.assertEqual(b['summary']['input_tokens'], 37)
        self.assertEqual(b['summary']['output_tokens'], 7)

    def test_board_daily_excludes_replay(self):
        b = self.board()
        self.assertEqual(sum(r['events'] for r in b['daily']), 3)

    def test_board_clients_exclude_replay(self):
        b = self.board()
        dsh = [c for c in b['clients'] if c['client'] == 'dsh'][0]
        self.assertEqual(dsh['events'], 3)

    def test_board_models_exclude_replay(self):
        # 6) replay-only 的重复计数不得让 m-a 变成 4 条；m-b 仍在
        b = self.board()
        mods = {m['model']: m for m in b['models']}
        self.assertEqual(mods['m-a']['events'], 2)
        self.assertEqual(mods['m-b']['events'], 1)

    def test_dedup_relation(self):
        b = self.board()
        d = b['dedup']
        self.assertEqual(d['default_view'], 'effective')
        self.assertEqual(d['raw_events'], d['replay_events'] + d['effective_events'])
        self.assertEqual(d['raw_summary']['usage_events'], 5)
        self.assertEqual(d['effective_summary']['usage_events'], 3)
        # by_client：zcode/workbuddy 无 replay（此处仅 dsh 在账本中）
        for row in d['by_client']:
            if row['client'] != 'dsh':
                self.assertEqual(row['replay'], 0)

    def test_board_cost_uses_effective(self):
        b = self.board()
        s = b['summary']
        d = b['dedup']
        # m-a 有效输入 30 token × $1/M = 0.00003；raw 为 60 token × $1/M = 0.00006
        self.assertIsNotNone(s['estimated_cost_usd_top50_models'])
        self.assertEqual(s['estimated_cost_usd_top50_models'],
                         d['effective_summary']['estimated_cost_usd_top50_models'])
        self.assertNotEqual(s['estimated_cost_usd_top50_models'],
                            d['raw_summary']['estimated_cost_usd_top50_models'])
        # 未定价模型（m-b）cost_usd 必须为 null
        mb = [m for m in b['models'] if m['model'] == 'm-b'][0]
        self.assertIsNone(mb['cost'])               # R8-C：cost_usd → cost（币种见 currency）
        self.assertIsNone(mb['currency'])
        self.assertFalse(mb['priced'])

    def test_activity_unaffected(self):
        # 9) activity 与口径切换无关：catpaw 夹具计数原样
        cp = os.path.join(self.tmp, 'catpaw-projects', 'p1')
        os.makedirs(cp, exist_ok=True)
        with open(os.path.join(cp, 'conv1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({'messageId': 'm%d' % i,
                                    'conversationId': 'c1',
                                    'timestamp': '2026-09-01T0%d:00:00Z' % i}) + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite', 'workbuddy': empty,
                       'traecn': empty, 'catpaw': os.path.join(
                           self.tmp, 'catpaw-projects').replace('\\', '/'),
                       'dsh': os.path.join(self.tmp, 'dsh-home').replace('\\', '/'),
                       'codex': empty}, f)
        code, out = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(code, 0, out)
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.board()
        act = [a for a in b['activity'] if a['client'] == 'catpaw'][0]
        self.assertEqual(act['records'], 3)
        self.assertEqual(b['summary']['usage_events'], 3)   # 口径不受 activity 影响

    def test_opaque_stores_preserved(self):
        b = self.board()
        self.assertIn('opaque_stores', b)          # 键保留（沙盒中通常为空列表）
        self.assertIsInstance(b['opaque_stores'], list)
        self.assertIn('dedup', b)


if __name__ == '__main__':
    unittest.main()
