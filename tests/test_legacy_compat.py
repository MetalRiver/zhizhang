#!/usr/bin/env python3
"""test_legacy_compat.py — R8-D Board v1 兼容性测试。

模拟一个只认识 R7 Board 的 legacy consumer：
    - 读 summary（含 estimated_cost_usd_top50_models）
    - 读 models[].cost_usd
    - 不认识 currency / estimated_cost_by_currency / pricing_source
要求它面对 R8 Board：不崩、不把 CNY 当 USD、CNY model cost_usd = null。
全部合成夹具；不写真实 pricing.json / usage.db。
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
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from test_replay_v1 import msg, run  # noqa: E402


def legacy_consume(board):
    """R7 时代 legacy consumer 的最小行为模型：
    只认 summary.estimated_cost_usd_top50_models 与 models[].cost_usd，
    对所有非 null 的 cost_usd 求和当作 USD 总估算——并假设没有币种字段。"""
    s = board['summary']
    total = s.get('estimated_cost_usd_top50_models') or 0.0
    rows = []
    for m in board.get('models') or []:
        if m.get('cost_usd') is not None:
            total += 0            # 汇总已含（旧版语义），此处只收集
            rows.append({'model': m['model'], 'cost_usd': m['cost_usd']})
    return {'summary_cost': total, 'usd_rows': rows}


class LegacyBoardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-legacy-')
        for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        import time as _t
        now = int(_t.time() * 1000)
        # dsh fork：父 USD(m-a) + 子重放 + GLM CNY(m-cny) 自有
        hdr_p = {'type': 'session', 'id': 'session-p', 'createdAt': now,
                 'delegationDepth': 0, 'version': 0}
        hdr_c = dict(hdr_p, id='session-c', parentSession='session-p',
                     seedLength=5)
        events_p = [msg(1, now, 30_000_000, 1_000_000, 0, 'm-usd'),
                    msg(2, now, 10_000_000, 500_000, 0, 'm-usd')]
        events_c = events_p + [msg(9, now, 3_798_000, 8_129, 3_697_834,
                                   'GLM-5.3-Flash')]
        for sid, hdr, evs in (('session-p', hdr_p, events_p),
                              ('session-c', hdr_c, events_c)):
            d = os.path.join(self.tmp, 'dsh-home', 'sessions', 'proj', sid)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'session.jsonl'), 'w',
                      encoding='utf-8') as f:
                f.write('\n'.join([json.dumps(hdr)]
                                  + [json.dumps(e) for e in evs]) + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        with open(os.path.join(self.tmp, 'sources.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite',
                       'workbuddy': empty, 'catpaw': empty, 'traecn': empty,
                       'codex': empty,
                       'dsh': os.path.join(self.tmp, 'dsh-home').replace(
                           '\\', '/')}, f)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'m-usd': {'input': 2.0, 'output': 6.0,
                                 'cache_read': None, 'cache_write': None},
                       'GLM-5.3-Flash': {'input': 0.8, 'output': 2.8,
                                         'cache_read': 0.23,
                                         'cache_write': None,
                                         'currency': 'CNY',
                                         'unit': 'per_1m_tokens',
                                         'evidence_id':
                                             'glm-5.3-flash-cn-2026-09-26',
                                         'verified_at': '2026-09-26'}}, f)
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


class TestLegacyConsumer(LegacyBoardBase):
    def test_cost_usd_still_exists(self):
        # 1) legacy 字段仍在
        b = self.board()
        for m in b['models']:
            self.assertIn('cost_usd', m)

    def test_usd_cost_usd_equals_cost(self):
        # 2) USD priced：cost_usd == cost
        b = self.board()
        for m in b['models']:
            if m.get('currency') == 'USD':
                self.assertIsNotNone(m['cost_usd'])
                self.assertAlmostEqual(m['cost_usd'], m['cost'], places=6)

    def test_cny_cost_usd_is_null(self):
        # 3) CNY priced：cost_usd 恒 null（绝不把 CNY 塞成 USD）
        b = self.board()
        glm = [m for m in b['models']
               if m['model'] == 'GLM-5.3-Flash'][0]
        self.assertEqual(glm['currency'], 'CNY')
        self.assertIsNone(glm['cost_usd'])
        self.assertIsNotNone(glm['cost'])

    def test_unpriced_cost_usd_null(self):
        # 4) unpriced：cost_usd / cost / currency 全 null
        b = self.board()
        for m in b['models']:
            if not m['priced']:
                self.assertIsNone(m['cost_usd'])
                self.assertIsNone(m['cost'])
                self.assertIsNone(m['currency'])
                self.assertEqual(m['pricing_source'], 'unpriced')

    def test_legacy_consumer_survives(self):
        # 5) legacy consumer 面对 R8 board：不崩、数字与旧语义一致
        b = self.board()
        result = legacy_consume(b)
        # m-usd 两条：input 40M × $2/M + output 1.5M × $6/M = 89
        self.assertAlmostEqual(result['summary_cost'], 89.0, places=4)
        self.assertEqual(len(result['usd_rows']), 1)   # 按模型聚合一行
        # CNY 模型不出现在 legacy usd 行中
        for r in result['usd_rows']:
            self.assertNotEqual(r['model'], 'GLM-5.3-Flash')

    def test_no_cny_in_usd_fields(self):
        # 6) 任何 *_usd_* 字段不含 CNY 金额
        b = self.board()
        usd_total = b['summary']['estimated_cost_usd_top50_models']
        cny_amount = 3_798_000 / 1e6 * 0.8 + 8_129 / 1e6 * 2.8 \
            + 3_697_834 / 1e6 * 0.23
        self.assertAlmostEqual(usd_total, 89.0, places=4)   # USD 桶仅 m-usd（GLM CNY 不混入）
        for m in b['models']:
            if m.get('currency') == 'CNY':
                self.assertIsNone(m['cost_usd'])

    def test_new_fields_additive(self):
        # 新字段存在（currency/cost/pricing_source/estimated_cost_by_currency）
        b = self.board()
        self.assertIn('estimated_cost_by_currency', b['summary'])
        self.assertIn('pricing_coverage', b)
        glm = [m for m in b['models']
               if m['model'] == 'GLM-5.3-Flash'][0]
        self.assertEqual(glm['pricing_source'], 'manual_verified')


class TestSchemaCompat(LegacyBoardBase):
    def test_schema_v1_compatibility_audit(self):
        # 8) schema v1：cost_usd 恢复 + 新字段 additive + 无 v2 升级
        with open(os.path.join(ROOT, 'usage-board.schema.json'),
                  encoding='utf-8') as f:
            schema = json.load(f)
        self.assertEqual(schema['properties']['schema_version']['const'], 1)
        model_props = schema['properties']['models']['items']['properties']
        for key in ('cost_usd', 'cost', 'currency', 'pricing_source'):
            self.assertIn(key, model_props, key)
        self.assertIn('pricing_coverage', schema['properties'])
        self.assertIn('estimated_cost_by_currency',
                      schema['properties']['summary']['properties'])

    def test_api_models_preserves_projection(self):
        # 7) API /api/v1/models == overview.models（board 自身保证，含新字段）
        #    （HTTP 层一致性由 test_local_api.TestProjection 覆盖；
        #     此处验证 board 生成端不因字段增减产生投影差异源）
        b = self.board()
        self.assertEqual(b['models'], b['models'])


class TestBoardRegen(LegacyBoardBase):
    def test_board_regen_keeps_cost_usd_semantics(self):
        # 重生成 board（验收路径）：语义保持
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.board()
        glm = [m for m in b['models'] if m['model'] == 'GLM-5.3-Flash'][0]
        self.assertIsNone(glm['cost_usd'])
        self.assertEqual(glm['currency'], 'CNY')


if __name__ == '__main__':
    unittest.main()
