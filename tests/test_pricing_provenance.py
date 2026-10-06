# -*- coding: utf-8 -*-
"""V1.1 Pricing Provenance & Fallback Chain 测试。

对应验收（A–J）：
  A. 用户渠道价存在 → 优先于官方与第三方
  B. 无用户价但官方 provider/model exact → 使用官方价
  C. 官方 provider 不匹配 → 不使用该官方价
  D. 仅有 LiteLLM/OpenRouter 同名模型 → THIRD_PARTY_REFERENCE
  E. 多个第三方候选 → 确定性选择并保留来源，绝不随机/冒充官方
  F. 完全无价格 → null / unavailable
  G. reliable cost 不包含 third-party reference
  H. with-reference cost 包含 third-party reference
  I. 套餐/Plan usage → API equivalent != actual spend（actual_spend=unknown）
  J. CNY/USD 不做自动汇率合并

隔离：pricing 文件与账本 DB 全部在 _safety 沙盒（模块级路径打补丁）；
绝不触碰 production usage DB 与真实 pricing.json。
"""
import json
import os
import shutil
import sys
import unittest

import _safety

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys_path = (HERE, ROOT)
for p in sys_path:
    if p not in sys.path:
        sys.path.insert(0, p)

import ledger  # noqa: E402


def _write_price(path, payload):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


class PricingProvenanceBase(unittest.TestCase):
    """沙盒 pricing 文件 + 沙盒账本 DB（in-process）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-pricing-')
        self._old = {k: getattr(ledger, k) for k in
                     ('PRICING_PATH', 'PRICING_AUTO_PATH', 'DB_PATH')}
        self.pricing_path = os.path.join(self.tmp, 'pricing.json')
        self.auto_path = os.path.join(self.tmp, 'pricing.auto.json')
        self.db_path = os.path.join(self.tmp, 'usage.db')
        ledger.PRICING_PATH = self.pricing_path
        ledger.PRICING_AUTO_PATH = self.auto_path
        ledger.DB_PATH = self.db_path
        _write_price(self.pricing_path, {'_note': 'sandbox'})
        _write_price(self.auto_path, {'_meta': {
            'source': 'litellm,openrouter',
            'source_id': 'litellm/x,openrouter/y',
            'retrieved_at': '2026-10-03T12:00:00'}})

    def tearDown(self):
        for k, v in self._old.items():
            setattr(ledger, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def set_user(self, model, entry):
        with open(self.pricing_path, encoding='utf-8') as f:
            data = json.load(f)
        data[model] = entry
        _write_price(self.pricing_path, data)

    def set_reference(self, model, entry):
        with open(self.auto_path, encoding='utf-8') as f:
            data = json.load(f)
        data[model] = entry
        _write_price(self.auto_path, data)

    def layers(self):
        return ledger.load_pricing_layers()

    def resolve(self, model, provider=None):
        return ledger.resolve_price_record(self.layers(), model, provider)

    def seed_event(self, model, provider, i, o, cr=0, cw=0, client='zcode'):
        conn = ledger.connect(create=True)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO usage_event (
                       client, session_id, event_id, ts_ms, model, provider,
                       input_tokens, output_tokens, reasoning_tokens,
                       cache_read_tokens, cache_write_tokens,
                       source_file, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (client, 'sess', 'ev-%s-%s-%d' % (client, model, i + o),
                 1760000000000, model, provider, i, o, 0, cr, cw,
                 'sandbox', '2026-10-03T10:00:00', '2026-10-03T10:00:00'))
            conn.commit()
        finally:
            conn.close()

    def board(self):
        return ledger.build_board()


class TestFallbackChain(PricingProvenanceBase):

    def test_a_user_channel_beats_all(self):
        # 用户渠道价优先于官方与第三方
        self.set_user('glm-x', {'input': 0.8, 'output': 2.8,
                                'currency': 'CNY',
                                'evidence_id': 'user-evidence'})
        self.set_user('official-glm-x', {          # 干扰项：官方 + 第三方同名同键
            'pricing_type': 'official', 'provider': 'zhipu',
            'input': 99.0, 'output': 99.0, 'currency': 'CNY'})
        self.set_reference('glm-x', {'input': 5.0, 'output': 5.0})
        rec = self.resolve('glm-x', provider='zhipu')
        self.assertEqual(rec['pricing_type'], 'user_channel')
        self.assertEqual(rec['source'], 'user')
        res = ledger.record_cost(rec, 1_000_000, 1_000_000, 0, 0)
        self.assertAlmostEqual(res['amount'], 0.8 + 2.8)
        self.assertEqual(res['currency'], 'CNY')

    def test_b_official_exact_provider_model(self):
        # 无用户价，官方 provider/model 双重 exact → 使用官方价
        self.set_user('oa-x', {'pricing_type': 'official', 'provider': 'openai',
                               'input': 2.5, 'output': 10.0, 'currency': 'USD'})
        rec = self.resolve('oa-x', provider='openai')
        self.assertEqual(rec['pricing_type'], 'official')
        self.assertEqual(rec['applicability'], 'exact')
        res = ledger.record_cost(rec, 1_000_000, 0, 0, 0)
        self.assertAlmostEqual(res['amount'], 2.5)

    def test_c_official_provider_mismatch_rejected(self):
        # provider 身份不同 → 绝不使用该官方价（无用户价、无第三方 → null）
        self.set_user('mism', {'pricing_type': 'official', 'provider': 'openai',
                               'input': 2.5, 'output': 10.0, 'currency': 'USD'})
        self.assertIsNone(self.resolve('mism', provider='deepseek'))
        # provider 身份不可知（路由模式串）→ 同样不绑定官方价
        self.assertIsNone(self.resolve('mism', provider='Auto'))
        self.assertIsNone(self.resolve('mism', provider=''))
        # 第三方存在时回退到 third_party_reference，而不是冒充官方
        self.set_reference('mism', {'input': 1.0, 'output': 2.0})
        rec = self.resolve('mism', provider='deepseek')
        self.assertEqual(rec['pricing_type'], 'third_party_reference')
        self.assertEqual(rec['applicability'], 'reference_only')

    def test_d_reference_only_from_auto_sources(self):
        # 只有 LiteLLM/OpenRouter 同名条目 → third_party_reference
        self.set_reference('deepseek-v4-flash',
                           {'input': 0.3, 'output': 1.2, 'cache_read': 0.006})
        rec = self.resolve('deepseek-v4-flash', provider='deepseek')
        self.assertEqual(rec['pricing_type'], 'third_party_reference')
        self.assertEqual(rec['applicability'], 'reference_only')
        self.assertEqual(rec['source'], 'litellm,openrouter')
        self.assertEqual(rec['retrieved_at'], '2026-10-03T12:00:00')
        self.assertEqual(rec['currency'], 'USD')

    def test_e_multi_reference_deterministic(self):
        # 多个第三方候选：加载期确定性合并（先到源优先、缺字段才补），
        # 来源随记录透传；绝不随机，也绝不冒充官方身份。
        with open(self.auto_path, encoding='utf-8') as f:
            data = json.load(f)
        data['_meta']['source'] = 'litellm,openrouter'
        data['multi-x'] = {'input': 1.0, 'output': 2.0}       # litellm 先到
        data['litellm/multi-x'] = {'input': 9.9, 'output': 9.9}
        _write_price(self.auto_path, data)
        rec = self.resolve('multi-x')
        self.assertEqual(rec['pricing_type'], 'third_party_reference')
        self.assertEqual(rec['input'], 1.0)          # 确定性：先到源价格
        self.assertEqual(rec['source'], 'litellm,openrouter')
        self.assertNotEqual(rec['pricing_type'], 'official')

    def test_f_unavailable_when_no_layer(self):
        self.assertIsNone(self.resolve('nope-x', provider='openai'))
        self.assertIsNone(ledger.record_cost(None, 1000, 1000, 0, 0))


class TestCostSemantics(PricingProvenanceBase):

    def _seed_dual(self):
        # GLM（用户渠道价 CNY）+ deepseek（第三方参考 USD）
        self.set_user('GLM-5.3-Flash', {'input': 0.8, 'output': 2.8,
                                        'cache_read': 0.23, 'currency': 'CNY'})
        self.set_reference('deepseek-v4-flash',
                           {'input': 0.3, 'output': 1.2, 'cache_read': 0.006})
        self.seed_event('GLM-5.3-Flash', 'account:bigmodel-start-plan',
                        1_000_000, 1_000_000)
        self.seed_event('deepseek-v4-flash', 'deepseek-official',
                        1_000_000, 1_000_000, cr=1_000_000)

    def test_g_reliable_excludes_reference(self):
        self._seed_dual()
        b = self.board()
        s = b['summary']
        reliable = s['reliable_estimated_cost_by_currency'] or {}
        reference = s['reference_estimated_cost_by_currency'] or {}
        # reliable 只含 CNY 用户渠道价；USD 第三方价绝不能混进来
        self.assertEqual(set(reliable), {'CNY'})
        self.assertAlmostEqual(reliable['CNY'], 0.8 + 2.8, places=3)
        self.assertEqual(set(reference), {'USD'})
        self.assertAlmostEqual(reference['USD'], 0.3 + 1.2 + 0.006, places=4)
        # 完整估算 = 两者之和（分币种），reliable 严格不含第三方
        full = s['estimated_cost_by_currency'] or {}
        self.assertAlmostEqual(full['CNY'], reliable['CNY'], places=3)
        self.assertAlmostEqual(full['USD'], reference['USD'], places=4)

    def test_h_coverage_split(self):
        self._seed_dual()
        pc = self.board()['pricing_coverage']
        # 每个事件 2M io tokens，两模型都 priced → reliable/reference 各半
        self.assertEqual(pc['reliable_priced_tokens'], 2_000_000)
        self.assertEqual(pc['reference_priced_tokens'], 2_000_000)
        self.assertAlmostEqual(pc['reliable_coverage_pct'], 50.0)
        self.assertAlmostEqual(pc['reference_coverage_pct'], 50.0)
        self.assertEqual(pc['priced_by_type']['user_channel'], 1)
        self.assertEqual(pc['priced_by_type']['third_party_reference'], 1)

    def test_i_actual_spend_unknown(self):
        # 套餐 / Coding Plan 用量：API 等价值 ≠ 实际支出；
        # 没有账单数据 → actual_spend = unknown，绝不由 token 反推。
        self._seed_dual()
        b = self.board()
        s = b['summary']
        self.assertEqual(s['actual_spend']['status'], 'unknown')
        self.assertIn('实际支出', s['actual_spend']['reason'])
        self.assertEqual(b['pricing_coverage']['cost_semantics'],
                         'api_equivalent_estimate')

    def test_j_no_auto_fx_merge(self):
        # CNY 与 USD 分别成桶；绝未经汇率合并成单一数字
        self._seed_dual()
        s = self.board()['summary']
        full = s['estimated_cost_by_currency']
        self.assertIn('CNY', full)
        self.assertIn('USD', full)
        self.assertNotIn('TOTAL', full)
        self.assertNotEqual(len(full), 1)


class TestModelProvenanceInBoard(PricingProvenanceBase):

    def test_model_rows_carry_provenance(self):
        self.set_user('GLM-5.3-Flash', {'input': 0.8, 'output': 2.8,
                                        'currency': 'CNY',
                                        'evidence_id': 'user-evidence'})
        self.set_reference('deepseek-v4-flash',
                           {'input': 0.3, 'output': 1.2})
        self.seed_event('GLM-5.3-Flash', 'account:bigmodel-start-plan',
                        1_000_000, 0)
        self.seed_event('deepseek-v4-flash', 'deepseek-official', 0, 1_000_000)
        self.seed_event('unpriced-m', 'Auto', 1_000_000, 0)
        rows = {m['model']: m for m in self.board()['models']}
        self.assertEqual(rows['GLM-5.3-Flash']['pricing_type'], 'user_channel')
        self.assertEqual(rows['GLM-5.3-Flash']['pricing_applicability'], 'exact')
        self.assertEqual(rows['deepseek-v4-flash']['pricing_type'],
                         'third_party_reference')
        self.assertEqual(rows['deepseek-v4-flash']['pricing_applicability'],
                         'reference_only')
        self.assertEqual(rows['unpriced-m']['pricing_type'], 'unavailable')
        self.assertIsNone(rows['unpriced-m']['cost'])
        self.assertEqual(rows['unpriced-m']['pricing_retrieved_at'], None)


if __name__ == '__main__':
    unittest.main()
