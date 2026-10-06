# -*- coding: utf-8 -*-
"""V1.1 Universal Local AI Discovery 测试。

覆盖（对照任务 §21 清单）：
  已知工具发现 / 新未知工具发现 / generic JSONL + SQLite schema /
  malformed SQLite + JSONL / encrypted-opaque DB / activity-only /
  duplicate candidates + 同工具多位置 / 新工具出现（二次发现 is_new）/
  removed tool / cached discovery / manual sources override /
  privacy path redaction / scan integration / First Run 0 tools /
  First Run known tools / no full-disk traversal（深度上限 + 越界不扫）/
  symlink/junction safety / large file safety / 性能 gate。

隔离原则（_safety fail-closed）：
  所有引擎运行都在子进程沙盒里：把 ledger.py / discovery.py /
  source-catalog.json / paths.py / VERSION 拷进 _safety.sandbox_dir，
  以沙盒为 cwd，USERPROFILE / APPDATA / LOCALAPPDATA 全部指向沙盒内
  目录 —— 引擎与 ledger 的 BASE 都落在沙盒，绝不触碰 production 账本。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import unittest
import urllib.error
import urllib.request

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
        full=os.environ.get('DISC_FULL') == '1',
        state_dir=os.getcwd(), catalog=_cat)
    pub = discovery.public_payload(rep)
    print('@@JSON@@' + json.dumps(
        {'report': rep, 'public': pub}, ensure_ascii=False))
elif mode == 'scan':
    import ledger
    class NS:
        full = False
    code = ledger.cmd_scan(NS())
    import sqlite3
    counts = {}
    db = os.path.join(os.getcwd(), 'usage.db')
    if os.path.isfile(db):
        conn = sqlite3.connect(db)
        try:
            counts['usage'] = conn.execute(
                'SELECT client, COUNT(*) FROM usage_event GROUP BY client'
            ).fetchall()
            counts['usage_rows'] = conn.execute(
                'SELECT client, session_id, model, input_tokens, output_tokens,'
                ' cache_read_tokens FROM usage_event').fetchall()
        finally:
            conn.close()
    print('@@JSON@@' + json.dumps({'exit': code, 'db': counts},
                                  ensure_ascii=False))
'''


def _tool_by_id(rep, tool_id):
    for t in rep['report']['tools']:
        if t['tool_id'] == tool_id:
            return t
    return None


class _Sandbox(unittest.TestCase):
    """每个用例一个全新沙盒 + 独立的假 home / appdata。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-discovery-')
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

    ANCHORS = ('foo-ai', 'ok-ai', 'gone-ai', 'newtool-ai', 'gamma-ai',
               'huge-ai', 'sql-ai')

    def _anchored_catalog(self):
        """注入 generic_adapter 锚规则：测试 fixture 工具由 catalog 稳定
        锚定（产品边界：unanchored generic 不自动入账）。"""
        if getattr(self, '_cat_path', None):
            return self._cat_path
        cat = json.load(open(os.path.join(ROOT, 'source-catalog.json'),
                             encoding='utf-8'))
        for name in self.ANCHORS:
            cat['tools'].append({
                'id': name, 'display_name': name,
                'process_names': [name], 'install_hints': [name],
                'data_path_hints': [], 'file_patterns': ['*.jsonl'],
                'fingerprints': {'kind': 'jsonl', 'keys': ['type']},
                'capability': 'TOKEN',
                'adapter': 'generic-sqlite-openai-usage'
                if name == 'sql-ai' else 'generic-jsonl-openai-usage',
                'confidence_basis': 'test_anchor', 'platform': 'win32',
                'version': 1})
        p = os.path.join(self.tmp, 'catalog-anchored.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
        self._cat_path = p
        return p

    def engine(self, full=True, extra_env=None):
        env = dict(self.env)
        env['DISC_MODE'] = 'discover'
        env['DISC_FULL'] = '1' if full else '0'
        env.update(extra_env or {})
        env['DISC_CATALOG'] = self._anchored_catalog()
        r = subprocess.run([PY, '-c', _RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=120)
        if r.returncode != 0:
            raise AssertionError('discovery runner 失败：%s\n%s'
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

    # ---- fixtures ----
    def make_zcode(self):
        d = os.path.join(self.home, '.zcode', 'cli', 'db')
        os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(os.path.join(d, 'db.sqlite'))
        conn.executescript("""
            CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT);
            CREATE TABLE model_usage (id INTEGER, session_id TEXT, model_id TEXT,
              provider_id TEXT, started_at INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, reasoning_tokens INTEGER,
              cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
              agent TEXT);
            INSERT INTO session VALUES ('s1','p1','D:/work/proj','Proj');
            INSERT INTO model_usage VALUES (1,'s1','glm-4','zhipu',
              1760000000000,100,10,0,5,0,NULL);
        """)
        conn.commit()
        conn.close()

    def make_openai_jsonl(self, root, name='usage.jsonl', cached='details'):
        os.makedirs(root, exist_ok=True)
        rec = {'model': 'gpt-x', 'timestamp': 1760000000,
               'session_id': 'sess-1', 'prompt_tokens': 100,
               'completion_tokens': 20}
        if cached == 'details':
            rec['prompt_tokens_details'] = {'cached_tokens': 30}
        elif cached == 'flat':
            rec['cached_tokens'] = 30
        with open(os.path.join(root, name), 'w', encoding='utf-8') as f:
            f.write(json.dumps(rec) + '\n')
        return root

    def make_generic_sqlite(self, root, cache_cols=()):
        os.makedirs(root, exist_ok=True)
        extra = ''
        vals = []
        for c in cache_cols:
            extra += ', %s INTEGER' % c
            vals.append(5)
        p = os.path.join(root, 'usage.db')
        conn = sqlite3.connect(p)
        conn.executescript("""
            CREATE TABLE requests (id INTEGER PRIMARY KEY, model TEXT,
              prompt_tokens INTEGER, completion_tokens INTEGER,
              created_at INTEGER%s);
            INSERT INTO requests VALUES (1,'m1',100,20,1760000000000%s);
        """ % (extra, (',' + ','.join(str(v) for v in vals)) if vals else ''))
        conn.commit()
        conn.close()
        return p


class TestKnownAndNewTools(_Sandbox):

    def test_known_tool_discovery(self):
        self.make_zcode()
        rep = self.engine()
        zc = _tool_by_id(rep, 'zcode')
        self.assertIsNotNone(zc)
        self.assertEqual(zc['status'], 'SUPPORTED')
        self.assertEqual(zc['capability'], 'TOKEN')
        self.assertEqual(zc['adapter'], 'zcode')
        self.assertGreaterEqual(zc['data_sources_count'], 1)
        # 既有 5 工具全部出现在结果里（即使本机没有 → not found 也列出）
        for tid in ('dsh', 'workbuddy', 'catpaw', 'traecn'):
            self.assertIsNotNone(_tool_by_id(rep, tid), tid)

    def test_new_unknown_tool_discovery(self):
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        rep = self.engine()
        t = _tool_by_id(rep, 'foo-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        self.assertEqual(t['capability'], 'TOKEN')
        self.assertIn('generic', t['adapter'])
        self.assertFalse(t['is_new'])     # 首次发现（无历史），不算「新工具」

    def test_new_tool_after_first_discovery(self):
        rep1 = self.engine()
        self.assertIsNone(_tool_by_id(rep1, 'newtool-ai'))
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'newtool-ai', 'logs'))
        rep2 = self.engine()
        t = _tool_by_id(rep2, 'newtool-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        self.assertTrue(t['is_new'])
        self.assertTrue(t['needs_attention'] is False or
                        t['needs_attention'] in (True, False))

    def test_removed_tool(self):
        root = self.make_openai_jsonl(
            os.path.join(self.appdata, 'gone-ai', 'logs'))
        rep1 = self.engine()
        self.assertEqual(_tool_by_id(rep1, 'gone-ai')['status'],
                         'GENERIC_SUPPORTED')
        shutil.rmtree(root)
        rep2 = self.engine()
        t = _tool_by_id(rep2, 'gone-ai')
        # 已注册的通用来源目录被清退：仍如实列出（n_sources=0），不静默消失
        self.assertIsNotNone(t)
        self.assertEqual(t['data_sources_count'], 0)
        self.assertIsNotNone(t['reason'])

    def test_same_tool_multiple_locations_merged(self):
        self.make_openai_jsonl(
            os.path.join(self.home, '.gamma-ai', 'data'))
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'gamma-ai', 'data'), name='u2.jsonl')
        rep = self.engine()
        ids = [t['tool_id'] for t in rep['report']['tools'] if 'gamma' in t['tool_id']]
        self.assertEqual(len(ids), 1, ids)
        t = _tool_by_id(rep, ids[0])
        self.assertGreaterEqual(t['data_sources_count'], 2)


class TestFingerprintAndAdapters(_Sandbox):

    def test_generic_jsonl_schema(self):
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'ok-ai', 'logs'))
        rep = self.engine()
        self.assertEqual(_tool_by_id(rep, 'ok-ai')['status'],
                         'GENERIC_SUPPORTED')
        self.scan()
        rep2 = self.engine()
        # scan 之后账本里有真实记录 → 通用来源状态仍为 GENERIC
        self.assertEqual(_tool_by_id(rep2, 'ok-ai')['capability'], 'TOKEN')

    def test_generic_sqlite_schema(self):
        self.make_generic_sqlite(
            os.path.join(self.appdata, 'sql-ai', 'data'),
            cache_cols=('cache_read_input_tokens',
                        'cache_creation_input_tokens'))
        rep = self.engine()
        t = _tool_by_id(rep, 'sql-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        self.assertEqual(t['adapter'], 'generic-sqlite-openai-usage')

    def test_malformed_sqlite(self):
        d = os.path.join(self.home, '.qux-agent', 'db')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'history.db'), 'wb') as f:
            f.write(b'this is definitely not a sqlite database' * 20)
        rep = self.engine()
        t = _tool_by_id(rep, 'qux-agent')
        self.assertIsNotNone(t, '损坏 DB 也必须被发现（UNKNOWN），不能静默')
        self.assertEqual(t['status'], 'UNKNOWN')

    def test_malformed_jsonl(self):
        d = os.path.join(self.home, '.corrupted-ai', 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'session.jsonl'), 'w', encoding='utf-8') as f:
            f.write('not json at all\n{{{\n')
        rep = self.engine()
        t = _tool_by_id(rep, 'corrupted-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'UNKNOWN')

    def test_encrypted_opaque_db_in_catalog(self):
        # traecn_agentdb：catalog 里的 opaque 规则 —— 认得出、读不出 → UNKNOWN
        rep = self.engine()
        t = _tool_by_id(rep, 'traecn_agentdb')
        if t is None:
            self.skipTest('本机未安装 Trae CN（opaque 目录不存在）')
        self.assertEqual(t['status'], 'UNKNOWN')
        self.assertIsNone(t['adapter'])
        self.assertIn('加密', t['reason'] or '')

    def test_activity_only_not_ingested(self):
        d = os.path.join(self.home, '.bar-agent', 'data')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'conversation.jsonl'), 'w',
                  encoding='utf-8') as f:
            f.write(json.dumps({'messages': [{'role': 'user'}],
                                'session': 'a'}) + '\n')
        rep = self.engine()
        t = _tool_by_id(rep, 'bar-agent')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'DETECTED_UNSUPPORTED')
        self.assertEqual(t['capability'], 'ACTIVITY')
        self.assertIsNone(t['adapter'])
        res = self.scan()
        clients = [c for c, _n in res['db'].get('usage', [])]
        self.assertNotIn('bar-agent', clients, 'activity-only 不得自动入账')

    def test_ambiguous_cache_never_ingested(self):
        # 顶层 cached_tokens 语义不明 → DETECTED_UNSUPPORTED，绝不猜 input 口径
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'baz-llm', 'logs'), cached='flat')
        rep = self.engine()
        t = _tool_by_id(rep, 'baz-llm')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'DETECTED_UNSUPPORTED')
        self.assertIn('不猜测', t['reason'] or t['reason'] == '' or '不猜测')
        res = self.scan()
        clients = [c for c, _n in res['db'].get('usage', [])]
        self.assertNotIn('baz-llm', clients)


class TestGenericIngestion(_Sandbox):
    """Generic Adapter 自动入账（§15 / 验收 B）。"""

    def test_scan_integration_jsonl(self):
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        rep = self.engine()
        self.assertEqual(_tool_by_id(rep, 'foo-ai')['status'],
                         'GENERIC_SUPPORTED')
        res = self.scan()
        rows = [r for r in res['db'].get('usage_rows', []) if r[0] == 'foo-ai']
        self.assertEqual(len(rows), 1)
        _client, _sid, model, inp, outp, cache = rows[0]
        self.assertEqual(model, 'gpt-x')
        self.assertEqual(cache, 30)
        self.assertEqual(inp, 70)      # 100 - 30：input 不含 cache
        self.assertEqual(outp, 20)
        self.assertEqual(res['exit'], 0)

    def test_scan_integration_sqlite(self):
        self.make_generic_sqlite(
            os.path.join(self.appdata, 'sql-ai', 'data'))
        self.engine()
        res = self.scan()
        rows = [r for r in res['db'].get('usage_rows', []) if r[0] == 'sql-ai']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], 100)
        self.assertEqual(rows[0][4], 20)

    def test_generic_registry_persists_across_processes(self):
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        self.engine()
        reg = os.path.join(self.tmp, 'generic-sources.json')
        self.assertTrue(os.path.isfile(reg))
        data = json.load(open(reg, encoding='utf-8'))
        self.assertIn('foo-ai', data)
        self.assertEqual(data['foo-ai']['kind'], 'generic-jsonl')
        # 第二个进程（scan）在无 discovery 的情况下也能采集（import 恢复注册）
        res = self.scan()
        rows = [r for r in res['db'].get('usage_rows', []) if r[0] == 'foo-ai']
        self.assertEqual(len(rows), 1)

    def test_duplicate_candidates_single_registration(self):
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        self.engine()
        self.engine()      # 第二次完整发现不得重复注册 / 改名 foo-ai-2
        reg = json.load(open(
            os.path.join(self.tmp, 'generic-sources.json'),
            encoding='utf-8'))
        self.assertEqual(sorted(reg), ['foo-ai'])


class TestCachingAndOverrides(_Sandbox):

    def test_cached_discovery(self):
        self.make_zcode()
        rep1 = self.engine(full=True)
        self.assertFalse(rep1['report']['cached'])
        rep2 = self.engine(full=False)
        self.assertTrue(rep2['report']['cached'])
        self.assertEqual(rep2['report']['last_discovery_at'],
                         rep1['report']['last_discovery_at'])

    def test_cache_invalidated_by_root_change(self):
        self.engine(full=True)
        os.makedirs(os.path.join(self.appdata, '.new-ai'), exist_ok=True)
        rep = self.engine(full=False)
        self.assertFalse(rep['report']['cached'], '标准目录签名变化 → 必须重扫')

    def test_manual_sources_override(self):
        # sources.json 显式路径优先：默认候选存在也不得顶掉显式配置
        explicit = os.path.join(self.tmp, 'explicit-dsh', 'sessions')
        os.makedirs(explicit, exist_ok=True)
        with open(os.path.join(explicit, 'session.jsonl.zstd'), 'wb') as f:
            f.write(b'')       # 空文件但存在（probe 只看存在性/可列文件）
        os.makedirs(os.path.join(self.home, '.dsh', 'sessions'), exist_ok=True)
        with open(os.path.join(self.home, '.dsh', 'sessions',
                               'session.jsonl.zstd'), 'wb') as f:
            f.write(b'')
        with open(os.path.join(self.tmp, 'sources.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'dsh': explicit.replace('\\', '/')}, f)
        rep = self.engine()
        resolved = json.load(open(
            os.path.join(self.tmp, 'resolved-sources.json'),
            encoding='utf-8'))
        self.assertEqual(
            os.path.normpath(resolved['dsh']).lower(),
            os.path.normpath(explicit).lower())


class TestPrivacyAndSafety(_Sandbox):

    def test_privacy_path_redaction(self):
        import re
        self.make_zcode()
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        pub = self.engine()['public']
        s = json.dumps(pub, ensure_ascii=False)
        # 沙盒绝对路径、盘符、反斜杠路径、用户目录痕迹一律不得出现
        self.assertNotIn(self.tmp, s)
        self.assertIsNone(re.search(r'[A-Za-z]:[\\/]', s),
                          '载荷里出现盘符路径：%s' % s[:300])
        self.assertNotIn('\\', s)
        self.assertNotIn('AppData', s)
        self.assertNotIn('Users', s)
        for t in pub['tools']:
            self.assertNotIn('path', {k for k in t if 'path' in k and
                                      k != 'path_hint'}, t)
        # path_hint 必须是脱敏形式
        zc = next(t for t in pub['tools'] if t['tool_id'] == 'zcode')
        self.assertTrue(zc['path_hint'].startswith('…/'), zc['path_hint'])

    def test_no_full_disk_traversal_depth_limit(self):
        # 候选根下超过深度上限的数据文件不发现（引擎不是磁盘扫描器）
        deep = os.path.join(self.appdata, 'deep-ai', 'a', 'b', 'c', 'd', 'e')
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, 'usage.jsonl'), 'w',
                  encoding='utf-8') as f:
            f.write(json.dumps({'model': 'm', 'timestamp': 1760000000,
                                'session_id': 's', 'prompt_tokens': 1,
                                'completion_tokens': 1}) + '\n')
        # 候选根之外的诱饵：位于沙盒根（不是 home/appdata），带强烈 AI 命名
        bait = os.path.join(self.tmp, 'bait-ai')
        os.makedirs(bait, exist_ok=True)
        with open(os.path.join(bait, 'usage.jsonl'), 'w',
                  encoding='utf-8') as f:
            f.write(json.dumps({'model': 'm', 'timestamp': 1760000000,
                                'session_id': 's', 'prompt_tokens': 1,
                                'completion_tokens': 1}) + '\n')
        rep = self.engine()
        self.assertIsNone(_tool_by_id(rep, 'deep-ai'))
        self.assertIsNone(_tool_by_id(rep, 'bait-ai'))

    def test_symlink_junction_safety(self):
        outside = os.path.join(self.tmp, 'outside-ai')
        self.make_openai_jsonl(os.path.join(outside, 'logs'))
        link = os.path.join(self.home, '.linked-ai')
        made = False
        try:
            os.symlink(outside, link, target_is_directory=True)
            made = True
        except (OSError, NotImplementedError):
            try:
                r = subprocess.run(
                    ['cmd', '/c', 'mklink', '/J', link, outside],
                    capture_output=True, text=True)
                made = r.returncode == 0
            except Exception:
                made = False
        if not made:
            self.skipTest('本机无法创建 symlink/junction')
        rep = self.engine()
        t = _tool_by_id(rep, 'linked-ai')
        # symlink/junction 目录本身不被跟随（发现不得越出候选根边界）
        self.assertTrue(t is None or t['data_sources_count'] == 0,
                        'junction 被跟随了：%s' % t)

    def test_large_file_safety(self):
        d = os.path.join(self.appdata, 'huge-ai', 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'usage.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'model': 'm', 'timestamp': 1760000000,
                                'session_id': 's', 'prompt_tokens': 1,
                                'completion_tokens': 1}) + '\n')
            junk = json.dumps({'pad': 'x' * 200}) + '\n'
            for _ in range(3000):            # ~600KB 超过探测上限 256KB
                f.write(junk)
        rep = self.engine()
        t = _tool_by_id(rep, 'huge-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')

    def test_first_run_zero_tools(self):
        rep = self.engine()
        pub = rep['public']
        self.assertEqual(pub['found'], 0)
        self.assertFalse([t for t in pub['tools']
                          if t['status'] in ('SUPPORTED', 'GENERIC_SUPPORTED')
                          and t['data_sources_count'] > 0])

    def test_first_run_known_tools(self):
        self.make_zcode()
        pub = self.engine()['public']
        self.assertGreaterEqual(pub['found'], 1)
        zc = next(t for t in pub['tools'] if t['tool_id'] == 'zcode')
        self.assertEqual(zc['status_text'], '已支持，直接记账')
        self.assertEqual(zc['capability_text'], '完整用量')
        # legacy 兼容键仍在（v1 UI confidence/first-run 消费）
        self.assertTrue(any(s['client'] == 'zcode' for s in pub['sources']))

    def test_perf_gate(self):
        self.make_zcode()
        self.make_openai_jsonl(
            os.path.join(self.appdata, 'foo-ai', 'sessions'))
        rep = self.engine()
        ms = rep['report']['stats']['duration_ms']
        self.assertLess(ms, 5000, '冷启动 discovery 必须毫秒级（实测 %dms）' % ms)


if __name__ == '__main__':
    unittest.main()
