# -*- coding: utf-8 -*-
"""test_frozen_contract.py — frozen 路径契约验证（RC.7-r6 沙盒实测根因回归）。

根因：frozen sidecar 中 autopilot 的 HERE = PyInstaller _internal（程序
目录），run-state/health/auto.log 写进程序目录，而 serve/前端从
paths.DATA_ROOT 读 → status=None → 首次引导「建立空账本」被误判失败；
独立 health='unknown' 与 board 内嵌 'failed' 分裂。

本测试用独立合成目录**模拟 frozen 环境**（sys.frozen/_MEIPASS/executable
三件套 + onedir 布局），做真实文件读写验证：

  A. 零工具无记录 → run-state/health/auto.log 必须落在 Data Root；
     serve.run_scan_once() 能读到 status='no_supported_sources'、
     sources_found=0；get_health() 返回 run 的 health（'failed'），而非
     'unknown' 回退；程序目录零残留。
  B. 四种预置模拟来源 → 发现 found≥4（前端契约字段 state='found'）→
     扫描入库 → query has_records=True。
  A2. 自定义 Data Root（USAGE_LEDGER_HOME）与默认 Data Root 行为一致。

隔离：全部路径位于 _safety.sandbox_dir 合成目录；不触碰真实账本。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
import urllib.request

import _safety

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

PY = sys.executable

_APP_FILES = ('ledger.py', 'discovery.py', 'paths.py', 'autopilot.py',
              'serve.py')
_INTERNAL_FILES = ('VERSION', 'source-catalog.json',
                   'usage-board.schema.json')

_RUNNER = r'''
import os, sys, json
sys.frozen = True                       # frozen 语义开关（必须在 import 前）
sys._MEIPASS = os.environ['ZZ_MEIPASS']
sys.executable = os.path.join(os.environ['ZZ_APP_DIR'], 'UsageLedger.exe')
sys.path.insert(0, sys._MEIPASS)
import serve, paths, ledger, autopilot

# 与 serve.main() frozen 分支同款：HERE 重指 Data Root + 运行态路径重绑
serve.HERE = paths.DATA_ROOT
serve.rebind_state_paths_to_data_root()

out = {}
# ---- 路径契约（门禁一：三种读取口径同源）----
out['paths_data_root'] = paths.DATA_ROOT
out['ledger_base'] = ledger.BASE
out['autopilot_here'] = autopilot.HERE
out['serve_server_state'] = serve.SERVER_STATE_PATH
dr = os.path.normcase(os.path.abspath(paths.DATA_ROOT))
out['contract_ok'] = (
    all(os.path.normcase(os.path.normpath(p)) == dr
        for p in (out['ledger_base'], out['autopilot_here']))
    and os.path.normcase(os.path.normpath(
        out['serve_server_state'])).startswith(dr))

# ---- 完整 scan（真实编排，非直呼 run_discovery）----
sys.argv = ['autopilot', 'auto', '--no-pricing', '--trigger', 'api']
code = 0
try:
    autopilot.main()
except SystemExit as e:
    code = e.code if isinstance(e.code, int) else 0
out['autopilot_exit'] = code

# ---- serve 侧读取（frozen 分支的真实取数路径）----
rs = serve._load_json(paths.run_state_path()) or {}
out['run_state_status'] = rs.get('status')
out['run_state_sources_found'] = rs.get('sources_found')
out['run_state_errors'] = (rs.get('errors') or [])[:5]
out['run_state_file_in_data_root'] = os.path.isfile(
    os.path.join(paths.DATA_ROOT, 'run-state.json'))
out['health_file_in_data_root'] = os.path.isfile(
    os.path.join(paths.DATA_ROOT, 'health.json'))
out['auto_log_in_data_root'] = os.path.isfile(
    os.path.join(paths.DATA_ROOT, 'auto.log'))
h = serve.get_health()
out['health_status'] = h.get('status') if h else None

# ---- 程序目录零残留 ----
stray = []
for root, dirs, files in os.walk(sys._MEIPASS):
    for f in files:
        if f in ('run-state.json', 'health.json', 'auto.log',
                 'server-state.json', 'auto-schedule.json'):
            stray.append(os.path.join(root, f))
out['stray_in_meipass'] = stray
# server-state 必须落 Data Root（rebind 生效）—— 真实写入一次
serve._write_state({'port': 0})
out['server_state_in_data_root'] = os.path.isfile(
    os.path.join(paths.DATA_ROOT, 'server-state.json'))

# ---- HTTP 层（与前端完全相同的读取口径）----
import socket, threading
s = socket.socket(); s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()
httpd = serve.make_server(port)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % port

def call(method, path, body=None):
    import urllib.request, urllib.error
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json',
                                          'Origin': base})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

out['http_discover'] = call('GET', '/api/v1/discover')
out['http_scan'] = call('POST', '/api/v1/scan', {})
out['http_query'] = call('GET', '/api/v1/query?range=all&audit=effective')
out['http_health'] = call('GET', '/api/v1/health')
print('@@JSON@@' + json.dumps(out, ensure_ascii=False))
'''


class FrozenContract(unittest.TestCase):
    """frozen 模拟环境下的完整 scan + serve 读取（真实文件 I/O）。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-frozen-')
        self.app = os.path.join(self.tmp, 'app')
        self.internal = os.path.join(self.app, '_internal')
        self.la = os.path.join(self.tmp, 'Local')
        os.makedirs(self.internal)
        os.makedirs(os.path.join(self.internal, 'web', 'v1'))
        os.makedirs(os.path.join(self.app, 'web', 'v1'))
        os.makedirs(self.la)
        # onedir 布局：程序模块与随包资源在 _internal；exe 在 app 根
        for f in _APP_FILES:
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.internal, f))
        for f in _INTERNAL_FILES:
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.internal, f))
        for rel in ('web/v1/shell.html',):
            shutil.copy(os.path.join(ROOT, rel), os.path.join(self.internal, rel))
            shutil.copy(os.path.join(ROOT, rel), os.path.join(self.app, rel))
        open(os.path.join(self.app, 'UsageLedger.exe'), 'wb').write(b'MZ')
        # 沙盒同构：sources.json + fixtures 位于 %LOCALAPPDATA% 布局
        self.fixtures = os.path.join(self.la, 'ZhiZhangFixtures')
        self.data_root = os.path.join(self.la, 'UsageLedger')
        os.makedirs(self.data_root, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 合成 fixtures（与 rc7-desk-kit 套件同源语义）----
    def _wb(self, n=3):
        d = os.path.join(self.fixtures, 'wb')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'sess-1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(n):
                f.write(json.dumps({
                    'id': 'wb-%d' % i, 'sessionId': 'sess-1',
                    'timestamp': 1787000000000 + i * 1000,
                    'type': 'function_call',
                    'providerData': {'model': 'glm-test-model',
                                     'rawUsage': {'prompt_tokens': 1200,
                                                  'completion_tokens': 300,
                                                  'prompt_tokens_details': {
                                                      'cached_tokens': 200}}}},
                    ensure_ascii=False) + '\n')

    def _cp(self):
        d = os.path.join(self.fixtures, 'cp')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'conv-1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(4):
                f.write(json.dumps({'messageId': 'cp-%d' % i,
                                    'conversationId': 'conv-1',
                                    'type': 'user' if i % 2 == 0 else 'assistant',
                                    'timestamp': '2026-08-01T10:0%d:00.000Z' % i})
                        + '\n')

    def _dsh(self):
        import zstandard
        d = os.path.join(self.fixtures, 'dsh-home', 'sessions', 'p', 's1')
        os.makedirs(d, exist_ok=True)
        lines = [
            json.dumps({'type': 'session', 'id': 'dsh-1',
                        'cwd': os.path.join(self.fixtures, 'dsh-proj').replace(os.sep, '/')}),
            json.dumps({'type': 'assistant/message', 'seq': 1,
                        'time': 1787000000000,
                        'data': {'usage': {'inputTokens': 400, 'outputTokens': 60,
                                           'reasoningTokens': 0,
                                           'cacheReadTokens': 0,
                                           'cacheWriteTokens': 0},
                                 'message': {'source': {
                                     'model': 'deepseek-chat',
                                     'provider': 'deepseek'}}}}),
        ]
        raw = ''.join(l + '\n' for l in lines).encode('utf-8')
        with open(os.path.join(d, 'session.jsonl.zstd'), 'wb') as f:
            f.write(zstandard.ZstdCompressor().compress(raw))

    def _codex(self):
        d = os.path.join(self.fixtures, 'codex')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'rollout-1.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'session_meta', 'payload': {
                'session_id': 'c-1',
                'cwd': os.path.join(self.fixtures, 'proj').replace(os.sep, '/')}})
                + '\n')
            f.write(json.dumps({'type': 'token_usage_record',
                                'timestamp': '2026-08-01T10:00:00.000Z',
                                'ordinal': 1,
                                'payload': {'response_id': 'r1',
                                            'usage': {'input_tokens': 900,
                                                      'output_tokens': 100}}})
                + '\n')

    def _sources_json(self, bom=False):
        with open(os.path.join(self.data_root, 'sources.json'), 'wb') as f:
            body = json.dumps({
                'zcode': os.path.join(self.fixtures, 'none-zcode.sqlite').replace(os.sep, '/'),
                'dsh': os.path.join(self.fixtures, 'dsh-home').replace(os.sep, '/'),
                'workbuddy': os.path.join(self.fixtures, 'wb').replace(os.sep, '/'),
                'catpaw': os.path.join(self.fixtures, 'cp').replace(os.sep, '/'),
                'traecn': os.path.join(self.fixtures, 'none-trae').replace(os.sep, '/'),
                'codex': os.path.join(self.fixtures, 'codex').replace(os.sep, '/'),
            }, ensure_ascii=False)
            if bom:
                body = '\ufeff' + body
            f.write(body.encode('utf-8'))

    def _run(self, scenario):
        env = dict(os.environ)
        env.update({
            'LOCALAPPDATA': self.la,
            'USERPROFILE': os.path.join(self.tmp, 'home'),
            'HOME': os.path.join(self.tmp, 'home'),
            'APPDATA': os.path.join(self.tmp, 'Roaming'),
            'ZZ_APP_DIR': self.app,
            'ZZ_MEIPASS': self.internal,
            'ZZ_SCENARIO': scenario,
        })
        for k in ('DSH_HOME', 'USAGE_LEDGER_HOME', 'USAGE_LEDGER_BACKEND_EXE'):
            env.pop(k, None)
        r = subprocess.run([PY, '-c', _RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=600)
        if r.returncode != 0:
            raise AssertionError('frozen-sim 失败：%s | %s'
                                 % (r.stderr[-800:], r.stdout[-400:]))
        line = [l for l in r.stdout.splitlines()
                if l.startswith('@@JSON@@')][-1]
        return json.loads(line[len('@@JSON@@'):])

    # ---- 门禁一.5：真实文件读写验证 ----

    def test_frozen_zero_tools_empty_ledger(self):
        """场景 A：零工具 → run-state/health/auto.log 落 Data Root；
        serve 读到 no_supported_sources / sources_found=0；
        get_health 返回 'failed'（真实 run 的 health，而非 unknown 回退）；
        程序目录零残留。"""
        out = self._run('A')
        self.assertTrue(out['contract_ok'], out)
        self.assertEqual(out['run_state_status'], 'no_supported_sources')
        self.assertEqual(out['run_state_sources_found'], 0)
        self.assertTrue(out['run_state_file_in_data_root'])
        self.assertTrue(out['health_file_in_data_root'])
        self.assertTrue(out['auto_log_in_data_root'])
        self.assertTrue(out['server_state_in_data_root'])
        self.assertEqual(out['health_status'], 'failed',
                         'serve.get_health 必须读到真实 run 的 health（failed），'
                         '而不是 Data Root 缺 health.json 时的 unknown 回退')
        self.assertEqual(out['stray_in_meipass'], [], out['stray_in_meipass'])
        # HTTP 契约（与前端一致）
        self.assertEqual(out['http_scan'][0], 200)
        self.assertEqual(out['http_scan'][1]['status'], 'no_supported_sources')
        self.assertEqual(out['http_scan'][1]['sources_found'], 0)
        self.assertFalse(out['http_query'][1]['ledger_state']['has_records'])

    def test_frozen_four_sources_discover_and_ingest(self):
        """场景 B：四种预置模拟来源 → 发现 found≥4（前端 state='found'
        契约）→ 扫描入库 → has_records=True。发现 0 个 = FAIL。"""
        self._wb()
        self._cp()
        self._dsh()
        self._codex()
        self._sources_json()
        out = self._run('B')
        self.assertTrue(out['contract_ok'], out)
        disc = out['http_discover']
        self.assertEqual(disc[0], 200)
        pub = disc[1]
        self.assertGreaterEqual(pub['found'], 4,
                                '四种模拟来源必须全部被发现：%s' % pub.get('found'))
        ui_found = sum(1 for s in pub['sources'] if s.get('state') == 'found')
        self.assertGreaterEqual(ui_found, 4,
                                '前端计数字段（sources[].state）必须正确：%s'
                                % [(s.get('client'), s.get('state'))
                                   for s in pub['sources']])
        for tid in ('workbuddy', 'catpaw', 'dsh', 'codex'):
            row = next(s for s in pub['sources'] if s.get('client') == tid)
            self.assertEqual(row.get('state'), 'found', tid)
        scan = out['http_scan']
        self.assertEqual(scan[0], 200)
        self.assertIn(scan[1]['status'], ('success', 'partial_failure'),
                      scan[1])
        self.assertGreaterEqual(scan[1]['sources_found'], 4)
        # HTTP 扫描是编排首扫之后的第二次增量扫描 → events_inserted=0
        # （幂等语义）；入库证据以 query 聚合为准（9 = wb3+codex1+dsh1+活动4）
        self.assertEqual(scan[1]['events_inserted'], 0, scan[1])
        self.assertTrue(out['http_query'][1]['ledger_state']['has_records'])
        self.assertGreaterEqual(out['http_query'][1]['aggregate']['events'], 9)
        # dsh 的 path_hint 必须是最终有效路径（覆盖生效），而非默认 ~/.dsh
        # 注意：path_hint 在 tools 行上（legacy sources 行不含该字段）
        dsh_tool = next(t for t in pub['tools'] if t.get('tool_id') == 'dsh')
        hint = dsh_tool.get('path_hint') or ''
        self.assertIn('dsh-home', hint, 'dsh path_hint 必须反映覆盖后的有效路径')
        self.assertNotIn('.dsh/', hint, 'path_hint 不得是默认 ~/.dsh 位置')

    def test_frozen_custom_data_root_same_behavior(self):
        """门禁一.3：自定义 Data Root（USAGE_LEDGER_HOME）与默认根行为一致。"""
        custom = os.path.join(self.tmp, 'custom-root')
        os.makedirs(custom, exist_ok=True)
        env_extra = {'USAGE_LEDGER_HOME': custom}
        out = self._run_custom('A2', custom)
        self.assertTrue(out['contract_ok'], out)
        self.assertEqual(os.path.normcase(out['paths_data_root']),
                         os.path.normcase(custom))
        self.assertEqual(out['run_state_status'], 'no_supported_sources')
        self.assertTrue(out['run_state_file_in_data_root'])
        self.assertFalse(os.path.isfile(
            os.path.join(self.la, 'UsageLedger', 'run-state.json')),
            '自定义根模式下默认根不得出现运行态文件')

    def _run_custom(self, scenario, custom):
        # 与 _run 相同，但额外注入 USAGE_LEDGER_HOME
        env_extra = {'USAGE_LEDGER_HOME': custom}
        return self._run_env(scenario, env_extra)

    def _run_env(self, scenario, extra):
        env = dict(os.environ)
        env.update({
            'LOCALAPPDATA': self.la,
            'USERPROFILE': os.path.join(self.tmp, 'home'),
            'HOME': os.path.join(self.tmp, 'home'),
            'APPDATA': os.path.join(self.tmp, 'Roaming'),
            'ZZ_APP_DIR': self.app,
            'ZZ_MEIPASS': self.internal,
            'ZZ_SCENARIO': scenario,
        })
        env.pop('DSH_HOME', None)
        env.update(extra)
        for k in ('USAGE_LEDGER_BACKEND_EXE',):
            env.pop(k, None)
        r = subprocess.run([PY, '-c', _RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=600)
        if r.returncode != 0:
            raise AssertionError('frozen-sim 失败：%s | %s'
                                 % (r.stderr[-800:], r.stdout[-400:]))
        line = [l for l in r.stdout.splitlines()
                if l.startswith('@@JSON@@')][-1]
        return json.loads(line[len('@@JSON@@'):])


if __name__ == '__main__':
    unittest.main(verbosity=2)
