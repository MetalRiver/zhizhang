#!/usr/bin/env python3
"""test_product_pages.py — Round 9 产品页面与验收测试。

全部使用合成夹具 + 沙盒。浏览器测试用 Chrome headless + ready signal。
不修改 production DB / pricing / scheduler。
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
import threading
import time
import unittest
import urllib.error
import urllib.request
from importlib.util import find_spec

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import serve        # noqa: E402
import ledger       # noqa: E402
from test_replay_v1 import msg, run  # noqa: E402
from test_pricing_semantics import V1BoardBase  # noqa: E402


def chrome_path():
    for c in (os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
              r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if os.path.isfile(c):
            return c
    return None


HAS_CHROME = chrome_path() is not None


def dump_dom(url, timeout=60):
    c = chrome_path()
    if not c:
        return None
    import tempfile as tf
    prof = _safety.sandbox_dir(prefix='ul-dom-')
    r = subprocess.run([c, '--headless=new', '--disable-gpu', '--no-sandbox',
                        '--enable-unsafe-swiftshader',
                        '--user-data-dir=' + prof,
                        '--window-size=1920,1080',
                        '--virtual-time-budget=9000',
                        '--dump-dom', url],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout)
    shutil.rmtree(prof, ignore_errors=True)
    return r.stdout or ''


def start_server(root, port=0):
    """启动 serve.py 子进程，返回 (proc, base_url)。"""
    if port == 0:
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
    # 沙盒自洽：serve.py + paths.py 都从沙盒运行（paths.py 的
    # APP_ROOT = 脚本目录 = 沙盒），公开仓无需根目录 usage.db。
    proc = subprocess.Popen(
        [sys.executable, os.path.join(root, 'serve.py'), '--port', str(port)],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:%d' % port
    for _ in range(30):
        try:
            r = urllib.request.urlopen(base + '/api/v1/overview', timeout=2)
            r.read()
            return proc, base
        except Exception:
            time.sleep(0.3)
    return proc, base


import socket  # noqa: E402


class NavFixture(unittest.TestCase):
    """产出一个有 board + 7 页面的沙盒 serve 实例。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-nav-')
        for f in ('ledger.py', 'paths.py', 'autopilot.py',
                  'usage-board.schema.json', 'serve.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        web_src = os.path.join(ROOT, 'web')
        web_dst = os.path.join(self.tmp, 'web')
        shutil.copytree(web_src, web_dst, dirs_exist_ok=True)
        # V1 reads real query facts; a handcrafted board alone is not a ledger.
        from test_v1_runtime import _seed_db
        _seed_db(os.path.join(self.tmp, 'usage.db'), [
            ('zcode', 'nav-session', 'nav-event', int(time.time()*1000),
             'nav-model', 100, 10, 't', 't', 'nav-project')])
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({c: os.path.join(self.tmp, 'missing', c) for c in
                       ('zcode','dsh','workbuddy','catpaw','traecn','codex')}, f)
        for f in ('usage-board.json',):
            bp = os.path.join(web_dst, f)
            if os.path.isfile(bp):
                os.remove(bp)
        # 最小 board（多币种 + coverage + dedup + opaque）
        board = {
            'schema_version': 1, 'generated_at': '2026-09-26T12:00:00',
            'privacy': {'paths_included': False, 'event_ids_included': False},
            'summary': {'usage_events': 3, 'input_tokens': 30,
                        'output_tokens': 6, 'reasoning_tokens': 0,
                        'cache_read_tokens': 30, 'cache_write_tokens': 0,
                        'total_tokens': 36, 'usage_first_day': '2026-09-01',
                        'usage_last_day': '2026-09-01',
                        'priced_models': 1, 'unpriced_models': 1,
                        'estimated_cost_usd_top50_models': 0.00006,
                        'estimated_cost_by_currency': {'USD': 0.00006}},
            'grades': {'counts': {'TOKEN': 1, 'ACTIVITY': 0, 'UNKNOWN': 1},
                       'by_client': {'dsh': 'TOKEN'}, 'opaque': ['test_opaque']},
            'window_days': 45,
            'daily': [{'day': '2026-09-01', 'client': 'dsh', 'events': 3,
                       'input': 30, 'output': 6, 'reasoning': 0,
                       'cache_read': 30, 'cache_write': 0}],
            'clients': [{'client': 'dsh', 'label': 'dsh', 'mode': 'usage',
                         'data_grade': 'TOKEN', 'events': 3, 'input': 30,
                         'output': 6, 'reasoning': 0, 'cache_read': 30,
                         'cache_write': 0, 'first_day': '2026-09-01',
                         'last_day': '2026-09-01'}],
            'activity': [{'client': 'catpaw', 'label': 'CatPaw',
                          'mode': 'activity', 'records': 5, 'sessions': 2,
                          'days': 1, 'first_day': '2026-09-01',
                          'last_day': '2026-09-01'}],
            'models': [
                {'model': 'm-usd', 'provider': 'P', 'events': 2,
                 'input': 20, 'output': 4, 'reasoning': 0, 'cache_read': 0,
                 'cache_write': 0, 'cost': 0.000048, 'cost_usd': 0.000048,
                 'currency': 'USD', 'pricing_source': 'manual_verified',
                 'priced': True},
                {'model': 'GLM-5.3-Flash', 'provider': 'bigmodel',
                 'events': 1, 'input': 3_798_000, 'output': 8_129,
                 'reasoning': 0, 'cache_read': 3_697_834, 'cache_write': 0,
                 'cost': 3911.665155, 'cost_usd': None,
                 'currency': 'CNY', 'pricing_source': 'manual_verified',
                 'priced': True},
            ],
            'pricing_gaps': [],
            'sources': [
                {'client': 'dsh', 'label': 'dsh', 'mode': 'usage',
                 'visibility': 'token', 'data_grade': 'TOKEN',
                 'grade_basis': 'verified', 'grade_provisional': False,
                 'state': 'found', 'files_alive': 2, 'files_missing': 0,
                 'records': 3, 'path_hint': '…/x#ab'},
                {'client': 'catpaw', 'label': 'CatPaw', 'mode': 'activity',
                 'visibility': 'activity', 'data_grade': 'ACTIVITY',
                 'grade_basis': 'verified', 'grade_provisional': False,
                 'state': 'found', 'files_alive': 1, 'files_missing': 0,
                 'records': 5, 'path_hint': '…/y#cd'},
            ],
            'opaque_stores': [{'client': 'test_opaque', 'label': 'Test Opaque',
                               'mode': 'opaque', 'data_grade': 'UNKNOWN',
                               'grade_basis': 'unreadable_store',
                               'state': 'found', 'reason': 'test',
                               'bytes': 1000, 'path_hint': '…/z#ef'}],
            'automation': {'status': 'success', 'last_run_at':
                           '2026-09-26T12:00:00',
                           'schedule': {'enabled': False,
                                        'interval_minutes': None,
                                        'installed_at': None, 'method': None,
                                        'task_name': None, 'next_run_at': None,
                                        'next_run_basis': 'not_configured',
                                        'note': '未检测到自动同步配置'}},
            'health': {'status': 'partial', 'reason': 'fixture'},
            'dedup': {'status': 'validated', 'method': 'lineage_seed_v1',
                      'default_view': 'effective', 'raw_events': 5,
                      'replay_events': 2, 'effective_events': 3,
                      'unresolved_events': 0, 'confidence': 'confirmed_lineage',
                      'raw_summary': {'usage_events': 5},
                      'effective_summary': {'usage_events': 3},
                      'by_client': [{'client': 'dsh', 'label': 'dsh',
                                     'raw': 5, 'replay': 2, 'effective': 3}]},
            'pricing_coverage': {
                'effective_models': 2, 'priced_models': 2,
                'unpriced_models': 0, 'effective_tokens': 36,
                'priced_tokens': 36, 'unpriced_tokens': 0,
                'token_coverage_pct': 100.0, 'model_coverage_pct': 100.0,
                'priced_by_source': {'manual_verified': 2,
                                     'auto_reference': 0},
                'multi_currency': True, 'pricing_basis': 'current_reference',
                'cost_semantics': 'api_equivalent_estimate',
                'as_of': '2026-09-26T12:00:00'},
        }
        bp = os.path.join(self.tmp, 'usage-board.json')
        with open(bp, 'w', encoding='utf-8') as f:
            json.dump(board, f, ensure_ascii=False)
        self.proc, self.base = start_server(self.tmp)

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fetch(self, path):
        r = urllib.request.urlopen(self.base + path, timeout=10)
        return r.status, json.loads(r.read())

    def dom(self, path='/'):
        d = dump_dom(self.base + path)
        return d or ''


class TestNavItems(NavFixture):
    """Functional V1：/ = v1 shell，一级入口只有 总览 / 探索 / 设置。
    （V1.1 Legacy Retirement：六个旧版二级页与 /index.html 已退役，
    仅存在于 Git 历史，无运行时入口。）"""

    def test_home_nav_three_entries(self):
        # Functional V1：/ = v1 shell，一级导航固定 总览/探索/设置（无 /analyze）
        dom = dump_dom(self.base + '/')
        for name in ('overview', 'explore', 'settings'):
            self.assertIn('data-nav="%s"' % name, dom, name)
        self.assertNotIn('data-nav="analyze"', dom)

    def test_legacy_pages_retired(self):
        # 旧 SPA 承载页与其 ?page= 二级路由必须 404（无运行时入口）
        for path in ('/index.html', '/index.html?page=sources',
                     '/index.html?page=settings'):
            try:
                urllib.request.urlopen(self.base + path, timeout=10)
                self.fail('legacy 页仍可达：%s' % path)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 404, path)

    def test_overview_route_default(self):
        # V1 Overview：真实数据渲染的首页（Project Ledger 为主角）
        dom = dump_dom(self.base + '/')
        self.assertIn('最近 AI 都用在哪？', dom)
        self.assertIn('我的项目', dom)


class TestBoardData(unittest.TestCase):
    """读真实 board JSON，验证数据结构关系（不用固定数字）。"""

    @classmethod
    def setUpClass(cls):
        cls.b = json.load(open(
            os.path.join(ROOT, 'usage-board.json'), encoding='utf-8'))

    def test_sources_token_grade(self):
        for x in self.b['sources']:
            if x['client'] == 'dsh':
                self.assertEqual(x['data_grade'], 'TOKEN')

    def test_sources_activity_grade(self):
        for x in self.b['sources']:
            if x['client'] == 'catpaw':
                self.assertEqual(x['data_grade'], 'ACTIVITY')

    def test_unknown_opaque_store(self):
        for x in self.b['opaque_stores']:
            self.assertEqual(x['data_grade'], 'UNKNOWN')

    def test_clients_effective(self):
        for c in self.b['clients']:
            self.assertGreater(c['events'], 0)

    def test_dsh_replay_audit(self):
        dd = self.b['dedup']
        self.assertEqual(dd['method'], 'lineage_seed_v1')
        self.assertGreater(dd['replay_events'], 0)
        self.assertEqual(dd['raw_events'],
                         dd['replay_events'] + dd['effective_events'])

    def test_activity_client_semantics(self):
        for a in self.b['activity']:
            self.assertGreater(a['records'], 0)

    def test_models_cny(self):
        for m in self.b['models']:
            if m.get('currency') == 'CNY':
                self.assertIsNone(m['cost_usd'])

    def test_models_usd(self):
        for m in self.b['models']:
            if m.get('currency') == 'USD':
                self.assertIsNotNone(m['cost_usd'])

    def test_models_unpriced_not_zero(self):
        for m in self.b['models']:
            if not m['priced']:
                self.assertIsNone(m.get('cost'))

    def test_pricing_coverage(self):
        pc = self.b['pricing_coverage']
        self.assertEqual(pc['cost_semantics'], 'api_equivalent_estimate')

    def test_health_honest(self):
        self.assertIn(self.b.get('health', {}).get('status', 'unknown'),
                      ('partial', 'healthy', 'unknown'))

    def test_scheduler_honest(self):
        sched = self.b.get('automation', {}).get('schedule', {})
        self.assertIsInstance(sched.get('enabled', False), bool)

    def test_no_absolute_path_leak(self):
        body = json.dumps(self.b, ensure_ascii=False)
        self.assertNotIn('C:' + chr(92), body)
        self.assertNotIn('D:' + chr(92), body)

    def test_no_event_id_leak(self):
        body = json.dumps(self.b, ensure_ascii=False)
        self.assertNotIn('"event_id"', body)

    def test_currency_buckets_exist(self):
        by = self.b['summary']['estimated_cost_by_currency']
        self.assertIn('CNY', by)
        self.assertIn('USD', by)

class TestUsagePage(NavFixture):
    def test_usage_effective_default(self):
        # 26) EFFECTIVE 默认：daily 行是 effective（3 行）
        st, b = self.fetch('/api/v1/overview')
        self.assertGreater(len(b['daily']), 0)   # daily 有数据
        for d in b['daily']:
            self.assertGreater(d['events'], 0)

    def test_usage_raw_not_in_daily(self):
        # 28) RAW 不伪造 daily（daily 只有 effective 行）
        st, b = self.fetch('/api/v1/overview')
        self.assertNotIn('raw_daily', b)


class TestSettingsPage(NavFixture):
    def test_settings_schema_version(self):
        # 29) settings 显示 schema version
        st, b = self.fetch('/api/v1/overview')
        self.assertEqual(b['schema_version'], 1)

    def test_settings_privacy(self):
        # 30) privacy 声明
        st, b = self.fetch('/api/v1/overview')
        self.assertIs(b['privacy']['paths_included'], False)


if __name__ == '__main__':
    unittest.main()
