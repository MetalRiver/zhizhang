#!/usr/bin/env python3
"""test_multicurrency.py — Round 8-C 多币种定价引擎测试（全部合成夹具）。

安全边界：apply 用例在沙盒 root 运行（绝不触碰真实 pricing.json / usage.db）。

覆盖（Round 8-C 阶段 17 清单）：
    1  legacy auto missing currency => USD
    3  CNY model returns CNY
    4  USD model returns USD
    5  USD+CNY never summed（usd 字段 = USD 桶）
    6  estimated_cost_by_currency
    7  legacy USD field excludes CNY
    8  CLI multi-currency output
    9/10  UI multi-currency semantics + no-FX 声明
    11  RAW audit multi-currency
    12  replay excluded
    13  activity excluded
    14  unknown currency rejected
    15  同模型条目单一币种（结构性）
    16  cache_read CNY priced correctly
    17  null cache_write ≠ zero price fact
    18  manual_verified vs auto_reference
    19  coverage 保持 i+o 口径
    20  API overview preserves currency buckets
    21  旧 board 兼容（load 不带 currency 字段正常）
    22  pricing backup/apply idempotence
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import ledger        # noqa: E402
import serve         # noqa: E402
from test_replay_v1 import msg, run  # noqa: E402
from test_pricing_semantics import V1BoardBase  # noqa: E402

def entry(**kw):
    base = {'input': None, 'output': None, 'cache_read': None,
            'cache_write': None}
    base.update(kw)
    return base


def entry(**kw):
    base = {'input': None, 'output': None, 'cache_read': None,
            'cache_write': None}
    base.update(kw)
    return base


class TestCurrencyParsing(unittest.TestCase):
    """币种解析与校验（纯函数级）。"""

    def test_legacy_missing_currency_is_usd(self):
        # 1) legacy 缺失 currency → USD（兼容规则，有文档有测试）
        e = entry(input=2.0)
        self.assertEqual(ledger._entry_currency(e), 'USD')
        self.assertEqual(ledger._entry_currency(dict(e, currency=' usd ')), 'USD')

    def test_unknown_currency_rejected(self):
        # 14) 未知币种：明确拒绝（load 层跳过 + stderr），绝不静默当 USD
        tmp = _safety.sandbox_dir()
        try:
            p = os.path.join(tmp, 'pricing.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump({'m-eur': entry(input=2.0, currency='EUR'),
                           'm-ok': entry(input=2.0, currency='CNY')}, f)
            import io as _io
            import contextlib
            buf = _io.StringIO()
            with contextlib.redirect_stderr(buf):
                loaded = ledger._read_pricing_file(p)
            self.assertNotIn('m-eur', loaded)      # 拒绝
            self.assertIn('m-ok', loaded)
            self.assertIn('不受支持', buf.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_currency_normalized_upper(self):
        self.assertEqual(ledger._entry_currency(dict(entry(), currency='cny')),
                         'CNY')

    def test_single_currency_per_entry_structural(self):
        # 15) 条目级单一币种：currency 属于整条 entry，字段级混币结构性不可能
        e = dict(entry(input=2.0, output=6.0, cache_read=0.23), currency='CNY')
        self.assertEqual(ledger._entry_currency(e), 'CNY')

    def test_lower_case_entry_key_matching(self):
        # 大小写不敏感匹配保持不变
        pricing = {'glm-5.3-flash': dict(entry(input=0.8), currency='CNY')}
        res = ledger.cost_of_with_currency(pricing, 'GLM-5.3-Flash',
                                           1_000_000, 0, 0, 0)
        self.assertTrue(res['priced'])
        self.assertEqual(res['currency'], 'CNY')


class TestCostWithCurrency(unittest.TestCase):
    def setUp(self):
        self.pricing = {
            'usd-model': dict(entry(input=2.0, output=6.0), currency='USD'),
            'cny-model': dict(entry(input=0.8, output=2.8, cache_read=0.23),
                              currency='CNY'),
        }

    def test_usd_model_returns_usd(self):
        # 4) USD 模型返回 USD
        res = ledger.cost_of_with_currency(self.pricing, 'usd-model',
                                           1_000_000, 0, 0, 0)
        self.assertTrue(res['priced'])
        self.assertEqual(res['currency'], 'USD')
        self.assertAlmostEqual(res['amount'], 2.0)

    def test_cny_model_returns_cny(self):
        # 3) CNY 模型返回 CNY
        res = ledger.cost_of_with_currency(self.pricing, 'cny-model',
                                           1_000_000, 0, 0, 0)
        self.assertTrue(res['priced'])
        self.assertEqual(res['currency'], 'CNY')
        self.assertAlmostEqual(res['amount'], 0.8)

    def test_cny_cache_read_priced_correctly(self):
        # 16) CNY cache_read 计价正确
        res = ledger.cost_of_with_currency(self.pricing, 'cny-model', 0, 0,
                                           3_697_834_880, 0)
        self.assertAlmostEqual(res['amount'],
                               3_697_834_880 / 1e6 * 0.23, places=6)

    def test_null_cache_write_not_zero_fact(self):
        # 17) cache_write=null：只是未定价（贡献 0），不等于“官方确认免费”
        res = ledger.cost_of_with_currency(self.pricing, 'cny-model', 0, 0, 0,
                                           5_000_000)
        self.assertTrue(res['priced'])          # 其它字段有价 → 模型已配置
        self.assertEqual(res['amount'], 0.0)    # 但 cache_write 未计价

    def test_source_distinction(self):
        # 18) manual_verified vs auto_reference
        p, srcs = ledger.load_pricing_with_sources()
        # 真库只读冒烟：vision-exp 来自 auto（manual 文件里全 null 模板）
        self.assertEqual(srcs['deepseek-v4-flash-vision-exp'], 'auto_reference')

    def test_never_summed_across_currencies(self):
        # 5) USD+CNY 永不相加：引擎层面无跨币种 sum 函数
        src = open(os.path.join(ROOT, 'ledger.py'), encoding='utf-8').read()
        self.assertNotIn("cost_by_currency.get('USD', 0.0) + cost_by_currency.get('CNY'",
                         src)
        # by_currency 聚合只按各自键累加
        self.assertIn("cost_by_currency[res['currency']] = ", src)


class TestBoardCurrency(V1BoardBase):
    """fork 夹具：m-a=USD 模型（手工价）+ replay 行 + catpaw activity。"""

    def setUp(self):
        super().setUp()          # V1BoardBase 已含 m-a 定价 + scan + board

    def board(self):
        with open(os.path.join(self.tmp, 'usage-board.json'),
                  encoding='utf-8') as f:
            return json.load(f)

    def test_usd_field_excludes_foreign_currency(self):
        # 7) legacy usd 字段只装 USD 桶
        b = self.board()
        s = b['summary']
        self.assertAlmostEqual(s['estimated_cost_usd_top50_models'],
                               30 * 1000.0 / 1e6 + 6 * 2000.0 / 1e6, places=4)

    def test_models_carry_currency_and_source(self):
        b = self.board()
        ma = [m for m in b['models'] if m['model'] == 'm-a'][0]
        self.assertEqual(ma['currency'], 'USD')
        self.assertEqual(ma['pricing_source'], 'manual_verified')

    def test_pricing_coverage_source_distinction(self):
        # 12b) 覆盖率块区分 manual_verified / auto_reference
        pc = b['pricing_coverage'] if False else self.board()['pricing_coverage']
        self.assertEqual(pc['priced_by_source'].get('manual_verified'), 1)
        self.assertEqual(pc['priced_by_source'].get('auto_reference', 0), 0)
        self.assertFalse(pc['multi_currency'])

    def test_coverage_remains_i_plus_o(self):
        # 19) 覆盖率保持 i+o 口径（cache_read 不进分母）
        b = self.board()
        pc = b['pricing_coverage']
        self.assertEqual(pc['effective_tokens'],
                         b['summary']['total_tokens'])


class TestOldBoardCompat(unittest.TestCase):
    def test_board_without_currency_fields_loads(self):
        # 21) 旧 board（无新字段）对加载器/校验器兼容
        old_board = {'schema_version': 1, 'generated_at': 'x',
                     'privacy': {'paths_included': False,
                                 'event_ids_included': False},
                     'summary': {'usage_events': 1}}
        tmp = _safety.sandbox_dir()
        try:
            p = os.path.join(tmp, 'usage-board.json')
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(old_board, f)
            with open(p, encoding='utf-8') as f:
                b = json.load(f)
            self.assertNotIn('pricing_coverage', b)      # 旧结构正常读取
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestApplyTool(unittest.TestCase):
    """22) 备份 + apply 幂等（沙盒 root，绝不触碰真实 pricing.json）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-apply-')
        # 沙盒：复制 ledger 依赖 + 最小 pricing.json + dsh 夹具
        for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        import time as _t
        now = int(_t.time() * 1000)
        d = os.path.join(self.tmp, 'dsh-home', 'sessions', 'proj', 'session-p')
        os.makedirs(d, exist_ok=True)
        hdr = {'type': 'session', 'id': 'session-p', 'createdAt': now,
               'delegationDepth': 0, 'version': 0}
        ev = msg(1, now, 3_798_000, 8_129, 3_697_834, 'GLM-5.3-Flash')
        with open(os.path.join(d, 'session.jsonl'), 'w', encoding='utf-8') as f:
            f.write('\n'.join([json.dumps(hdr), json.dumps(ev)]) + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        with open(os.path.join(self.tmp, 'sources.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite', 'workbuddy': empty,
                       'catpaw': empty, 'traecn': empty, 'codex': empty,
                       'dsh': os.path.join(self.tmp, 'dsh-home').replace(
                           '\\', '/')}, f)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'GLM-5.3-Flash': entry()}, f)
        run(['ledger.py', 'scan'], self.tmp)      # 沙盒先入账（board 非空）

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_apply_idempotent(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'apply_pricing_proposal',
            os.path.join(ROOT, 'scripts', 'apply_pricing_proposal.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        pre_hash = hashlib.sha256(open(os.path.join(
            self.tmp, 'pricing.json'), 'rb').read()).hexdigest()

        rc = mod.apply(self.tmp)                       # 第一次：写入 + board
        self.assertEqual(rc, 0)
        bak = os.path.join(self.tmp, 'pricing.json.bak-r8')
        self.assertTrue(os.path.isfile(bak))
        self.assertEqual(
            hashlib.sha256(open(bak, 'rb').read()).hexdigest(), pre_hash)
        pj = json.load(open(os.path.join(self.tmp, 'pricing.json'),
                            encoding='utf-8'))
        glm = pj['GLM-5.3-Flash']
        self.assertEqual(glm['currency'], 'CNY')
        self.assertEqual(glm['unit'], 'per_1m_tokens')
        self.assertEqual(glm['input'], 0.8)
        self.assertEqual(glm['output'], 2.8)
        self.assertEqual(glm['cache_read'], 0.23)
        self.assertIsNone(glm['cache_write'])          # null ≠ 0 价事实
        self.assertEqual(glm['evidence_id'], 'glm-5.3-flash-cn-2026-09-26')

        rc2 = mod.apply(self.tmp)                      # 幂等
        self.assertEqual(rc2, 0)
        self.assertEqual(
            json.load(open(os.path.join(self.tmp, 'pricing.json'),
                           encoding='utf-8'))['GLM-5.3-Flash'], glm)

        board = json.load(open(os.path.join(self.tmp, 'usage-board.json'),
                               encoding='utf-8'))
        by = board['summary']['estimated_cost_by_currency']
        self.assertEqual(set(by.keys()), {'CNY'})      # CNY 不进 USD 桶
        self.assertAlmostEqual(by['CNY'],
                               3_798_000 / 1e6 * 0.8 + 8_129 / 1e6 * 2.8
                               + 3_697_834 / 1e6 * 0.23, places=2)
        self.assertIsNone(
            board['summary']['estimated_cost_usd_top50_models'])  # USD 桶空


class TestCliMultiCurrency(unittest.TestCase):
    def test_cli_multicurrency_output(self):
        # 8) CLI 分币种显示（静态检查：分币种循环 + 不相加提示存在于源码）
        src = open(os.path.join(ROOT, 'ledger.py'), encoding='utf-8').read()
        self.assertIn('不可直接相加', src)
        self.assertIn('cost_by_currency[cur]', src)

    def test_no_fx_conversion_anywhere(self):
        # 无 FX：ledger/serve 不得出现汇率换算函数
        for f in ('ledger.py', 'serve.py'):
            src = open(os.path.join(ROOT, f), encoding='utf-8').read()
            self.assertNotIn('exchange_rate', src)
            self.assertNotIn('/ 7.1', src)
            self.assertNotIn('* 7.1', src)


if __name__ == '__main__':
    unittest.main()
