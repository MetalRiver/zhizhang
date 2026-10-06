# -*- coding: utf-8 -*-
"""V1.1 产品边界锁定测试（PRODUCT BOUNDARY）。

对应验收：
  A. Codex 新/旧格式共存 → capability TOKEN，completeness PARTIAL
  B. 只有新格式 → completeness COMPLETE
  C. 旧格式不解析 → 缺失历史绝不计为 0 token
  D. anchored generic 多目录（目录名不同但命中同一 anchor）
     → 同一 stable tool_id → 自动 ingestion
  E. unanchored generic schema 可读 → 发现（UNANCHORED）但不入账
  F. 后续 catalog 新增 anchor → 下一次 discovery 允许 ingestion
  G. identity strategy 变化 → needs_attention + 暂停入账，不双记

隔离：全部 _safety 沙盒子进程；绝不触碰真实 ~/.codex 与两本正式账本。
"""
import json
import os
import shutil
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
import json, os, sys
sys.path.insert(0, os.getcwd())
mode = os.environ.get('DISC_MODE', 'discover')
if mode == 'discover':
    import discovery
    _cp = os.environ.get('DISC_CATALOG')
    _cat = json.load(open(_cp, encoding='utf-8')) if _cp else None
    rep = discovery.run_discovery(
        full=os.environ.get('DISC_FULL') == '1', state_dir=os.getcwd(),
        catalog=_cat)
    print('@@JSON@@' + json.dumps(
        {'report': rep, 'public': discovery.public_payload(rep)},
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
            counts['by_client'] = conn.execute(
                'SELECT client, COUNT(*) FROM usage_event GROUP BY client'
            ).fetchall()
            counts['zero_token_rows'] = conn.execute(
                'SELECT COUNT(*) FROM usage_event WHERE'
                ' input_tokens + output_tokens = 0').fetchone()[0]
        finally:
            conn.close()
    counts['stats'] = dict(ledger.LAST_SCAN_STATS)
    print('@@JSON@@' + json.dumps(counts, ensure_ascii=False))
'''


def _tool_by_id(pub, tool_id):
    for t in pub['tools']:
        if t['tool_id'] == tool_id:
            return t
    return None


def count_client(res, client):
    for c, n in (res.get('by_client') or []):
        if c == client:
            return n
    return 0


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-boundary-')
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
        self._cat_path = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- catalog 锚定（测试可用 anchors/aliases 增删规则，验证 F 的「补锚」
    #      与 V1.1 严格锚匹配：备份目录名必须走 catalog 显式 alias）----
    def write_catalog(self, anchors=(), aliases=None):
        cat = json.load(open(os.path.join(ROOT, 'source-catalog.json'),
                             encoding='utf-8'))
        for name in anchors:
            cat['tools'].append({
                'id': name, 'display_name': name,
                'process_names': [name], 'install_hints': [name],
                'install_aliases': list((aliases or {}).get(name) or []),
                'data_path_hints': [], 'file_patterns': ['*.jsonl'],
                'fingerprints': {'kind': 'jsonl', 'keys': ['type']},
                'capability': 'TOKEN',
                'adapter': 'generic-jsonl-openai-usage',
                'confidence_basis': 'test_anchor', 'platform': 'win32',
                'version': 1})
        p = os.path.join(self.tmp, 'catalog.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
        self._cat_path = p

    def engine(self, full=True):
        env = dict(self.env)
        env['DISC_MODE'] = 'discover'
        env['DISC_FULL'] = '1' if full else '0'
        if self._cat_path:
            env['DISC_CATALOG'] = self._cat_path
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

    # ---- Codex rollout fixture（新 / 旧格式）----
    def make_rollout(self, name, old_format=False, records=3):
        d = os.path.join(self.home, '.codex', 'sessions', '2026', '10', '03')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        lines = [{'timestamp': '2026-10-03T06:06:11.795Z',
                  'type': 'session_meta',
                  'payload': {'id': 'sid-x', 'session_id': 'sid-x',
                              'cwd': 'D:/work/proj'}}]
        if old_format:
            lines.append({'timestamp': '2026-10-03T06:06:12.000Z',
                          'type': 'response_item',
                          'payload': {'type': 'message'}})
        else:
            lines.append({'timestamp': '2026-10-03T06:06:12.000Z',
                          'type': 'turn_context',
                          'payload': {'model': 'gpt-test'}})
            for i in range(records):
                lines.append({'ordinal': 10 + i,
                              'timestamp': '2026-10-03T06:06:%02d.000Z'
                                           % (20 + i),
                              'type': 'token_usage_record',
                              'payload': {'response_id': 'resp_%s_%d'
                                          % (name, i),
                                          'session_id': 'sid-x',
                                          'usage': {
                                              'input_tokens': 100 + i,
                                              'cached_input_tokens': 30,
                                              'output_tokens': 20,
                                              'reasoning_output_tokens': 0,
                                              'cache_write_input_tokens': 0,
                                              'total_tokens': 100 + i + 20}}})
        with open(p, 'w', encoding='utf-8') as f:
            for o in lines:
                f.write(json.dumps(o) + '\n')
        return p

    # ---- generic JSONL fixture ----
    def make_generic_jsonl(self, dirname, with_request_id=False, records=2):
        d = os.path.join(self.appdata, dirname, 'logs')
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, 'usage.jsonl')
        with open(p, 'w', encoding='utf-8') as f:
            for i in range(records):
                rec = {'model': 'm', 'timestamp': 1760000000 + i,
                       'session_id': 's%d' % i, 'prompt_tokens': 100,
                       'completion_tokens': 20}
                if with_request_id:
                    rec['request_id'] = 'req-%d' % i
                f.write(json.dumps(rec) + '\n')
        return d


class TestCodexCompleteness(_Sandbox):
    """A / B / C：Codex 完整性口径。"""

    def test_a_mixed_formats_partial(self):
        self.make_rollout('rollout-new.jsonl', old_format=False, records=3)
        self.make_rollout('rollout-old.jsonl', old_format=True)
        self.make_rollout('rollout-old2.jsonl', old_format=True)
        pub = self.engine()['public']
        t = _tool_by_id(pub, 'codex')
        self.assertIsNotNone(t)
        self.assertEqual(t['capability'], 'TOKEN')
        self.assertEqual(t['completeness'], 'PARTIAL')
        self.assertEqual(t['completeness_text'], '存在历史缺口')
        self.assertEqual(t['capability_text'], '完整用量')
        # 完整性缺口必须在用户层可见文本里（非工程字段）
        self.assertIn('历史', json.dumps(pub, ensure_ascii=False))

    def test_b_new_format_only_complete(self):
        self.make_rollout('rollout-new.jsonl', old_format=False, records=3)
        pub = self.engine()['public']
        t = _tool_by_id(pub, 'codex')
        self.assertEqual(t['completeness'], 'COMPLETE')

    def test_c_old_format_not_zero_tokens(self):
        # 旧格式文件不产生任何事件 —— 更不会产生 0 token 的「占位」事件
        self.make_rollout('rollout-new.jsonl', old_format=False, records=3)
        self.make_rollout('rollout-old.jsonl', old_format=True)
        self.engine()
        res = self.scan()
        self.assertEqual(count_client(res, 'codex'), 3)
        self.assertEqual(res['zero_token_rows'], 0)
        # 重扫幂等
        res2 = self.scan()
        self.assertEqual(count_client(res2, 'codex'), 3)
        self.assertEqual(res2['stats']['events_inserted'], 0)


class TestIngestionEligibility(_Sandbox):
    """D / E / F：anchored vs unanchored generic。"""

    def test_d_anchored_generic_multi_location(self):
        # catalog 锚定 'nuvo-ai'：备份目录名不同，但由 catalog **显式
        # alias** 声明 → 同一 stable tool_id → 自动入账（去重）。
        # （严格锚匹配下，未声明 alias 的目录名绝不误锚 —— 见
        # test_anchor_matching.py。）
        self.write_catalog(anchors=('nuvo-ai',),
                           aliases={'nuvo-ai': ['nuvo-ai-backup-2024']})
        self.make_generic_jsonl('nuvo-ai')
        self.make_generic_jsonl('nuvo-ai-backup-2024')
        pub = self.engine()['public']
        tools = [t for t in pub['tools'] if t['status'] == 'GENERIC_SUPPORTED']
        ids = sorted(t['tool_id'] for t in tools)
        self.assertEqual(ids, ['nuvo-ai'], ids)
        res = self.scan()
        self.assertEqual(count_client(res, 'nuvo-ai'), 2)

    def test_e_unanchored_discovered_not_ingested(self):
        # schema 完全可读、身份可生成，但没有 catalog anchor →
        # 发现（UNANCHORED）但绝不自动入账
        self.make_generic_jsonl('lone-ai')
        pub = self.engine()['public']
        t = _tool_by_id(pub, 'lone-ai')
        self.assertIsNotNone(t, '必须被发现')
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED_UNANCHORED')
        self.assertEqual(t['capability'], 'TOKEN')
        self.assertTrue(t['needs_attention'])
        res = self.scan()
        self.assertEqual(count_client(res, 'lone-ai'), 0)
        # 用户层文案：不暴露工程术语
        self.assertEqual(t['status_text'], '可读取数据 · 工具身份待确认')
        s = json.dumps(pub, ensure_ascii=False)
        self.assertNotIn('UNANCHORED', s.replace(
            'GENERIC_SUPPORTED_UNANCHORED', ''))   # 状态枚举仅一处内部标识

    def test_f_anchor_added_later_enables_ingestion(self):
        # 先无锚：只发现不入账；补 catalog anchor → 下一次 discovery 入账
        self.make_generic_jsonl('orla-ai')
        self.write_catalog(anchors=())           # 无锚 catalog
        pub1 = self.engine()['public']
        self.assertEqual(_tool_by_id(pub1, 'orla-ai')['status'],
                         'GENERIC_SUPPORTED_UNANCHORED')
        res1 = self.scan()
        self.assertEqual(count_client(res1, 'orla-ai'), 0)
        self.write_catalog(anchors=('orla-ai',))  # 新增锚 → catalog digest 变
        pub2 = self.engine()['public']
        self.assertEqual(_tool_by_id(pub2, 'orla-ai')['status'],
                         'GENERIC_SUPPORTED')
        res2 = self.scan()
        self.assertEqual(count_client(res2, 'orla-ai'), 2)

    def test_g_identity_strategy_change_pauses_ingestion(self):
        # 策略钉为 content（无 provider id）→ 入账；
        # 来源随后出现 provider id（策略将变为 pid）→ needs_attention +
        # 暂停自动入账，已入账记录不变，绝不双记。
        self.write_catalog(anchors=('vim-ai',))
        d = self.make_generic_jsonl('vim-ai', with_request_id=False)
        self.engine()
        r1 = self.scan()
        self.assertEqual(count_client(r1, 'vim-ai'), 2)
        # 来源升级：新事件带上 request_id（策略将变 pid）
        p = os.path.join(d, 'usage.jsonl')
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'model': 'm', 'timestamp': 1760009000,
                                'session_id': 's9', 'prompt_tokens': 55,
                                'completion_tokens': 5,
                                'request_id': 'req-new'}) + '\n')
        pub = self.engine(full=True)['public']
        t = _tool_by_id(pub, 'vim-ai')
        self.assertTrue(t['needs_attention'])
        self.assertIn('身份策略', t['reason'])
        res = self.scan()
        # 暂停入账：已入账 2 条保留，新记录绝不以另一套身份双记
        self.assertEqual(count_client(res, 'vim-ai'), 2)
        self.assertEqual(res['stats']['events_inserted'], 0)


if __name__ == '__main__':
    unittest.main()
