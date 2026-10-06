#!/usr/bin/env python3
"""test_pricing_semantics.py — Round 8 定价语义与覆盖率测试（全部合成夹具）。

覆盖：
    1  unknown price stays null
    2  zero price requires explicit verified_zero
    3  manual overrides auto（字段级合并）
    4  auto never overwrites manual（sync 合并方向）
    5  cost 基于 effective usage
    6  replay 不参与成本
    7  activity 不参与成本
    8  raw 审计成本仍可用
    9  cache_read 单独计价
    10 cache 单价缺失正确处理
    11 pricing_coverage 计算
    12 token coverage 计算
    13 unpriced ≠ $0
    14 语义常量（pricing_basis / cost_semantics）
    15 verified_zero merge 透传
    16 UI 覆盖率语义（静态检查）
    17 coverage 缺失时 UI 优雅降级

禁止对真实 usage.db / 真实 pricing.json 做任何写入。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import ledger  # noqa: E402
from test_replay_v1 import msg, run  # noqa: E402


def price_entry(**kw):
    base = {'input': None, 'output': None, 'cache_read': None, 'cache_write': None}
    base.update(kw)
    return base


class TestCostSemantics(unittest.TestCase):
    """cost_of 纯函数语义（零 IO）。"""

    def setUp(self):
        self.pricing = {
            'known-model': price_entry(input=2.0, output=6.0, cache_read=0.2),
            'partial-model': price_entry(input=2.0),          # 缓存单价缺失
            'null-model': price_entry(),                       # 全 null
            'free-unverified': price_entry(input=0, output=0),  # 全 0 无标记
            'free-verified': dict(price_entry(input=0, output=0),
                                  verified_zero=True),          # 有证据的 0
        }

    def test_unknown_stays_unconfigured(self):
        # 1) 未知模型：unconfigured，绝不折算 $0 当作"有价"
        c, ok = ledger.cost_of(self.pricing, 'never-seen', 1000, 100, 500, 0)
        self.assertFalse(ok)
        self.assertEqual(c, 0.0)

    def test_null_entry_unconfigured(self):
        c, ok = ledger.cost_of(self.pricing, 'null-model', 1000, 100, 0, 0)
        self.assertFalse(ok)

    def test_zero_requires_verified_flag(self):
        # 2) 无证据的全 0 = 未配置；有 verified_zero = 已配置、成本 0
        c, ok = ledger.cost_of(self.pricing, 'free-unverified', 1000, 100, 0, 0)
        self.assertFalse(ok)
        c2, ok2 = ledger.cost_of(self.pricing, 'free-verified', 1000, 100, 0, 0)
        self.assertTrue(ok2)
        self.assertEqual(c2, 0.0)

    def test_cache_read_priced_independently(self):
        # 9) cache_read 单列计价：2M cache × 0.2/M = 0.4 + 输入输出
        c, ok = ledger.cost_of(self.pricing, 'known-model',
                               1_000_000, 500_000, 2_000_000, 0)
        self.assertTrue(ok)
        self.assertAlmostEqual(c, 1_000_000 * 2.0 / 1e6 + 500_000 * 6.0 / 1e6
                               + 2_000_000 * 0.2 / 1e6)

    def test_missing_cache_rate_ignored(self):
        # 10) 缓存单价缺失：只按 input/output 计价，不崩溃不虚增
        c, ok = ledger.cost_of(self.pricing, 'partial-model',
                               1_000_000, 1_000_000, 5_000_000, 0)
        self.assertTrue(ok)
        # output 未定价 → 贡献 0（不虚增也不崩溃）；cache 5M 无单价 → 不计
        self.assertAlmostEqual(c, 2.0)

    def test_manual_overrides_auto_field_level(self):
        # 3+15) 手工覆盖 + verified_zero 经合并透传
        auto = {'free-verified': price_entry(input=1.0, output=2.0),
                'partial-model': dict(price_entry(input=9.0), _key='auto/x')}
        manual = {'free-verified': {'verified_zero': True},
                  'partial-model': price_entry(input=2.0)}
        merged = {}
        for k, v in auto.items():
            merged[k] = dict(v)
        for k, v in manual.items():
            merged[k] = ledger._merge_price_entry(merged.get(k), v)
        c, ok = ledger.cost_of(merged, 'free-verified', 1000, 100, 0, 0)
        self.assertTrue(ok)          # 手工 verified_zero 压过 auto 正数价
        self.assertEqual(c, 0.0)
        c2, ok2 = ledger.cost_of(merged, 'partial-model', 1_000_000, 0, 0, 0)
        self.assertTrue(ok2)
        self.assertAlmostEqual(c2, 2.0)   # 手工 2.0 覆盖 auto 9.0

    def test_auto_cannot_overwrite_manual_positive(self):
        # 4) 合并方向：auto 永远不覆盖手工已填的正数
        merged = {'m': dict(price_entry(input=1.0), _key='auto/x')}   # auto 为底
        merged['m'] = ledger._merge_price_entry(merged['m'],
                                                price_entry(input=5.0))  # 手工覆盖
        self.assertEqual(merged['m']['input'], 5.0)


class V1BoardBase(unittest.TestCase):
    """与 test_replay_v1 相同的沙盒模式：fork 夹具 + 定价 + board。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-price-')
        for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        import time as _t
        now = int(_t.time() * 1000)
        hdr_p = {'type': 'session', 'id': 'session-p', 'createdAt': now,
                 'delegationDepth': 0, 'version': 0}
        hdr_c = dict(hdr_p, id='session-c', parentSession='session-p',
                     seedLength=5)
        events_p = [msg(1, now, 10, 2, 30, 'm-a'), msg(2, now, 20, 4, 60, 'm-a')]
        events_c = events_p + [msg(9, now, 7, 1, 0, 'm-b')]
        for sid, hdr, evs in (('session-p', hdr_p, events_p),
                              ('session-c', hdr_c, events_c)):
            d = os.path.join(self.tmp, 'dsh-home', 'sessions', 'proj', sid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'session.jsonl'), 'w', encoding='utf-8') as f:
                f.write('\n'.join([json.dumps(hdr)] + [json.dumps(e) for e in evs])
                        + '\n')
        # catpaw activity 夹具
        cp = os.path.join(self.tmp, 'catpaw-projects', 'p1')
        os.makedirs(cp, exist_ok=True)
        with open(os.path.join(cp, 'conv1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(2):
                f.write(json.dumps({'messageId': 'm%d' % i,
                                    'conversationId': 'c1',
                                    'timestamp': '2026-09-0%dT00:00:00Z' % (i + 1)})
                        + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite', 'dsh': os.path.join(
                self.tmp, 'dsh-home').replace('\\', '/'),
                'workbuddy': empty, 'catpaw': os.path.join(
                    self.tmp, 'catpaw-projects').replace('\\', '/'),
                'traecn': empty, 'codex': empty}, f)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w', encoding='utf-8') as f:
            json.dump({'m-a': {'input': 1000.0, 'output': 2000.0, 'cache_read': None,
                               'cache_write': None}}, f)
        code, out = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(code, 0, out)
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def board(self):
        with open(os.path.join(self.tmp, 'usage-board.json'),
                  encoding='utf-8') as f:
            return json.load(f)


class TestUiSemantics(unittest.TestCase):
    def test_ui_shows_token_coverage_and_estimate_wording(self):
        # 16) UI 成本语义（Functional V1 runtime）：估算 ≠ 实际账单；
        # 未定价 ≠ 免费（无价格不当作 0 计入）
        src = open(os.path.join(ROOT, 'web', 'v1', 'app.js'),
                   encoding='utf-8').read()
        self.assertIn('估算成本', src)
        self.assertIn('按公开 API 价格估算', src)
        self.assertIn('不代表你的实际账单', src)
        self.assertIn('暂不能估算', src)
        self.assertIn('没有价格不代表免费', src)

    def test_ui_degrades_without_coverage_block(self):
        # 17) 无价格数据 → 诚实降级文案（不是 0 成本）
        src = open(os.path.join(ROOT, 'web', 'v1', 'app.js'),
                   encoding='utf-8').read()
        self.assertIn('unpriced', src)
        self.assertIn('部分用量暂不能估算', src)


class TestCoverageAndBoard(V1BoardBase):
    def test_board_pricing_coverage_block(self):
        # 11+12+14) 覆盖率块：数值自洽 + 语义常量
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.board()
        pc = b['pricing_coverage']
        self.assertEqual(pc['pricing_basis'], 'current_reference')
        self.assertEqual(pc['cost_semantics'], 'api_equivalent_estimate')
        self.assertEqual(pc['priced_models'] + pc['unpriced_models'],
                         pc['effective_models'])
        self.assertEqual(pc['priced_tokens'] + pc['unpriced_tokens'],
                         pc['effective_tokens'])
        self.assertEqual(pc['effective_tokens'], b['summary']['total_tokens'])
        exp_pct = round(pc['priced_tokens'] * 100.0 / pc['effective_tokens'], 2)
        self.assertEqual(pc['token_coverage_pct'], exp_pct)
        self.assertIsNotNone(pc['as_of'])

    def test_cost_uses_effective_and_excludes_replay(self):
        # 5+6) 成本只算 effective；重放行不贡献成本
        b = self.board()
        s = b['summary']
        # 父 2 条(m-a: 10+2缓存30, 20+4缓存60) + 子自有 1 条(m-b 无价)
        # m-a: input 30, output 6 → 成本 = 30×1/M + 6×2/M（pricing 见 setUp）
        self.assertAlmostEqual(s['estimated_cost_usd_top50_models'],
                               30 * 1000.0 / 1e6 + 6 * 2000.0 / 1e6, places=6)
        d = b['dedup']
        # raw 审计成本（含重放）必须大于 effective 成本
        self.assertGreater(d['raw_summary']['estimated_cost_usd_top50_models'],
                           s['estimated_cost_usd_top50_models'])

    def test_unpriced_not_zero_in_board(self):
        # 13) 未定价模型 cost_usd=null
        b = self.board()
        mb = [m for m in b['models'] if m['model'] == 'm-b'][0]
        self.assertIsNone(mb['cost'])               # Round 8-C：字段更名 cost（币种见 currency）
        self.assertIsNone(mb['currency'])
        self.assertFalse(mb['priced'])
        self.assertEqual(mb['pricing_source'], 'unpriced')   # R8-D：未定价标记

    def test_activity_excluded_from_cost(self):
        # 7) activity 记录不进入任何成本
        b = self.board()
        self.assertAlmostEqual(b['summary']['estimated_cost_usd_top50_models'],
                               30 * 1000.0 / 1e6 + 6 * 2000.0 / 1e6, places=4)


if __name__ == '__main__':
    unittest.main()
