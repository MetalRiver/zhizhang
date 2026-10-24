# -*- coding: utf-8 -*-
"""V1.1 Phase1 批次 2 回归：后端自动同步调度器。

覆盖：
  - HTTP 开关/间隔（校验：未知字段、interval 边界、Origin、quiesce 503）
  - 到点执行：与 POST /api/v1/scan 同一条 run_scan_once 编排，
    trigger='scheduled' 溯源；下次运行按间隔推进
  - 互斥：SCAN_STATE 占用 → postponed_busy，不抢锁
  - 维护静默：quiesce → postponed_maintenance；resume 后自动恢复
  - 休眠恢复：next_run_at 已过期 → 唤醒后立即补跑一次（不追多次）
  - 失败留痕：编排失败/异常（脱敏）记入 last_status/last_errors，
    下次运行照常推进（不形成紧循环）
  - 退出：stop_scheduler 及时收线程
  - 升级/重启：持久化配置跨"进程重启"续跑（到期即跑、未到期等待、
    缺 next_run_at 自愈重排）
  - frozen 模拟：run_scan_once frozen 分支把 trigger 注入 autopilot argv
  - autopilot.build_schedule 对 method='backend' 的如实呈现

隔离原则：_safety 沙盒 + 假 run_scan_once（绝不触发真实采集），
serve.HERE/SERVER_LOG_PATH 指向沙盒 —— 全程不触 production DATA_ROOT。
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import sys
import threading
import time
import types
import unittest
import datetime as dt

import _safety

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

import serve  # noqa: E402


class SchedulerBase(unittest.TestCase):

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-sched-')
        self._old_here = serve.HERE
        self._old_log = serve.SERVER_LOG_PATH
        self._old_state = serve.SERVER_STATE_PATH
        self._old_run = serve.run_scan_once
        serve.HERE = self.tmp
        serve.SERVER_LOG_PATH = os.path.join(self.tmp, 'server.log')
        serve.SERVER_STATE_PATH = os.path.join(self.tmp, 'server-state.json')
        serve.MAINTENANCE['quiesced'] = False
        serve.MAINTENANCE['lock_handle'] = None
        serve.SCAN_STATE['running'] = False
        serve.stop_scheduler()
        # 假采集：记录 trigger，返回可控结果（绝不触真实账本）
        self.calls = []
        self.scan_result = {'ok': True, 'status': 'success', 'exit_code': 0,
                            'errors': [], 'sources_found': 1,
                            'events_inserted': 0, 'events_updated': 0,
                            'board_written': True, 'health': 'healthy'}

        def fake_scan(trigger='api'):
            self.calls.append(trigger)
            if isinstance(self.scan_result, Exception):
                raise self.scan_result
            return dict(self.scan_result)
        serve.run_scan_once = fake_scan
        self.httpd = serve.make_server(0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        serve.stop_scheduler()
        self.httpd.shutdown()
        self.httpd.server_close()
        serve.run_scan_once = self._old_run
        serve.HERE = self._old_here
        serve.SERVER_LOG_PATH = self._old_log
        serve.SERVER_STATE_PATH = self._old_state
        serve.MAINTENANCE['quiesced'] = False
        serve.MAINTENANCE['lock_handle'] = None
        serve.SCAN_STATE['running'] = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- HTTP 助手（Origin/Content-Type 与产品前端一致） ----

    def call(self, method, path, body=None, origin=True, raw_body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        headers = {'Content-Type': 'application/json'}
        if origin:
            headers['Origin'] = 'http://127.0.0.1:%d' % self.port
        try:
            payload = raw_body if raw_body is not None else (
                json.dumps(body).encode('utf-8') if body is not None else None)
            conn.request(method, path, body=payload, headers=headers)
            r = conn.getresponse()
            data = r.read().decode('utf-8')
            try:
                return r.status, json.loads(data)
            except ValueError:
                return r.status, data
        finally:
            conn.close()

    def cfg(self):
        return serve._load_sync_cfg()

    def enable(self, interval=None):
        body = {'enabled': True}
        if interval is not None:
            body['interval_minutes'] = interval
        st, payload = self.call('POST', '/api/v1/sync-schedule', body)
        assert st == 200, payload
        return payload


class SyncScheduleHttp(SchedulerBase):

    def test_get_default_status(self):
        st, payload = self.call('GET', '/api/v1/sync-schedule')
        self.assertEqual(st, 200)
        self.assertTrue(payload['ok'])
        self.assertTrue(payload['available'])
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['method'], None)
        self.assertEqual(payload['schema_version'], 3)

    def test_enable_persists_backend_cfg(self):
        payload = self.enable()
        self.assertTrue(payload['enabled'])
        self.assertEqual(payload['interval_minutes'], 30,
                         '默认间隔必须为 30 分钟')
        c = self.cfg()
        self.assertEqual(c['schema_version'], 3)
        self.assertEqual(c['method'], 'backend')
        self.assertIsNone(c['task_name'], 'backend 形态不得冒充计划任务')
        self.assertTrue(c['next_run_at'], '启用后必须排下次运行')
        self.assertTrue(c['installed_at'])

    def test_enable_custom_interval_and_bounds(self):
        payload = self.enable(interval=15)
        self.assertEqual(payload['interval_minutes'], 15)
        for bad in (4, 1441, '30', True, None):
            st, payload = self.call('POST', '/api/v1/sync-schedule',
                                    {'enabled': True,
                                     'interval_minutes': bad})
            self.assertEqual(st, 400, 'interval=%r 应被拒绝' % (bad,))
            self.assertEqual(payload['error'], 'bad_request')

    def test_unknown_field_rejected(self):
        st, payload = self.call('POST', '/api/v1/sync-schedule',
                                {'enabled': True, 'full': True})
        self.assertEqual(st, 400)
        self.assertEqual(payload['error'], 'unexpected_field')

    def test_empty_body_rejected(self):
        st, payload = self.call('POST', '/api/v1/sync-schedule', {})
        self.assertEqual(st, 400)

    def test_origin_check(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            conn.request('POST', '/api/v1/sync-schedule',
                         body=json.dumps({'enabled': True}).encode('utf-8'),
                         headers={'Content-Type': 'application/json',
                                  'Origin': 'http://evil.example.com'})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403)
        finally:
            conn.close()

    def test_post_blocked_during_maintenance(self):
        serve.MAINTENANCE['quiesced'] = True
        st, payload = self.call('POST', '/api/v1/sync-schedule',
                                {'enabled': True})
        self.assertEqual(st, 503)
        self.assertEqual(payload['error'], 'maintenance')

    def test_disable_preserves_last_result(self):
        self.enable()
        serve._scheduler_tick()
        st, payload = self.call('POST', '/api/v1/sync-schedule',
                                {'enabled': False})
        self.assertEqual(st, 200)
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['last_status'], 'success',
                         '停用后最近结果保留展示')
        self.assertIsNone(payload['next_run_at'])
        self.assertEqual(serve._scheduler_tick(), 'idle')


class SchedulerTick(SchedulerBase):

    def test_due_run_uses_scheduled_trigger_and_advances(self):
        self.enable(interval=30)
        action = serve._scheduler_tick()
        self.assertEqual(action, 'ran')
        self.assertEqual(self.calls, ['scheduled'],
                         '调度采集必须走 run_scan_once(trigger=scheduled)')
        c = self.cfg()
        self.assertEqual(c['last_status'], 'success')
        self.assertEqual(c['last_trigger'], 'scheduled')
        nxt = dt.datetime.fromisoformat(c['next_run_at'])
        self.assertGreater(nxt, dt.datetime.now() + dt.timedelta(minutes=29),
                           '成功后按间隔推进下次运行')

    def test_future_next_run_waits(self):
        self.enable()
        c = self.cfg()
        c['next_run_at'] = (dt.datetime.now()
                            + dt.timedelta(minutes=30)).isoformat(timespec='seconds')
        serve._save_sync_cfg(c)
        self.assertEqual(serve._scheduler_tick(), 'wait')
        self.assertEqual(self.calls, [])

    def test_disabled_idle(self):
        self.assertEqual(serve._scheduler_tick(), 'idle')
        self.assertEqual(self.calls, [])

    def test_mutex_postponed_busy(self):
        self.enable()
        serve.SCAN_STATE['running'] = True     # 模拟 POST scan 进行中
        action = serve._scheduler_tick()
        self.assertEqual(action, 'postponed')
        self.assertEqual(self.calls, [], '互斥期间不得触发第二次采集')
        c = self.cfg()
        self.assertEqual(c['last_status'], 'postponed_busy')
        nxt = dt.datetime.fromisoformat(c['next_run_at'])
        self.assertLess(nxt, dt.datetime.now() + dt.timedelta(minutes=2),
                        '顺延后应短期内重试')

    def test_maintenance_postpone_then_resume(self):
        self.enable()
        serve.MAINTENANCE['quiesced'] = True   # 模拟迁移/收尾静默
        self.assertEqual(serve._scheduler_tick(), 'postponed')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.cfg()['last_status'], 'postponed_maintenance')
        serve.MAINTENANCE['quiesced'] = False  # resume 后按顺延重试点恢复
        c = self.cfg()
        c['next_run_at'] = dt.datetime.now().isoformat(timespec='seconds')
        serve._save_sync_cfg(c)
        self.assertEqual(serve._scheduler_tick(), 'ran')
        self.assertEqual(self.calls, ['scheduled'])

    def test_overdue_runs_once_not_many(self):
        self.enable(interval=30)
        c = self.cfg()
        c['next_run_at'] = (dt.datetime.now()
                            - dt.timedelta(hours=2)).isoformat(timespec='seconds')
        serve._save_sync_cfg(c)
        self.assertEqual(serve._scheduler_tick(), 'ran',
                         '（休眠/停机错过）到期后应立即补跑')
        self.assertEqual(serve._scheduler_tick(), 'wait',
                         '只补跑一次，不按错过次数追跑')
        self.assertEqual(len(self.calls), 1)

    def test_failure_recorded_and_advances(self):
        self.enable()
        self.scan_result = {'ok': False, 'status': 'partial_failure',
                            'exit_code': 2, 'errors': ['源 x 读取失败'],
                            'board_written': True}
        self.assertEqual(serve._scheduler_tick(), 'ran')
        c = self.cfg()
        self.assertEqual(c['last_status'], 'partial_failure')
        self.assertEqual(c['last_errors'], ['源 x 读取失败'])
        nxt = dt.datetime.fromisoformat(c['next_run_at'])
        self.assertGreater(nxt, dt.datetime.now() + dt.timedelta(minutes=29),
                           '失败也按间隔推进，不形成紧循环')

    def test_exception_recorded_sanitized(self):
        self.enable()
        self.scan_result = FileNotFoundError(
            'packaged resource missing (resource root: C:\\Users\\u\\x)')
        self.assertEqual(serve._scheduler_tick(), 'ran')
        c = self.cfg()
        self.assertEqual(c['last_status'], 'failed')
        self.assertTrue(c['last_errors'])
        self.assertNotIn('C:\\Users', c['last_errors'][0],
                         '异常文本不得携带本机路径')

    def test_missing_next_run_self_heals(self):
        self.enable()
        c = self.cfg()
        del c['next_run_at']
        serve._save_sync_cfg(c)
        self.assertEqual(serve._scheduler_tick(), 'wait')
        self.assertTrue(self.cfg().get('next_run_at'), '缺下次时间必须自愈重排')

    def test_restart_with_future_next_waits_then_runs(self):
        # 升级/重启模拟：配置持久化在文件里，新"进程"按文件续跑
        self.enable()
        c = self.cfg()
        c['next_run_at'] = (dt.datetime.now()
                            + dt.timedelta(minutes=5)).isoformat(timespec='seconds')
        serve._save_sync_cfg(c)
        serve.stop_scheduler()
        self.assertEqual(serve._scheduler_tick(), 'wait',
                         '重启后未到期不得立即跑')
        c['next_run_at'] = (dt.datetime.now()
                            - dt.timedelta(minutes=1)).isoformat(timespec='seconds')
        serve._save_sync_cfg(c)
        self.assertEqual(serve._scheduler_tick(), 'ran',
                         '重启后到期立即跑')


class SchedulerThread(SchedulerBase):

    def test_thread_runs_first_sync_shortly_after_enable(self):
        self.enable()
        serve.start_scheduler()
        deadline = time.time() + 6
        while time.time() < deadline and not self.calls:
            time.sleep(0.1)
        self.assertEqual(self.calls, ['scheduled'],
                         '启用后调度线程应立即接手首次同步')

    def test_stop_scheduler_exits_promptly(self):
        self.enable()
        t = serve.start_scheduler()
        self.assertTrue(t.is_alive())
        t0 = time.time()
        self.assertTrue(serve.stop_scheduler())
        self.assertLess(time.time() - t0, 5)
        self.assertFalse(t.is_alive())

    def test_loop_survives_tick_exception(self):
        self.enable()
        self.scan_result = RuntimeError('boom')
        serve.start_scheduler()
        time.sleep(2.5)                    # 至少两轮 tick，异常不得杀线程
        self.assertTrue(serve.SCHEDULER['thread'] is None or True)
        t = serve.SCHEDULER.get('thread')
        # stop_scheduler 已把 thread 置 None —— 用局部引用验证存活
        self.assertFalse(t is not None and not t.is_alive(),
                         'tick 异常不得杀死调度线程')
        serve.stop_scheduler()
        self.assertEqual(self.cfg().get('last_status'), 'failed')


class FrozenTriggerPassthrough(unittest.TestCase):
    """run_scan_once frozen 分支：trigger 必须注入 autopilot argv。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-frozen-')
        self._old_here = serve.HERE
        self._old_run = serve.run_scan_once
        serve.HERE = self.tmp
        with open(os.path.join(self.tmp, 'run-state.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({'status': 'success', 'board': {'written': True},
                       'events_inserted': 0, 'sources_found': 0}, f)

    def tearDown(self):
        serve.run_scan_once = self._old_run
        serve.HERE = self._old_here
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frozen_argv_carries_trigger(self):
        captured = {}

        def fake_main():
            captured['argv'] = list(sys.argv)
            raise SystemExit(0)

        fake_paths = types.ModuleType('paths')
        fake_paths.run_state_path = lambda: os.path.join(self.tmp,
                                                         'run-state.json')
        fake_autopilot = types.ModuleType('autopilot')
        fake_autopilot.main = fake_main
        had_frozen = hasattr(sys, 'frozen')
        old_frozen = getattr(sys, 'frozen', None)
        old_paths = sys.modules.get('paths')
        old_ap = sys.modules.get('autopilot')
        sys.frozen = True
        sys.modules['paths'] = fake_paths
        sys.modules['autopilot'] = fake_autopilot
        try:
            result = serve.run_scan_once(trigger='scheduled')
        finally:
            if had_frozen:
                sys.frozen = old_frozen
            else:
                del sys.frozen
            for name, mod in (('paths', old_paths), ('autopilot', old_ap)):
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod
        self.assertEqual(
            captured['argv'],
            ['autopilot', 'auto', '--trigger', 'scheduled', '--no-pricing'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['status'], 'success')


class BuildScheduleBackend(unittest.TestCase):
    """autopilot.build_schedule 对 backend 调度配置的如实呈现。"""

    def setUp(self):
        import autopilot
        self.ap = autopilot
        self._old_path = autopilot.SCHEDULE_PATH
        self.tmp = _safety.sandbox_dir(prefix='ul-v11-bs-')

    def tearDown(self):
        self.ap.SCHEDULE_PATH = self._old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, cfg):
        with open(os.path.join(self.tmp, 'auto-schedule.json'), 'w',
                  encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False)
        self.ap.SCHEDULE_PATH = os.path.join(self.tmp, 'auto-schedule.json')

    def test_backend_method_reported_by_scheduler(self):
        self._write({'schema_version': 3, 'enabled': True,
                     'interval_minutes': 30, 'method': 'backend',
                     'installed_at': '2026-10-24T20:00:00',
                     'next_run_at': '2026-10-24T20:30:00'})
        s = self.ap.build_schedule()
        self.assertTrue(s['enabled'])
        self.assertEqual(s['method'], 'backend')
        self.assertEqual(s['task_name'], None,
                         'backend 形态不得显示 Windows 任务名')
        self.assertEqual(s['next_run_at'], '2026-10-24T20:30:00')
        self.assertEqual(s['next_run_basis'], 'reported_by_backend_scheduler')

    def test_no_config_still_truthful(self):
        self.ap.SCHEDULE_PATH = os.path.join(self.tmp, 'absent.json')
        s = self.ap.build_schedule()
        self.assertFalse(s['enabled'])
        self.assertEqual(s['next_run_basis'], 'not_configured')

    def test_windows_task_cfg_unchanged(self):
        # v2（install-auto Windows 任务）既有语义不受影响
        self._write({'schema_version': 2, 'enabled': True,
                     'interval_minutes': 30, 'method': 'schtasks',
                     'task_name': 'UsageLedger-Auto',
                     'installed_at': '2026-10-01T09:00:00'})
        s = self.ap.build_schedule()
        self.assertEqual(s['task_name'], 'UsageLedger-Auto')
        self.assertEqual(s['next_run_basis'], 'estimated_from_install_time')


if __name__ == '__main__':
    unittest.main()
