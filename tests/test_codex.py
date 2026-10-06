# -*- coding: utf-8 -*-
"""Codex discovery / adapter correctness 测试。

覆盖（对应 REAL-WORLD DISCOVERY GAP — CODEX 验收）：
  - installed detection：fixture rollout → 自动发现 SUPPORTED / TOKEN
  - absent：无 ~/.codex → 仍列出（not found + reason），不静默消失
  - path relocation：数据目录移走 → not_found；移回 → 恢复
  - old-format fallback：无 token_usage_record 的旧格式 → 显示但 0 入账
  - 不误记：累计 token_count 事件 / 未知结构 绝不入账
  - 逐请求口径：input excludes cache（input = input_tokens − cached）
  - 稳定身份：改名 / 会话内副本 / 重扫 不重复；append 只增新事件
  - 隐私：public payload 无完整路径

隔离：全部在 _safety 沙盒子进程；绝不触碰真实 ~/.codex 与两本正式账本。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid

import _safety

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

PY = sys.executable

_RUNNER = r'''
import json, os, sys
sys.path.insert(0, os.getcwd())
mode = os.environ.get('DISC_MODE', 'discover')
if mode == 'discover':
    import discovery
    rep = discovery.run_discovery(
        full=os.environ.get('DISC_FULL') == '1', state_dir=os.getcwd())
    print('@@JSON@@' + json.dumps(discovery.public_payload(rep),
                                  ensure_ascii=False))
elif mode == 'scan':
    import ledger
    class NS:
        full = False
    code = ledger.cmd_scan(NS())
    import sqlite3
    db = os.path.join(os.getcwd(), 'usage.db')
    counts = {'exit': code}
    if os.path.isfile(db):
        conn = sqlite3.connect(db)
        try:
            counts['rows'] = conn.execute(
                'SELECT client, session_id, model, input_tokens, output_tokens,'
                ' cache_read_tokens, COUNT(*) FROM usage_event'
                ' GROUP BY client, session_id, model, input_tokens,'
                ' output_tokens, cache_read_tokens').fetchall()
        finally:
            conn.close()
    counts['stats'] = dict(ledger.LAST_SCAN_STATS)
    print('@@JSON@@' + json.dumps(counts, ensure_ascii=False))
'''


def codex_count(res):
    return sum(row[6] for row in (res.get('rows') or []) if row[0] == 'codex')


def make_rollout(path, session_id=None, records=3, with_token_count=False,
                 old_format=False, cwd='D:/work/proj'):
    """合成 Codex rollout fixture（结构与真机取证一致，内容全为合成值）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sid = session_id or str(uuid.uuid4())
    lines = [{'timestamp': '2026-10-03T06:06:11.795Z', 'type': 'session_meta',
              'payload': {'id': sid, 'session_id': sid, 'cwd': cwd}}]
    if not old_format:
        lines.append({'timestamp': '2026-10-03T06:06:12.000Z',
                      'type': 'turn_context',
                      'payload': {'model': 'gpt-test'}})
    for i in range(records):
        lines.append({'ordinal': 10 + i,
                      'timestamp': '2026-10-03T06:06:%02d.000Z' % (20 + i),
                      'type': 'token_usage_record',
                      'payload': {'response_id': 'resp_%03d' % i,
                                  'session_id': sid,
                                  'usage': {'input_tokens': 100 + i,
                                            'cached_input_tokens': 30,
                                            'output_tokens': 20,
                                            'reasoning_output_tokens': 5,
                                            'cache_write_input_tokens': 2,
                                            'total_tokens': 100 + i + 20}}})
    if with_token_count:
        # 会话内累计值（真机取证：单调递增）；引擎绝不能把它当新鲜用量
        for i in range(records):
            lines.append({'timestamp': '2026-10-03T06:07:%02d.000Z' % (20 + i),
                          'type': 'event_msg',
                          'payload': {'type': 'token_count',
                                      'info': {'total_token_usage': {
                                          'input_tokens': 100 * (i + 1),
                                          'cached_input_tokens': 30 * (i + 1),
                                          'output_tokens': 20 * (i + 1),
                                          'reasoning_output_tokens': 0,
                                          'cache_write_input_tokens': 0,
                                          'total_tokens': 120 * (i + 1)}}}})
    with open(path, 'w', encoding='utf-8') as f:
        for o in lines:
            f.write(json.dumps(o) + '\n')
    return sid


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-codex-')
        self.home = os.path.join(self.tmp, 'home')
        self.appdata = os.path.join(self.tmp, 'appdata')
        self.appdata_local = os.path.join(self.tmp, 'appdata-local')
        for d in (self.home, self.appdata, self.appdata_local):
            os.makedirs(d)
        for f in ('ledger.py', 'discovery.py', 'paths.py', 'autopilot.py',
                  'source-catalog.json', 'usage-board.schema.json', 'VERSION'):
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

    def engine(self, full=True):
        env = dict(self.env)
        env['DISC_MODE'] = 'discover'
        env['DISC_FULL'] = '1' if full else '0'
        r = subprocess.run([PY, '-c', _RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=120)
        if r.returncode != 0:
            raise AssertionError('discover runner 失败：%s\n%s'
                                 % (r.stderr[-800:], r.stdout[-400:]))
        line = [l for l in r.stdout.splitlines()
                if l.startswith('@@JSON@@')][-1]
        return json.loads(line[len('@@JSON@@'):])

    def scan(self):
        env = dict(self.env)
        env['DISC_MODE'] = 'scan'
        r = subprocess.run([PY, '-c', _RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=180)
        if r.returncode != 0:
            raise AssertionError('scan runner 失败：%s\n%s'
                                 % (r.stderr[-800:], r.stdout[-400:]))
        line = [l for l in r.stdout.splitlines()
                if l.startswith('@@JSON@@')][-1]
        return json.loads(line[len('@@JSON@@'):])

    def tool(self, pub, tool_id='codex'):
        return next((t for t in pub['tools'] if t['tool_id'] == tool_id),
                    None)

    def codex_root(self):
        return os.path.join(self.home, '.codex', 'sessions')


class TestCodexDiscovery(_Sandbox):

    def test_installed_detection(self):
        make_rollout(os.path.join(self.codex_root(), '2026', '10', '03',
                                  'rollout-x.jsonl'))
        pub = self.engine()
        t = self.tool(pub)
        self.assertIsNotNone(t, 'Codex 必须被自动发现')
        self.assertEqual(t['status'], 'SUPPORTED')
        self.assertEqual(t['capability'], 'TOKEN')
        self.assertEqual(t['adapter'], 'codex')
        self.assertGreaterEqual(t['data_sources_count'], 1)
        self.assertEqual(t['display_name'], 'Codex')
        self.assertGreaterEqual(pub['found'], 1)
        import re
        self.assertIsNone(re.search(r'[A-Za-z]:[\\/]',
                                    json.dumps(pub, ensure_ascii=False)))

    def test_absent_still_listed(self):
        pub = self.engine()
        t = self.tool(pub)
        self.assertIsNotNone(t, '未安装也必须列出（不静默消失）')
        self.assertEqual(t['status'], 'SUPPORTED')       # 产品已支持
        self.assertEqual(t['data_sources_count'], 0)     # 本机没有数据
        self.assertIsNotNone(t['reason'])

    def test_path_relocation(self):
        root = os.path.join(self.codex_root(), '2026', '10', '03')
        make_rollout(os.path.join(root, 'rollout-x.jsonl'))
        pub1 = self.engine()
        self.assertGreaterEqual(self.tool(pub1)['data_sources_count'], 1)
        # 数据目录移走（改名）→ 如实转为 not found；移回 → 恢复
        shutil.move(self.codex_root(),
                    os.path.join(self.home, '.codex', 'moved-away'))
        pub2 = self.engine()
        self.assertEqual(self.tool(pub2)['data_sources_count'], 0)
        self.assertIsNotNone(self.tool(pub2)['reason'])
        shutil.move(os.path.join(self.home, '.codex', 'moved-away'),
                    self.codex_root())
        pub3 = self.engine()
        self.assertGreaterEqual(self.tool(pub3)['data_sources_count'], 1)


class TestCodexIngestion(_Sandbox):

    def _setup(self, **kw):
        return make_rollout(os.path.join(
            self.codex_root(), '2026', '10', '03', 'rollout-x.jsonl'), **kw)

    def test_token_semantics_and_cumulative_not_ingested(self):
        self._setup(records=3, with_token_count=True)
        self.engine()
        res = self.scan()
        self.assertEqual(codex_count(res), 3, '只入账逐请求记录')
        # 每请求口径：input = (100+i)−30 / output=20 / cache_read=30
        rows = [r for r in res['rows'] if r[0] == 'codex']
        self.assertEqual(sorted(r[3] for r in rows), [70, 71, 72])
        self.assertEqual({r[4] for r in rows}, {20})
        self.assertEqual({r[5] for r in rows}, {30})
        self.assertEqual(res['stats']['events_inserted'], 3)
        # 重扫幂等
        res2 = self.scan()
        self.assertEqual(codex_count(res2), 3)
        self.assertEqual(res2['stats']['events_inserted'], 0)

    def test_cumulative_only_file_not_ingested(self):
        # 只有累计 token_count、无逐请求记录（ hypothetic 变体）→ 0 入账
        root = self.codex_root()
        sid = make_rollout(os.path.join(root, '2026', '10', '03',
                                        'rollout-c.jsonl'),
                           records=2, with_token_count=True)
        # 手工删除 token_usage_record 行，只留累计事件
        p = os.path.join(root, '2026', '10', '03', 'rollout-c.jsonl')
        out = []
        for line in open(p, encoding='utf-8'):
            if '"token_usage_record"' not in line:
                out.append(line)
        open(p, 'w', encoding='utf-8').writelines(out)
        self.engine()
        res = self.scan()
        self.assertEqual(codex_count(res), 0, '累计值绝不能当逐请求用量入账')

    def test_old_format_fallback(self):
        # 旧格式（无 token_usage_record）：Codex 仍显示，但 0 入账（不猜）
        self._setup(old_format=True, records=0)
        self.engine()
        pub = self.engine()
        self.assertIsNotNone(self.tool(pub))
        res = self.scan()
        self.assertEqual(codex_count(res), 0)

    def test_unknown_structure_not_misrecorded(self):
        # rollout 命名的文件但内容完全不可识别 → 不入账，不影响其他文件
        p = os.path.join(self.codex_root(), '2026', '10', '03',
                         'rollout-broken.jsonl')
        make_rollout(p, records=2)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('this is not json at all\n\x00\x01binary junk\n')
        make_rollout(os.path.join(self.codex_root(), '2026', '10', '03',
                                  'rollout-good.jsonl'), records=2)
        self.engine()
        res = self.scan()
        self.assertEqual(codex_count(res), 2)

    def test_rename_and_copy_no_duplicates(self):
        sid = self._setup(records=3)
        self.engine()
        self.scan()
        root = self.codex_root()
        d = os.path.join(root, '2026', '10', '03')
        # 同内容复制为不同文件名（如备份）→ 身份去重
        shutil.copy(os.path.join(d, 'rollout-x.jsonl'),
                    os.path.join(d, 'rollout-x-copy.jsonl'))
        self.engine(full=True)
        r1 = self.scan()
        self.assertEqual(codex_count(r1), 3)
        self.assertEqual(r1['stats']['events_inserted'], 0)
        # 改名 → 身份不变
        shutil.move(os.path.join(d, 'rollout-x-copy.jsonl'),
                    os.path.join(d, 'rollout-renamed.jsonl'))
        r2 = self.scan()
        self.assertEqual(codex_count(r2), 3)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_append_only_new_events(self):
        d = os.path.join(self.codex_root(), '2026', '10', '03')
        sid = make_rollout(os.path.join(d, 'rollout-x.jsonl'), records=3)
        self.engine()
        self.scan()
        with open(os.path.join(d, 'rollout-x.jsonl'), 'a',
                  encoding='utf-8') as f:
            f.write(json.dumps({
                'ordinal': 50,
                'timestamp': '2026-10-03T06:09:00.000Z',
                'type': 'token_usage_record',
                'payload': {'response_id': 'resp_new',
                            'session_id': sid,
                            'usage': {'input_tokens': 500,
                                      'cached_input_tokens': 100,
                                      'output_tokens': 80,
                                      'reasoning_output_tokens': 0,
                                      'cache_write_input_tokens': 0,
                                      'total_tokens': 580}}}) + '\n')
        self.engine(full=True)      # 内容签名变化 → 重新发现
        res = self.scan()
        self.assertEqual(codex_count(res), 4)
        self.assertEqual(res['stats']['events_inserted'], 1)


if __name__ == '__main__':
    unittest.main()
