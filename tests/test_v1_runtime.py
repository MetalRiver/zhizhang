# -*- coding: utf-8 -*-
"""Functional V1 Runtime 测试。

覆盖：
  1. V1 路由（/ /explore /settings = v1 shell；无一级 /analyze；
     V1.1 起 legacy 页 /index.html、/overview-v2.html、/v2/* 必须 404）
  2. v1 前端零示例数据（无母版 fixture 项目/数字、无夜航山河残留）
  3. BLOCKER：MANUAL USER ATTRIBUTION > AUTOMATIC ADAPTER ATTRIBUTION
     —— 手工 assign/merge/rename 后重扫（adapter upsert）不被覆盖，
        原始归因保留在 project_key_auto，merge 后新事件落到目标项目
  4. 联合筛选查询 /api/v1/query（筛选后聚合 + 同范围 total + 分页）
  5. 写入能力：alias / assign / merge（沙盒 DB）
  6. 扫描锁互斥：锁持有期间写入 409
  7. First Run 发现 API /api/v1/discover

DB 一律在 _safety 沙盒；子进程 serve.py 以沙盒为 cwd/DATA_ROOT。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import _safety  # noqa: E402

PY = sys.executable


def free_port():
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def req(method, url, body=None, timeout=8):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.read(), dict(resp.headers)


def http_code(url, body=None):
    try:
        st, _, _ = req('GET' if body is None else 'POST', url, body)
        return st
    except urllib.error.HTTPError as e:
        return e.code


def _sandbox_serve(prefix):
    tmp = _safety.sandbox_dir(prefix=prefix)
    for f in ('serve.py', 'paths.py', 'ledger.py', 'autopilot.py',
              'discovery.py', 'source-catalog.json', 'usage-board.schema.json',
              'VERSION'):
        src = os.path.join(ROOT, f)
        if os.path.isfile(src):
            shutil.copy(src, os.path.join(tmp, f))
    shutil.copytree(os.path.join(ROOT, 'web'), os.path.join(tmp, 'web'))
    return tmp


def _start_serve(tmp):
    port = free_port()
    proc = subprocess.Popen(
        [PY, os.path.join(tmp, 'serve.py'), '--port', str(port)],
        cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = 'http://127.0.0.1:%d' % port
    for _ in range(50):
        try:
            req('GET', base + '/api/v1/identity', timeout=2)
            return proc, base
        except Exception:
            import time
            time.sleep(0.2)
    proc.terminate()
    raise AssertionError('serve.py 未就绪')


def _stop_serve(proc, tmp):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


class TestV1Pages(unittest.TestCase):
    """V1 页面资产、路由与「零示例数据」契约。"""

    def test_v1_files_exist(self):
        for f in ('shell.html', 'app.css', 'app.js'):
            p = os.path.join(ROOT, 'web', 'v1', f)
            self.assertTrue(os.path.isfile(p), '缺少 %s' % p)

    def test_v1_shell_references_master_css(self):
        with open(os.path.join(ROOT, 'web', 'v1', 'shell.html'), encoding='utf-8') as fh:
            html = fh.read()
        self.assertIn('/v1/app.css', html)
        self.assertIn('/v1/app.js', html)

    def test_v1_no_sample_data_and_no_dark_v3(self):
        """母版示例与旧 V3 视觉体系都不得进正式 Runtime。"""
        with open(os.path.join(ROOT, 'web', 'v1', 'app.js'), encoding='utf-8') as fh:
            js = fh.read()
        for w in ('狐写', '公众号系统', '知识整理', '模型实验', 'p_fox', 'p_ledger',
                  '2026-10-02T14:30', 'mapC', 'mapScene', '夜航山河', 'AI Work Map',
                  'workmap', '/v3/'):
            self.assertNotIn(w, js, 'app.js 含示例/旧视觉标记：%s' % w)
        for f in ('shell.html', 'app.css'):
            with open(os.path.join(ROOT, 'web', 'v1', f), encoding='utf-8') as fh:
                text = fh.read()
            self.assertNotIn('mapC', text, f)

    def test_v1_references_real_apis(self):
        with open(os.path.join(ROOT, 'web', 'v1', 'app.js'), encoding='utf-8') as fh:
            js = fh.read()
        for api in ('/api/v1/query', '/api/v1/identity', '/api/v1/health',
                    '/api/v1/discover', '/api/v1/scan',
                    '/api/v1/sessions/unassigned',
                    '/api/v1/projects/alias', '/api/v1/projects/assign',
                    '/api/v1/projects/merge'):
            self.assertIn(api, js)

    def test_serve_routes(self):
        tmp = _sandbox_serve('ul-v1route-')
        proc, base = _start_serve(tmp)
        try:
            # 首页 / 探索 / 设置 = 同一 V1 壳
            for path in ('/', '/explore', '/settings'):
                st, body, _ = req('GET', base + path)
                html = body.decode('utf-8')
                self.assertEqual(st, 200, path)
                self.assertIn('/v1/app.js', html)
                self.assertIn('Functional V1', html)
            # 无一级 /analyze
            self.assertEqual(http_code(base + '/analyze'), 404)
            # 静态资源
            self.assertEqual(http_code(base + '/v1/app.css'), 200)
            # V1.1 Legacy Retirement：旧页面无任何运行时入口
            for legacy in ('/overview-v2.html', '/index.html', '/v2/overview-v2.js'):
                self.assertEqual(http_code(base + legacy), 404, legacy)
            # First Run 发现 API（空账本可用）：V1.1 引擎载荷 + legacy 兼容键
            st, body, _ = req('GET', base + '/api/v1/discover')
            self.assertEqual(st, 200)
            d = json.loads(body)
            self.assertIn('sources', d)
            self.assertIn('found', d)
            self.assertIn('tools', d)
            self.assertIn('groups', d)
            self.assertTrue(d['privacy']['local_only'])
            # POST 重新发现（完整 discovery）
            st, body, _ = req('POST', base + '/api/v1/discover/refresh', {})
            self.assertEqual(st, 200)
            self.assertIn('tools', json.loads(body))
            # 未归属会话 API（空账本 → 空列表）
            st, body, _ = req('GET', base + '/api/v1/sessions/unassigned')
            self.assertEqual(st, 200)
            self.assertEqual(json.loads(body)['items'], [])
        finally:
            _stop_serve(proc, tmp)


def _seed_db(db_path, rows):
    """手工建 v2 形态账本并种入事件（project_key 初始为 adapter 自动归因）。"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
    CREATE TABLE usage_event (
        client TEXT NOT NULL, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
        ts_ms INTEGER NOT NULL, model TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        source_file TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, is_replay INTEGER NOT NULL DEFAULT 0,
        replay_reason TEXT, replay_source_session TEXT, project_key TEXT,
        PRIMARY KEY (client, session_id, event_id));
    CREATE TABLE source_file (
        client TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
        mtime REAL NOT NULL DEFAULT 0, events INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, missing_since TEXT,
        last_error TEXT, PRIMARY KEY (client, path));
    CREATE TABLE scan_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT,
        events_seen INTEGER NOT NULL DEFAULT 0, events_inserted INTEGER NOT NULL DEFAULT 0,
        events_updated INTEGER NOT NULL DEFAULT 0, files_scanned INTEGER NOT NULL DEFAULT 0,
        files_skipped INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE activity_event (
        client TEXT NOT NULL, session_key TEXT NOT NULL, event_id TEXT NOT NULL,
        ts_ms INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'message',
        source_file TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, PRIMARY KEY (client, session_key, event_id));
    CREATE TABLE project_registry (
        project_key  TEXT PRIMARY KEY, project_kind TEXT NOT NULL,
        display_name TEXT NOT NULL, first_seen_at TEXT, last_seen_at TEXT,
        metadata_json TEXT);
    CREATE TABLE session_registry (
        source TEXT NOT NULL, session_id TEXT NOT NULL, project_key TEXT,
        display_name TEXT, first_seen_at TEXT, last_seen_at TEXT, agent TEXT,
        PRIMARY KEY(source, session_id));
    """)
    conn.executemany(
        'INSERT INTO usage_event (client, session_id, event_id, ts_ms, model,'
        ' input_tokens, output_tokens, first_seen, last_seen, project_key)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
    conn.commit()
    conn.close()


class TestManualAttributionPersistence(unittest.TestCase):
    """BLOCKER 回归：MANUAL USER ATTRIBUTION 优先于 AUTOMATIC ADAPTER ATTRIBUTION。"""

    def setUp(self):
        import ledger
        self.ledger = ledger
        self.tmp = _safety.sandbox_dir(prefix='ul-v1attr-')
        self._old = (ledger.DB_PATH, ledger.BOARD_PATH)
        ledger.DB_PATH = os.path.join(self.tmp, 'usage.db')
        ledger.BOARD_PATH = os.path.join(self.tmp, 'usage-board.json')
        rows = [('zcode', 'sess-1', 'ev-%d' % i, 1700000000000 + i * 1000,
                 'model-x', 100 + i, 10 + i, 't0', 't1', 'pAUTO')
                for i in range(4)]
        _seed_db(ledger.DB_PATH, rows)
        # connect() 会做探测式迁移（v2 形态 → v3 列）
        conn = ledger.connect(create=False)
        self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
        conn.close()

    def tearDown(self):
        self.ledger.DB_PATH, self.ledger.BOARD_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self):
        conn = sqlite3.connect(self.ledger.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def test_manual_assign_survives_rescan(self):
        key = self.ledger.project_alias_add('我的项目', display_name='我的项目')['project_key']
        out = self.ledger.project_assign_sessions(key, [
            {'source': 'zcode', 'session_id': 'sess-1'}])
        self.assertEqual(out['events_updated'], 4)
        conn = self._db()
        try:
            row = conn.execute(
                "SELECT project_key, project_key_manual FROM usage_event"
                " WHERE client='zcode' AND session_id='sess-1' LIMIT 1").fetchone()
            self.assertEqual(row['project_key'], key)
            self.assertEqual(row['project_key_manual'], 1)
        finally:
            conn.close()
        # 重扫：走真实扫描 upsert 路径（ledger.UPSERT，adapter 派生 pAUTO2）
        conn = self.ledger.connect(create=False)
        try:
            conn.execute(self.ledger.UPSERT, (
                'zcode', 'sess-1', 'ev-0', 1700000000000, 'model-x', 'prov',
                100, 10, 0, 0, 0, 'src', 't0', 't1',
                0, None, None, 'pAUTO2'))
            conn.commit()
            row = conn.execute(
                "SELECT project_key, project_key_manual, project_key_auto"
                " FROM usage_event WHERE client='zcode' AND session_id='sess-1'"
                " AND event_id='ev-0'").fetchone()
            self.assertEqual(row['project_key'], key,
                             '手工归因被重扫覆盖 —— BLOCKER 回归')
            self.assertEqual(row['project_key_manual'], 1)
            self.assertEqual(row['project_key_auto'], 'pAUTO2',
                             'adapter 原始归因必须保留（provenance）')
            srow = conn.execute(
                "SELECT project_key, attribution_manual FROM session_registry"
                " WHERE source='zcode' AND session_id='sess-1'").fetchone()
            self.assertEqual(srow['project_key'], key)
            self.assertEqual(srow['attribution_manual'], 1)
        finally:
            conn.close()

    def test_merge_survives_rescan_and_remaps_new_events(self):
        ka = self.ledger.project_alias_add('项目甲')['project_key']
        kb = self.ledger.project_alias_add('项目乙')['project_key']
        self.ledger.project_assign_sessions(ka, [{'source': 'zcode', 'session_id': 'sess-1'}])
        r = self.ledger.project_merge(ka, kb)
        self.assertGreater(r['events_moved'], 0)
        conn = self.ledger.connect(create=False)
        try:
            # 旧事件停在目标项目
            row = conn.execute(
                "SELECT project_key, project_key_manual FROM usage_event"
                " WHERE client='zcode' AND session_id='sess-1' LIMIT 1").fetchone()
            self.assertEqual(row['project_key'], kb)
            self.assertEqual(row['project_key_manual'], 1)
            # 重扫再派生旧 key（甲）：真实 upsert 路径 + 注册表重映射到目标（乙）
            conn.execute(self.ledger.UPSERT, (
                'zcode', 'sess-1', 'ev-0', 1700000000000, 'model-x', 'prov',
                100, 10, 0, 0, 0, 'src', 't0', 't1',
                0, None, None, ka))
            self.ledger._upsert_project_and_session(conn, {
                'client': 'zcode', 'session_id': 'sess-1',
                'project_key': ka, 'project_kind': 'project',
                'project_display': '项目甲'}, '2026-10-02T00:00:00')
            conn.commit()
            row = conn.execute(
                "SELECT project_key, project_key_auto FROM usage_event"
                " WHERE client='zcode' AND session_id='sess-1'"
                " AND event_id='ev-0'").fetchone()
            self.assertEqual(row['project_key'], kb,
                             'merge 后重扫不得重新产生旧项目归属')
            self.assertEqual(row['project_key_auto'], ka)
        finally:
            conn.close()

    def test_rename_survives_rescan(self):
        key = self.ledger.project_alias_add('pAUTO', display_name='我的名字')['project_key']
        conn = self.ledger.connect(create=False)
        try:
            # 重扫：adapter 带来自动识别名
            self.ledger._upsert_project_and_session(conn, {
                'client': 'zcode', 'session_id': 'sess-1',
                'project_key': key, 'project_kind': 'project',
                'project_display': 'pAUTO'}, '2026-10-02T00:00:00')
            conn.commit()
            row = conn.execute(
                'SELECT display_name, display_name_manual FROM project_registry'
                ' WHERE project_key=?', (key,)).fetchone()
            self.assertEqual(row['display_name'], '我的名字',
                             '用户改名被重扫回退')
            self.assertEqual(row['display_name_manual'], 1)
        finally:
            conn.close()

    def test_alias_keeps_stable_identity(self):
        key = self.ledger.project_alias_add('原名', display_name='原名')['project_key']
        self.ledger.project_alias_add('别名一', project_key=key)
        conn = self._db()
        try:
            row = conn.execute(
                'SELECT project_key, display_name FROM project_registry'
                ' WHERE project_key=?', (key,)).fetchone()
            self.assertEqual(row['project_key'], key)
            self.assertEqual(row['display_name'], '原名')
        finally:
            conn.close()

    def test_backfill_skips_manual_rows(self):
        key = self.ledger.project_alias_add('手工项目')['project_key']
        self.ledger.project_assign_sessions(key, [{'source': 'zcode', 'session_id': 'sess-1'}])
        # backfill 的 UPDATE 语句必须带 project_key_manual=0 守护
        with open(os.path.join(ROOT, 'ledger.py'), encoding='utf-8') as fh:
            src = fh.read()
        for client in ('zcode', 'dsh', 'workbuddy'):
            self.assertIn(
                "UPDATE usage_event SET project_key=? WHERE client='%s'"
                " AND session_id=? AND project_key_manual=0" % client, src)


class TestQueryAPI(unittest.TestCase):
    """联合筛选：筛选后聚合 + 同范围 total + 分页。"""

    def setUp(self):
        import ledger
        self.ledger = ledger
        self.tmp = _safety.sandbox_dir(prefix='ul-v1q-')
        self._old = (ledger.DB_PATH, ledger.BOARD_PATH)
        ledger.DB_PATH = os.path.join(self.tmp, 'usage.db')
        ledger.BOARD_PATH = os.path.join(self.tmp, 'usage-board.json')
        rows = []
        for i in range(6):
            rows.append(('zcode', 'sess-%d' % (i % 2), 'ev-%d' % i,
                         1700000000000 + i * 1000, 'model-x' if i % 2 else '',
                         100 + i, 10 + i, 't0', 't1', 'pK1' if i < 3 else None))
        _seed_db(ledger.DB_PATH, rows)

    def tearDown(self):
        self.ledger.DB_PATH, self.ledger.BOARD_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_query_aggregate_and_paging(self):
        tmp = _sandbox_serve('ul-v1q-serve-')
        # 沙盒 serve 使用沙盒账本：把种子库放进去
        shutil.copy(self.ledger.DB_PATH, os.path.join(tmp, 'usage.db'))
        proc, base = _start_serve(tmp)
        try:
            st, body, _ = req('GET', base + '/api/v1/query?range=all')
            self.assertEqual(st, 200)
            d = json.loads(body)
            a = d['aggregate']
            self.assertEqual(a['events'], 6)
            self.assertEqual(a['sessions'], 2)
            self.assertEqual(a['projects'], 1)   # NULL 不计项目
            self.assertEqual(a['total'], sum(100 + i + 10 + i for i in range(6)))
            self.assertEqual(d['total'], 6)
            self.assertEqual(len(d['items']), 6)
            self.assertEqual(len(d['by_project']), 2)  # pK1 + 待整理（NULL 组）
            pk_keys = [p['project_key'] for p in d['by_project']]
            self.assertIn('pK1', pk_keys)
            self.assertIn(None, pk_keys)
            self.assertEqual(len(d['daily']), 1)
            # 分页
            st, body, _ = req('GET', base + '/api/v1/query?range=all&page=1')
            d2 = json.loads(body)
            self.assertEqual(d2['total'], 6)
            self.assertEqual(len(d2['items']), 0)
            # 审计口径参数校验
            self.assertEqual(http_code(base + '/api/v1/query?audit=bogus'), 400)
            # 未归属
            st, body, _ = req('GET', base + '/api/v1/query?range=all&project=unassigned')
            d3 = json.loads(body)
            self.assertEqual(d3['aggregate']['events'], 3)
            # 会话下钻
            st, body, _ = req('GET',
                              base + '/api/v1/query?range=all&session=zcode%7Csess-0')
            d4 = json.loads(body)
            self.assertEqual(d4['aggregate']['events'], 3)
        finally:
            _stop_serve(proc, tmp)


class TestWriteCapabilities(unittest.TestCase):
    """alias / assign / merge —— 进程内、沙盒 DB。"""

    def setUp(self):
        import ledger
        self.ledger = ledger
        self.tmp = _safety.sandbox_dir(prefix='ul-v1write-')
        self._old = (ledger.DB_PATH, ledger.BOARD_PATH)
        ledger.DB_PATH = os.path.join(self.tmp, 'usage.db')
        ledger.BOARD_PATH = os.path.join(self.tmp, 'usage-board.json')
        rows = [('zcode', 'sess-%d' % (i % 2), 'ev-%d' % i, 1700000000000 + i * 1000,
                 'model-x', 100 + i, 10 + i, 't0', 't1', None) for i in range(6)]
        _seed_db(ledger.DB_PATH, rows)

    def tearDown(self):
        self.ledger.DB_PATH, self.ledger.BOARD_PATH = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _db(self):
        conn = sqlite3.connect(self.ledger.DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def test_board_summary_has_totals(self):
        b = self.ledger.build_board()
        self.assertEqual(b['summary']['projects_total'], 0)
        self.assertGreater(b['summary']['sessions_total'], 0)

    def test_alias_creates_manual_project(self):
        r = self.ledger.project_alias_add('测试项目甲')
        self.assertTrue(r['project_key'].startswith('m'))
        conn = self._db()
        try:
            row = conn.execute(
                'SELECT project_kind, display_name FROM project_registry'
                ' WHERE project_key=?', (r['project_key'],)).fetchone()
            self.assertEqual(row[0], 'manual')
            self.assertEqual(row[1], '测试项目甲')
        finally:
            conn.close()

    def test_assign_moves_sessions_and_events(self):
        key = self.ledger.project_alias_add('测试项目乙')['project_key']
        out = self.ledger.project_assign_sessions(key, [
            {'source': 'zcode', 'session_id': 'sess-0'},
            {'source': 'zcode', 'session_id': 'sess-1'}])
        self.assertEqual(out['events_updated'], 6)
        self.assertEqual(out['sessions_updated'], 2)
        with open(self.ledger.BOARD_PATH, encoding='utf-8') as f:
            board = json.load(f)
        keys = [p['project_key'] for p in board['by_project']]
        self.assertIn(key, keys)
        self.assertEqual(board['summary']['projects_total'], 1)

    def test_merge_moves_everything_and_leaves_trace(self):
        ka = self.ledger.project_alias_add('项目甲')['project_key']
        kb = self.ledger.project_alias_add('项目乙')['project_key']
        self.ledger.project_assign_sessions(ka, [{'source': 'zcode', 'session_id': 'sess-0'}])
        r = self.ledger.project_merge(ka, kb)
        self.assertGreater(r['events_moved'], 0)
        conn = self._db()
        try:
            meta_from = json.loads(conn.execute(
                'SELECT metadata_json FROM project_registry WHERE project_key=?',
                (ka,)).fetchone()[0])
            meta_to = json.loads(conn.execute(
                'SELECT metadata_json FROM project_registry WHERE project_key=?',
                (kb,)).fetchone()[0])
        finally:
            conn.close()
        self.assertEqual(meta_from['merged_into'], kb)
        self.assertIn('项目甲', meta_to['aliases'])
        with self.assertRaises(ValueError):
            self.ledger.project_merge(kb, kb)
        with self.assertRaises(ValueError):
            self.ledger.project_merge('m-not-exist', kb)


class TestScanLockWriteGuard(unittest.TestCase):
    """扫描锁持有期间，写入 API 必须 409（互斥）。"""

    def test_locked_write_returns_409(self):
        tmp = _sandbox_serve('ul-v1lock-')
        with open(os.path.join(tmp, 'usage-ledger-scan.lock'), 'w', encoding='utf-8') as f:
            json.dump({'pid': os.getpid(), 'started_at': 'now', 'trigger': 'test'}, f)
        proc, base = _start_serve(tmp)
        try:
            self.assertEqual(http_code(base + '/api/v1/projects/alias',
                                       {'alias': '测试'}), 409)
            self.assertEqual(http_code(base + '/api/v1/projects/merge',
                                       {'from': 'a', 'to': 'b'}), 409)
        finally:
            _stop_serve(proc, tmp)


if __name__ == '__main__':
    unittest.main()
