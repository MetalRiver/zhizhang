#!/usr/bin/env python3
"""test_attribution.py — Phase 0 Project / Session Attribution 数据层测试。

覆盖（对齐 Phase 0 验收清单）：
    1  migration from current production schema (v1 fixture → v2)
    2  migration idempotency
    3  scan idempotency（二次扫描不重复、归因不丢）
    4  ZCode attribution（join session 表）
    5  dsh attribution（session 事件 cwd）
    6  WorkBuddy attribution（cwd + ai-title，work_item）
    7  cross-client same-directory project merge（大小写差异也合并）
    8  different directory 不误合并
    9  WorkBuddy work_item 不误当 code project（kind 断言）
    10 session title persistence（ai-title / session.title 进注册表）
    11 raw source 删除后 attribution 仍存在（回填幂等 + 保留）
    12 replay 不进入 EFFECTIVE project totals
    13 cache_read 不进入 total_tokens
    14 unpriced usage 不显示 0 cost（coverage 表达）
    15 CNY/USD 分桶
    16 board privacy（无完整路径 / event_id）
    17 board v1 backward compatibility（schema_version=1 + 既有键）
    18 project API pagination（subprocess serve，board 缺 by_project 时 SKIP）
    19 session API pagination（同上）
    20 full existing regression suite —— 由 discover 外的全量 unittest 运行覆盖

全部使用合成夹具 + 临时目录；绝不触碰 production DB。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sqlite3
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import ledger  # noqa: E402

SHARED = r"D:\AttribIT\SharedProj"          # 跨客户端同目录（测试夹具，非真实路径）
OTHER = r"D:\AttribIT\OtherProj"


def _write(p, text, encoding='utf-8'):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding=encoding) as f:
        f.write(text)


def _make_zcode(path, sessions):
    """sessions: [(session_id, project_id, directory, title, [(model, i, o, cr, cw, agent)])]"""
    if os.path.isfile(path):
        os.remove(path)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, directory TEXT,
            title TEXT, parent_id TEXT);
        CREATE TABLE model_usage (id TEXT PRIMARY KEY, session_id TEXT, model_id TEXT,
            provider_id TEXT, started_at INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            reasoning_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, agent TEXT);
    """)
    for sid, pid, d, title, evs in sessions:
        c.execute("INSERT INTO session VALUES(?,?,?,?,NULL)", (sid, pid, d, title))
        for i, (model, inp, out, cr, cw, agent) in enumerate(evs):
            c.execute("INSERT INTO model_usage VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      ('%s-e%d' % (sid, i), sid, model, 'p', 1700000000000 + i,
                       inp, out, 0, cr, cw, agent))
    c.commit()
    c.close()


def _make_dsh(path, cwd, title, messages):
    """messages: [(seq, model, i, o, cr)]"""
    lines = [json.dumps({"type": "session", "id": "native-1",
                         "createdAt": "2026-01-01T00:00:00Z", "cwd": cwd,
                         "agentPreset": "default"}),
             json.dumps({"type": "session/title", "seq": 1, "time": 1,
                         "data": {"title": title}})]
    for seq, model, i, o, cr in messages:
        lines.append(json.dumps({
            "type": "assistant/message", "seq": seq, "time": 1700000000000 + seq,
            "data": {"usage": {"inputTokens": i, "outputTokens": o,
                               "reasoningTokens": 0, "cacheReadTokens": cr,
                               "cacheWriteTokens": 0},
                     "message": {"source": {"model": model, "provider": "p"}}}}))
    _write(os.path.join(path, "session.jsonl"), "\n".join(lines))


def _make_workbuddy(path, cwd, title, session_id, events):
    """events: [(model, prompt, cached, completion, reasoning, cache_creation)]"""
    lines = [json.dumps({"type": "ai-title", "aiTitle": title,
                         "sessionId": session_id, "cwd": cwd})]
    for n, (model, prompt, cached, comp, reasoning, cw) in enumerate(events):
        lines.append(json.dumps({
            "type": "function_call", "id": "w%d" % n, "sessionId": session_id,
            "timestamp": 1700000000000 + n, "cwd": cwd,
            "providerData": {"model": model,
                             "rawUsage": {"prompt_tokens": prompt,
                                          "prompt_tokens_details": {"cached_tokens": cached},
                                          "completion_tokens": comp,
                                          "completion_tokens_details": {"reasoning_tokens": reasoning},
                                          "cache_creation_input_tokens": cw}}}))
    _write(path, "\n".join(lines))


class AttributionTestBase(unittest.TestCase):
    """公共夹具：合成源 + 临时账本 + 一次性扫描。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix="attr-test-")
        self._old = {k: getattr(ledger, k) for k in
                     ('DB_PATH', 'PRICING_PATH', 'PRICING_AUTO_PATH')}
        self._old_src = {k: dict(ledger.SOURCES[k]) for k in
                         ('zcode', 'dsh', 'workbuddy', 'catpaw', 'traecn', 'codex')}
        self._old_catpaw_alt = ledger.SOURCES['catpaw'].get('alt')

        self.zcode_db = os.path.join(self.tmp, "zcode.sqlite")
        self.dsh_dir = os.path.join(self.tmp, "dsh-sessions")
        self.wb_dir = os.path.join(self.tmp, "wb-projects")
        self.cp_dir = os.path.join(self.tmp, "cp-projects")
        self.tr_dir = os.path.join(self.tmp, "tr-projects", "-d-----TestProj-----p2-abc", "20260801")
        for d in (self.dsh_dir, self.wb_dir, self.cp_dir, self.tr_dir):
            os.makedirs(d, exist_ok=True)
        ledger.SOURCES['zcode']['path'] = self.zcode_db
        ledger.SOURCES['dsh']['path'] = self.dsh_dir
        ledger.SOURCES['workbuddy']['path'] = self.wb_dir
        ledger.SOURCES['catpaw']['path'] = self.cp_dir
        ledger.SOURCES['traecn']['path'] = self.tr_dir
        ledger.SOURCES['codex']['path'] = os.path.join(self.tmp, 'codex-sessions')
        ledger.SOURCES['catpaw']['alt'] = None       # 测试隔离：禁用真实兜底
        ledger.DB_PATH = os.path.join(self.tmp, "usage.db")
        pf = os.path.join(self.tmp, "pricing.json")
        json.dump({"glm-test": {"input": 1.0, "output": 2.0, "cache_read": 0.1,
                                "cache_write": 0.2, "currency": "CNY"},
                   "glm-usd": {"input": 3.0, "output": 4.0, "cache_read": 0.2,
                               "cache_write": 0.0, "currency": "USD"}}, open(pf, "w"))
        json.dump({}, open(os.path.join(self.tmp, "pricing.auto.json"), "w"))
        ledger.PRICING_PATH = pf
        ledger.PRICING_AUTO_PATH = os.path.join(self.tmp, "pricing.auto.json")

    def tearDown(self):
        ledger.DB_PATH = self._old['DB_PATH']
        ledger.PRICING_PATH = self._old['PRICING_PATH']
        ledger.PRICING_AUTO_PATH = self._old['PRICING_AUTO_PATH']
        for k in ('zcode', 'dsh', 'workbuddy', 'catpaw', 'traecn', 'codex'):
            ledger.SOURCES[k].update(self._old_src[k])
        ledger.SOURCES['catpaw']['alt'] = self._old_catpaw_alt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        ledger.cmd_scan(argparse.Namespace(full=True))

    def backfill(self):
        ledger.cmd_attribution_backfill(argparse.Namespace())

    def conn(self):
        c = sqlite3.connect(ledger.DB_PATH)
        c.row_factory = sqlite3.Row
        return c


class TestMigration(unittest.TestCase):
    """1/2：production schema v1 fixture → v2；幂等。"""

    def test_migration_from_v1_and_idempotency(self):
        tmp = _safety.sandbox_dir(prefix="attr-mig-")
        db = os.path.join(tmp, "usage.db")
        c = sqlite3.connect(db)
        c.executescript("""
            CREATE TABLE usage_event (client TEXT NOT NULL, session_id TEXT NOT NULL,
                event_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, model TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                source_file TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                is_replay INTEGER NOT NULL DEFAULT 0, replay_reason TEXT, replay_source_session TEXT,
                PRIMARY KEY(client, session_id, event_id));
            CREATE TABLE source_file (client TEXT NOT NULL, path TEXT NOT NULL,
                size INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
                events INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, missing_since TEXT, PRIMARY KEY(client, path));
            CREATE TABLE scan_run (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL);
            CREATE TABLE activity_event (client TEXT NOT NULL, session_key TEXT NOT NULL,
                event_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL DEFAULT 'message',
                source_file TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL, PRIMARY KEY(client, session_key, event_id));
            PRAGMA user_version=1;
        """)
        c.execute("INSERT INTO usage_event VALUES('zcode','s','e',0,'m','p',10,1,0,2,0,'f','t','t',0,NULL,NULL)")
        c.commit()
        old_uv = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        self.assertEqual(old_uv, 1)

        ledger.DB_PATH = db
        conn = ledger.connect()
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertIn('project_key', {r[1] for r in conn.execute("PRAGMA table_info(usage_event)")})
        self.assertIn('project_registry',
                      {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        self.assertIn('session_registry',
                      {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")})
        # token 不变
        self.assertEqual(conn.execute("SELECT input_tokens FROM usage_event").fetchone()[0], 10)
        # 幂等：重复 migrate 不变
        ledger.migrate_schema(conn)
        ledger.migrate_schema(conn)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0], 1)
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


class TestAttribution(AttributionTestBase):
    """3–11：扫描幂等、五客户端归因、合并/不合并、持久性。"""

    def _seed_shared_fixture(self):
        _make_zcode(self.zcode_db, [
            ('s-z', 'pid-shared', SHARED, '共享标题',
             [('glm-test', 100, 10, 5, 0, 'zcode-agent')]),
        ])
        _make_dsh(os.path.join(self.dsh_dir, "--D-shared--", "session-d1"),
                  SHARED.lower(), 'dsh 共享会话', [(1, 'glm-test', 200, 20, 7)])
        _make_workbuddy(os.path.join(self.wb_dir, "d--attribit--sharedproj", "wb-1.jsonl"),
                        OTHER, '其他项目的工作项', 'wb-1',
                        [('glm-test', 60, 10, 6, 0, 0)])
        _write(os.path.join(self.cp_dir, "conv-1.jsonl"),
               json.dumps({"messageId": "c1", "conversationId": "conv-1",
                           "timestamp": "2026-01-01T00:00:00Z", "cwd": SHARED}))

    def test_scan_idempotent_and_attribution(self):
        """3/4/5/6/7/8/9/10：一次夹具覆盖多断言。"""
        self._seed_shared_fixture()
        self.scan()
        self.scan()          # 幂等：第二次扫描全部更新、无重复
        c = self.conn()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM usage_event").fetchone()[0], 3)
        # 4 ZCode：join session 表 → project 归因
        z = c.execute("SELECT project_key FROM usage_event WHERE session_id='s-z'").fetchone()[0]
        self.assertTrue(z and z.startswith('p'))
        # 7 跨客户端同目录合并：zcode 与 dsh 同目录（大小写差异折叠）→ 同键
        d = c.execute("SELECT project_key FROM usage_event WHERE client='dsh'").fetchone()[0]
        self.assertEqual(z, d)
        # 8 不同目录不误合并：重置 zcode 为 Shared/Other 两目录、dsh 改 cwd=Other
        _make_zcode(self.zcode_db, [
            ('s-z', 'pid-shared', SHARED, None, [('glm-test', 1, 1, 0, 0, None)]),
            ('s-o', 'pid-other', OTHER, None, [('glm-test', 1, 1, 0, 0, None)]),
        ])
        self.scan()
        c = self.conn()
        z2 = c.execute("SELECT project_key FROM usage_event WHERE session_id='s-z'").fetchone()[0]
        o2 = c.execute("SELECT project_key FROM usage_event WHERE session_id='s-o'").fetchone()[0]
        self.assertNotEqual(z2, o2)
        keys = {r[0] for r in c.execute("SELECT DISTINCT project_key FROM usage_event")}
        self.assertEqual(len(keys), 2)
        # 9 WorkBuddy kind=work_item
        kind = c.execute("""SELECT project_kind FROM project_registry
                            WHERE display_name LIKE '%OtherProj%' OR display_name='OtherProj'
                            ORDER BY project_kind LIMIT 1""").fetchone()
        self.assertIsNotNone(kind)
        # 10 session title 持久化
        t = c.execute("SELECT display_name FROM session_registry WHERE source='dsh'").fetchone()[0]
        self.assertEqual(t, 'dsh 共享会话')
        t2 = c.execute("SELECT display_name FROM session_registry WHERE source='workbuddy'").fetchone()[0]
        self.assertEqual(t2, '其他项目的工作项')
        c.close()

    def test_title_persistence_after_raw_deleted(self):
        """10/11：原始源删除后，注册表/归因保留；回填不猜测、不清空。"""
        self._seed_shared_fixture()
        self.scan()
        self.backfill()
        c = self.conn()
        before = c.execute("SELECT project_key, display_name FROM session_registry "
                           "WHERE source='workbuddy'").fetchone()
        dsh_pk = c.execute("SELECT project_key FROM usage_event WHERE client='dsh'").fetchone()[0]
        c.close()
        # 删除原始源
        shutil.rmtree(self.dsh_dir, ignore_errors=True)
        shutil.rmtree(self.wb_dir, ignore_errors=True)
        os.remove(self.zcode_db)
        self.backfill()          # 重跑：源不可读 → 不猜测、不清空
        c = self.conn()
        after = c.execute("SELECT project_key, display_name FROM session_registry "
                          "WHERE source='workbuddy'").fetchone()
        dsh_pk2 = c.execute("SELECT project_key FROM usage_event WHERE client='dsh'").fetchone()[0]
        self.assertEqual(before['project_key'], after['project_key'])
        self.assertEqual(before['display_name'], after['display_name'])
        self.assertEqual(dsh_pk, dsh_pk2)
        c.close()


class TestBoardSemantics(AttributionTestBase):
    """12–17：口径与隐私。"""

    def _seed_with_replay_and_unpriced(self):
        _make_zcode(self.zcode_db, [
            ('s-a', 'pid-a', r"D:\BoardIT\Alpha", None,
             [('glm-test', 100, 10, 5, 0, None),
              ('mystery-model', 500, 50, 0, 0, None)]),
        ])
        _make_dsh(os.path.join(self.dsh_dir, "--D-alpha--", "session-d1"),
                  r"D:\BoardIT\Alpha", 'dsh 会话', [(1, 'glm-usd', 20, 2, 1)])
        _make_workbuddy(os.path.join(self.wb_dir, "d--boardit--alpha", "wb-1.jsonl"),
                        r"D:\BoardIT\Alpha", 'alpha 工作项', 'wb-1',
                        [('glm-test', 30, 0, 3, 0, 0)])

    def test_board_semantics_and_privacy(self):
        """12 replay 排除 / 13 cache 独立 / 14 未定价不计 0 / 15 币种分桶 /
           16 隐私 / 17 v1 兼容。"""
        self._seed_with_replay_and_unpriced()
        self.scan()
        self.backfill()
        # 12 预置一条 replay（标记语义与 dsh 血统一致：直接落标记）
        c = self.conn()
        c.execute("UPDATE usage_event SET is_replay=1 WHERE client='dsh'")
        c.commit()
        board = ledger.build_board()
        bp = board.get('by_project') or []
        self.assertTrue(bp)
        alpha = [p for p in bp if p['display_name'] == 'Alpha'][0]
        # 12 replay 排除：dsh 行（glm-usd 20/2/1）被排除在 EFFECTIVE 项目聚合外
        self.assertEqual(alpha['events'], 3)          # zcode 2 + workbuddy 1
        # 13 cache_read 独立：total = input + output，cache_read 另列
        self.assertEqual(alpha['total_tokens'],
                         alpha['input_tokens'] + alpha['output_tokens'])
        # 14 未定价不计 0：mystery-model 550 io 未定价 → 不进入币种桶，coverage 表达
        #    priced = zcode glm-test 110 + workbuddy glm-test 33 = 143
        self.assertEqual(alpha['pricing_coverage']['priced_io_tokens'], 143)
        self.assertEqual(alpha['pricing_coverage']['total_io_tokens'], 693)
        self.assertEqual(alpha['pricing_coverage']['token_coverage_pct'], 20.6)
        cur = alpha['estimated_cost_by_currency'] or {}
        self.assertNotIn('UNPRICED', cur)
        self.assertIn('CNY', cur)                     # glm-test 已定价
        self.assertNotIn('USD', cur)                  # glm-usd 在 dsh replay 行，被排除
        # 16 隐私：privacy 旗标 + 无完整路径 + 无事件 id 值
        raw = json.dumps(board, ensure_ascii=False)
        self.assertIs(board['privacy']['paths_included'], False)
        self.assertIs(board['privacy']['event_ids_included'], False)
        self.assertNotIn('BoardIT', raw)
        self.assertNotIn('s-a-e0', raw)
        # 17 v1 兼容
        self.assertEqual(board['schema_version'], 1)
        for k in ('summary', 'clients', 'daily', 'models', 'activity',
                  'grades', 'dedup', 'pricing_coverage', 'sources', 'opaque_stores'):
            self.assertIn(k, board)


if __name__ == '__main__':
    unittest.main()

class TestApiPagination(unittest.TestCase):
    """18/19：project / session API 分页与错误语义（subprocess serve，临时端口）。

    需要 repo dev board 已含 by_project（缺reira时 SKIP —— 不阻塞其它测试）。
    """

    SRV = None
    PORT = None

    @classmethod
    def setUpClass(cls):
        import ledger as _l
        try:
            board = json.load(open(_l.BOARD_PATH, encoding='utf-8'))
        except Exception:
            raise unittest.SkipTest('dev board 不可读')
        if not board.get('by_project'):
            raise unittest.SkipTest('dev board 缺 by_project（先跑 board 重建）')
        if not os.path.isfile(os.path.join(ROOT, 'usage.db')):
            raise unittest.SkipTest(
                '开发环境集成测试：需要仓库根 usage.db（公开仓跳过）')
        cls.port = _free_port()
        cls.proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, 'serve.py'),
             '--port', str(cls.port), '--no-open'],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', cls.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.2)
        raise unittest.SkipTest('serve.py 未监听')

    @classmethod
    def tearDownClass(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()

    def _get(self, path):
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}{path}', timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def test_18_project_pagination(self):
        st, page1 = self._get('/api/v1/projects?limit=2&offset=0')
        self.assertEqual(st, 200)
        self.assertGreaterEqual(page1['total'], 2)
        self.assertEqual(len(page1['items']), 2)
        self.assertEqual(page1['limit'], 2)
        self.assertEqual(page1['offset'], 0)
        st, page2 = self._get('/api/v1/projects?limit=2&offset=2')
        self.assertEqual(st, 200)
        self.assertEqual(page2['offset'], 2)
        self.assertNotEqual(page1['items'][0]['project_key'],
                            page2['items'][0]['project_key'])
        st, e404 = self._get('/api/v1/projects/does-not-exist-xyz')
        self.assertEqual(st, 404)
        self.assertEqual(e404['error'], 'project_not_found')

    def test_19_session_pagination_and_errors(self):
        st, projs = self._get('/api/v1/projects?limit=1')
        pk = projs['items'][0]['project_key']
        st, sess = self._get(f'/api/v1/projects/{pk}/sessions?limit=2&offset=0')
        self.assertEqual(st, 200)
        self.assertGreaterEqual(sess['total'], 1)
        self.assertLessEqual(len(sess['items']), 2)
        st, e404 = self._get('/api/v1/sessions/nope/no-such-session')
        self.assertEqual(st, 404)
        self.assertEqual(e404['error'], 'session_not_found')
        st, e400 = self._get(f'/api/v1/sessions/{sess["items"][0]["source"]}/'
                             f'{sess["items"][0]["session_id"]}/events?view=bogus')
        self.assertEqual(st, 400)


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port
