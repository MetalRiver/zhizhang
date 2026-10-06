#!/usr/bin/env python3
"""test_replay_v1.py — Round 4 生产迁移能力测试（全部合成 fixture，沙盒隔离）。

覆盖：
    1  migration v0 → v1（旧库自动迁移，行与字段完好）
    2  migration 重跑幂等
    3  新库直接以 v1 落盘
    4  report 默认 RAW（无 --view 时行为与历史一致）
    5  report --view effective 剔除重放
    6  board dedup 块（additive；顶层仍 RAW）
    7  断链父保守保留（is_replay=0，仍在 effective 内）
    8  ingest 阶段 fork 重放自动标记
    9  ingest 阶段 subagent / own 事件保持 0
    10 replay-mark 幂等（第二遍 0 行更新）
    11 备份恢复演练（沙盒复制件 integrity_check + 哈希一致）

禁止使用真实 dsh 会话与真实 usage.db。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable


def run(args, cwd):
    p = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def msg(seq, time, i, o, cr, model='model-x'):
    return {'type': 'assistant/message', 'seq': seq, 'time': time,
            'data': {'usage': {'inputTokens': i, 'outputTokens': o,
                               'cacheReadTokens': cr},
                     'message': {'source': {'model': model}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v1-')
        for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        self.sources_json()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sources_json(self, dsh=None):
        # 显式覆盖全部数据源：绝不触碰真实机器
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        spec = {'zcode': empty + '/none.sqlite', 'workbuddy': empty,
                'catpaw': empty, 'traecn': empty, 'codex': empty}
        if dsh:
            spec['dsh'] = dsh.replace('\\', '/')
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump(spec, f)

    def write_dsh_session(self, sid, events, parent=None, seed=None, depth=0):
        d = os.path.join(self.tmp, 'dsh-home', 'sessions', 'proj', sid)
        os.makedirs(d, exist_ok=True)
        header = {'type': 'session', 'id': sid, 'createdAt': 1000,
                  'delegationDepth': depth, 'version': 0}
        if parent is not None:
            header['parentSession'] = parent
        if seed is not None:
            header['seedLength'] = seed
        lines = [json.dumps(header)] + [json.dumps(e) for e in events]
        with open(os.path.join(d, 'session.jsonl'), 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        # 夹具落位后，显式把 dsh 指到沙盒（显式配置优先，绝不触碰真实机器）
        self.sources_json(dsh=os.path.join(self.tmp, 'dsh-home'))

    def db(self):
        c = sqlite3.connect(os.path.join(self.tmp, 'usage.db'))
        c.row_factory = sqlite3.Row
        return c

    def scan(self):
        code, out = run(['ledger.py', 'scan'], self.tmp)
        self.assertEqual(code, 0, out)
        return out


def make_v0_db(path, rows):
    """手工构造一份 v0（无重放列）账本。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE usage_event (
        client TEXT NOT NULL, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
        ts_ms INTEGER NOT NULL, model TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '', input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        source_file TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL, PRIMARY KEY (client, session_id, event_id));
    CREATE TABLE source_file (
        client TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
        mtime REAL NOT NULL DEFAULT 0, events INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, missing_since TEXT,
        PRIMARY KEY (client, path));
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
    """)
    for r in rows:
        conn.execute("INSERT INTO usage_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()


class TestMigration(Base):
    def test_v0_migrates_to_current_intact(self):
        # 1) 旧库自动迁移：列补齐、user_version=2（schema v2 为 Phase 0 起的
        #    正式事实，v1→v2 兼容由 project_registry/project_key 断言覆盖）、原行原值完好
        rows = [('zcode', 's1', 'seq1', 1000, 'm', 'p', 10, 2, 0, 30, 0, 'f', 't0', 't1'),
                ('dsh', 's2', 'seq1', 2000, 'm', 'p', 5, 1, 0, 9, 0, 'f', 't0', 't1')]
        make_v0_db(os.path.join(self.tmp, 'usage.db'), rows)
        code, out = run(['ledger.py', 'status'], self.tmp)
        self.assertEqual(code, 0, out)
        c = self.db()
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        cols = {r[1] for r in c.execute('PRAGMA table_info(usage_event)')}
        self.assertTrue({'is_replay', 'replay_reason', 'replay_source_session'} <= cols)
        # v1 → v2 additive 迁移：project_key 列 + 注册表就位
        self.assertTrue({'project_key'} <= cols)
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'project_registry', 'session_registry'} <= tables)
        r = c.execute("SELECT * FROM usage_event WHERE client='zcode'").fetchone()
        self.assertEqual((r['input_tokens'], r['output_tokens'], r['is_replay']),
                         (10, 2, 0))
        c.close()

    def test_migration_rerun_idempotent(self):
        # 2) 迁移重跑：版本不再变化、列/表不重复
        make_v0_db(os.path.join(self.tmp, 'usage.db'), [])
        run(['ledger.py', 'status'], self.tmp)
        run(['ledger.py', 'status'], self.tmp)
        c = self.db()
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        n = c.execute('PRAGMA table_info(usage_event)').fetchall()
        self.assertEqual(len([r for r in n if r[1] == 'is_replay']), 1)
        self.assertEqual(len([r for r in n if r[1] == 'project_key']), 1)
        c.close()

    def test_fresh_scan_starts_current(self):
        # 3) 新库直接以当前版本（v2）创建
        self.write_dsh_session('session-init', [msg(1, 100, 10, 2, 30)])
        self.scan()
        c = self.db()
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        c.close()


class TestDualView(Base):
    def setUp(self):
        super().setUp()
        # 父 2 条 + 子 fork（种子区 2 条重放 + 1 条自有）
        self.write_dsh_session('session-p', [msg(1, 100, 10, 2, 30),
                                             msg(2, 110, 20, 4, 60)])
        self.write_dsh_session('session-c',
                               [msg(1, 100, 10, 2, 30), msg(2, 110, 20, 4, 60),
                                msg(9, 300, 7, 1, 0)],
                               parent='session-p', seed=5)
        self.scan()

    def counts(self, view):
        c = self.db()
        where = 'WHERE is_replay = 0' if view == 'effective' else ''
        r = c.execute('SELECT COUNT(*) c, SUM(input_tokens) i, '
                      'SUM(output_tokens) o FROM usage_event ' + where).fetchone()
        c.close()
        return (r['c'], r['i'], r['o'])

    def test_report_default_effective(self):
        # 4) Round 5 起默认口径 = EFFECTIVE：5 行中剔除 2 条重放
        code, out = run(['ledger.py', 'report', '--by', 'client'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('已剔除', out)
        self.assertEqual(self.counts('effective'), (3, 10 + 20 + 7, 2 + 4 + 1))

    def test_report_view_raw_explicit(self):
        # 4b) 显式 raw 审计口径：完整账本
        code, out = run(['ledger.py', 'report', '--by', 'client', '--view', 'raw'],
                        self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('完整账本', out)
        self.assertEqual(self.counts('raw'), (5, 10 + 20 + 7 + 10 + 20,
                                              2 + 4 + 1 + 2 + 4))

    def test_report_view_effective(self):
        # 5) effective：剔除子会话种子区 2 条重放
        code, out = run(['ledger.py', 'report', '--by', 'client', '--view',
                         'effective'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('已剔除', out)
        self.assertEqual(self.counts('effective'), (3, 10 + 20 + 7, 2 + 4 + 1))

    def test_board_dedup_block(self):
        # 6) board：顶层 = EFFECTIVE；dedup 审计块自洽
        code, out = run(['autopilot.py', 'auto', '--no-pricing'], self.tmp)
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.tmp, 'usage-board.json'), encoding='utf-8') as f:
            b = json.load(f)
        self.assertEqual(b['summary']['usage_events'], 3)       # 顶层已切 EFFECTIVE
        d = b['dedup']
        self.assertEqual(d['default_view'], 'effective')
        self.assertEqual(d['method'], 'lineage_seed_v1')
        self.assertEqual(d['raw_events'], 5)
        self.assertEqual(d['replay_events'], 2)
        self.assertEqual(d['effective_events'], 3)
        self.assertEqual(d['raw_events'], d['replay_events'] + d['effective_events'])
        self.assertEqual(d['raw_summary']['usage_events'], 5)
        byc = {x['client']: x for x in d['by_client']}
        self.assertEqual(byc['dsh']['replay'], 2)
        self.assertEqual(byc['dsh']['effective'], 3)

    def test_ingest_marks_replay(self):
        # 8) ingest 即标记：种子区 parent_seed_exact + 源会话；自有行为 0
        c = self.db()
        rows = {r['event_id']: r for r in c.execute(
            "SELECT * FROM usage_event WHERE session_id='session-c'")}
        self.assertEqual(rows['seq1']['is_replay'], 1)
        self.assertEqual(rows['seq1']['replay_reason'], 'parent_seed_exact')
        self.assertEqual(rows['seq1']['replay_source_session'], 'session-p')
        self.assertEqual(rows['seq2']['is_replay'], 1)
        self.assertEqual(rows['seq9']['is_replay'], 0)
        self.assertIsNone(rows['seq9']['replay_reason'])
        # 父会话全部 0
        p = c.execute("SELECT COUNT(*) c FROM usage_event WHERE session_id='session-p' "
                      'AND is_replay<>0').fetchone()['c']
        self.assertEqual(p, 0)
        c.close()

    def test_replay_mark_idempotent(self):
        # 10) replay-mark 重跑：第二遍 0 行更新
        code, out = run(['ledger.py', 'replay-mark'], self.tmp)
        self.assertEqual(code, 0, out)
        self.assertIn('账本更新行数 0', out)


class TestBrokenParentKept(Base):
    def test_broken_parent_remains_effective(self):
        # 7) 断链：父文件缺失 → 保守保留 is_replay=0，仍在 effective 内
        self.write_dsh_session('session-orphan', [msg(1, 100, 10, 2, 30)],
                               parent='session-vanished', seed=5)
        self.scan()
        c = self.db()
        r = c.execute("SELECT is_replay, replay_reason FROM usage_event "
                      "WHERE session_id='session-orphan'").fetchone()
        self.assertEqual(r['is_replay'], 0)
        self.assertIsNone(r['replay_reason'])
        self.assertEqual(c.execute('SELECT COUNT(*) c FROM usage_event '
                                   'WHERE is_replay = 0').fetchone()['c'], 1)
        c.close()


class TestSubagentZero(Base):
    def test_subagent_and_own_stay_zero(self):
        # 9) subagent（有父无 seed）保持 0；own 事件即使与父某事件指纹完全相同
        #    （同毫秒/同模型/同 token，但 seq 不同）也必须保持 0 —— 不得按指纹误删
        self.write_dsh_session('session-root', [msg(1, 100, 10, 2, 30)])
        self.write_dsh_session('session-sub', [msg(1, 500, 3, 3, 3)],
                               parent='session-root', depth=1)
        # owner：种子区 seq1 是真重放；seq50 是自有调用，指纹却与 root seq1 完全一致
        self.write_dsh_session('session-owner',
                               [msg(1, 100, 10, 2, 30), msg(50, 100, 10, 2, 30)],
                               parent='session-root', seed=3)
        self.scan()
        c = self.db()
        sub = c.execute("SELECT is_replay FROM usage_event WHERE session_id='session-sub'"
                        ).fetchall()
        self.assertEqual({r['is_replay'] for r in sub}, {0})
        own = c.execute("SELECT is_replay FROM usage_event "
                        "WHERE session_id='session-owner' AND event_id='seq50'").fetchone()
        self.assertEqual(own['is_replay'], 0)
        seed = c.execute("SELECT is_replay FROM usage_event "
                         "WHERE session_id='session-owner' AND event_id='seq1'").fetchone()
        self.assertEqual(seed['is_replay'], 1)
        c.close()


class TestBackupRestoreDrill(Base):
    def test_restore_copy_integrity(self):
        # 11) 备份→恢复演练：字节一致 + integrity_check + 计数一致
        self.write_dsh_session('session-bak', [msg(1, 100, 10, 2, 30)])
        self.scan()
        src = os.path.join(self.tmp, 'usage.db')
        bak = os.path.join(self.tmp, 'usage.db.bak-v0')
        shutil.copy2(src, bak)
        self.assertEqual(hashlib.sha256(open(src, 'rb').read()).hexdigest(),
                         hashlib.sha256(open(bak, 'rb').read()).hexdigest())
        restored = os.path.join(self.tmp, 'restored.db')
        shutil.copy2(bak, restored)
        c = sqlite3.connect(restored)
        self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.assertEqual(c.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertGreater(c.execute('SELECT COUNT(*) FROM usage_event').fetchone()[0], 0)
        c.close()


if __name__ == '__main__':
    unittest.main()
