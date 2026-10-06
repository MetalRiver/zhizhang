# -*- coding: utf-8 -*-
"""V1.1 Generic 事件身份稳定性 + 跨位置去重 correctness 测试。

对应 correctness closeout 契约：
  A. scan → scan                      事件数不变
  B. scan → VACUUM → scan             事件数不变
  C. scan → rebuild（不同 rowid）→ scan 事件数不变
  D. 文件 rename/move → scan          事件数不变
  E. 完整 copy 到第二位置 → scan      事件数不变
  F. 部分重叠 A=1–100 / B=51–150      最终 150
  G. 真正不同事件（同 token tuple、不同稳定字段）不得误合并
     （同 tuple 且无任何可区分稳定字段 = 同一逻辑事件，合并是契约）
  H. JSONL 同内容复制到第二文件       不重复
  I. JSONL 重排/重序列化              不重复
  J. JSONL append 新事件              只新增新事件
  K. 歧义身份（无时间/会话/事件 id）   DETECTED_UNSUPPORTED，不入账
  L. 缓存：候选根内新增数据文件        必须触发重新发现

隔离：全部在 _safety 沙盒子进程（USERPROFILE/APPDATA/LOCALAPPDATA
指向沙盒）——绝不触碰 repo dev ledger 与 desktop production ledger。
"""
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
    print('@@JSON@@' + json.dumps(rep, ensure_ascii=False))
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
            counts['usage_total'] = conn.execute(
                'SELECT COUNT(*) FROM usage_event').fetchone()[0]
            counts['by_client'] = conn.execute(
                'SELECT client, COUNT(*) FROM usage_event GROUP BY client'
            ).fetchall()
        finally:
            conn.close()
    counts['stats'] = dict(ledger.LAST_SCAN_STATS)
    print('@@JSON@@' + json.dumps(counts, ensure_ascii=False))
'''


def _tool_by_id(rep, tool_id):
    for t in rep['tools']:
        if t['tool_id'] == tool_id:
            return t
    return None


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-identity-')
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

    ANCHORS = ('epsilon-ai', 'zetab-ai', 'etab-ai', 'kappa-ai', 'pid-ai',
               'iota-ai')

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
                # 点前缀是另一种命名形态：严格锚下必须显式声明别名
                'install_aliases': ['.epsilon-ai']
                if name == 'epsilon-ai' else [],
                'data_path_hints': [], 'file_patterns': ['*.jsonl'],
                'fingerprints': {'kind': 'jsonl', 'keys': ['type']},
                'capability': 'TOKEN',
                'adapter': 'generic-sqlite-openai-usage'
                if name != 'kappa-ai' else 'generic-jsonl-openai-usage',
                'confidence_basis': 'test_anchor', 'platform': 'win32',
                'version': 1})
        p = os.path.join(self.tmp, 'catalog-anchored.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(cat, f, ensure_ascii=False, indent=2)
        self._cat_path = p
        return p

    def engine(self, full=True):
        env = dict(self.env)
        env['DISC_MODE'] = 'discover'
        env['DISC_FULL'] = '1' if full else '0'
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
    def make_usage_db(self, path, start, end, model='m1',
                      cache_cols=('cache_read_input_tokens',)):
        """events start..end-1：同一 token tuple（除 cache 交替），ts 递增
        —— 每个事件都有唯一的稳定区分字段（timestamp）。"""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        extra = ''
        for c in cache_cols:
            extra += ', %s INTEGER' % c
        conn = sqlite3.connect(path)
        conn.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, '
                     'model TEXT, prompt_tokens INTEGER, '
                     'completion_tokens INTEGER, created_at INTEGER%s)' % extra)
        for i in range(start, end):
            cached = (i % 3) * 10
            row = [i, 'm1', 100, 20, 1760000000000 + i * 1000]
            if 'cache_read_input_tokens' in cache_cols:
                row.append(cached)
            conn.execute('INSERT INTO requests VALUES (%s)'
                         % ','.join('?' * len(row)), row)
        conn.commit()
        conn.close()
        return path

    def count(self, res, client):
        for c, n in res.get('by_client') or []:
            if c == client:
                return n
        return 0


class TestSqliteIdentity(_Sandbox):

    def _setup(self, name='epsilon-ai'):
        root = os.path.join(self.appdata, name)
        self.make_usage_db(os.path.join(root, 'live', 'history.db'), 0, 100)
        self.engine(full=True)
        return root

    def test_a_rescan_idempotent(self):
        self._setup()
        r1 = self.scan()
        self.assertEqual(self.count(r1, 'epsilon-ai'), 100)
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_b_vacuum_no_duplicates(self):
        root = self._setup()
        self.scan()
        db = os.path.join(root, 'live', 'history.db')
        conn = sqlite3.connect(db)
        conn.execute('VACUUM')
        conn.close()
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_c_table_rebuild_different_rowids(self):
        root = self._setup()
        self.scan()
        db = os.path.join(root, 'live', 'history.db')
        # 重建：以不同顺序重新插入（rowid 全变），内容不变
        conn = sqlite3.connect(db)
        rows = conn.execute(
            'SELECT model, prompt_tokens, completion_tokens, created_at,'
            ' cache_read_input_tokens FROM requests').fetchall()
        conn.execute('DROP TABLE requests')
        conn.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, '
                     'model TEXT, prompt_tokens INTEGER, '
                     'completion_tokens INTEGER, created_at INTEGER, '
                     'cache_read_input_tokens INTEGER)')
        for row in reversed(rows):               # rowid 顺序完全颠倒
            conn.execute('INSERT INTO requests VALUES (NULL,?,?,?,?,?)', row)
        conn.commit()
        new_rowids = [r[0] for r in conn.execute('SELECT id FROM requests')]
        conn.close()
        self.assertEqual(new_rowids, sorted(new_rowids, reverse=True)[:0]
                         or list(range(1, 101)))  # 新 rowid = 1..100 按倒序内容
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_d_rename_move_file(self):
        root = self._setup()
        self.scan()
        old = os.path.join(root, 'live', 'history.db')
        new = os.path.join(root, 'live', 'renamed.db')
        shutil.move(old, new)
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_e_full_copy_second_location(self):
        root = self._setup()
        self.scan()
        # 完整副本：根内 backup/ 子目录（递归发现范围内）
        shutil.copytree(os.path.join(root, 'live'),
                        os.path.join(root, 'backup'))
        self.engine(full=True)      # 内容签名变化 → 重新发现
        r1 = self.scan()            # 副本首次被扫：跨文件重复观察留痕
        self.assertEqual(self.count(r1, 'epsilon-ai'), 100)
        self.assertEqual(r1['stats']['events_inserted'], 0)
        self.assertGreater(r1['stats']['events_reobserved_cross_file'], 0)
        r2 = self.scan()            # 再扫：稳定，无新增
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_e2_same_slug_second_root(self):
        # 同名工具出现在第二个候选根（home dotdir）：多位置注册，事件不重复
        root = self._setup()
        self.scan()
        self.make_usage_db(os.path.join(self.home, '.epsilon-ai',
                                        'history.db'), 0, 100)
        rep = self.engine(full=True)
        t = _tool_by_id(rep, 'epsilon-ai')
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'GENERIC_SUPPORTED')
        self.assertGreaterEqual(t['data_sources_count'], 2)
        reg = json.load(open(os.path.join(self.tmp, 'generic-sources.json'),
                             encoding='utf-8'))
        self.assertEqual(len(reg['epsilon-ai']['paths']), 2)
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'epsilon-ai'), 100)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_f_partial_overlap_union(self):
        # live = 1..100，backup = 51..150（部分重叠）→ 逻辑事件并集 150
        root = os.path.join(self.appdata, 'zetab-ai')
        self.make_usage_db(os.path.join(root, 'live', 'history.db'), 0, 100)
        self.make_usage_db(os.path.join(root, 'backup', 'history.db'),
                           50, 150)
        self.engine(full=True)
        res = self.scan()
        self.assertEqual(self.count(res, 'zetab-ai'), 150)
        self.assertEqual(res['usage_total'], 150)
        # 再扫一遍：不新增
        res2 = self.scan()
        self.assertEqual(self.count(res2, 'zetab-ai'), 150)
        self.assertEqual(res2['stats']['events_inserted'], 0)

    def test_g_distinct_events_same_token_tuple(self):
        # 同 model / 同 token 五元组、不同 timestamp = 两个真实事件，不得合并
        root = os.path.join(self.appdata, 'etab-ai')
        p = os.path.join(root, 'history.db')
        os.makedirs(root, exist_ok=True)
        conn = sqlite3.connect(p)
        conn.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, '
                     'model TEXT, prompt_tokens INTEGER, '
                     'completion_tokens INTEGER, created_at INTEGER)')
        conn.executemany('INSERT INTO requests VALUES (NULL,?,?,?,?)', [
            ('m1', 100, 20, 1760000000000),
            ('m1', 100, 20, 1760000000001),      # 只有时间不同
            ('m1', 100, 20, 1760000000002),
        ])
        conn.commit()
        conn.close()
        self.engine(full=True)
        res = self.scan()
        self.assertEqual(self.count(res, 'etab-ai'), 3)
        # 对照契约：连稳定时间都相同的同 tuple 记录 = 同一逻辑事件（合并）
        conn = sqlite3.connect(p)
        conn.execute("INSERT INTO requests VALUES (NULL,'m1',100,20,1760000000000)")
        conn.commit()
        conn.close()
        res2 = self.scan()
        self.assertEqual(self.count(res2, 'etab-ai'), 3)
        self.assertEqual(res2['stats']['events_inserted'], 0)


class TestJsonlIdentity(_Sandbox):

    RECORD = {'model': 'gpt-x', 'timestamp': 1760000000,
              'session_id': 'sess-1', 'prompt_tokens': 100,
              'completion_tokens': 20,
              'prompt_tokens_details': {'cached_tokens': 30}}

    def _setup(self, name='kappa-ai'):
        root = os.path.join(self.appdata, name, 'logs')
        os.makedirs(root, exist_ok=True)
        self._write(os.path.join(root, 'usage.jsonl'), [self.RECORD])
        self.engine(full=True)
        return root

    @staticmethod
    def _write(path, records):
        with open(path, 'w', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(rec) + '\n')

    def test_h_copy_to_second_file(self):
        root = self._setup()
        self.scan()
        shutil.copy(os.path.join(root, 'usage.jsonl'),
                    os.path.join(root, 'usage-copy.jsonl'))
        self.engine(full=True)
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'kappa-ai'), 1)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_i_reorder_and_re_serialize(self):
        root = self._setup()
        records = []
        for i in range(5):
            rec = dict(self.RECORD)
            rec['timestamp'] = 1760000000 + i
            rec['session_id'] = 'sess-%d' % i
            records.append(rec)
        self._write(os.path.join(root, 'usage.jsonl'), records)
        self.engine(full=True)
        r1 = self.scan()
        self.assertEqual(self.count(r1, 'kappa-ai'), 5)
        # 重排 + 重序列化（不同键序/多余空白）：内容等价 → 身份必须不变
        shuffled = list(reversed(records))
        with open(os.path.join(root, 'usage.jsonl'), 'w',
                  encoding='utf-8') as f:
            for rec in shuffled:
                f.write(json.dumps(dict(reversed(list(rec.items()))),
                                   indent=None, separators=(', ', ': '))
                        + '\n')
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'kappa-ai'), 5)
        self.assertEqual(r2['stats']['events_inserted'], 0)

    def test_j_append_new_events_only(self):
        root = self._setup()
        self.scan()
        with open(os.path.join(root, 'usage.jsonl'), 'a',
                  encoding='utf-8') as f:
            f.write(json.dumps({**self.RECORD, 'timestamp': 1760009000,
                                'session_id': 'sess-new'}) + '\n')
        self.engine(full=True)      # 内容签名变化 → 重新发现
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'kappa-ai'), 2)
        self.assertEqual(r2['stats']['events_inserted'], 1)


class TestAmbiguousIdentity(_Sandbox):

    def test_jsonl_no_timestamp_no_ingest(self):
        # 只有 model + prompt/completion：无时间/会话/事件 id → 身份不足
        root = os.path.join(self.appdata, 'nokey-ai', 'logs')
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, 'usage.jsonl'), 'w',
                  encoding='utf-8') as f:
            f.write(json.dumps({'model': 'm', 'prompt_tokens': 10,
                                'completion_tokens': 2}) + '\n')
        rep = self.engine(full=True)
        t = next((x for x in rep['tools'] if x['tool_id'] == 'nokey-ai'), None)
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'DETECTED_UNSUPPORTED')
        self.assertIn('事件身份', t['reason'])
        res = self.scan()
        self.assertEqual(self.count(res, 'nokey-ai'), 0)

    def test_sqlite_no_timestamp_no_ingest(self):
        root = os.path.join(self.appdata, 'nots-ai', 'data')
        os.makedirs(root, exist_ok=True)
        conn = sqlite3.connect(os.path.join(root, 'history.db'))
        conn.execute('CREATE TABLE requests (id INTEGER PRIMARY KEY, '
                     'model TEXT, prompt_tokens INTEGER, '
                     'completion_tokens INTEGER)')
        conn.execute("INSERT INTO requests VALUES (NULL,'m',10,2)")
        conn.commit()
        conn.close()
        rep = self.engine(full=True)
        t = next((x for x in rep['tools'] if x['tool_id'] == 'nots-ai'), None)
        self.assertIsNotNone(t)
        self.assertEqual(t['status'], 'DETECTED_UNSUPPORTED')
        self.assertIn('事件身份', t['reason'])
        res = self.scan()
        self.assertEqual(self.count(res, 'nots-ai'), 0)

    def test_sqlite_provider_id_preferred(self):
        # 有 provider 稳定 id 列：身份走 pid；行序/rowid 变化仍不重复
        root = os.path.join(self.appdata, 'pid-ai', 'data')
        os.makedirs(root, exist_ok=True)
        p = os.path.join(root, 'history.db')
        conn = sqlite3.connect(p)
        conn.execute('CREATE TABLE usage (request_id TEXT, model TEXT, '
                     'prompt_tokens INTEGER, completion_tokens INTEGER, '
                     'created_at INTEGER)')
        for i in range(10):
            conn.execute('INSERT INTO usage VALUES (?,?,?,?,?)',
                         ('req-%03d' % i, 'm1', 100, 20,
                          1760000000000 + i))
        conn.commit()
        conn.close()
        self.engine(full=True)
        self.scan()
        # 重建：乱序重插（rowid 乱掉），request_id 不变
        conn = sqlite3.connect(p)
        rows = conn.execute('SELECT * FROM usage').fetchall()
        conn.execute('DROP TABLE usage')
        conn.execute('CREATE TABLE usage (request_id TEXT, model TEXT, '
                     'prompt_tokens INTEGER, completion_tokens INTEGER, '
                     'created_at INTEGER)')
        for row in reversed(rows):
            conn.execute('INSERT INTO usage VALUES (?,?,?,?,?)', row)
        conn.commit()
        conn.close()
        r2 = self.scan()
        self.assertEqual(self.count(r2, 'pid-ai'), 10)
        self.assertEqual(r2['stats']['events_inserted'], 0)


class TestCacheInvalidation(_Sandbox):

    def test_new_data_file_inside_root_triggers_rediscovery(self):
        # 昨天：只有 live.db。今天：根内新增 backup.db。
        # cached discovery 的内容签名必须捕捉变化（不做全盘内容 hash）。
        root = os.path.join(self.appdata, 'iota-ai')
        self.make_usage_db(os.path.join(root, 'live', 'history.db'), 0, 10)
        self.engine(full=True)
        cached = self.engine(full=False)
        self.assertTrue(cached['cached'])
        self.scan()
        # 今天新增 backup 副本
        self.make_usage_db(os.path.join(root, 'backup', 'history.db'),
                           0, 10)
        cached2 = self.engine(full=False)
        self.assertFalse(cached2['cached'],
                         '候选根内容签名变化必须触发重新发现')
        r = self.scan()
        self.assertEqual(self.count(r, 'iota-ai'), 10)


if __name__ == '__main__':
    unittest.main()
