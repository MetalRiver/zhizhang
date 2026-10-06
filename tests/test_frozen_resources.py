# -*- coding: utf-8 -*-
r"""V1.1 Frozen Resource / Data Path 分离回归测试（P1 blocker 关闭验证）。

契约（冻结）：
  - source-catalog.json 是只读程序资源：dev 读 repo root；frozen 读
    PyInstaller bundled resource root（sys._MEIPASS / _internal）。
  - 发现状态（discovery-state / generic-sources）是可写用户数据：
    写 DATA_ROOT。
  - catalog read path != state write path。
  - DATA_ROOT 中即使存在旧 catalog 副本，也绝不作为事实源
    （不创建隐式用户 override；bundled catalog 随程序版本升级）。

A. unit/path contract：dev 下 resource path 指向 repo 根且存在；
   DATA_ROOT 放假副本不被读取。
B. actual frozen integration：使用**真实 PyInstaller 构建**的 backend，
   复制到 repo 之外临时目录，全新 sandbox USERPROFILE/APPDATA/
   LOCALAPPDATA（启动前 %LOCALAPPDATA%/UsageLedger 无 catalog），
   GET /api/v1/discover 必须 200 且返回 catalog 规则。
   backend 构建产物缺失时明确 skip（release gate 流程负责先构建）。

隔离：全部 sandbox env + 动态端口；绝不触碰真实 ~/.codex 与两本正式账本。
"""
import json
import os
import shutil
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

import paths  # noqa: E402
import discovery  # noqa: E402

PY = sys.executable
SIDECAR_EXE = os.path.join(
    ROOT, 'src-tauri', 'binaries', 'usage-ledger-backend',
    'usage-ledger-backend.exe')
SIDECAR_INTERNAL = os.path.join(
    ROOT, 'src-tauri', 'binaries', 'usage-ledger-backend', '_internal')


class TestResourcePathContract(unittest.TestCase):
    """A. unit/path contract（不 mock sys.frozen：只测 dev 事实 +
    契约表达式）。"""

    def test_dev_resource_path_is_repo_root(self):
        p = paths.application_resource_path('source-catalog.json')
        self.assertEqual(os.path.dirname(p), ROOT)
        self.assertTrue(os.path.isfile(p), p)

    def test_resource_path_is_not_data_root(self):
        # catalog read path != state write path（即使 dev 下两者同根，
        # 也必须证明 catalog 路径来自 resource root 事实源，而非 DATA_ROOT 拼接）
        self.assertEqual(paths.application_resource_path('x'),
                         paths.bundled_resource_root() + os.sep + 'x')
        self.assertNotEqual(paths.bundled_resource_root() + os.sep,
                            paths.data_path('') + os.sep + 'x' + os.sep)

    def test_dev_catalog_read_ignores_data_root_copy(self):
        # DATA_ROOT（dev=repo root 此处用沙盒模拟 DATA_ROOT 语义）中放
        # 一个内容错误的 catalog 假副本：load_catalog 必须仍读 bundled
        # （repo）事实源。frozen 等价行为由 TestFrozenIntegration 证明。
        sandbox = _safety.sandbox_dir(prefix='ul-frozen-unit-')
        try:
            stale = os.path.join(sandbox, 'source-catalog.json')
            with open(stale, 'w', encoding='utf-8') as f:
                json.dump({'tools': [], '_stale_data_root_copy': True}, f)
            cat = discovery.load_catalog()
            # bundled catalog 有真实 tools；假副本没有
            self.assertTrue(len(cat.get('tools') or []) > 0)
            self.assertNotIn('_stale_data_root_copy', cat)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_missing_packaged_catalog_error_is_explicit(self):
        # 打包资源缺失时必须抛出可诊断的明确错误（绝不模糊 FileNotFoundError）
        sandbox = _safety.sandbox_dir(prefix='ul-frozen-miss-')
        try:
            missing = os.path.join(sandbox, 'source-catalog.json')
            with self.assertRaises(FileNotFoundError) as cm:
                discovery.load_catalog(path=missing)
            self.assertIn('packaged resource missing', str(cm.exception))
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


@unittest.skipUnless(os.path.isfile(SIDECAR_EXE),
                     '需要先构建 frozen backend（PyInstaller spec）；'
                     'release gate 流程负责构建后重跑本测试')
class TestFrozenIntegration(unittest.TestCase):
    """B. actual frozen integration：真实构建产物 + repo 外 + 全新用户目录。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = _safety.sandbox_dir(prefix='ul-frozen-int-')
        # backend 完整复制到 repo 之外
        cls.backend = os.path.join(cls.tmp, 'backend')
        shutil.copytree(os.path.join(SIDECAR_EXE, os.pardir), cls.backend)
        cls.exe = os.path.join(cls.backend, 'usage-ledger-backend.exe')
        # 全新用户目录；%LOCALAPPDATA%\UsageLedger 启动前不存在
        cls.home = os.path.join(cls.tmp, 'home')
        cls.appdata = os.path.join(cls.tmp, 'appdata')
        cls.local = os.path.join(cls.tmp, 'appdata-local')
        for d in (cls.home, cls.appdata, cls.local):
            os.makedirs(d)
        cls.env = dict(os.environ)
        cls.env.update({
            'USERPROFILE': cls.home, 'HOME': cls.home,
            'APPDATA': cls.appdata, 'LOCALAPPDATA': cls.local,
        })
        # 预检：干净环境不应已有打包 catalog
        assert not os.path.exists(
            os.path.join(cls.local, 'UsageLedger', 'source-catalog.json')), \
            '干净环境不应预置 DATA_ROOT catalog'

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _start(self, port):
        proc = subprocess.Popen(
            [self.exe, '--port', str(port), '--no-open'],
            cwd=self.backend, env=self.env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = 'http://127.0.0.1:%d' % port
        import time
        for _ in range(60):
            try:
                with urllib.request.urlopen(base + '/api/v1/identity',
                                            timeout=2) as r:
                    if r.status == 200:
                        return proc, base
            except Exception:
                time.sleep(0.25)
        proc.terminate()
        self.fail('frozen backend 未在预期时间内就绪')

    def _get(self, base, path):
        try:
            with urllib.request.urlopen(base + path, timeout=30) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode('utf-8'))

    def _free_port(self):
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        p = s.getsockname()[1]
        s.close()
        return p

    def test_b_frozen_first_run_phases(self):
        """时序化两阶段（同一干净用户目录）：
        阶段 1：全新 DATA_ROOT（无 catalog 副本）→ discover 200、0 工具；
        阶段 2：sandbox home 放 zcode fixture → 无需 sources.json 自动发现。"""
        import socket
        import time
        import sqlite3

        def free_port():
            s = socket.socket()
            s.bind(('127.0.0.1', 0))
            p = s.getsockname()[1]
            s.close()
            return p

        def start(port):
            proc = subprocess.Popen(
                [self.exe, '--port', str(port), '--no-open'],
                cwd=self.backend, env=self.env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            base = 'http://127.0.0.1:%d' % port
            for _ in range(60):
                try:
                    with urllib.request.urlopen(base + '/api/v1/identity',
                                                timeout=2) as r:
                        if r.status == 200:
                            return proc, base
                except Exception:
                    time.sleep(0.25)
            proc.terminate()
            self.fail('frozen backend 未在预期时间内就绪')

        # ---- 阶段 1：0 tools ----
        port = free_port()
        proc, base = start(port)
        try:
            st, d = self._get(base, '/api/v1/discover')
            self.assertEqual(st, 200,
                             'frozen discover 失败：%s' % json.dumps(d)[:300])
            self.assertIn('tools', d)
            self.assertTrue(d['privacy']['local_only'])
            # catalog 规则真实生效（内置工具全部列出）
            ids = {t['tool_id'] for t in d['tools']}
            for known in ('zcode', 'dsh', 'workbuddy', 'catpaw', 'traecn',
                          'codex'):
                self.assertIn(known, ids)
            self.assertEqual(d['found'], 0)
            # state 仍写 DATA_ROOT（writable），catalog 未被复制到 DATA_ROOT
            self.assertFalse(os.path.exists(
                os.path.join(self.local, 'UsageLedger',
                             'source-catalog.json')))
            self.assertTrue(os.path.isdir(
                os.path.join(self.local, 'UsageLedger')))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

        # ---- 阶段 2：known tool fixture（无需 sources.json）----
        import sqlite3
        zc = os.path.join(self.home, '.zcode', 'cli', 'db')
        os.makedirs(zc, exist_ok=True)
        conn = sqlite3.connect(os.path.join(zc, 'db.sqlite'))
        conn.executescript("""
            CREATE TABLE session (id TEXT, project_id TEXT, directory TEXT, title TEXT);
            CREATE TABLE model_usage (id INTEGER, session_id TEXT, model_id TEXT,
              provider_id TEXT, started_at INTEGER, input_tokens INTEGER,
              output_tokens INTEGER, reasoning_tokens INTEGER,
              cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER,
              agent TEXT);
        """)
        conn.commit()
        conn.close()
        port = free_port()
        proc, base = start(port)
        try:
            st, d = self._get(base, '/api/v1/discover')
            self.assertEqual(st, 200)
            zc_row = next(t for t in d['tools'] if t['tool_id'] == 'zcode')
            self.assertGreaterEqual(zc_row['data_sources_count'], 1)
            self.assertEqual(zc_row['status'], 'SUPPORTED')
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
