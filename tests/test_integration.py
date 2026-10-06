#!/usr/bin/env python3
"""Usage Ledger Core 集成测试。

全部在临时目录里跑，用合成 fixture，不触碰真实账本与真实客户端数据。

覆盖：
    - 自动发现结构 + 数据等级自动判定（TOKEN / ACTIVITY / UNKNOWN）
    - 运行状态契约：run-state.json 必须含 started_at / finished_at /
      sources_found / sources_scanned / events_seen / events_inserted /
      events_updated / errors
    - 退出码：0 成功 / 1 失败 / 2 部分失败 / 3 无数据源
    - 无数据源必须明确报错，绝不静默
    - Board Schema v1 结构与 privacy 约束
    - 留存：源文件消失后账本记录仍在
    - 价格：全 null 单价不得算成 $0；手工 > 自动 的字段级合并
    - Core 与 PathOrbit 完全解耦（源码零引用、不读写 PathOrbit 文件）
    - Dashboard HTML 结构 + 无外部资源 + JS 语法

运行：
    python -m unittest discover -s tests -p "test_*.py"
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# Core 源码文件（不得出现任何 PathOrbit 引用）
CORE_SOURCES = ('ledger.py', 'autopilot.py', 'scripts/verify_retention.py',
                'usage-board.schema.json')


def run(args, cwd, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=e)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


class Base(unittest.TestCase):
    """每个用例一套独立临时环境——账本是累积型存储，共用目录会互相污染。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-test-')
        for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home, exist_ok=True)
        self.fixtures = os.path.join(self.tmp, 'fixtures')
        os.makedirs(self.fixtures, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 便利读取 ----
    @staticmethod
    def _json(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    # ---- fixture 构造 ----
    def write_workbuddy(self, d, n=3, cached=200):
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'sess-1.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            for i in range(n):
                f.write(json.dumps({
                    'id': 'wb-%d' % i,
                    'sessionId': 'sess-1',
                    'timestamp': 1787000000000 + i * 1000,
                    'type': 'function_call',
                    'providerData': {
                        'model': 'glm-test-model',
                        'requestModelName': 'Auto',
                        'rawUsage': {
                            'prompt_tokens': 1200, 'completion_tokens': 300,
                            'prompt_tokens_details': {'cached_tokens': cached},
                            'completion_tokens_details': {'reasoning_tokens': 40},
                        },
                    },
                }, ensure_ascii=False) + '\n')
        return p

    def write_workbuddy_without_usage(self, d):
        """看起来像会话文件，但里面没有任何用量字段 —— 用来验证等级降级为 UNKNOWN。"""
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'sess-nousage.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({'id': 'x-%d' % i, 'timestamp': 1787000000000,
                                    'type': 'text', 'text': 'hello'}) + '\n')
        return p

    def write_catpaw(self, d):
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'conv-1.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            for i in range(4):
                f.write(json.dumps({
                    'messageId': 'cp-%d' % i, 'conversationId': 'conv-1',
                    'type': 'user' if i % 2 == 0 else 'assistant',
                    'timestamp': '2026-08-01T10:0%d:00.000Z' % i,
                }) + '\n')
        return p

    def sources_json(self, **over):
        base = {
            'zcode': os.path.join(self.fixtures, 'none.sqlite'),
            'dsh': os.path.join(self.fixtures, 'none-dsh'),
            'workbuddy': os.path.join(self.fixtures, 'wb'),
            'catpaw': os.path.join(self.fixtures, 'cp'),
            'traecn': os.path.join(self.fixtures, 'none-trae'),
            'codex': os.path.join(self.fixtures, 'none-codex'),
        }
        base.update(over)
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump(base, f, ensure_ascii=False, indent=2)

    def read_board(self):
        with open(os.path.join(self.tmp, 'usage-board.json'), encoding='utf-8') as f:
            return json.load(f)


# ==================================================================== 自动发现 / 数据等级

class TestDiscovery(Base):
    def test_discovery_structure(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['ledger.py', 'discover'], self.tmp)
        self.assertEqual(code, 0, out)
        rep = self._json(os.path.join(self.tmp, 'discovery.json'))
        self.assertEqual(rep['schema_version'], 1)
        self.assertIn('sources', rep)
        self.assertIn('grades', rep)
        for client, row in rep['sources'].items():
            self.assertIn(row['state'], ('found', 'not_found'), client)
            self.assertIn(row['mode'], ('usage', 'activity'), client)
            self.assertIn(row['data_grade'], ('TOKEN', 'ACTIVITY', 'UNKNOWN'), client)
            self.assertIsInstance(row['candidates_checked'], list, client)
            self.assertTrue(row['candidates_checked'], client)
            self.assertIn(row['category'], ('collectable',), client)
            # 脱敏：候选路径里不得出现盘符
            for c in row['candidates_checked']:
                self.assertNotRegex(c, r'^[A-Za-z]:', '候选路径未脱敏: %s' % c)
        self.assertEqual(rep['sources']['workbuddy']['state'], 'found')
        self.assertEqual(rep['sources']['workbuddy']['mode'], 'usage')

    def test_discovery_finds_without_manual_paths(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['ledger.py', 'discover'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('自动发现', out)
        self.assertIn('数据等级', out)

    def test_opaque_store_reported_as_unknown(self):
        """不透明存储必须被登记为 UNKNOWN，不能静默略过。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['ledger.py', 'discover'], self.tmp)
        self.assertEqual(code, 0, out)
        rep = self._json(os.path.join(self.tmp, 'discovery.json'))
        self.assertIn('opaque', rep)
        self.assertTrue(rep['opaque'], '必须有不透明存储登记表')
        for row in rep['opaque'].values():
            self.assertEqual(row['data_grade'], 'UNKNOWN')
            self.assertEqual(row['category'], 'unreadable')
            self.assertIn(row['state'], ('found', 'not_found'))


class TestDataGrade(Base):
    def test_token_grade_after_scan(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        rep = self._json(os.path.join(self.tmp, 'discovery.json'))
        wb = rep['sources']['workbuddy']
        self.assertEqual(wb['data_grade'], 'TOKEN')
        self.assertEqual(wb['grade_basis'], 'verified')
        self.assertGreater(wb['grade_records'], 0)

    def test_activity_grade_not_token(self):
        self.write_catpaw(os.path.join(self.fixtures, 'cp'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        rep = self._json(os.path.join(self.tmp, 'discovery.json'))
        self.assertEqual(rep['sources']['catpaw']['data_grade'], 'ACTIVITY')
        self.assertEqual(rep['sources']['catpaw']['grade_basis'], 'verified')

    def test_found_but_unparsed_downgrades_to_unknown(self):
        """找到了存储但解析不出任何记录 —— 必须降级成 UNKNOWN，不许继续假装 TOKEN。"""
        self.write_workbuddy_without_usage(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        rep = self._json(os.path.join(self.tmp, 'discovery.json'))
        wb = rep['sources']['workbuddy']
        self.assertEqual(wb['state'], 'found', '文件确实在，所以是 found')
        self.assertEqual(wb['data_grade'], 'UNKNOWN',
                         '解析不出记录就必须降级为 UNKNOWN')
        self.assertEqual(wb['grade_basis'], 'found_but_unparsed')
        self.assertEqual(rep['grades']['UNKNOWN'] >= 1, True)

    def test_not_found_is_unknown(self):
        empty = os.path.join(self.tmp, 'empty-home')
        os.makedirs(empty, exist_ok=True)
        d = os.path.join(self.tmp, 'nofind')
        os.makedirs(d, exist_ok=True)
        for f in ('ledger.py', 'autopilot.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(d, f))
        env = {'HOME': empty, 'USERPROFILE': empty,
               'APPDATA': os.path.join(empty, 'AppData', 'Roaming')}
        code, out = run(['ledger.py', 'discover'], d, env=env)
        self.assertEqual(code, 3, '什么都没发现时 discover 应返回 3\n%s' % out)
        rep = self._json(os.path.join(d, 'discovery.json'))
        for client, row in rep['sources'].items():
            self.assertEqual(row['state'], 'not_found', client)
            self.assertEqual(row['data_grade'], 'UNKNOWN', client)
            self.assertEqual(row['grade_basis'], 'not_found', client)


# ==================================================================== 运行状态契约

class TestRunStateContract(Base):
    REQUIRED = ('started_at', 'finished_at', 'sources_found', 'sources_scanned',
                'events_seen', 'events_inserted', 'events_updated', 'errors')

    def test_run_state_has_contract_fields(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        st = self._json(os.path.join(self.tmp, 'run-state.json'))
        for k in self.REQUIRED:
            self.assertIn(k, st, 'run-state.json 缺少契约字段 %s' % k)
        self.assertEqual(st['status'], 'success')
        self.assertEqual(st['exit_code'], 0)
        self.assertEqual(st['sources_found'], 1)
        self.assertEqual(st['sources_scanned'], 1)
        self.assertEqual(st['events_seen'], 3)
        self.assertEqual(st['events_inserted'], 3)
        self.assertEqual(st['errors'], [])
        self.assertIsInstance(st['grades'], dict)
        # started_at <= finished_at
        self.assertLessEqual(st['started_at'], st['finished_at'])
        # 三种诊断产物都要有
        for f in ('discovery.json', 'run-state.json', 'auto.log'):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, f)), f)

    def test_auto_log_written(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        with open(os.path.join(self.tmp, 'auto.log'), encoding='utf-8') as f:
            log = f.read()
        self.assertIn('status=success', log)

    def test_status_json_flag(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        code, out = run(['autopilot.py', 'status', '--json'], self.tmp)
        self.assertEqual(code, 0, out)
        st = json.loads(out)
        for k in self.REQUIRED:
            self.assertIn(k, st)

    def test_incremental_second_scan_is_noop(self):
        """增量扫描：文件未变则整文件跳过，不重复解压、不重复写库。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        st = self._json(os.path.join(self.tmp, 'run-state.json'))
        self.assertEqual(st['events_seen'], 0, '未变文件不该被重新解析')
        self.assertEqual(st['events_inserted'], 0, '不该重复插入')
        self.assertEqual(st['events_updated'], 0)
        self.assertEqual(st['scan']['files_skipped'], 1, '未变文件应被整文件跳过')
        self.assertEqual(st['sources_scanned'], 1)
        # 账本总量不变（幂等）
        b = self.read_board()
        self.assertEqual(b['summary']['usage_events'], 3)

    def test_explicit_catpaw_path_blocks_alt_fallback(self):
        """显式配置了 catpaw 路径就只读它，不许偷偷去读兜底位置（沙箱隔离的前提）。"""
        import sqlite3
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json(catpaw=os.path.join(self.fixtures, 'no-such-catpaw'))
        code, out = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('no-such-catpaw', out.replace('\\', '/').replace('…/', ''), 
                      'catpaw 必须报告为空，而不是去读别的地方')
        self.assertIn('跳过：没找到源文件', out)
        conn = sqlite3.connect(os.path.join(self.tmp, 'usage.db'))
        try:
            n = conn.execute("SELECT COUNT(*) FROM activity_event WHERE client='catpaw'"
                             ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, '显式路径为空时不得回退到兜底位置读写数据')


class TestFailureVisibility(Base):
    def test_no_supported_sources_exit_code(self):
        empty_home = os.path.join(self.tmp, 'empty-home')
        os.makedirs(empty_home, exist_ok=True)
        d = os.path.join(self.tmp, 'nosrc')
        os.makedirs(d, exist_ok=True)
        for f in ('ledger.py', 'autopilot.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(d, f))
        env = {'HOME': empty_home, 'USERPROFILE': empty_home,
               'APPDATA': os.path.join(empty_home, 'AppData', 'Roaming')}
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], d, env=env)
        self.assertEqual(code, 3, '期望退出码 3，实际 %s\n%s' % (code, out))
        st = self._json(os.path.join(d, 'run-state.json'))
        self.assertEqual(st['status'], 'no_supported_sources')
        self.assertEqual(st['exit_code'], 3)
        self.assertEqual(st['sources_found'], 0)
        self.assertEqual(st['sources_scanned'], 0)
        self.assertEqual(st['events_seen'], 0)
        self.assertTrue(st['errors'], '无数据源必须记录错误，不能静默')
        # 仍要产出诊断产物
        self.assertTrue(os.path.isfile(os.path.join(d, 'usage-board.json')))
        self.assertTrue(os.path.isfile(os.path.join(d, 'auto.log')))
        self.assertTrue(os.path.isfile(os.path.join(d, 'discovery.json')))

    def test_fatal_failure_exit_code_1_and_state_written(self):
        """board 无法写出 = 核心链路不可用 → failed(1)，且状态必须落盘。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        blocked = os.path.join(self.tmp, 'usage-board.json')
        os.makedirs(blocked)              # 目录顶替文件，写入必失败
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 1, '期望退出码 1，实际 %s\n%s' % (code, out))
        st = self._json(os.path.join(self.tmp, 'run-state.json'))
        self.assertEqual(st['status'], 'failed')
        self.assertEqual(st['exit_code'], 1)
        self.assertFalse(st['board']['written'])
        self.assertTrue(st['errors'], '致命失败必须留下错误说明')
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'auto.log')))


# ==================================================================== Board / 留存

class TestBoardAndRetention(Base):
    def test_board_schema_and_privacy(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.read_board()
        self.assertEqual(b['schema_version'], 1)
        for k in ('generated_at', 'privacy', 'summary', 'window_days',
                  'daily', 'clients', 'activity', 'models', 'sources',
                  'grades', 'opaque_stores'):
            self.assertIn(k, b, '缺少必需字段 %s' % k)
        self.assertIs(b['privacy']['paths_included'], False)
        self.assertIs(b['privacy']['event_ids_included'], False)
        self.assertEqual(b['summary']['usage_events'], 3)
        # prompt 1200 - cached 200 = 1000 新增输入
        self.assertEqual(b['summary']['input_tokens'], 3000)
        self.assertEqual(b['summary']['output_tokens'], 900)
        # 绝不含本机路径
        blob = json.dumps(b, ensure_ascii=False)
        for pat in ('C:\\', 'D:\\', self.tmp):
            self.assertNotIn(pat, blob, 'board 泄漏了本机路径 %s' % pat)
        self.assertIn('automation', b)
        self.assertEqual(b['automation']['status'], 'success')
        self.assertEqual(b['automation']['exit_code'], 0)

    def test_board_contains_grades(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.write_catpaw(os.path.join(self.fixtures, 'cp'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.read_board()
        self.assertEqual(b['grades']['by_client']['workbuddy'], 'TOKEN')
        self.assertEqual(b['grades']['by_client']['catpaw'], 'ACTIVITY')
        self.assertEqual(b['grades']['by_client']['dsh'], 'UNKNOWN')
        self.assertIn('traecn_agentdb', b['grades']['opaque'])
        wb = [x for x in b['sources'] if x['client'] == 'workbuddy'][0]
        self.assertEqual(wb['data_grade'], 'TOKEN')
        self.assertEqual(wb['grade_basis'], 'verified')

    def test_activity_not_counted_as_token(self):
        self.write_catpaw(os.path.join(self.fixtures, 'cp'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.read_board()
        # 只有 CatPaw（activity）时：不能有任何 Token
        self.assertEqual(b['summary']['total_tokens'], 0)
        self.assertEqual(b['summary']['usage_events'], 0)
        self.assertTrue(b['activity'], '活动量必须有记录')
        self.assertEqual(b['activity'][0]['records'], 4)
        # CatPaw 不得出现在 Token 客户端里
        self.assertEqual([c for c in b['clients'] if c['mode'] != 'activity'], [])

    def test_retention_keeps_records_after_source_removed(self):
        wb = os.path.join(self.fixtures, 'wb2')
        p = self.write_workbuddy(wb, n=2)
        self.sources_json(workbuddy=wb)
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        b1 = self.read_board()
        self.assertEqual(b1['summary']['usage_events'], 2)
        self.assertEqual(b1['sources'][0]['files_missing'], 0)

        os.remove(p)                        # 模拟客户端清退历史
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        # 源文件全被清退、但账本仍有留存记录 → 部分失败（不是「首次就无数据源」）
        self.assertEqual(code, 2,
                         '源被清退但数据仍在应为 partial_failure(2)，实际 %s\n%s' % (code, out))
        st = self._json(os.path.join(self.tmp, 'run-state.json'))
        self.assertEqual(st['status'], 'partial_failure')
        self.assertTrue(any('留存记录' in e for e in st['errors']),
                        '必须明确说明数据仍留存：%s' % st['errors'])
        b2 = self.read_board()
        self.assertEqual(b2['summary']['usage_events'], 2,
                         '源文件消失后账本记录不应减少')
        wb_src = [x for x in b2['sources'] if x['client'] == 'workbuddy'][0]
        self.assertEqual(wb_src['files_missing'], 1, '应标记为已清退')
        self.assertEqual(wb_src['files_alive'], 0)


# ==================================================================== 价格

class TestPricing(Base):
    def test_all_null_pricing_is_not_zero(self):
        """pricing.json 模板里全是 null —— 绝不能算成 $0。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        with open(os.path.join(self.tmp, 'pricing.json'), 'w', encoding='utf-8') as f:
            json.dump({'glm-test-model': {'input': None, 'output': None,
                                          'cache_read': None, 'cache_write': None}}, f)
        rc, so = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(rc, 0, so)
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.read_board()
        self.assertIsNone(b['summary']['estimated_cost_usd_top50_models'],
                          '未匹配价格必须是 null，不能是 0')
        self.assertEqual(b['summary']['priced_models'], 0)
        self.assertEqual(b['summary']['unpriced_models'], 1)

    def test_manual_overrides_auto_field_level(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        with open(os.path.join(self.tmp, 'pricing.auto.json'), 'w', encoding='utf-8') as f:
            json.dump({'_meta': {'source': 'test'}, 'glm-test-model':
                       {'input': 1.0, 'output': 2.0, 'cache_read': 0.5, 'cache_write': None}}, f)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w', encoding='utf-8') as f:
            # 手工只填 output，其余留 null：不能把自动同步的 input/cache_read 抹掉
            json.dump({'glm-test-model': {'input': None, 'output': 9.0,
                                          'cache_read': None, 'cache_write': None}}, f)
        rc, so = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(rc, 0, so)
        code, out = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(code, 0, out)
        b = self.read_board()
        self.assertEqual(b['summary']['priced_models'], 1)
        # 1000*3 in @1/1e6 + 300*3 out @9/1e6 + 200*3 cr @0.5/1e6
        expect = (3000 * 1.0 + 900 * 9.0 + 600 * 0.5) / 1e6
        self.assertAlmostEqual(b['summary']['estimated_cost_usd_top50_models'],
                               expect, places=6)

    def test_pricing_meta_in_board(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        with open(os.path.join(self.tmp, 'pricing.auto.json'), 'w', encoding='utf-8') as f:
            json.dump({'_meta': {'source': 'litellm', 'source_id': 'x',
                                 'retrieved_at': '2026-09-18T00:00:00'},
                       'glm-test-model': {'input': 1.0, 'output': 2.0,
                                          'cache_read': 0.5, 'cache_write': None}}, f)
        run(['ledger.py', 'scan'], self.tmp)
        run(['ledger.py', 'board'], self.tmp)
        b = self.read_board()
        meta = b['summary']['pricing']
        self.assertTrue(meta['auto'])
        self.assertEqual(meta['source'], 'litellm')
        self.assertEqual(meta['retrieved_at'], '2026-09-18T00:00:00')


# ==================================================================== 与 PathOrbit 解耦

class TestNoPathOrbitDependency(unittest.TestCase):
    """Core 必须完全脱离 PathOrbit 知识库项目：不引用、不读取、不修改。

    V1.1 品牌迁移注：产品正式品牌为「智账 · PathOrbit AI Ledger」
    （品牌归属 PathOrbit / 途有引力），品牌名出现在核心源码属正常。
    本守卫禁止的是**对 PathOrbit 知识库项目的依赖**：adapter 导入、
    integrations 路径、知识库看板/目录引用 —— 这些检查全部保留。"""

    FORBIDDEN = ('pathorbit_adapter', 'integrations/pathorbit',
                 'integrations\pathorbit', '知识库看板', 'ai-usage',
                 'ai_usage')

    def test_core_sources_have_no_pathorbit_reference(self):
        for f in CORE_SOURCES:
            p = os.path.join(ROOT, f)
            with open(p, encoding='utf-8') as fh:
                text = fh.read()
            hits = [w for w in self.FORBIDDEN if w in text]
            self.assertEqual(hits, [], '%s 出现了 PathOrbit 相关引用：%s' % (f, hits))

    def test_core_sources_do_not_reference_pathorbit_paths(self):
        for f in CORE_SOURCES:
            with open(os.path.join(ROOT, f), encoding='utf-8') as fh:
                text = fh.read()
            self.assertNotIn('04-领域', text, f)
            self.assertNotIn('可视化看板', text, f)

    def test_no_core_module_imports_adapter(self):
        for f in ('ledger.py', 'autopilot.py'):
            with open(os.path.join(ROOT, f), encoding='utf-8') as fh:
                text = fh.read()
            self.assertNotIn('pathorbit_adapter', text, f)
            self.assertNotIn('integrations', text, f)

    def test_batch_scripts_have_no_pathorbit_reference(self):
        for f in ('start-auto.bat', 'install-auto.bat', 'uninstall-auto.bat'):
            p = os.path.join(ROOT, f)
            if not os.path.isfile(p):
                continue
            with open(p, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            hits = [w for w in self.FORBIDDEN if w in text]
            self.assertEqual(hits, [], '%s 出现了 PathOrbit 相关引用：%s' % (f, hits))

    def test_adapter_lives_under_integrations_only(self):
        """适配器只能存在于 integrations/ 下，不得留在 Core 根目录。"""
        self.assertFalse(os.path.isfile(os.path.join(ROOT, 'pathorbit_adapter.py')),
                         '适配器不该留在项目根目录')
        integ = os.path.join(ROOT, 'integrations', 'pathorbit', 'pathorbit_adapter.py')
        self.assertTrue(os.path.isfile(integ), '适配器应在 integrations/pathorbit/ 下')

    def test_core_runs_without_integrations_dir(self):
        """把 integrations/ 整个藏起来，Core 的扫描/出板/自动运行必须照常工作。"""
        tmp = _safety.sandbox_dir(prefix='ul-decouple-')
        try:
            for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
                shutil.copy(os.path.join(ROOT, f), os.path.join(tmp, f))
            fx = os.path.join(tmp, 'fixtures', 'wb')
            os.makedirs(fx, exist_ok=True)
            p = os.path.join(fx, 'sess-1.jsonl')
            with open(p, 'w', encoding='utf-8') as fh:
                fh.write(json.dumps({
                    'id': 'wb-1', 'sessionId': 'sess-1',
                    'timestamp': 1787000000000,
                    'providerData': {'model': 'glm-test-model',
                                     'rawUsage': {'prompt_tokens': 100,
                                                  'completion_tokens': 10}}}) + '\n')
            with open(os.path.join(tmp, 'sources.json'), 'w', encoding='utf-8') as fh:
                json.dump({'workbuddy': fx}, fh)
            code, out = run(['autopilot.py', 'auto', '--no-pricing'], tmp)
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.isfile(os.path.join(tmp, 'usage-board.json')))
            # 目录里绝不能出现 integrations
            self.assertNotIn('integrations', os.listdir(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ==================================================================== health.json

class TestHealth(Base):
    REQUIRED = ('last_run', 'sources', 'ledger', 'board')

    def test_health_generated_with_contract(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        h = self._json(os.path.join(self.tmp, 'health.json'))
        self.assertIn(h['status'], ('healthy', 'partial', 'failed', 'unknown'))
        for k in self.REQUIRED:
            self.assertIn(k, h, 'health.json 缺少 %s' % k)
        self.assertEqual(h['last_run']['result'], 'success')
        self.assertEqual(h['last_run']['trigger'], 'manual')
        self.assertTrue(h['sources']['found'] >= 1)
        self.assertTrue(h['ledger']['exists'])
        self.assertGreater(h['ledger']['usage_events'], 0)
        self.assertTrue(h['board']['exists'])
        self.assertEqual(h['board']['schema_version'], 1)
        self.assertTrue(h['reason'], '健康状态必须给出可读原因')

    def test_health_unknown_before_any_run(self):
        """从没跑过 → unknown，不能假装健康。"""
        empty = os.path.join(self.tmp, 'emptyhome')
        os.makedirs(empty, exist_ok=True)
        code, out = run(['autopilot.py', 'health', '--json'], self.tmp,
                        env={'HOME': empty, 'USERPROFILE': empty})
        self.assertEqual(code, 2, out)
        payload = json.loads(out)
        self.assertEqual(payload['status'], 'unknown')

    def test_health_partial_when_source_purged(self):
        wb = os.path.join(self.fixtures, 'wb3')
        p = self.write_workbuddy(wb, n=2)
        self.sources_json(workbuddy=wb)
        run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        os.remove(p)
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 2, out)
        h = self._json(os.path.join(self.tmp, 'health.json'))
        self.assertEqual(h['status'], 'partial')
        self.assertGreater(h['ledger']['source_files_purged'], 0)
        self.assertIn('清退', h['reason'])

    def test_health_injected_into_board(self):
        """Dashboard 只读 board 也能拿到健康状态。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        b = self.read_board()
        self.assertIn('health', b)
        self.assertIn(b['health']['status'], ('healthy', 'partial', 'failed', 'unknown'))
        for k in self.REQUIRED:
            self.assertIn(k, b['health'])


# ==================================================================== 价格系统

class TestPricingSystem(Base):
    def _litellm_fixture(self, path, entries):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(entries, f, ensure_ascii=False)
        return path

    def test_readonly_commands_do_not_create_db(self):
        """只是看一眼的命令不该凭空造出 usage.db。"""
        d = os.path.join(self.tmp, 'ro')
        os.makedirs(d, exist_ok=True)
        for f in ('ledger.py', 'autopilot.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(d, f))
        for cmd in (['ledger.py', 'status'], ['ledger.py', 'discover'],
                    ['ledger.py', 'report'], ['autopilot.py', 'status']):
            code, out = run(cmd, d)
            self.assertFalse(os.path.isfile(os.path.join(d, 'usage.db')),
                             '%s 不该创建 usage.db\n%s' % (cmd, out))

    def test_pricing_candidates_does_not_write(self):
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['ledger.py', 'scan'], self.tmp)
        before = {f: os.path.getsize(os.path.join(self.tmp, f))
                  if os.path.isfile(os.path.join(self.tmp, f)) else None
                  for f in ('pricing.json', 'pricing.auto.json', 'usage.db')}
        code, out = run(['ledger.py', 'pricing-candidates'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('不会', out)
        after = {f: os.path.getsize(os.path.join(self.tmp, f))
                 if os.path.isfile(os.path.join(self.tmp, f)) else None
                 for f in ('pricing.json', 'pricing.auto.json', 'usage.db')}
        self.assertEqual(before, after, 'pricing-candidates 不得写入任何文件')

    def test_candidates_only_no_auto_selection(self):
        """候选必须带供应商前缀信息，且不替用户选。"""
        self.write_workbuddy(os.path.join(self.fixtures, 'wb'))
        self.sources_json()
        run(['ledger.py', 'scan'], self.tmp)
        with open(os.path.join(self.tmp, 'pricing.auto.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({
                '_meta': {'source': 'test'},
                'vendorA/glm-test-model': {'input': 1.0, 'output': 2.0,
                                           'cache_read': 0.5, 'cache_write': None},
                'vendorB/glm-test-model': {'input': 3.0, 'output': 4.0,
                                           'cache_read': 1.5, 'cache_write': None},
                'glm-test-model': {'input': None, 'output': None,
                                   'cache_read': None, 'cache_write': None},
            }, f)
        with open(os.path.join(self.tmp, 'pricing.json'), 'w', encoding='utf-8') as f:
            json.dump({'glm-test-model': {'input': None, 'output': None,
                                          'cache_read': None, 'cache_write': None}}, f)
        code, out = run(['ledger.py', 'pricing-candidates'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('vendorA/glm-test-model', out)
        self.assertIn('vendorB/glm-test-model', out)
        self.assertIn('供应商 vendorA', out)
        self.assertIn('该键没有可用价格数据', out)
        # 未匹配单价时成本仍必须是 null
        rc, so = run(['ledger.py', 'board'], self.tmp)
        self.assertEqual(rc, 0, so)
        b = self.read_board()
        self.assertIsNone(b['summary']['estimated_cost_usd_top50_models'])
        self.assertIn('pricing_gaps', b)
        self.assertTrue(b['pricing_gaps'])
        self.assertTrue(b['pricing_gaps'][0]['candidates'], '候选必须进 board 供 Dashboard 展示')

    def test_sync_total_failure_keeps_old_cache(self):
        """所有来源都拉不到 → 旧缓存一字节不动，返回非零码。"""
        pc = os.path.join(self.tmp, 'pricing.auto.json')
        payload = {'_meta': {'source': 'old'}, 'keepme/model': {
            'input': 1.0, 'output': 2.0, 'cache_read': 0.5, 'cache_write': None}}
        with open(pc, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        with open(pc, 'rb') as _f:
            before = _f.read()
        env = {'USAGE_LEDGER_PRICING_URL_LITELLM': 'file:///' + self.tmp.replace(
                   '\\', '/') + '/missing-litellm.json',
               'USAGE_LEDGER_PRICING_URL_OPENROUTER': 'file:///' + self.tmp.replace(
                   '\\', '/') + '/missing-openrouter.json'}
        code, out = run(['ledger.py', 'pricing-sync', '--source', 'all'], self.tmp, env=env)
        self.assertNotEqual(code, 0, out)
        with open(pc, 'rb') as _f:
            self.assertEqual(_f.read(), before, '失败时旧缓存必须原样保留')
        self.assertIn('保留', out)

    def test_sync_partial_failure_keeps_uncovered_entries(self):
        """一个来源成功、一个失败：旧缓存里"本轮没覆盖到"的条目必须保留。"""
        # 旧缓存里有两条：一条本轮会被刷新，一条只有旧缓存才有
        pc = os.path.join(self.tmp, 'pricing.auto.json')
        with open(pc, 'w', encoding='utf-8') as f:
            json.dump({
                '_meta': {'source': 'old'},
                'glm-test-model': {'input': 0.1, 'output': 0.2,
                                   'cache_read': 0.05, 'cache_write': None},
                'only-in-old-cache/model': {'input': 9.0, 'output': 9.0,
                                            'cache_read': None, 'cache_write': None},
            }, f)
        fx = self._litellm_fixture(
            os.path.join(self.tmp, 'litellm.json'),
            {'glm-test-model': {'input_cost_per_token': 1e-6,
                                'output_cost_per_token': 2e-6,
                                'cache_read_input_token_cost': 0.5e-6}})
        env = {'USAGE_LEDGER_PRICING_URL_LITELLM': 'file:///' + fx.replace('\\', '/'),
               'USAGE_LEDGER_PRICING_URL_OPENROUTER': 'file:///' + self.tmp.replace(
                   '\\', '/') + '/nope.json'}
        code, out = run(['ledger.py', 'pricing-sync', '--source', 'all'], self.tmp, env=env)
        self.assertEqual(code, 2, '部分来源失败应返回 2\n%s' % out)
        after = self._json(pc)
        self.assertIn('only-in-old-cache/model', after,
                      '本轮没覆盖到的旧条目不能被抹掉')
        self.assertEqual(after['only-in-old-cache/model']['input'], 9.0)
        self.assertEqual(after['glm-test-model']['input'], 1.0, '新值应覆盖旧值')
        meta = after['_meta']
        self.assertEqual(meta['models_kept_from_cache'], 1)
        self.assertTrue(meta['errors'])


# ==================================================================== Dashboard

class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, 'usage-dashboard.html')
        with open(self.path, encoding='utf-8') as f:
            self.text = f.read()

    def test_no_external_resources(self):
        urls = set(re.findall(r'https?://[^"\'\s)>]+', self.text))
        external = [u for u in urls if 'w3.org' not in u]   # w3.org 是 SVG 命名空间
        self.assertEqual(external, [], '不得引入 CDN / 外部资源：%s' % external)
        self.assertNotIn('<link', self.text)
        self.assertNotIn('@import', self.text)
        self.assertNotIn('@font-face', self.text)

    def test_no_pathorbit_reference(self):
        for w in ('PathOrbit', 'pathorbit', '途有引力', '知识库看板'):
            self.assertNotIn(w, self.text, 'Dashboard 不得出现 %s' % w)

    def test_html_parses(self):
        from html.parser import HTMLParser

        class C(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.err = [], []

            def handle_starttag(self, tag, attrs):
                if tag not in ('meta', 'br', 'img', 'input', 'link', 'hr'):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()
                elif tag in self.stack:
                    self.err.append(tag)

        c = C()
        c.feed(self.text)
        self.assertEqual(c.stack, [], '标签未闭合：%s' % c.stack)
        self.assertEqual(c.err, [], '标签不匹配：%s' % c.err)

    def test_js_syntax(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('环境没有 node，跳过 JS 语法检查')
        scripts = re.findall(r'<script[^>]*>(.*?)</script>', self.text, re.S)
        self.assertTrue(scripts)
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                         encoding='utf-8') as f:
            f.write(scripts[-1])
            tmp = f.name
        try:
            p = subprocess.run([node, '--check', tmp], capture_output=True,
                               text=True, encoding='utf-8', errors='replace')
            self.assertEqual(p.returncode, 0, p.stderr)
        finally:
            os.unlink(tmp)

    def test_has_all_required_sections(self):
        """七块内容必须在位：趋势 / 客户端 / 模型 / 覆盖 / 留存 / 自动化 / 价格。

        （Phase 1B 重构后的 id：东方数字中枢布局，区块与旧版一一对应）
        """
        for idd in ('flowSvg', 'starSvg', 'mList',
                    'nodeList', 'ret', 'pipe', 'hmCost'):
            self.assertIn('id="%s"' % idd, self.text, '缺少区块 %s' % idd)

    def test_has_demo_and_empty_state(self):
        self.assertIn('演示数据', self.text)
        self.assertIn('账本里暂时没有用量记录', self.text)
        self.assertIn('源文件消失', self.text)
        self.assertIn('未配置', self.text)

    def test_states_visible(self):
        """三档数据等级与自动化失败态都要有可视化呈现。"""
        for w in ('TOKEN', 'ACTIVITY', 'UNKNOWN'):
            self.assertIn(w, self.text)
        for w in ('success', 'failed', 'partial_failure', 'no_supported_sources'):
            self.assertIn(w, self.text, '自动化状态 %s 未呈现' % w)

    def test_offline_fallback_explained(self):
        """直接双击打开（file://）读不到同目录 json 时，必须给出可执行的替代路径。"""
        self.assertIn('载入本地 board', self.text)
        self.assertIn('FileReader', self.text)
        self.assertIn('演示数据', self.text)

    def test_self_contained_size(self):
        n = len(self.text.encode('utf-8'))
        self.assertLess(n, 400 * 1024, 'Dashboard 应保持轻量自包含')


if __name__ == '__main__':
    unittest.main(verbosity=2)
