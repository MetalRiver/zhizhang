# -*- coding: utf-8 -*-
"""V1.1 Anchor Match Boundary 测试（严格目录锚 / registry 锚）。

对应 FINAL DISCOVERY SAFETY FIX 验收：
  A. 'eta-ai' 不得匹配 'theta-ai'（真机曾观察到的误锚）
  B. 'codex' 不得误匹配 'my-codex-backup'（除非 catalog 显式 alias）
  C. 大小写差异按 normalized exact 匹配（Codex / CODEX / codex）
  D. 显式 alias（'beta-eta-ai-old' ← catalog install_aliases）允许锚定
  E. 目录 basename exact match 允许 ingestion
  F. 仅宽松 candidate 命中、无 exact anchor → UNANCHORED，不入账
  G. 两个名称有公共子串 → 绝不归为同一 tool_id
  H. installed-app identity（注入模拟，不扩展真实 Registry 扫描）：
     exact/alias → 稳定 tool_id → Generic ingestion；
     相似但非同一 display name → 不得误锚定

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
    _reg = os.environ.get('DISC_REGISTRY')
    rep = discovery.run_discovery(
        full=os.environ.get('DISC_FULL') == '1', state_dir=os.getcwd(),
        catalog=_cat,
        registry_names=json.loads(_reg) if _reg else None)
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
            counts['by_client'] = conn.execute(
                'SELECT client, COUNT(*) FROM usage_event GROUP BY client'
            ).fetchall()
        finally:
            conn.close()
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
        self.tmp = _safety.sandbox_dir(prefix='ul-anchor-')
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

    def write_catalog(self, anchors):
        """anchors: [(id, install_hints, install_aliases)]。"""
        cat = json.load(open(os.path.join(ROOT, 'source-catalog.json'),
                             encoding='utf-8'))
        for tid, hints, aliases in anchors:
            cat['tools'].append({
                'id': tid, 'display_name': tid,
                'process_names': [tid],
                'install_hints': list(hints),
                'install_aliases': list(aliases),
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

    def engine(self, full=True, registry=None):
        env = dict(self.env)
        env['DISC_MODE'] = 'discover'
        env['DISC_FULL'] = '1' if full else '0'
        if self._cat_path:
            env['DISC_CATALOG'] = self._cat_path
        if registry is not None:
            env['DISC_REGISTRY'] = json.dumps(registry)
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

    def make_generic_jsonl(self, dirname, records=2):
        d = os.path.join(self.appdata, dirname, 'logs')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'usage.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(records):
                f.write(json.dumps({
                    'model': 'm', 'timestamp': 1760000000 + i,
                    'session_id': 's%d' % i, 'prompt_tokens': 100,
                    'completion_tokens': 20}) + '\n')
        return d


class TestStrictAnchorMatching(_Sandbox):

    def test_a_eta_does_not_anchor_theta(self):
        # 真机曾观察到的误锚：'eta-ai' 裸 substring 命中 'theta-ai'。
        # 严格匹配下：theta-ai 只能是 unanchored 候选，绝不入账；
        # eta-ai 目录不存在 → eta-ai 未找到。
        self.write_catalog(anchors=[('eta-ai', ['eta-ai'], [])])
        self.make_generic_jsonl('theta-ai')
        pub = self.engine()
        theta = _tool_by_id(pub, 'theta-ai')
        self.assertIsNotNone(theta)
        self.assertEqual(theta['status'], 'GENERIC_SUPPORTED_UNANCHORED')
        eta = _tool_by_id(pub, 'eta-ai')
        self.assertTrue(eta is None or eta['data_sources_count'] == 0)
        res = self.scan()
        self.assertEqual(count_client(res, 'theta-ai'), 0)
        self.assertEqual(count_client(res, 'eta-ai'), 0)

    def test_b_codex_backup_not_anchored(self):
        # 'my-codex-backup' 含 'codex' 子串，但 basename ≠ '.codex'
        # 且无 alias → 绝不锚定为 codex，也不作为 codex 数据采集
        self.make_generic_jsonl('my-codex-backup')
        pub = self.engine()
        codex = _tool_by_id(pub, 'codex')
        self.assertEqual(codex['data_sources_count'], 0)
        res = self.scan()
        self.assertEqual(count_client(res, 'codex'), 0)

    def test_c_case_normalization(self):
        # Codex / CODEX / codex 大小写差异按 normalized exact 匹配
        self.write_catalog(anchors=[
            ('zetax-ai', ['ZetaX-AI'], [])])
        self.make_generic_jsonl('ZETAX-AI')          # 大小写不同
        pub = self.engine()
        t = _tool_by_id(pub, 'zetax-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        res = self.scan()
        self.assertEqual(count_client(res, 'zetax-ai'), 2)

    def test_d_explicit_alias_allows_anchor(self):
        # 'beta-eta-ai-old' 与 'eta-ai' 有公共子串，但 catalog 明确
        # 声明 alias → 允许锚定（除非声明，否则禁止）
        self.write_catalog(anchors=[
            ('eta-ai', ['eta-ai'], ['beta-eta-ai-old'])])
        self.make_generic_jsonl('beta-eta-ai-old')
        pub = self.engine()
        t = _tool_by_id(pub, 'eta-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        res = self.scan()
        self.assertEqual(count_client(res, 'eta-ai'), 2)

    def test_e_basename_exact_match_ingests(self):
        self.write_catalog(anchors=[('etax-ai', ['etax-ai'], [])])
        self.make_generic_jsonl('etax-ai')
        pub = self.engine()
        self.assertEqual(_tool_by_id(pub, 'etax-ai')['status'],
                         'GENERIC_SUPPORTED')
        res = self.scan()
        self.assertEqual(count_client(res, 'etax-ai'), 2)

    def test_f_loose_candidate_without_anchor(self):
        # 目录名命中宽松候选提示（'ai' 边界）但无 exact anchor →
        # GENERIC_SUPPORTED_UNANCHORED → 不入账
        self.make_generic_jsonl('mystic-ai')
        pub = self.engine()
        t = _tool_by_id(pub, 'mystic-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED_UNANCHORED')
        res = self.scan()
        self.assertEqual(count_client(res, 'mystic-ai'), 0)

    def test_g_common_substring_not_merged(self):
        # 'alphax-ai' 与 'alpha-ai-x' 有公共子串 'alpha-ai'，
        # 都未被锚定 → 两个独立 tool_id，绝不合并、绝不入账
        self.make_generic_jsonl('alphax-ai')
        self.make_generic_jsonl('alpha-ai-x')
        pub = self.engine()
        ids = sorted(t['tool_id'] for t in pub['tools']
                     if t['tool_id'].startswith('alpha'))
        self.assertEqual(ids, ['alpha-ai-x', 'alphax-ai'], ids)
        self.assertTrue(all(
            t['status'] == 'GENERIC_SUPPORTED_UNANCHORED'
            for t in pub['tools'] if t['tool_id'] in ids))
        res = self.scan()
        self.assertEqual(count_client(res, 'alphax-ai'), 0)
        self.assertEqual(count_client(res, 'alpha-ai-x'), 0)


class TestRegistryAnchor(_Sandbox):
    """§5：installed-app identity → catalog exact/alias anchor →
    stable tool_id → Generic ingestion。Registry 名为注入模拟，
    不扩展真实 Registry 扫描逻辑。"""

    def _catalog(self):
        # display_name 'RegCheck Suite'，alias 'RegCheck Suite Pro'？
        # 否——alias 必须显式；这里只声明 exact 集合。
        self.write_catalog(anchors=[
            ('regcheck-ai', ['regcheck-ai'], ['RegCheck Suite'])])
        self.make_generic_jsonl('regcheck-ai')

    def test_registry_exact_alias_anchors_and_ingests(self):
        # 注意：registry 名只影响 registry_hint 补充根；真正的目录锚
        # 仍走 basename exact —— 本用例验证二者共同给出稳定 tool_id。
        self._catalog()
        pub = self.engine(full=True, registry=['RegCheck Suite'])
        t = _tool_by_id(pub, 'regcheck-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        self.assertEqual(t['detected_via'], 'std_dir_match')
        res = self.scan()
        self.assertEqual(count_client(res, 'regcheck-ai'), 2)

    def test_similar_display_name_not_misanchored(self):
        # 'RegCheck Suite Pro' ≠ 'RegCheck Suite'（registry exact 语义），
        # 且目录名仍 exact 匹配 → 工具仍由目录锚定，不受相似名干扰；
        # 换成无 exact 目录锚的场景时，相似名绝不能凭 substring 锚定。
        self._catalog()
        self.write_catalog(anchors=[
            ('regcheck-ai', ['regcheck-ai'], ['RegCheck Suite']),
            ('other-ai', ['other-ai'], [])])
        pub = self.engine(full=True, registry=['RegCheck Suite Pro'])
        # 'RegCheck Suite Pro' 不得给 regcheck-ai / other-ai 带来任何
        # registry 锚定（无 registry_hint 根）；目录 exact 锚不受影响。
        t = _tool_by_id(pub, 'regcheck-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['detected_via'], 'std_dir_match')
        res = self.scan()
        self.assertEqual(count_client(res, 'regcheck-ai'), 2)
        self.assertEqual(count_client(res, 'other-ai'), 0)


if __name__ == '__main__':
    unittest.main()
