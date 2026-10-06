#!/usr/bin/env python3
"""test_maintenance.py — RC.3 维护静默端点测试（quiesce / resume）。

安全边界：in-process 沙盒（临时目录 + 覆盖 serve.HERE / ledger.SCAN_LOCK_PATH），
绝不触碰真实 usage.db 与仓库根扫描锁。

契约：
  - POST /api/v1/maintenance/quiesce → 200，占用扫描锁，之后一切写路径 503 maintenance。
  - 重复 quiesce 幂等（200，不重复加锁）。
  - POST /api/v1/maintenance/resume → 200，释放锁，写路径恢复可用。
  - GET 只读路径不受 quiesce 影响。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import unittest
import http.client
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import _safety  # noqa: E402
import serve  # noqa: E402
import ledger  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(port, method, path, body=None, headers=None, timeout=15):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload,
                     headers=dict(headers or {},
                                  **({'Content-Type': 'application/json'}
                                     if payload else {})))
        r = conn.getresponse()
        data = r.read()
        return r.status, json.loads(data.decode('utf-8') or '{}')
    finally:
        conn.close()


class MaintenanceQuiesce(unittest.TestCase):
    """quiesce / resume 生命周期（in-process 沙盒）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-maint-')
        os.makedirs(os.path.join(self.tmp, 'web', 'v1'), exist_ok=True)
        shutil.copy(os.path.join(ROOT, 'web', 'v1', 'shell.html'),
                    os.path.join(self.tmp, 'web', 'v1', 'shell.html'))
        self._old_here, self._old_web = serve.HERE, serve.WEB_DIR
        self._old_lock = ledger.SCAN_LOCK_PATH
        serve.HERE = self.tmp
        serve.WEB_DIR = os.path.join(self.tmp, 'web')
        ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
        # 清空维护态（其它用例/进程内残留防御）
        serve.MAINTENANCE['quiesced'] = False
        serve.MAINTENANCE['lock_handle'] = None
        while serve.SCAN_LOCK.locked():
            serve.SCAN_LOCK.release()

        self.port = free_port()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', self.port), serve.LedgerHandler)
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()

    def tearDown(self):
        # 无论用例成败都恢复锁/维护态，避免污染后续用例
        try:
            if serve.MAINTENANCE['quiesced']:
                if serve.MAINTENANCE.get('lock_handle'):
                    ledger.release_scan_lock(serve.MAINTENANCE['lock_handle'])
                serve.MAINTENANCE['quiesced'] = False
                serve.MAINTENANCE['lock_handle'] = None
                serve.SCAN_LOCK.release()
        except Exception:
            pass
        serve.HERE, serve.WEB_DIR = self._old_here, self._old_web
        ledger.SCAN_LOCK_PATH = self._old_lock
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_quiesce_blocks_writes_then_resume_releases(self):
        status, body = req(self.port, 'POST', '/api/v1/maintenance/quiesce', {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get('quiesced'))
        # 跨进程锁文件确实落在沙盒 DATA_ROOT
        self.assertTrue(os.path.isfile(ledger.SCAN_LOCK_PATH))

        # 写路径全部 503 maintenance
        status, body = req(self.port, 'POST', '/api/v1/scan', {})
        self.assertEqual(status, 503, body)
        self.assertEqual(body.get('error'), 'maintenance')
        status, body = req(self.port, 'POST', '/api/v1/projects/alias',
                           {'project_key': 'x', 'alias': 'y'})
        self.assertEqual(status, 503, body)
        self.assertEqual(body.get('error'), 'maintenance')
        status, body = req(self.port, 'POST', '/api/v1/discover/refresh', {})
        self.assertEqual(status, 503, body)
        self.assertEqual(body.get('error'), 'maintenance')

        # 只读路径不受影响（沙盒内 board 缺失 → 503 board_unavailable，而非 maintenance）
        status, body = req(self.port, 'GET', '/api/v1/overview')
        self.assertNotEqual(body.get('error'), 'maintenance')

        # 重复 quiesce 幂等
        status, body = req(self.port, 'POST', '/api/v1/maintenance/quiesce', {})
        self.assertEqual(status, 200, body)

        # resume 释放
        status, body = req(self.port, 'POST', '/api/v1/maintenance/resume', {})
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get('quiesced'))
        self.assertFalse(os.path.isfile(ledger.SCAN_LOCK_PATH),
                         'resume 后扫描锁文件残留')
        # 写路径恢复（沙盒内 scan 编排失败会得到 5xx，但绝不是 maintenance）
        status, body = req(self.port, 'POST', '/api/v1/scan', {})
        self.assertNotEqual(body.get('error'), 'maintenance')

    def test_quiesce_rejects_foreign_origin(self):
        status, body = req(self.port, 'POST', '/api/v1/maintenance/quiesce', {},
                           headers={'Origin': 'http://evil.example.com'})
        self.assertEqual(status, 403, body)
        self.assertFalse(serve.MAINTENANCE['quiesced'])

    def test_resume_without_quiesce_is_noop_200(self):
        status, body = req(self.port, 'POST', '/api/v1/maintenance/resume', {})
        self.assertEqual(status, 200, body)


class MigrationEngine(unittest.TestCase):
    """安全迁移 plan/execute（沙盒内完整验证，§31/§34/§35 契约）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-migr-')
        self.src = os.path.join(self.tmp, 'src-root')
        self.dst = os.path.join(self.tmp, 'dst-root')
        os.makedirs(self.src)
        import paths
        self._old_root = paths.DATA_ROOT
        paths.DATA_ROOT = self.src
        # 造一本最小账本：schema v3 + 核心表 + 数据
        import sqlite3
        conn = sqlite3.connect(os.path.join(self.src, 'usage.db'))
        conn.executescript('''
            PRAGMA user_version = 3;
            CREATE TABLE usage_event (id INTEGER PRIMARY KEY, project_key TEXT, tokens INTEGER);
            CREATE TABLE activity_event (id INTEGER PRIMARY KEY);
            CREATE TABLE project_registry (project_key TEXT PRIMARY KEY, display_name_manual TEXT);
            CREATE TABLE session_registry (session_id TEXT PRIMARY KEY, attribution_manual INTEGER DEFAULT 0);
            CREATE TABLE source_file (path TEXT PRIMARY KEY);
            INSERT INTO usage_event (project_key, tokens) VALUES ('p1', 100), ('p2', 200);
            INSERT INTO activity_event (id) VALUES (1), (2), (3);
            INSERT INTO session_registry (session_id, attribution_manual) VALUES ('s1', 1);
            INSERT INTO project_registry (project_key) VALUES ('p1');
            INSERT INTO source_file (path) VALUES ('a'), ('b');
        ''')
        conn.commit()
        conn.close()
        # durable 配置文件 + 运行态文件
        with open(os.path.join(self.src, 'pricing.json'), 'w', encoding='utf-8') as f:
            f.write('{"manual": true}')
        with open(os.path.join(self.src, 'server-state.json'), 'w', encoding='utf-8') as f:
            f.write('{"pid": 123}')
        with open(os.path.join(self.src, 'usage-ledger-scan.lock'), 'w', encoding='utf-8') as f:
            f.write('{"pid": 123}')

    def tearDown(self):
        import paths
        paths.DATA_ROOT = self._old_root
        serve.MAINTENANCE['quiesced'] = False

    def test_plan_validates_targets(self):
        # 同位置 → blocked
        r = serve.migration_plan(self.src)
        self.assertTrue(r['blocked'])
        # 空新目录 → 可迁移 + 分类正确
        r = serve.migration_plan(self.dst)
        self.assertFalse(r['blocked'], r)
        names = {d['name'] for d in r['durable']}
        self.assertIn('usage.db', names)
        self.assertIn('pricing.json', names)
        skipped = {d['name'] for d in r['skipped']}
        self.assertIn('server-state.json', skipped)
        self.assertIn('usage-ledger-scan.lock', skipped)
        # 目标已有账本 → 禁止覆盖（§32）
        os.makedirs(self.dst)
        with open(os.path.join(self.dst, 'usage.db'), 'w') as f:
            f.write('existing ledger')
        r = serve.migration_plan(self.dst)
        self.assertTrue(r['has_ledger'])
        self.assertTrue(any(i['code'] == 'target_has_ledger' for i in r['issues']))
        # 非空普通目录 → 提示不覆盖（§33）
        other = os.path.join(self.tmp, 'other-dir')
        os.makedirs(other)
        with open(os.path.join(other, 'photo.txt'), 'w') as f:
            f.write('x')
        r = serve.migration_plan(other)
        self.assertTrue(r['nonempty_dir'])

    def test_execute_copies_verifies_and_preserves_source(self):
        serve.SCAN_LOCK.acquire()
        try:
            r = serve.migration_execute(self.dst)
        finally:
            serve.SCAN_LOCK.release()
        self.assertTrue(r.get('ok'), r)
        ev = r['evidence']
        self.assertEqual(ev['counts']['usage'], 2)
        self.assertEqual(ev['counts']['activity'], 3)
        self.assertEqual(ev['counts']['manual_attribution'], 1)
        self.assertEqual(ev['counts']['user_version'], 3)
        # 目标库完整且计数一致
        dst_counts = serve._db_counts(os.path.join(self.dst, 'usage.db'))
        for k in ('usage', 'activity', 'projects', 'sessions', 'manual_attribution',
                  'user_version'):
            self.assertEqual(dst_counts[k], ev['counts'][k], k)
        # durable 配置到位、运行态未搬运（§31）
        self.assertTrue(os.path.isfile(os.path.join(self.dst, 'pricing.json')))
        self.assertFalse(os.path.isfile(os.path.join(self.dst, 'server-state.json')))
        self.assertFalse(os.path.isfile(os.path.join(self.dst, 'usage-ledger-scan.lock')))
        # 源账本原样保留（§30：RC.3 不删旧账）
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'usage.db')))
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'server-state.json')))

    def test_execute_rejects_target_with_ledger(self):
        os.makedirs(self.dst)
        with open(os.path.join(self.dst, 'usage.db'), 'w') as f:
            f.write('existing')
        serve.SCAN_LOCK.acquire()
        try:
            r = serve.migration_execute(self.dst)
        finally:
            serve.SCAN_LOCK.release()
        self.assertFalse(r.get('ok'))
        # 源账本未被触碰
        self.assertTrue(os.path.isfile(os.path.join(self.src, 'usage.db')))
        self.assertFalse(os.path.isfile(os.path.join(self.dst, 'pricing.json')))

    def test_execute_missing_source_aborts(self):
        os.remove(os.path.join(self.src, 'usage.db'))
        serve.SCAN_LOCK.acquire()
        try:
            r = serve.migration_execute(self.dst)
        finally:
            serve.SCAN_LOCK.release()
        self.assertFalse(r.get('ok'))


if __name__ == '__main__':
    unittest.main()
