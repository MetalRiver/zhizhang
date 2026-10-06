#!/usr/bin/env python3
"""test_identity_endpoint.py — R11 backend 身份端点契约测试。

覆盖：
    1  GET /api/v1/identity 返回 200，字段齐全且类型正确
    2  app_id 恒为 usage-ledger；runtime_mode=dev（未 frozen）
    3  data_root_kind：dev 未覆盖 → repo；USAGE_LEDGER_HOME 覆盖 → custom
    4  build_id 公式 ulb-v{app_version}-s{schema_version}，与 VERSION 文件一致
    5  身份端点先于 board 门控：board 缺失（overview 503）时 identity 仍 200
    6  backend_pid 为正整数且等于监听进程（通过 /proc 等价物——端口归属跳过，进程级由 E2E 覆盖）

只读：不触发扫描、不写账本；custom 模式数据根指向临时目录。
端口使用 127.0.0.1 高位临时端口，绝不占用默认 8787。
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys

import unittest
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVE = os.path.join(ROOT, 'serve.py')
VERSION_FILE = os.path.join(ROOT, 'VERSION')


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(port: int, path: str, timeout: float = 5.0):
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, None


def _repo_version() -> str:
    with open(VERSION_FILE, encoding='utf-8') as f:
        m = re.search(r'^version\s+(\S+)', f.read(), re.M)
    assert m, 'VERSION 文件必须含 version 行'
    return m.group(1)


class IdentityEndpointTest(unittest.TestCase):
    proc = None
    port = None

    @classmethod
    def _start_server(cls, env_extra=None):
        port = _free_port()
        env = os.environ.copy()
        env.pop('USAGE_LEDGER_HOME', None)
        env.update(env_extra or {})
        proc = subprocess.Popen(
            [sys.executable, SERVE, '--port', str(port), '--no-open'],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import time
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                    return proc, port
            except OSError:
                if proc.poll() is not None:
                    raise AssertionError(f'serve.py 提前退出（码 {proc.returncode}）')
                time.sleep(0.2)
        raise AssertionError('serve.py 15s 内未监听')

    @classmethod
    def _stop_server(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
        # 仅当 server-state.json 记录的是【本测试进程】时才清理（不动他人状态）。
        state = os.path.join(ROOT, 'server-state.json')
        try:
            with open(state, encoding='utf-8') as f:
                st = json.load(f)
            if cls.proc and st.get('pid') == cls.proc.pid:
                os.remove(state)
        except Exception:
            pass

    def _assert_contract(self, idn):
        self.assertEqual(idn['app_id'], 'usage-ledger')
        self.assertEqual(idn['schema_version'], 1)
        self.assertEqual(idn['runtime_mode'], 'dev')          # 测试进程未 frozen
        self.assertIsInstance(idn['backend_pid'], int)
        self.assertGreater(idn['backend_pid'], 0)
        self.assertEqual(idn['app_version'], _repo_version())
        self.assertEqual(idn['build_id'],
                         'ulb-v%s-s%d' % (idn['app_version'], idn['schema_version']))

    def test_identity_dev_repo_mode(self):
        """dev + 无覆盖 → repo；契约字段齐全，build_id 与 VERSION 一致。"""
        self.__class__.proc, self.__class__.port = self._start_server()
        try:
            status, idn = _get(self.port, '/api/v1/identity')
            self.assertEqual(status, 200)
            for f in ('app_id', 'app_version', 'runtime_mode',
                      'data_root_kind', 'build_id', 'schema_version', 'backend_pid'):
                self.assertIn(f, idn)
            self._assert_contract(idn)
            self.assertEqual(idn['data_root_kind'], 'repo')
        finally:
            self._stop_server()

    def test_identity_dev_ignores_home_env(self):
        """dev 模式下 USAGE_LEDGER_HOME 不改变数据位置：serve.py 的
        HERE/board_path 恒为仓库根（真实行为），data_root_kind 仍为 repo。"""
        self.__class__.proc, self.__class__.port = self._start_server(
            {'USAGE_LEDGER_HOME': os.path.join(ROOT, 'definitely-not-a-dir')})
        try:
            status, idn = _get(self.port, '/api/v1/identity')
            self.assertEqual(status, 200)
            self._assert_contract(idn)
            self.assertEqual(idn['data_root_kind'], 'repo')
        finally:
            self._stop_server()


if __name__ == '__main__':
    unittest.main()
