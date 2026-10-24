# -*- coding: utf-8 -*-
"""V1.1 第二阶段回归：Qoder CN ACTIVITY 适配（发现 + 采集 + 口径 + 共存）。

覆盖任务四清单 10 项：
  1 候选发现成功（严格锚：.qoder-cn/projects；反例：相似名不误判）
  2 user/assistant/system/compact 分类正确
  3 同一事件重复读取不增计数
  4 复制文件、改名、增量追加不导致重复
  5 时间戳混合格式（秒/毫秒/ISO）正常
  6 损坏 JSONL 行与缺失身份处理安全
  7 路径脱敏（discovery.json / 产物不泄露 home 绝对路径）
  8 ACTIVITY 不污染 TOKEN（usage_event 零行、board 口径正确）
  9 与 catpaw / traecn 共存
  10 自动调度器回归（在 test_backend_scheduler，本文件不重复）

隔离原则：_safety 沙盒 + 假 home/appdata + 合成 JSONL，
绝不读取真实 ~/.qoder-cn，绝不写 production 账本。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest

import _safety

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

PY = sys.executable

_RUNNER = r'''
import json, os, sys, sqlite3
sys.path.insert(0, os.getcwd())
mode = os.environ.get('Q_MODE', 'discover')
if mode == 'discover':
    import discovery
    import ledger
    # 与 autopilot.run_once 同链：ledger 发现 → 重定级 → 写 discovery.json
    lrep = ledger.discover_sources(verbose=False)
    ledger.regrade_sources(lrep)
    ledger.write_discovery(lrep)
    rep = discovery.run_discovery(full=True, state_dir=os.getcwd())
    pub = discovery.public_payload(rep)
    ljson = json.load(open('discovery.json', encoding='utf-8'))
    print('@@JSON@@' + json.dumps(
        {'report': rep, 'public': pub, 'ledger_discovery': ljson},
        ensure_ascii=False))
elif mode == 'scan':
    import ledger
    class NS:
        full = False
    code = ledger.cmd_scan(NS())
    db = os.path.join(os.getcwd(), 'usage.db')
    counts = {}
    if os.path.isfile(db):
        conn = sqlite3.connect(db)
        try:
            counts['activity_by_client'] = conn.execute(
                'SELECT client, COUNT(*) FROM activity_event GROUP BY client'
            ).fetchall()
            counts['activity_kinds'] = conn.execute(
                "SELECT kind, COUNT(*) FROM activity_event"
                " WHERE client='qodercn' GROUP BY kind").fetchall()
            counts['activity_sessions'] = conn.execute(
                "SELECT COUNT(DISTINCT session_key) FROM activity_event"
                " WHERE client='qodercn'").fetchall()
            counts['usage_rows_qodercn'] = conn.execute(
                "SELECT COUNT(*) FROM usage_event WHERE client='qodercn'"
            ).fetchone()[0]
            counts['usage_rows_total'] = conn.execute(
                'SELECT COUNT(*) FROM usage_event').fetchone()[0]
            counts['ts_range'] = conn.execute(
                "SELECT MIN(ts_ms), MAX(ts_ms) FROM activity_event"
                " WHERE client='qodercn'").fetchall()
        finally:
            conn.close()
    board = ledger.build_board()          # 只读 SELECT，与 autopilot 产物同构
    out = {'exit': code, 'db': counts,
           'board_activity': board.get('activity'),
           'board_grades': board.get('grades'),
           'board_summary': board.get('summary')}
    print('@@JSON@@' + json.dumps(out, ensure_ascii=False))
'''


def _run(cwd, env, timeout=240):
    r = subprocess.run([PY, '-c', _RUNNER], cwd=cwd, env=env,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout)
    if r.returncode != 0:
        raise AssertionError('runner 失败：%s\n%s' % (r.stderr[-800:], r.stdout[-400:]))
    line = [l for l in r.stdout.splitlines() if l.startswith('@@JSON@@')][-1]
    return json.loads(line[len('@@JSON@@'):])


def _rec(rtype, sid, uid, ts, cwd=None, role=None, compact=False,
         tool_use=False, no_uuid=False):
    o = {'type': rtype, 'sessionId': sid}
    if not no_uuid:
        o['uuid'] = uid
    if ts is not None:
        o['timestamp'] = ts
    if cwd:
        o['cwd'] = cwd
    if compact:
        o['isCompactSummary'] = True
        o['compactMetadata'] = {'trigger': 'manual', 'preTokens': 0}
    msg_content = []
    if tool_use:
        msg_content.append({'type': 'tool_use', 'id': 't-' + uid,
                            'name': 'Bash', 'input': {'command': 'REDACTED'}})
    msg_content.append({'type': 'text', 'text': 'REDACTED-BODY'})
    o['message'] = {'role': role or rtype, 'content': msg_content}
    if rtype == 'assistant':
        o['message']['model'] = 'auto'
    return o


class QoderSandbox(unittest.TestCase):
    """沙盒基座：假 home/appdata + 源码副本。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-qoder-')
        self.home = os.path.join(self.tmp, 'home')
        self.appdata = os.path.join(self.tmp, 'appdata')
        self.appdata_local = os.path.join(self.tmp, 'appdata-local')
        for d in (self.home, self.appdata, self.appdata_local):
            os.makedirs(d)
        for f in ('ledger.py', 'discovery.py', 'paths.py', 'autopilot.py',
                  'serve.py', 'source-catalog.json',
                  'usage-board.schema.json', 'VERSION'):
            src = os.path.join(ROOT, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(self.tmp, f))
        self.env = dict(os.environ)
        self.env.update({
            'USERPROFILE': self.home, 'HOME': self.home,
            'APPDATA': self.appdata, 'LOCALAPPDATA': self.appdata_local,
        })

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- fixture 助手 ----

    def qprojects(self):
        return os.path.join(self.home, '.qoder-cn', 'projects')

    def write_session(self, proj, name, records):
        d = os.path.join(self.qprojects(), proj)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        with open(p, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r) + '\n')
        return p

    def base_fixture(self):
        """一个覆盖全分类 + 混合时间戳 + 坏行 + 缺身份的会话文件。"""
        return [
            _rec('user', 's1', 'u1', 1792800000, cwd='D:/work/demo'),          # 秒
            _rec('assistant', 's1', 'u2', '2026-10-24T12:00:00Z',
                 cwd='D:/work/demo', tool_use=True),                            # ISO + tool_use
            _rec('assistant', 's1', 'u3', 1792800123456),                       # 毫秒
            _rec('system', 's1', 'u4', 1792800005),                             # 排除：system
            _rec('user', 's1', 'u5', 1792800006, compact=True),                 # 排除：压缩摘要
            _rec('user', 's1', 'u6', 1792800007, no_uuid=True),                 # 排除：缺 uuid
            _rec('assistant', 's1', 'u7', 'bad-date'),                          # 排除：坏时间
            _rec('active-leaf', 's1', 'u8', 1792800008),                        # 排除：非消息
            _rec('runtime-config', 's1', 'u9', 1792800009),                     # 排除：非消息
        ] + ['CORRUPT { LINE']                                                  # 坏行

    def discover(self):
        return _run(self.tmp, dict(self.env, Q_MODE='discover'))

    def scan(self):
        return _run(self.tmp, dict(self.env, Q_MODE='scan'))


class DiscoveryStrictness(QoderSandbox):

    def test_qoder_discovered_via_strict_anchor(self):
        self.write_session('proj1', 'a.jsonl',
                           [_rec('user', 's1', 'u1', 1792800000)])
        out = self.discover()
        tools = {t['tool_id']: t for t in out['public']['tools']}
        self.assertIn('qodercn', tools, 'qodercn 必须被发现')
        t = tools['qodercn']
        self.assertEqual(t['status'], 'SUPPORTED')
        self.assertEqual(t['capability'], 'ACTIVITY')
        self.assertEqual(t['adapter'], 'qodercn')
        self.assertGreaterEqual(t['data_sources_count'], 1)
        self.assertIn('qodercn', out['public']['groups']['supported'])
        # ledger 侧 discovery.json：ACTIVITY 等级
        ld = out['ledger_discovery']
        self.assertEqual(ld['sources']['qodercn']['state'], 'found')
        self.assertEqual(ld['sources']['qodercn']['visibility'], 'activity')
        self.assertEqual(ld['grades']['ACTIVITY'] >= 1, True)

    def test_similar_names_not_misidentified(self):
        # 反例：相似目录名 + 数据文件，不得锚定成 qodercn。
        # 注：catalog 工具在 universal 报告里始终列出（not_found 也是一行），
        # 正确的反例断言是 data_sources_count==0 + ledger 侧 not_found +
        # 诱饵目录名不出现在任何 tool_id。
        for name in ('my-qoder-backup', 'qoder-notes', 'qoder-cn-2'):
            d = os.path.join(self.appdata, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'history.jsonl'), 'w',
                      encoding='utf-8') as f:
                f.write(json.dumps(
                    _rec('user', 'sx', 'ux', 1792800000)) + '\n')
        out = self.discover()
        tools = {t['tool_id']: t for t in out['public']['tools']}
        self.assertIn('qodercn', tools,
                      'catalog 工具恒列出（v1.1 known-tools 契约）')
        self.assertEqual(tools['qodercn']['data_sources_count'], 0,
                         '非标准目录不得让 qodercn 判 found')
        ld = out['ledger_discovery']['sources']
        self.assertEqual(ld['qodercn']['state'], 'not_found')
        for tid in tools:
            for decoy in ('my-qoder-backup', 'qoder-notes', 'qoder-cn-2'):
                self.assertNotIn(decoy, tid)

    def test_real_catalog_strict_anchor_matrix(self):
        # 纯函数级：真实 catalog 的锚集合判定（无 FS 访问）
        import discovery
        cat = discovery.load_catalog()
        self.assertEqual(discovery._strict_anchor_id('.qoder-cn', cat['tools']),
                         'qodercn')
        self.assertEqual(discovery._strict_anchor_id('Qoder CN', cat['tools']),
                         'qodercn')
        for name in ('my-qoder-backup', 'qoder', 'qoder-cn-2', 'qodercnx',
                     'Qoder CN Lite', '.qoderwake-cn'):
            self.assertIsNone(discovery._strict_anchor_id(name, cat['tools']),
                              '%r 不得被锚定为 qodercn' % name)


class IngestSemantics(QoderSandbox):

    def test_classification_and_mixed_timestamps(self):
        self.write_session('proj1', 'a.jsonl', self.base_fixture())
        out = self.scan()
        c = out['db']
        self.assertEqual(out['exit'], 0)
        kinds = {r[0]: r[1] for r in c['activity_kinds']}
        self.assertEqual(kinds.get('user', 0), 1)
        self.assertEqual(kinds.get('assistant', 0), 1)
        self.assertEqual(kinds.get('assistant_tooluse', 0), 1)
        self.assertEqual(c['activity_sessions'][0][0], 1)
        # 时间归一：秒 u1→*1000；ISO u2；毫秒 u3 原样
        lo, hi = c['ts_range'][0]
        self.assertEqual(lo, 1792800000000)
        self.assertEqual(hi, max(1792800123456, 1792843200000))
        # 8 ACTIVITY 之外的用途隔离（任务 8 前半）
        self.assertEqual(c['usage_rows_qodercn'], 0,
                         'qodercn 绝不写 usage_event')

    def test_rescan_no_double_count(self):
        p = self.write_session('proj1', 'a.jsonl', self.base_fixture())
        first = self.scan()
        # 触碰文件（size/mtime 变化 → 重解析）后重扫：事件级去重必须兜住
        with open(p, 'a', encoding='utf-8') as f:
            f.write('')      # 内容不变但重开文件句柄；再显式 bump mtime
        os.utime(p, (os.path.getmtime(p) + 5, os.path.getmtime(p) + 5))
        second = self.scan()
        n1 = dict(first['db']['activity_by_client']).get('qodercn')
        n2 = dict(second['db']['activity_by_client']).get('qodercn')
        self.assertEqual(n1, 3)
        self.assertEqual(n2, 3, '同事件重读不得增计')

    def test_copy_rename_append_no_duplication(self):
        self.write_session('proj1', 'a.jsonl', self.base_fixture())
        self.scan()
        # 复制到另一个项目目录（同内容不同物理文件）
        src = os.path.join(self.qprojects(), 'proj1', 'a.jsonl')
        dst_dir = os.path.join(self.qprojects(), 'proj2')
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy(src, os.path.join(dst_dir, 'copy.jsonl'))
        # 改名原文件
        os.rename(src, os.path.join(self.qprojects(), 'proj1', 'renamed.jsonl'))
        out = self.scan()
        self.assertEqual(dict(out['db']['activity_by_client']).get('qodercn'), 3,
                         '复制/改名不得产生重复事件')
        # 增量追加：新增两条，只多两条
        with open(os.path.join(self.qprojects(), 'proj1', 'renamed.jsonl'),
                  'a', encoding='utf-8') as f:
            f.write(json.dumps(_rec('user', 's1', 'u10', 1792801000)) + '\n')
            f.write(json.dumps(_rec('assistant', 's1', 'u11', 1792802000)) + '\n')
        out = self.scan()
        self.assertEqual(dict(out['db']['activity_by_client']).get('qodercn'), 5,
                         '增量追加只新增新事件')

    def test_activity_not_polluting_token_totals(self):
        self.write_session('proj1', 'a.jsonl', self.base_fixture())
        out = self.scan()
        b = out['board_grades'] or {}
        by_client = (b.get('by_client') or {})
        self.assertEqual(by_client.get('qodercn'), 'ACTIVITY')
        s = out['board_summary'] or {}
        self.assertEqual(s.get('usage_events', 0), out['db']['usage_rows_total'])
        acts = {a.get('client'): a.get('records')
                for a in (out['board_activity'] or [])}
        self.assertEqual(acts.get('qodercn'), 3,
                         'board activity 必须含 qodercn 计数')
        self.assertEqual(out['db']['usage_rows_qodercn'], 0)

    def test_privacy_no_home_path_leak(self):
        self.write_session('proj1', 'a.jsonl', self.base_fixture())
        self.discover()
        self.scan()
        for name in ('discovery.json', 'usage-board.json', 'run-state.json'):
            p = os.path.join(self.tmp, name)
            if not os.path.isfile(p):
                continue
            with open(p, encoding='utf-8') as f:
                text = f.read()
            self.assertNotIn(self.home, text,
                             '%s 不得包含 home 绝对路径' % name)
            self.assertNotIn('REDACTED-BODY', text,
                             '%s 不得包含正文样本' % name)

    def test_coexistence_with_catpaw_traecn(self):
        self.write_session('proj1', 'a.jsonl', self.base_fixture())
        # catpaw 转录
        cp = os.path.join(self.home, '.catpaw', 'projects', 'w1')
        os.makedirs(cp, exist_ok=True)
        with open(os.path.join(cp, 'c1.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'messageId': 'm1', 'conversationId': 'cv1',
                                'timestamp': '2026-10-20T10:00:00Z',
                                'type': 'msg', 'cwd': 'D:/w1'}) + '\n')
        # traecn 记忆
        tr = os.path.join(self.home, '.trae-cn', 'memory', 'projects',
                          'prj-20261020')
        os.makedirs(tr, exist_ok=True)
        with open(os.path.join(tr, 'memo.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'message_id': 't1',
                                'message_summary_time': '2026-10-21 09:00:00',
                                'intent': 'x'}) + '\n')
        out = self.discover()
        ld = out['ledger_discovery']['sources']
        for c in ('qodercn', 'catpaw', 'traecn'):
            self.assertEqual(ld[c]['state'], 'found', '%s 应共存发现' % c)
        out = self.scan()
        by = dict(out['db']['activity_by_client'])
        self.assertEqual(by.get('qodercn'), 3)
        self.assertEqual(by.get('catpaw'), 1)
        self.assertEqual(by.get('traecn'), 1)


class ParserUnits(unittest.TestCase):
    """进程内纯函数单测（无 FS 副作用，除沙盒临时文件）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-qpu-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ts_normalization_matrix(self):
        import ledger
        f = ledger._qoder_ts_ms
        self.assertEqual(f(1792800000), 1792800000000)          # 秒
        self.assertEqual(f(1792800123456), 1792800123456)       # 毫秒
        self.assertEqual(f('2026-10-24T12:00:00Z'),
                         1792843200000)                         # ISO UTC
        self.assertIsNone(f('bad-date'))
        self.assertIsNone(f(0))
        self.assertIsNone(f(-5))
        self.assertIsNone(f(None))
        self.assertIsNone(f(True))

    def test_event_id_independent_of_location(self):
        import ledger
        p1 = os.path.join(self.tmp, 'a.jsonl')
        p2 = os.path.join(self.tmp, 'renamed-elsewhere.jsonl')
        recs = [_rec('user', 's1', 'u1', 1792800000)]
        for p in (p1, p2):
            with open(p, 'w', encoding='utf-8') as f:
                f.write(json.dumps(recs[0]) + '\n')
        r1 = list(ledger.parse_qodercn_file(p1))
        r2 = list(ledger.parse_qodercn_file(p2))
        self.assertEqual(r1[0]['event_id'], r2[0]['event_id'])
        self.assertEqual(r1[0]['session_key'], r2[0]['session_key'])

    def test_no_content_fields_in_rows(self):
        import ledger
        p = os.path.join(self.tmp, 'a.jsonl')
        rec = _rec('assistant', 's1', 'u2', 1792800000, tool_use=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
        rows = list(ledger.parse_qodercn_file(p))
        self.assertEqual(len(rows), 1)
        blob = json.dumps(rows, ensure_ascii=False)
        for banned in ('REDACTED-BODY', 'command', 'content', 'thinking',
                       'text', 'input'):
            self.assertNotIn(banned, blob, '行数据不得携带内容字段：%s' % banned)


if __name__ == '__main__':
    unittest.main()
