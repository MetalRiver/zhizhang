# -*- coding: utf-8 -*-
"""V1.1 Phase1 批次 1 回归：发现状态修复。

覆盖三项已确认缺陷（见 V1.1 体检报告 B1/B3 与「重新发现错误反馈」）：
  1. frozen app_version 误报：BASE 无 VERSION 时必须回退读打包资源根，
     否则缓存失效的版本腿断掉、报告版本恒为 unknown；
  2. 未知来源过滤：候选根里存在**结构化数据存储**（可读 sqlite/jsonl）
     但结构不认识时，必须留下 UNKNOWN（待识别）记录，不得静默吞掉；
     只有非结构化杂项文件（.log 等）仍是噪音、继续丢弃；
  3. POST /api/v1/discover/refresh 失败时，message 必须带脱敏后的具体
     原因（无本机绝对路径），而不是只有异常类型名。

隔离原则：与 test_discovery.py 相同 —— 子进程沙盒（_safety fail-closed），
假 home/appdata 全部指向沙盒，绝不触碰 production 账本。
"""
import json
import os
import re
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
mode = os.environ.get('V11_MODE', 'discover')
if mode == 'discover':
    import discovery
    _cp = os.environ.get('V11_CATALOG')
    _cat = json.load(open(_cp, encoding='utf-8')) if _cp else None
    rep = discovery.run_discovery(full=True, state_dir=os.getcwd(), catalog=_cat)
    pub = discovery.public_payload(rep)
    print('@@JSON@@' + json.dumps(
        {'report': rep, 'public': pub}, ensure_ascii=False))
elif mode == 'serve-refresh':
    import threading, socket, urllib.request, urllib.error
    import serve
    serve.HERE = os.getcwd()
    serve.WEB_DIR = os.path.join(os.getcwd(), 'web')
    os.makedirs(serve.WEB_DIR, exist_ok=True)
    s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
    httpd = serve.make_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = 'http://127.0.0.1:%d' % port
    data = json.dumps({}).encode('utf-8')
    req = urllib.request.Request(
        base + '/api/v1/discover/refresh', data=data, method='POST',
        headers={'Content-Type': 'application/json',
                 'Origin': 'http://127.0.0.1:%d' % port})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = {'status': r.status, 'body': json.loads(r.read().decode('utf-8'))}
    except urllib.error.HTTPError as e:
        out = {'status': e.code, 'body': json.loads(e.read().decode('utf-8'))}
    print('@@JSON@@' + json.dumps(out, ensure_ascii=False))
'''


def _run(cwd, env, timeout=180):
    r = subprocess.run([PY, '-c', _RUNNER], cwd=cwd, env=env,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout)
    if r.returncode != 0:
        raise AssertionError('runner 失败：%s\n%s' % (r.stderr[-800:], r.stdout[-400:]))
    line = [l for l in r.stdout.splitlines() if l.startswith('@@JSON@@')][-1]
    return json.loads(line[len('@@JSON@@'):])


def _make_sqlite(path, table_sql):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(table_sql)
        conn.execute('INSERT INTO automations (id, name) VALUES (1, "demo")'
                     if 'automations' in table_sql else table_sql)
        conn.commit()
    finally:
        conn.close()


class DiscoveryV11Sandbox(unittest.TestCase):
    """子进程沙盒：假 home/appdata + 仓库源码副本。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-disc-')
        self.home = os.path.join(self.tmp, 'home')
        self.appdata = os.path.join(self.tmp, 'appdata')
        self.appdata_local = os.path.join(self.tmp, 'appdata-local')
        for d in (self.home, self.appdata, self.appdata_local):
            os.makedirs(d)
        os.makedirs(os.path.join(self.tmp, 'web', 'v1'), exist_ok=True)
        for f in ('ledger.py', 'discovery.py', 'paths.py', 'autopilot.py',
                  'serve.py', 'source-catalog.json', 'VERSION'):
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

    # ---- B3：结构化但读不出 → 必须可见（UNKNOWN 待识别） ----

    def test_structured_unknown_store_visible(self):
        # 模拟本机实测形态：Roaming\CodeBuddy CN\automations\automations.db
        # （有效 sqlite、非 OpenAI 用量 schema —— 此前被噪音过滤整条吞掉）
        _make_sqlite(
            os.path.join(self.appdata, 'CodeBuddy CN', 'automations',
                         'automations.db'),
            'CREATE TABLE automations (id INTEGER PRIMARY KEY, name TEXT)')
        out = _run(self.tmp, dict(self.env, V11_MODE='discover'))
        tools = {t['tool_id']: t for t in out['public']['tools']}
        self.assertIn('codebuddy-cn', tools,
                      '可读 sqlite 存储不得被静默丢弃')
        t = tools['codebuddy-cn']
        self.assertEqual(t['status'], 'UNKNOWN')
        self.assertTrue(t['needs_attention'])
        self.assertGreaterEqual(t['data_sources_count'], 1)
        self.assertIn('codebuddy-cn', out['public']['groups']['unknown'])
        self.assertIsNone(t['adapter'], 'UNKNOWN 绝不自动入账')
        # 隐私契约不因可见性放宽：不出现本机绝对路径
        self.assertNotIn(self.tmp, json.dumps(out['public'], ensure_ascii=False))

    def test_non_structured_noise_still_dropped(self):
        # 只有 *.log（interesting_name 命中但非结构化）→ 仍是噪音，不得登记
        d = os.path.join(self.appdata, 'chat-notes')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'history.log'), 'w', encoding='utf-8') as f:
            f.write('not a database\n')
        out = _run(self.tmp, dict(self.env, V11_MODE='discover'))
        ids = [t['tool_id'] for t in out['public']['tools']]
        self.assertNotIn('chat-notes', ids)

    def test_unknown_visible_not_ingested(self):
        # 可见 ≠ 入账：UNKNOWN 工具不得注册成可采集来源（generic-sources）
        _make_sqlite(
            os.path.join(self.appdata, 'CodeBuddy CN', 'automations',
                         'automations.db'),
            'CREATE TABLE automations (id INTEGER PRIMARY KEY, name TEXT)')
        _run(self.tmp, dict(self.env, V11_MODE='discover'))
        self.assertFalse(
            os.path.isfile(os.path.join(self.tmp, 'generic-sources.json')),
            'UNKNOWN 工具不得写入 generic-sources.json')

    # ---- 重新发现错误反馈：脱敏 + 具体原因 ----

    def test_rediscover_error_feedback_sanitized(self):
        # 破坏 catalog（模拟打包资源缺失）→ 500 + 具体原因 + 无本机路径
        os.remove(os.path.join(self.tmp, 'source-catalog.json'))
        out = _run(self.tmp, dict(self.env, V11_MODE='serve-refresh'))
        self.assertEqual(out['status'], 500)
        body = out['body']
        self.assertFalse(body.get('ok'))
        self.assertEqual(body.get('error'), 'discover_failed')
        self.assertIn('重新发现失败', body.get('message', ''))
        self.assertIn('FileNotFoundError', body.get('message', ''))
        # 脱敏：message 里不得出现 Windows 绝对路径（盘符:\ 形态）
        self.assertIsNone(
            re.search(r'[A-Za-z]:\\', body.get('message', '')),
            '错误信息不得携带本机绝对路径：%r' % body.get('message'))


class AppVersionFallback(unittest.TestCase):
    """B1：frozen 版本号回退读打包资源根（进程内单测，不触任何数据）。"""

    def setUp(self):
        import discovery
        import paths
        self.disc = discovery
        self.paths = paths
        self._old_base = discovery.BASE
        self._old_res = paths.application_resource_path
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-ver-')

    def tearDown(self):
        self.disc.BASE = self._old_base
        self.paths.application_resource_path = self._old_res
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_version(self, d, ver):
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'VERSION'), 'w', encoding='utf-8') as f:
            f.write('version          %s\n' % ver)

    def test_reads_bundled_resource_when_base_lacks_version(self):
        # frozen 形态：BASE（=DATA_ROOT）没有 VERSION，打包资源根有
        self.disc.BASE = self.tmp                       # 无 VERSION
        bundled = os.path.join(self.tmp, '_internal')
        self._write_version(bundled, '9.9.9')
        self.paths.application_resource_path = (
            lambda name: os.path.join(bundled, name))
        self.assertEqual(self.disc._app_version(), '9.9.9',
                         'frozen 下必须读到打包 VERSION，而不是 unknown')

    def test_base_version_still_wins_in_dev(self):
        # dev 形态：BASE（仓库根）就有 VERSION —— 优先，行为不变
        self._write_version(self.tmp, '3.3.3')
        self.disc.BASE = self.tmp
        self.paths.application_resource_path = (
            lambda name: os.path.join(self.tmp, 'nonexistent', name))
        self.assertEqual(self.disc._app_version(), '3.3.3')

    def test_unknown_when_nowhere(self):
        self.disc.BASE = self.tmp
        self.paths.application_resource_path = (
            lambda name: os.path.join(self.tmp, 'nonexistent', name))
        self.assertEqual(self.disc._app_version(), 'unknown')


if __name__ == '__main__':
    unittest.main()
