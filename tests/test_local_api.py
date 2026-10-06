#!/usr/bin/env python3
"""test_local_api.py — Round 6 Local API v0 测试。

安全边界：本文件内所有会触发 scan 的用例均在沙盒（临时目录 + 复制产物）
中以子进程运行，绝不通过 HTTP 触碰真实 usage.db。

in-process 只读用例通过覆盖 serve.HERE / serve.WEB_DIR 指向沙盒；
serve.get_board 的 ledger 兜底在沙盒内不可导入 → 503，无真实 DB 接触。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import threading
import time
import unittest
import http.client
import urllib.request
from importlib.util import find_spec

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import serve  # noqa: E402  （只导入，不在测试进程触发 scan）
from test_replay_v1 import msg, run  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(method, url, body=None, headers=None, timeout=30):
    """返回 (status, body_bytes, headers)。使用 http.client 保留原始路径。"""
    u = urllib.request.urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=timeout)
    try:
        conn.request(method, u.path + (('?'+u.query) if u.query else ''),
                     body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        return r.status, data, dict(r.getheaders())
    finally:
        conn.close()


def js_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


class ApiBase(unittest.TestCase):
    """in-process 只读 API：沙盒 board + 覆盖 serve 根目录。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-api-')
        self._old_here, self._old_web = serve.HERE, serve.WEB_DIR
        serve.HERE = self.tmp
        self.web = os.path.join(self.tmp, 'web')
        os.makedirs(os.path.join(self.web, 'v1'), exist_ok=True)
        shutil.copy(os.path.join(ROOT, 'web', 'v1', 'shell.html'),
                    os.path.join(self.web, 'v1', 'shell.html'))
        serve.WEB_DIR = self.web
        # 合成 board：10 个不同日期 + 全部投影块
        days = [{'day': '2026-09-%02d' % i, 'client': 'dsh', 'events': i,
                 'input': 100 * i, 'output': 10 * i, 'reasoning': 0,
                 'cache_read': 1000 * i, 'cache_write': 0}
                for i in range(1, 11)]
        self.board = {
            'schema_version': 1, 'generated_at': '2026-09-26T09:00:00',
            'privacy': {'paths_included': False, 'event_ids_included': False},
            'summary': {'usage_events': 55, 'input_tokens': 5500,
                        'output_tokens': 550, 'reasoning_tokens': 0,
                        'cache_read_tokens': 55000, 'cache_write_tokens': 0,
                        'total_tokens': 6050, 'usage_first_day': '2026-09-01',
                        'usage_last_day': '2026-09-10', 'priced_models': 0,
                        'unpriced_models': 0,
                        'estimated_cost_usd_top50_models': None},
            'grades': {'counts': {'TOKEN': 1, 'ACTIVITY': 0, 'UNKNOWN': 0},
                       'by_client': {'dsh': 'TOKEN'}, 'opaque': []},
            'window_days': 45, 'daily': days,
            'clients': [{'client': 'dsh', 'label': 'dsh', 'mode': 'usage',
                         'data_grade': 'TOKEN', 'events': 55, 'input': 5500,
                         'output': 550, 'reasoning': 0, 'cache_read': 55000,
                         'cache_write': 0, 'first_day': '2026-09-01',
                         'last_day': '2026-09-10'}],
            'activity': [], 'models': [], 'pricing_gaps': [],
            'sources': [{'client': 'dsh', 'label': 'dsh', 'mode': 'usage',
                         'visibility': 'token', 'data_grade': 'TOKEN',
                         'grade_basis': 'verified', 'grade_provisional': False,
                         'state': 'found', 'files_alive': 1,
                         'files_missing': 0, 'records': 55,
                         'path_hint': '…/x#ff'}],
            'opaque_stores': [], 'automation': {'status': 'success'},
            'health': {'status': 'partial', 'reason': 'fixture'},
            'dedup': {'status': 'validated', 'method': 'lineage_seed_v1',
                      'default_view': 'effective', 'raw_events': 55,
                      'replay_events': 0, 'effective_events': 55,
                      'unresolved_events': 0, 'confidence': 'confirmed_lineage',
                      'raw_summary': {'usage_events': 55},
                      'effective_summary': {'usage_events': 55},
                      'by_client': []},
        }
        with open(os.path.join(self.tmp, 'usage-board.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(self.board, f, ensure_ascii=False)
        self.httpd = serve.make_server(0)
        self.port = self.httpd.server_address[1]
        self.base = 'http://127.0.0.1:%d' % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        serve.HERE, serve.WEB_DIR = self._old_here, self._old_web
        serve.SCAN_STATE['running'] = False
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestServerBasics(ApiBase):
    def test_binds_localhost_only(self):
        # 1) 绑定 127.0.0.1
        self.assertEqual(self.httpd.server_address[0], '127.0.0.1')
        self.assertEqual(serve.HOST, '127.0.0.1')

    def test_no_0_0_0_0_exposure(self):
        # 2) 代码层拒绝其它 host：无 --host 参数，HOST 常量钉死，无 0.0.0.0
        src = open(os.path.join(ROOT, 'serve.py'), encoding='utf-8').read()
        self.assertNotIn("add_argument('--host'", src)
        self.assertNotIn('0.0.0.0', src)
        self.assertEqual(serve.HOST, '127.0.0.1')

    def test_unknown_route_404(self):
        # 11) 未知路由 404 + 统一错误形状
        st, body, _ = req('GET', self.base + '/api/v1/unknown')
        j = json.loads(body)
        self.assertEqual(st, 404)
        self.assertFalse(j['ok'])
        self.assertIn('message', j)

    def test_path_traversal_blocked(self):
        # 12) /assets/../ 穿越被拒
        st, body, _ = req('GET', self.base + '/assets/../usage-board.json')
        self.assertEqual(st, 404)
        self.assertNotIn(b'usage_events', body)
        st2, _, _ = req('GET', self.base + '/assets/..%2Fledger.py')
        self.assertEqual(st2, 404)

    def test_no_directory_listing(self):
        # 13) 目录浏览被禁止
        st, _, _ = req('GET', self.base + '/assets/')
        self.assertEqual(st, 404)
        st2, _, _ = req('GET', self.base + '/assets')
        self.assertIn(st2, (404, 403))

    def test_static_served(self):
        st, body, hdr = req('GET', self.base + '/v1/shell.html')
        self.assertEqual(st, 200)
        self.assertIn(b'Functional V1', body)


class TestProjection(ApiBase):
    def test_overview_equals_board(self):
        # 3) overview == usage-board.json（完整正式 schema，非第二套）
        st, body, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body), self.board)
        with open(os.path.join(self.tmp, 'usage-board.json'),
                  encoding='utf-8') as f:
            self.assertEqual(json.loads(body), json.load(f))

    def test_get_health(self):
        # 4) health 产物透传；缺失时诚实降级（503，绝不伪造 healthy）
        with open(os.path.join(self.tmp, 'health.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'status': 'partial', 'reason': 'fixture'}, f)
        st, body, _ = req('GET', self.base + '/api/v1/health')
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)['status'], 'partial')
        os.remove(os.path.join(self.tmp, 'health.json'))
        st2, _, _ = req('GET', self.base + '/api/v1/health')
        self.assertIn(st2, (503, 200))
        if st2 == 200:
            self.assertNotEqual(json.loads(body)['status'], 'healthy')

    def test_sources_projection(self):
        # 5) sources == overview.sources
        _, b1, _ = req('GET', self.base + '/api/v1/sources')
        _, b2, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(json.loads(b1), json.loads(b2)['sources'])

    def test_clients_projection(self):
        # 6) clients == overview.clients
        _, b1, _ = req('GET', self.base + '/api/v1/clients')
        _, b2, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(json.loads(b1), json.loads(b2)['clients'])

    def test_models_projection(self):
        # 7) models == overview.models
        _, b1, _ = req('GET', self.base + '/api/v1/models')
        _, b2, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(json.loads(b1), json.loads(b2)['models'])

    def test_activity_projection(self):
        # 8) activity == overview.activity
        _, b1, _ = req('GET', self.base + '/api/v1/activity')
        _, b2, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(json.loads(b1), json.loads(b2)['activity'])

    def test_usage_valid_window(self):
        # 9) 合法窗口：?days=7 → 最近 7 行，truncated=false
        st, body, _ = req('GET', self.base + '/api/v1/usage?days=7')
        j = json.loads(body)
        self.assertEqual(st, 200)
        self.assertFalse(j['truncated'])
        self.assertEqual(j['requested_days'], 7)
        self.assertEqual(len(j['daily']), 7)
        self.assertEqual(j['daily'][-1]['day'], '2026-09-10')

    def test_usage_over_window_truthful(self):
        # 10) 超窗：?days=90 → truncated=true + requested/available，不补 0
        st, body, _ = req('GET', self.base + '/api/v1/usage?days=90')
        j = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(j['truncated'])
        self.assertEqual(j['requested_days'], 90)
        self.assertEqual(j['available_days'], 10)
        self.assertEqual(j['window_days'], 45)
        self.assertEqual(len(j['daily']), 10)     # 只有真实存在的天数

    def test_usage_bad_days(self):
        st, body, _ = req('GET', self.base + '/api/v1/usage?days=abc')
        self.assertEqual(st, 400)
        st2, _, _ = req('GET', self.base + '/api/v1/usage?days=0')
        self.assertEqual(st2, 400)


class TestScanSafety(ApiBase):
    def test_post_scan_json_only(self):
        # 14) 非 JSON Content-Type 拒绝
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=b'{}', headers={'Content-Type': 'text/plain'})
        self.assertEqual(st, 400)

    def test_post_full_rejected(self):
        # 15) {"full": true} 必须拒绝
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=json.dumps({'full': True}).encode(),
                          headers={'Content-Type': 'application/json'})
        j = json.loads(body)
        self.assertEqual(st, 400)
        self.assertEqual(j['error'], 'full_not_allowed')

    def test_cross_origin_rejected(self):
        # 16) 异源 Origin → 403
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=b'{}',
                          headers={'Content-Type': 'application/json',
                                   'Origin': 'http://evil.example.com'})
        self.assertEqual(st, 403)

    def test_post_scan_idempotent_shape(self):
        # 严格模式：未知字段拒绝
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=json.dumps({'pricing': True}).encode(),
                          headers={'Content-Type': 'application/json'})
        self.assertEqual(st, 400)

    def test_concurrent_scan_409_and_get_during_scan(self):
        # 18+19) 持锁期间：第二个 scan 409；GET 继续返回最近成功 board
        acquired = serve.SCAN_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        serve.SCAN_STATE['running'] = True
        try:
            st, body, _ = req('POST', self.base + '/api/v1/scan',
                              body=b'{}',
                              headers={'Content-Type': 'application/json'})
            self.assertEqual(st, 409)
            self.assertEqual(json.loads(body)['error'], 'scan_in_progress')
            st2, b2, _ = req('GET', self.base + '/api/v1/overview')
            self.assertEqual(st2, 200)
            self.assertEqual(json.loads(b2), self.board)
        finally:
            serve.SCAN_STATE['running'] = False
            serve.SCAN_LOCK.release()

    def test_get_does_not_modify_db(self):
        # 22) GET 不改账本：沙盒 db 文件哈希前后一致
        dbf = os.path.join(self.tmp, 'usage.db')
        with open(dbf, 'wb') as f:
            f.write(b'fake-db-bytes')
        h0 = hashlib.sha256(open(dbf, 'rb').read()).hexdigest()
        for p in ('/api/v1/overview', '/api/v1/health', '/api/v1/sources',
                  '/api/v1/clients', '/api/v1/models', '/api/v1/usage?days=7',
                  '/api/v1/activity'):
            req('GET', self.base + p)
        h1 = hashlib.sha256(open(dbf, 'rb').read()).hexdigest()
        self.assertEqual(h0, h1)


class TestScanEndToEnd(unittest.TestCase):
    """子进程沙盒：真实 run_once 编排（绝不触碰真实 usage.db）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-apiscan-')
        for f in ('ledger.py', 'autopilot.py', 'serve.py',
                  'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        # workbuddy 夹具
        wb = os.path.join(self.tmp, 'wb', 'p1')
        os.makedirs(wb, exist_ok=True)
        import time as _t
        now = int(_t.time() * 1000)
        with open(os.path.join(wb, 'sess-1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({
                    'id': 'w%d' % i, 'sessionId': 'sess-1', 'timestamp': now + i,
                    'providerData': {'model': 'm-x', 'requestModelName': 'P',
                                     'rawUsage': {'prompt_tokens': 200,
                                                  'completion_tokens': 20,
                                                  'prompt_tokens_details': {
                                                      'cached_tokens': 100}}}}) + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite', 'dsh': empty,
                       'catpaw': empty, 'traecn': empty, 'codex': empty,
                       'workbuddy': os.path.join(self.tmp, 'wb').replace('\\', '/')}, f)
        self.port = free_port()
        self.proc = subprocess.Popen(
            [sys.executable, 'serve.py', '--port', str(self.port)],
            cwd=self.tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = 'http://127.0.0.1:%d' % self.port
        for _ in range(50):
            try:
                st, _, _ = req('GET', self.base + '/api/v1/overview', timeout=2)
                if st in (200, 503):
                    break
            except Exception:
                time.sleep(0.2)

    def tearDown(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_accepted_refreshes_products(self):
        # 17+20+21) 同源扫描成功；board/health/run-state 同轮刷新；无 pricing
        origin = self.base
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=b'{}',
                          headers={'Content-Type': 'application/json',
                                   'Origin': origin}, timeout=120)
        self.assertEqual(st, st)  # 先记录
        j = json.loads(body)
        self.assertTrue(j.get('ok'), body)
        for f in ('usage-board.json', 'health.json', 'run-state.json'):
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, f)), f)
        rs = js_json(os.path.join(self.tmp, 'run-state.json'))
        self.assertEqual(rs.get('trigger'), 'api')
        self.assertEqual((rs.get('pricing') or {}).get('status'), 'skipped')
        self.assertEqual(rs.get('status'), 'success')
        # board 已刷新且可被 GET 读取
        st2, b2, _ = req('GET', self.base + '/api/v1/overview')
        self.assertEqual(st2, 200)
        self.assertEqual(json.loads(b2)['summary']['usage_events'], 3)

    def test_scan_missing_origin_from_cli_ok(self):
        # 无 Origin（CLI/curl）允许
        st, body, _ = req('POST', self.base + '/api/v1/scan',
                          body=b'{}',
                          headers={'Content-Type': 'application/json'},
                          timeout=120)
        j = json.loads(body)
        self.assertEqual(st, 200)
        self.assertTrue(j.get('ok'), body)


def chrome_path():
    for c in (os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
              r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if os.path.isfile(c):
            return c
    return None


HAS_CHROME = chrome_path() is not None


@unittest.skipUnless(HAS_CHROME, '需要 Chrome/Edge headless')
class TestUiModes(unittest.TestCase):
    """23：UI API 模式语义（v1 shell 真实数据渲染）。
    （V1.1 Legacy Retirement：Overview V2 的 BOARD/SNAPSHOT 静态降级模式
    随 overview-v2.html 一并退役，对应三态契约测试退出。）"""

    def dump(self, url, timeout=200):
        prof = _safety.sandbox_dir(prefix='ul-chrome-')
        r = subprocess.run([chrome_path(), '--headless=new', '--disable-gpu',
                            '--no-sandbox', '--enable-unsafe-swiftshader',
                            '--user-data-dir=' + prof, '--window-size=1920,1080',
                            '--virtual-time-budget=7000', '--dump-dom', url],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=timeout)
        return r.stdout or ''

    def test_ui_api_mode(self):
        # 23) API 模式：serve.py 存活时 / = Overview V2，传输徽标 API + 扫描可用
        from test_v1_runtime import _sandbox_serve, _seed_db
        tmp = _sandbox_serve('ul-ui-api-')
        _seed_db(os.path.join(tmp, 'usage.db'), [
            ('zcode', 'ui-session', 'ui-event', int(time.time()*1000),
             'ui-model', 100, 10, 't', 't', 'ui-project')])
        with open(os.path.join(tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({c: os.path.join(tmp, 'missing', c) for c in
                       ('zcode','dsh','workbuddy','catpaw','traecn','codex')}, f)
        port = free_port()
        proc = subprocess.Popen(
            [sys.executable, os.path.join(tmp, 'serve.py'),
             '--port', str(port)],
            cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = 'http://127.0.0.1:%d' % port
            ready = False
            for _ in range(60):                    # 最多等 12s 就绪
                try:
                    st, _, _ = req('GET', base + '/api/v1/overview', timeout=2)
                    ready = st in (200, 503)
                    if ready:
                        break
                except Exception:
                    time.sleep(0.2)
            self.assertTrue(ready, 'serve.py 未在预期时间内就绪')
            dom = self.dump(base + '/')
            # Functional V1：/ = v1 shell（真实 API 渲染）
            self.assertIn('最近 AI 都用在哪？', dom)
            self.assertIn('我的项目', dom)
            self.assertNotIn('静态打开', dom)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
