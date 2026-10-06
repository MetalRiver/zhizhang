#!/usr/bin/env python3
"""PathOrbit Adapter 测试（阶段二插件）。

这些用例**不属于 Core**：Core 不依赖它们，它们依赖 Core 的出口协议
（usage-board.schema.json）。适配器本身只读 usage-board.json，
不碰账本 SQLite、不写知识库正文。

运行：
    python -m unittest discover -s integrations/pathorbit/tests -t .
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(PLUGIN))
ADAPTER = os.path.join(PLUGIN, 'pathorbit_adapter.py')
PY = sys.executable


def run(args, cwd):
    p = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='po-adapter-')
        self.adapter = os.path.join(self.tmp, 'pathorbit_adapter.py')
        shutil.copy(ADAPTER, self.adapter)
        self._n = 0

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _valid_board(self, **over):
        b = {
            'schema_version': 1, 'generated_at': '2026-09-18T00:00:00',
            'privacy': {'paths_included': False, 'event_ids_included': False},
            'summary': {'usage_events': 10, 'input_tokens': 1000, 'output_tokens': 200,
                        'cache_read_tokens': 500, 'priced_models': 1,
                        'unpriced_models': 0, 'estimated_cost_usd_top50_models': 1.5,
                        'usage_first_day': '2026-08-01', 'usage_last_day': '2026-09-18'},
            'window_days': 45,
            'daily': [], 'clients': [{'client': 'zcode', 'label': 'ZCode', 'mode': 'usage',
                                      'data_grade': 'TOKEN', 'events': 10, 'input': 1000,
                                      'output': 200, 'cache_read': 500}],
            'activity': [], 'models': [],
            'sources': [{'client': 'zcode', 'label': 'ZCode', 'mode': 'usage',
                         'data_grade': 'TOKEN', 'grade_basis': 'verified',
                         'files_alive': 2, 'files_missing': 1, 'records': 10}],
            'grades': {'counts': {'TOKEN': 1, 'ACTIVITY': 0, 'UNKNOWN': 0},
                       'by_client': {'zcode': 'TOKEN'}, 'opaque': []},
            'opaque_stores': [],
        }
        b.update(over)
        return b

    def _write(self, b):
        self._n += 1
        p = os.path.join(self.tmp, 'b-%d.json' % self._n)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(b, f, ensure_ascii=False)
        return p

    def _json(self, path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)


class TestValidate(Base):
    def test_validate_ok(self):
        p = self._write(self._valid_board())
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 0, out)

    def test_rejects_paths(self):
        p = self._write(self._valid_board(
            privacy={'paths_included': True, 'event_ids_included': False}))
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('路径', out)

    def test_rejects_event_ids(self):
        p = self._write(self._valid_board(
            privacy={'paths_included': False, 'event_ids_included': True}))
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 2, out)

    def test_rejects_wrong_schema(self):
        p = self._write(self._valid_board(schema_version=2))
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 2, out)

    def test_rejects_missing_top_level(self):
        b = self._valid_board()
        del b['clients']
        p = self._write(b)
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('clients', out)

    def test_rejects_leaked_path_despite_flag(self):
        b = self._valid_board()
        b['sources'][0]['path_hint'] = 'D:\\secret\\path'
        p = self._write(b)
        code, out = run([self.adapter, p, '--check'], self.tmp)
        self.assertEqual(code, 2, out)
        self.assertIn('路径', out)


class TestInject(Base):
    def test_rejects_blind_injection_without_marker(self):
        p = self._write(self._valid_board())
        nofit = os.path.join(self.tmp, 'nofit.html')
        with open(nofit, 'w', encoding='utf-8') as f:
            f.write('<html><body>没有插槽</body></html>')
        code, out = run([self.adapter, p, '--board-html', nofit,
                         '--out-html', os.path.join(self.tmp, 'out.html')], self.tmp)
        self.assertEqual(code, 3, '无插槽必须拒绝盲插')
        self.assertIn('拒绝盲目插入', out)
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, 'out.html')))

    def test_injects_into_marker(self):
        p = self._write(self._valid_board())
        fit = os.path.join(self.tmp, 'fit.html')
        with open(fit, 'w', encoding='utf-8') as f:
            f.write('<html><body><h1>Host</h1><!-- PATHORBIT_AI_USAGE --></body></html>')
        out_html = os.path.join(self.tmp, 'out2.html')
        code, out = run([self.adapter, p, '--board-html', fit,
                         '--out-html', out_html], self.tmp)
        self.assertEqual(code, 0, out)
        with open(out_html, encoding='utf-8') as f:
            html = f.read()
        self.assertIn('data-module="ai-usage"', html)
        self.assertIn('Host', html, '原有内容必须保留')
        self.assertIn('AI USAGE', html)


class TestModule(Base):
    def test_cost_unconfigured_renders_not_zero(self):
        b = self._valid_board()
        b['summary']['priced_models'] = 0
        b['summary']['estimated_cost_usd_top50_models'] = None
        p = self._write(b)
        mod_out = os.path.join(self.tmp, 'mod.json')
        code, out = run([self.adapter, p, '--module-out', mod_out], self.tmp)
        self.assertEqual(code, 0, out)
        mod = self._json(mod_out)
        self.assertIsNone(mod['kpi']['cost_usd'])
        self.assertFalse(mod['kpi']['cost_configured'])

    def test_module_reports_grades(self):
        b = self._valid_board()
        b['clients'].append({'client': 'catpaw', 'label': 'CatPaw', 'mode': 'activity',
                             'data_grade': 'ACTIVITY', 'events': 0, 'input': 0,
                             'output': 0, 'cache_read': 0})
        # board 必须自洽：加了一个 ACTIVITY 客户端，grades.counts 也要跟着变
        b['grades'] = {'counts': {'TOKEN': 1, 'ACTIVITY': 1, 'UNKNOWN': 0},
                       'by_client': {'zcode': 'TOKEN', 'catpaw': 'ACTIVITY'},
                       'opaque': []}
        p = self._write(b)
        mod_out = os.path.join(self.tmp, 'mod2.json')
        code, out = run([self.adapter, p, '--module-out', mod_out], self.tmp)
        self.assertEqual(code, 0, out)
        mod = self._json(mod_out)
        self.assertEqual(mod['coverage']['token_visible'], 1)
        self.assertEqual(mod['coverage']['activity_only'], 1)

    def test_coverage_falls_back_to_mode_for_legacy_board(self):
        """旧版 board 没有 grades 字段时，退回按 mode 计数，不能崩。"""
        b = self._valid_board()
        del b['grades']
        b['clients'].append({'client': 'catpaw', 'label': 'CatPaw', 'mode': 'activity',
                             'events': 0, 'input': 0, 'output': 0, 'cache_read': 0})
        b['sources'].append({'client': 'catpaw', 'label': 'CatPaw', 'mode': 'activity',
                             'files_alive': 1, 'files_missing': 0, 'records': 4})
        p = self._write(b)
        mod_out = os.path.join(self.tmp, 'mod3.json')
        code, out = run([self.adapter, p, '--module-out', mod_out], self.tmp)
        self.assertEqual(code, 0, out)
        mod = self._json(mod_out)
        self.assertEqual(mod['coverage']['token_visible'], 1)
        self.assertEqual(mod['coverage']['activity_only'], 1)

    def test_does_not_touch_sqlite(self):
        """适配器只读 JSON：源码里不得出现 sqlite 访问。"""
        with open(ADAPTER, encoding='utf-8') as f:
            src = f.read()
        self.assertNotIn('sqlite3', src)
        self.assertNotIn('usage.db', src)

    def test_does_not_write_knowledge_base(self):
        """适配器不得出现任何知识库写入路径。"""
        with open(ADAPTER, encoding='utf-8') as f:
            src = f.read()
        for w in ('04-领域', '可视化看板', '.md'):
            self.assertNotIn(w, src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
