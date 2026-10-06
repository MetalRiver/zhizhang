#!/usr/bin/env python3
"""test_recent_activity.py — GET /api/v1/recent-activity 与
Project Session 列表「真实事件时间排序」的测试。

in-process 沙盒：临时 DATA_ROOT + 现场建 v2 schema 账本 + 种子事件，
起真实 HTTP server（临时端口）后只走只读 GET。绝不触碰真实账本。

验证点：
- recent-activity 返回最近事件摘要（ts 倒序、EFFECTIVE 剔除重放、
  tokens = input+output、project/session 用 display_name）；
- 不暴露 full path / event id；
- active_today = 今天有真实使用记录的唯一项目数（ts_ms 口径，
  与 upsert 用的 last_seen 扫描时间戳无关）；
- /api/v1/projects/{pk}/sessions 按 MAX(usage_event.ts_ms) 排序，
  并提供 last_active；
- 账本缺失 → 503（不伪造空数据）。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import threading
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import serve  # noqa: E402
import paths  # noqa: E402
import ledger  # noqa: E402
from test_local_api import free_port, req  # noqa: E402


def _today_ms() -> int:
    today = _dt.date.today()
    start = _dt.datetime.combine(today, _dt.time.min)
    return int(start.timestamp() * 1000)


def _ms_ago(hours: float) -> int:
    ts = int((_dt.datetime.now() - _dt.timedelta(hours=hours)).timestamp() * 1000)
    midnight = _today_ms()
    if ts < midnight:
        # 跨午夜运行：把「今天」种子夹回今天内部，保持相对先后
        # （h 越大越早；夹取后全部落在今天 00:01–00:05，active_today 语义不变）
        ts = midnight + int((6.0 - hours) * 60_000)
    return ts


class RecentActivityBase(unittest.TestCase):
    """沙盒：v2 schema 账本 + 种子数据 + 真实 HTTP server。"""

    def seed(self):
        # 注意：ledger.DB_PATH 是 import 时固化的模块级路径，必须一并
        # 打到沙盒，否则 connect() 会打开真实账本（绝不允许）。
        conn = ledger.connect(create=True)
        try:
            with conn:
                conn.execute(
                    'INSERT INTO project_registry '
                    '(project_key, project_kind, display_name, first_seen_at, last_seen_at) '
                    'VALUES (?,?,?,?,?)',
                    ('pk-alpha', 'project', '项目甲', '2026-09-01T08:00:00',
                     '2026-09-01T08:00:00'))
                conn.execute(
                    'INSERT INTO project_registry '
                    '(project_key, project_kind, display_name, first_seen_at, last_seen_at) '
                    'VALUES (?,?,?,?,?)',
                    ('pk-beta', 'project', '项目乙', '2026-09-02T08:00:00',
                     '2026-09-02T08:00:00'))
                conn.execute(
                    'INSERT INTO session_registry '
                    '(source, session_id, project_key, display_name, agent) '
                    'VALUES (?,?,?,?,?)',
                    ('zcode', 'sess-1', 'pk-alpha', '会话一', 'agentA'))
                conn.execute(
                    'INSERT INTO session_registry '
                    '(source, session_id, project_key, display_name, agent) '
                    'VALUES (?,?,?,?,?)',
                    ('zcode', 'sess-2', 'pk-alpha', '会话二', None))
                conn.execute(
                    'INSERT INTO session_registry '
                    '(source, session_id, project_key, display_name, agent) '
                    'VALUES (?,?,?,?,?)',
                    ('dsh', 'sess-9', 'pk-beta', '会话九', 'agentB'))
                evs = [
                    # (client, session, event, hours_ago, replay, project, model, in, out)
                    ('zcode', 'sess-1', 'e1', 1.0, 0, 'pk-alpha', 'm-1', 100, 10),
                    ('zcode', 'sess-1', 'e2', 2.0, 0, 'pk-alpha', 'm-1', 200, 20),
                    ('zcode', 'sess-2', 'e3', 3.0, 0, 'pk-alpha', 'm-2', 300, 30),
                    ('dsh', 'sess-9', 'e4', 4.0, 0, 'pk-beta', 'm-3', 400, 40),
                    # 重放副本：必须被 EFFECTIVE 过滤
                    ('dsh', 'sess-9', 'e5', 0.5, 1, 'pk-beta', 'm-3', 999, 999),
                    # 无项目归因：计入事件流，不计入 active_today
                    ('zcode', 'sess-x', 'e6', 5.0, 0, None, 'm-4', 10, 1),
                ]
                for client, sid, eid, h, replay, pk, model, i, o in evs:
                    conn.execute(
                        'INSERT INTO usage_event '
                        '(client, session_id, event_id, ts_ms, model, provider, '
                        ' input_tokens, output_tokens, reasoning_tokens, '
                        ' cache_read_tokens, cache_write_tokens, source_file, '
                        ' first_seen, last_seen, is_replay, project_key) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (client, sid, eid, _ms_ago(h), model, 'p',
                         i, o, 0, 0, 0, 'x.jsonl',
                         '2026-09-01T00:00:00', '2026-09-01T00:00:00',
                         replay, pk))
                # 同一项目一条昨天的事件（session sess-2 昨天活跃）
                conn.execute(
                    'INSERT INTO usage_event '
                    '(client, session_id, event_id, ts_ms, model, provider, '
                    ' input_tokens, output_tokens, reasoning_tokens, '
                    ' cache_read_tokens, cache_write_tokens, source_file, '
                    ' first_seen, last_seen, is_replay, project_key) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    ('zcode', 'sess-2', 'e-old', _today_ms() - 86400_000,
                     'm-2', 'p', 5, 5, 0, 0, 0, 'x.jsonl',
                     '2026-09-01T00:00:00', '2026-09-01T00:00:00', 0, 'pk-alpha'))
            conn.commit()
        finally:
            conn.close()

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-recent-')
        self._old_here, self._old_web = serve.HERE, serve.WEB_DIR
        self._old_root = paths.DATA_ROOT
        self._old_dbpath = ledger.DB_PATH
        serve.HERE = self.tmp
        self.web = os.path.join(self.tmp, 'web')
        os.makedirs(self.web, exist_ok=True)
        serve.WEB_DIR = self.web
        paths.DATA_ROOT = self.tmp
        ledger.DB_PATH = os.path.join(self.tmp, 'usage.db')
        self.seed()
        self.httpd = serve.make_server(0)
        self.port = self.httpd.server_address[1]
        self.base = 'http://127.0.0.1:%d' % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        serve.HERE, serve.WEB_DIR = self._old_here, self._old_web
        paths.DATA_ROOT = self._old_root
        ledger.DB_PATH = self._old_dbpath
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRecentActivity(RecentActivityBase):
    def test_items_effective_desc_and_privacy(self):
        st, body, _ = req('GET', self.base + '/api/v1/recent-activity?limit=10')
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d['ok'])
        items = d['items']
        # 重放 e5 被剔除；无归因事件 e6 保留
        eids = set()  # 响应不含 event_id；用 (session, ts) 组合核验
        self.assertEqual(len(items), 6)
        # ts 倒序：最近 1 小时前的事件在最前，且是 EFFECTIVE（e5 重放被剔除）
        self.assertEqual(items[0]['session_id'], 'sess-1')
        self.assertEqual(items[0]['model'], 'm-1')
        self.assertEqual(items[0]['tokens'], 110)
        self.assertEqual(items[0]['project_name'], '项目甲')
        self.assertEqual(items[0]['session_name'], '会话一')
        self.assertEqual(items[0]['agent'], 'agentA')
        # 全量隐私检查：无路径、无 event id 字段
        for it in items:
            self.assertNotIn('event_id', it)
            self.assertNotIn('path', it)
            for v in it.values():
                self.assertFalse(isinstance(v, str) and v.startswith('C:'))

    def test_active_today_counts_distinct_projects_by_event_time(self):
        st, body, _ = req('GET', self.base + '/api/v1/recent-activity')
        self.assertEqual(st, 200)
        d = json.loads(body)
        # 今天有事件的唯一项目：pk-alpha + pk-beta（e-old 在昨天，不计）
        self.assertEqual(d['active_today'], 2)
        self.assertEqual(d['today'], _dt.date.today().isoformat())
        # project_last：每个归因项目最近一次真实活动（含昨天事件的 pk-alpha）
        self.assertIn('pk-alpha', d['project_last'])
        self.assertIn('pk-beta', d['project_last'])
        # 重放副本（0.5 小时前）不得抬高 pk-beta 的最近活动时间：
        # pk-beta 最近 EFFECTIVE 活动是 4 小时前的 e4
        self.assertLess(d['project_last']['pk-beta'],
                        d['project_last']['pk-alpha'])

    def test_sessions_ordered_by_real_event_time(self):
        st, body, _ = req(
            'GET', self.base + '/api/v1/projects/pk-alpha/sessions')
        self.assertEqual(st, 200)
        d = json.loads(body)
        items = d['items']
        self.assertEqual([x['session_id'] for x in items], ['sess-1', 'sess-2'])
        self.assertTrue(items[0]['last_active'])
        # sess-1 最近事件在 1 小时前；sess-2 在 3 小时前
        self.assertGreater(items[0]['last_active'], items[1]['last_active'])

    def test_unknown_project_404(self):
        st, body, _ = req('GET', self.base + '/api/v1/projects/nope/sessions')
        self.assertEqual(st, 404)

    def test_missing_ledger_503(self):
        os.remove(os.path.join(self.tmp, 'usage.db'))
        st, body, _ = req('GET', self.base + '/api/v1/recent-activity')
        self.assertEqual(st, 503)


if __name__ == '__main__':
    unittest.main()
