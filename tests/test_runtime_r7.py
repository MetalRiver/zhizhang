#!/usr/bin/env python3
"""test_runtime_r7.py — Round 7 生产 Runtime / 跨进程锁 / 调度测试。

安全边界：
    - 涉及 scan 的用例全部在沙盒（临时目录 + 复制产物）运行
    - 不注册、不修改、不删除任何真实 Windows 计划任务
      （install-auto 命令构造用 monkeypatch 捕获，不真正执行）
    - 不强杀任何进程；stale 场景全部用自造的假 PID / 假状态文件
"""
from __future__ import annotations

import io
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import _safety  # 测试沙盒保险（fail-closed，禁写 production DATA_ROOT）
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import serve        # noqa: E402
import ledger       # noqa: E402
import autopilot    # noqa: E402
from test_replay_v1 import run  # noqa: E402


def dead_pid():
    """找一个几乎必然不存在的 PID（扫描自身 PID 附近但不等于）。"""
    me = os.getpid()
    return me + 4000000


def live_pid():
    return os.getpid()


class ScanLockBase(unittest.TestCase):
    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-lock-')
        self._old_path = ledger.SCAN_LOCK_PATH
        ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')

    def tearDown(self):
        ledger.SCAN_LOCK_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestScanLock(ScanLockBase):
    def test_atomic_acquire_and_release(self):
        h = ledger.acquire_scan_lock(trigger='test')
        self.assertFalse(h['reentrant'])
        with open(ledger.SCAN_LOCK_PATH, encoding='utf-8') as f:
            info = json.load(f)
        self.assertEqual(info['pid'], os.getpid())
        self.assertEqual(info['trigger'], 'test')
        ledger.release_scan_lock(h)
        self.assertFalse(os.path.exists(ledger.SCAN_LOCK_PATH))

    def test_second_writer_rejected(self):
        # 11/13) 持锁期间第二个 writer 被拒：用外部活进程（子进程）充当持有者
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(8)'])
        try:
            with open(ledger.SCAN_LOCK_PATH, 'w', encoding='utf-8') as f:
                json.dump({'pid': child.pid, 'trigger': 'first',
                           'started_at': '2026-01-01T00:00:00'}, f)
            with self.assertRaises(ledger.ScanLockBusy) as cm:
                ledger.acquire_scan_lock(trigger='second')
            self.assertEqual(cm.exception.info.get('trigger'), 'first')
            self.assertTrue(ledger.scan_lock_status()['locked'])
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_reentrant_same_pid(self):
        # serve→autopilot→cmd_scan 编排链：同 PID 重入安全
        h = ledger.acquire_scan_lock(trigger='api')
        h2 = ledger.acquire_scan_lock(trigger='api')
        self.assertTrue(h2['reentrant'])
        ledger.release_scan_lock(h)      # 重入句柄不释放
        self.assertTrue(os.path.exists(ledger.SCAN_LOCK_PATH))
        ledger.release_scan_lock(h2)     # 原始句柄释放
        self.assertFalse(os.path.exists(ledger.SCAN_LOCK_PATH))

    def test_stale_lock_recovered(self):
        # 14) PID 已死 → 原子自愈回收
        with open(ledger.SCAN_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump({'pid': dead_pid(), 'trigger': 'scheduled',
                       'started_at': '2020-01-01T00:00:00'}, f)
        h = ledger.acquire_scan_lock(trigger='recovery')
        self.assertFalse(h.get('reentrant'))
        ledger.release_scan_lock(h)

    def test_live_pid_lock_not_stolen(self):
        # 15) PID 存活的外部持有者 → 锁不被抢（宁可保守拒绝）
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(8)'])
        try:
            with open(ledger.SCAN_LOCK_PATH, 'w', encoding='utf-8') as f:
                json.dump({'pid': child.pid, 'trigger': 'scheduled',
                           'started_at': '2020-01-01T00:00:00'}, f)
            with self.assertRaises(ledger.ScanLockBusy):
                ledger.acquire_scan_lock(trigger='api')
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_access_denied_pid_conservative(self):
        # 无法判定时保守拒绝：用 PID=4（Windows System，OpenProcess 返回拒绝）
        if os.name != 'nt':
            self.skipTest('Windows 专用')
        with open(ledger.SCAN_LOCK_PATH, 'w', encoding='utf-8') as f:
            json.dump({'pid': 4, 'trigger': 'x', 'started_at': 'x'}, f)
        with self.assertRaises(ledger.ScanLockBusy):
            ledger.acquire_scan_lock(trigger='y')

    def test_cmd_scan_skips_when_locked(self):
        # 12) CLI/scheduled cmd_scan 在锁占用下明确跳过（partial），绝不静默。
        # 沙盒子进程（cwd=tmp），绝不触碰真实账本。
        for f in ('ledger.py', 'autopilot.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        old = ledger.SCAN_LOCK_PATH
        ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
        try:
            h = ledger.acquire_scan_lock(trigger='other')
            p = subprocess.run([sys.executable, 'ledger.py', 'scan'], cwd=self.tmp,
                               capture_output=True, text=True, encoding='utf-8',
                               errors='replace', timeout=120)
            self.assertEqual(p.returncode, ledger.EXIT_PARTIAL)
            self.assertIn('已有扫描正在进行', p.stdout)
            self.assertIn('本次跳过', p.stdout)
            ledger.release_scan_lock(h)
        finally:
            ledger.SCAN_LOCK_PATH = old

    def test_lock_file_no_paths(self):
        # 锁内容不含路径/用户数据
        ledger.acquire_scan_lock(trigger='x')
        raw = open(ledger.SCAN_LOCK_PATH, encoding='utf-8').read()
        self.assertNotIn(str(ROOT), raw)
        self.assertNotIn('dsh-home', raw)
        ledger.release_scan_lock(json.loads(raw) | {'reentrant': False})


class TestCrossProcessContention(ScanLockBase):
    def test_subprocess_scan_blocked_by_parent_lock(self):
        # 13) 跨进程争用：父进程持锁 → 子进程 ledger scan 明确跳过（沙盒）
        for f in ('ledger.py', 'autopilot.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
        h = ledger.acquire_scan_lock(trigger='parent')
        try:
            p = subprocess.run(
                [sys.executable, 'ledger.py', 'scan'], cwd=self.tmp,
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=120)
            self.assertEqual(p.returncode, ledger.EXIT_PARTIAL)
            self.assertIn('已有扫描正在进行', p.stdout + p.stderr)
        finally:
            ledger.release_scan_lock(h)


class TestServerRuntime(ScanLockBase):
    def setUp(self):
        super().setUp()
        self._old_here, self._old_web = serve.HERE, serve.WEB_DIR
        self._old_state, self._old_log = serve.SERVER_STATE_PATH, serve.SERVER_LOG_PATH
        serve.HERE = self.tmp
        serve.WEB_DIR = os.path.join(self.tmp, 'web')
        os.makedirs(os.path.join(serve.WEB_DIR, 'v1'), exist_ok=True)
        shutil.copy(os.path.join(ROOT, 'web', 'v1', 'shell.html'),
                    os.path.join(serve.WEB_DIR, 'v1', 'shell.html'))
        serve.SERVER_STATE_PATH = os.path.join(self.tmp, 'server-state.json')
        serve.SERVER_LOG_PATH = os.path.join(self.tmp, 'server.log')
        self.board = {'schema_version': 1, 'generated_at': '2026-09-26T10:00:00',
                      'privacy': {'paths_included': False,
                                  'event_ids_included': False},
                      'summary': {'usage_events': 1}, 'grades': {},
                      'window_days': 45, 'daily': [], 'clients': [],
                      'activity': [], 'models': [], 'pricing_gaps': [],
                      'sources': [], 'opaque_stores': []}
        with open(os.path.join(self.tmp, 'usage-board.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(self.board, f)

    def tearDown(self):
        serve.HERE, serve.WEB_DIR = self._old_here, self._old_web
        serve.SERVER_STATE_PATH, serve.SERVER_LOG_PATH = self._old_state, self._old_log
        serve.SCAN_STATE['running'] = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _server(self):
        httpd = serve.make_server(0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        return httpd, port

    def _get(self, port, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        conn.request('POST' if body is not None else 'GET',
                     path, body=body, headers=headers or {})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, data

    def test_preflight_free_and_stale_recovery(self):
        # 6) stale server-state 自愈：死 PID 状态文件被清理。
        # 端口必须真实空闲：本测试可能运行在装有本产品桌面版的机器上
        # （生产后端固定监听 8787）—— 绝不能对生产服务做探测/误判。
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        with open(serve.SERVER_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump({'pid': dead_pid(), 'port': port}, f)
        verdict, st = serve.preflight(port)
        self.assertEqual(verdict, 'free')
        self.assertFalse(os.path.exists(serve.SERVER_STATE_PATH))

    def test_preflight_foreign_port_not_killed(self):
        # 5) 端口被外部程序（本测试自己的 socket）占用 → foreign-busy，不杀
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            verdict, _ = serve.preflight(port)
            self.assertEqual(verdict, 'foreign-busy')
        finally:
            s.close()

    def test_second_instance_rejected(self):
        # 4) 已有本产品服务 → 不启动第二实例
        httpd, port = self._server()
        try:
            serve._write_state({'port': port})
            verdict, st = serve.preflight(port)
            self.assertEqual(verdict, 'ours-running')
            self.assertEqual(st.get('pid'), os.getpid())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_runtime_endpoint_redacts_paths(self):
        # 21) /api/v1/runtime 不泄露绝对路径
        httpd, port = self._server()
        try:
            serve._write_state({'port': port})
            serve.SCAN_STATE['running'] = False
            st, body = self._get(port, '/api/v1/runtime')
            self.assertEqual(st, 200)
            j = json.loads(body)
            self.assertIn('server', j)
            self.assertIn('scheduler', j)
            self.assertIn('scan_lock', j)
            self.assertNotIn(str(ROOT), body.decode('utf-8'))
            self.assertNotIn('dsh-home', body.decode('utf-8'))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_server_log_written_and_rotates(self):
        # 7) server.log 写入 + 轮转
        serve.slog('start test')
        serve.slog('stop test')
        self.assertIn('start test', open(serve.SERVER_LOG_PATH,
                                         encoding='utf-8').read())
        serve.SERVER_LOG_MAX = 10     # 强制轮转
        serve.slog('rotate me please')
        self.assertTrue(os.path.exists(serve.SERVER_LOG_PATH + '.1'))

    def _sandbox_source(self):
        """给沙盒一个 workbuddy 夹具（无源时子进程走 no_supported_sources，
        不会进入 cmd_scan / 锁路径）。"""
        import time as _t
        wb = os.path.join(self.tmp, 'wb', 'p1')
        os.makedirs(wb, exist_ok=True)
        now = int(_t.time() * 1000)
        with open(os.path.join(wb, 'sess-1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({
                    'id': 'w%d' % i, 'sessionId': 'sess-1', 'timestamp': now + i,
                    'providerData': {'model': 'm-x', 'requestModelName': 'P',
                                     'rawUsage': {'prompt_tokens': 200,
                                                  'completion_tokens': 20,
                                                  'prompt_tokens_details': {
                                                      'cached_tokens': 100}}}}) + '\n')
        empty = os.path.join(self.tmp, 'empty').replace('\\', '/')
        os.makedirs(empty, exist_ok=True)
        with open(os.path.join(self.tmp, 'sources.json'), 'w', encoding='utf-8') as f:
            json.dump({'zcode': empty + '/none.sqlite', 'dsh': empty,
                       'catpaw': empty, 'traecn': empty, 'codex': empty,
                       'workbuddy': os.path.join(self.tmp, 'wb').replace('\\', '/')}, f)

    def test_api_scan_409_on_cross_process_lock(self):
        # 11+13) 跨进程锁被占（父进程持有）→ API POST scan 409，绝不进入采集
        httpd, port = self._server()
        try:
            self._sandbox_source()
            for f in ('autopilot.py', 'usage-board.schema.json', 'ledger.py'):
                shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
            ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
            h = ledger.acquire_scan_lock(trigger='scheduled')
            st, body = self._get(port, '/api/v1/scan', body=b'{}',
                                 headers={'Content-Type': 'application/json'})
            self.assertEqual(st, 409)
            self.assertEqual(json.loads(body)['error'], 'scan_in_progress')
            ledger.release_scan_lock(h)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_api_scan_ok_when_lock_free(self):
        # 24) 锁空闲 → API scan 完整编排成功并刷新 board/health/run-state（沙盒）
        httpd, port = self._server()
        try:
            self._sandbox_source()
            for f in ('autopilot.py', 'usage-board.schema.json', 'ledger.py'):
                shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
            ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
            st, body = self._get(port, '/api/v1/scan', body=b'{}',
                                 headers={'Content-Type': 'application/json'})
            j = json.loads(body)
            self.assertEqual(st, 200)
            self.assertTrue(j.get('ok'), body)
            for f in ('usage-board.json', 'health.json', 'run-state.json'):
                self.assertTrue(os.path.isfile(os.path.join(self.tmp, f)), f)
            rs = json.load(open(os.path.join(self.tmp, 'run-state.json'),
                                encoding='utf-8'))
            self.assertEqual(rs.get('trigger'), 'api')
            self.assertEqual((rs.get('pricing') or {}).get('status'), 'skipped')
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_scheduled_scan_lock_skips(self):
        # 12b) scheduled 采集在锁占用下：partial + run-state 留痕（沙盒）
        self._sandbox_source()
        for f in ('autopilot.py', 'usage-board.schema.json', 'ledger.py'):
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.tmp, f))
        old = ledger.SCAN_LOCK_PATH
        ledger.SCAN_LOCK_PATH = os.path.join(self.tmp, 'usage-ledger-scan.lock')
        try:
            h = ledger.acquire_scan_lock(trigger='api')
            code, out = run(['autopilot.py', 'auto', '--trigger', 'scheduled',
                             '--no-pricing'], self.tmp)
            self.assertEqual(code, ledger.EXIT_PARTIAL)
            rs = json.load(open(os.path.join(self.tmp, 'run-state.json'),
                                encoding='utf-8'))
            self.assertTrue(rs.get('scan_skipped_lock'))
            self.assertTrue(any('已有扫描正在进行' in e for e in rs.get('errors', [])))
            ledger.release_scan_lock(h)
        finally:
            ledger.SCAN_LOCK_PATH = old


class TestSchedulerRuntime(unittest.TestCase):
    def test_task_command_uses_venv_pythonw(self):
        # 8/16) TR 强制项目 .venv pythonw；auto-schedule 字段齐备
        exe = autopilot._require_venv_runtime()
        self.assertTrue(exe.lower().endswith('pythonw.exe'))
        self.assertIn('.venv', exe)
        tr = autopilot.task_command()
        self.assertIn(exe, tr)
        self.assertIn('auto --trigger scheduled', tr)
        self.assertIn('autopilot.py', tr)
        tr2, sch, ps = autopilot.register_commands(30)
        for cmd in (tr2, ' '.join(sch), ps):
            self.assertIn(exe, cmd)
            self.assertNotIn('python.exe ', cmd.replace('pythonw.exe', ''))

    def test_uninstall_dry_run_touches_nothing(self):
        # 20/22) uninstall --dry-run：只报告，不删任务不删配置
        sched = os.path.join(HERE, 'auto-schedule.json')
        existed = os.path.isfile(sched)
        before = open(sched, encoding='utf-8').read() if existed else None
        code, out = run(['autopilot.py', 'uninstall-auto', '--dry-run'], ROOT)
        self.assertEqual(code, 0, out)
        self.assertIn('卸载预览', out)
        self.assertIn('UsageLedger-Auto', out)
        if existed:
            self.assertEqual(open(sched, encoding='utf-8').read(), before)
        else:
            self.assertFalse(os.path.isfile(sched))

    def test_pricing_never_invoked_by_schedule_shape(self):
        # 22) 调度编排源码层面：子进程扫描显式 --no-pricing；无 pricing-sync 面
        src = open(os.path.join(ROOT, 'serve.py'), encoding='utf-8').read()
        self.assertIn("'--no-pricing'", src)
        self.assertNotIn('pricing-sync', src.replace('#', '#'))
        auto = open(os.path.join(ROOT, 'autopilot.py'), encoding='utf-8').read()
        self.assertIn("choices=['manual', 'scheduled', 'api']", auto)

    def test_no_full_scan_surface(self):
        # 23) API 层无 --full 面：serve.py 拒绝 full 字段
        src = open(os.path.join(ROOT, 'serve.py'), encoding='utf-8').read()
        self.assertIn('full_not_allowed', src)


class TestRuntimeCheck(unittest.TestCase):
    def test_runtime_check_passes_in_venv(self):
        # 1) .venv 内检查通过（只读）；非 .venv 解释器跳过（语义由下一条覆盖）
        if '.venv' not in sys.executable.replace('\\', '/').lower():
            self.skipTest('当前不是 .venv 解释器')
        p = subprocess.run([sys.executable, os.path.join(ROOT, 'scripts',
                                                         'check_runtime.py')],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=60, cwd=ROOT)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('Runtime 就绪', p.stdout)

    def test_runtime_check_rejects_system_python(self):
        # 2) 非 .venv 解释器 → 明确失败，绝不退回系统 Python（源码契约）
        src = open(os.path.join(ROOT, 'scripts', 'check_runtime.py'),
                   encoding='utf-8').read()
        self.assertIn('.venv', src)
        self.assertIn('禁止使用系统 Python', src)
        self.assertIn('zstandard', src)

    def test_launcher_bats_require_venv(self):
        # 启动器必须先找 .venv 并做 Runtime 检查，绝不静默用系统 Python
        for bat in ('start-server.bat', 'run-scan.bat', 'stop-server.bat'):
            src = open(os.path.join(ROOT, bat), encoding='utf-8').read()
            self.assertIn('.venv\\Scripts\\python.exe', src, bat)
        start = open(os.path.join(ROOT, 'start-server.bat'), encoding='utf-8').read()
        self.assertIn('check_runtime.py', start)
        self.assertIn('pythonw.exe', start)
        self.assertIn('--check-only', start)
        stop = open(os.path.join(ROOT, 'stop-server.bat'), encoding='utf-8').read()
        self.assertIn('--stop', stop)


if __name__ == '__main__':
    unittest.main()
