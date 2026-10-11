# -*- coding: utf-8 -*-
"""test_product_core_loop.py — 产品核心闭环验收（RC.7-r7）。

验证产品核心价值：AI 记录扫描一次后，即使原始来源被清理，账本仍能
长期保存、查询和迁移。全流程使用 frozen 模拟环境（sys.frozen/_MEIPASS/
executable 三件套 + onedir 布局）与独立合成目录，五个阶段：

  P1 扫描入账：四来源（wb/cp/dsh/codex）→ 编排扫描 → 查询对账
     （按客户端/日期/项目/会话；token 与 activity 口径分离）。
  P2 来源清退：合成来源移出扫描路径 → 重扫 → 记录不丢、不重、
     统计不倒退，来源标记已清退。
  P3 来源恢复：重扫 → 幂等（events_inserted=0，记录数不变）。
  P4 账本迁移：桌面壳同款 HTTP 编排（quiesce → plan → execute）迁移到
     自定义 Data Root → 新根记录/身份/查询与迁移前一致，旧根数据安全。
  P5 重新加载：以迁移后的 Data Root 重启后端（等价桌面壳 bootstrap
     切换）→ 查询结果与迁移前一致。

隔离：全部读写位于 _safety.sandbox_dir 合成目录；不触碰真实账本。
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

APP_FILES = ('ledger.py', 'discovery.py', 'paths.py', 'autopilot.py',
             'serve.py')
INTERNAL_FILES = ('VERSION', 'source-catalog.json', 'usage-board.schema.json')

RUNNER = r'''
import os, sys, json, sqlite3
import threading, urllib.request, urllib.error

sys.frozen = True
sys._MEIPASS = os.environ['ZZ_MEIPASS']
sys.executable = os.path.join(os.environ['ZZ_APP_DIR'], 'UsageLedger.exe')
sys.path.insert(0, sys._MEIPASS)
import serve, paths, ledger, autopilot
serve.HERE = paths.DATA_ROOT
serve.rebind_state_paths_to_data_root()
mode = os.environ['ZZ_MODE']

# 隔离硬约束（RC.7 修复）：测试服务必须绑定系统分配的独立端口，
# 绝不复用生产端口 8787；Data Root / 账本路径必须落在本测试沙盒内。
# 任一不满足立即 STOP —— 防止测试把 HTTP 打到正在运行的真实后端。
PROD_PORT = 8787
_sandbox = os.path.dirname(os.environ['ZZ_APP_DIR'])
if not os.path.normcase(paths.DATA_ROOT).startswith(os.path.normcase(_sandbox)):
    raise SystemExit('STOP: Data Root 不在测试沙盒内：%s' % paths.DATA_ROOT)
if not os.path.normcase(ledger.DB_PATH).startswith(os.path.normcase(_sandbox)):
    raise SystemExit('STOP: 账本路径不在测试沙盒内：%s' % ledger.DB_PATH)

httpd = serve.make_server(0)                # 0 = 系统分配临时端口
PORT = httpd.server_address[1]
if PORT == PROD_PORT:
    raise SystemExit('STOP: 测试误绑定生产端口 %d' % PROD_PORT)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % PORT


def _assert_ours():
    """每次请求前确认目标服务就是本测试进程（backend_pid 一致），否则 STOP。"""
    req = urllib.request.Request(base + '/api/v1/identity')
    with urllib.request.urlopen(req, timeout=30) as r:
        ident = json.loads(r.read().decode('utf-8'))
    if int(ident.get('backend_pid', -1)) != os.getpid():
        raise SystemExit('STOP: 端口 %d 上的服务不是本测试进程'
                         '（backend_pid=%s 期望=%s）'
                         % (PORT, ident.get('backend_pid'), os.getpid()))


def call(method, path, body=None):
    _assert_ours()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={'Content-Type': 'application/json',
                                          'Origin': base})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, payload = r.status, json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read().decode('utf-8'))
    return status, payload

def snapshot(tag):
    q = call('GET', '/api/v1/query?range=all&audit=effective')[1]
    h = call('GET', '/api/v1/health')[1]
    db = os.path.join(paths.DATA_ROOT, 'usage.db')
    if not os.path.isfile(db):
        raise SystemExit('STOP: 扫描未写入测试沙盒账本 %s（隔离失败）' % db)
    conn = sqlite3.connect(db)
    try:
        ids = conn.execute('SELECT client, session_id, event_id '
                           'FROM usage_event').fetchall()
        acts = conn.execute('SELECT client, session_key, event_id '
                            'FROM activity_event').fetchall()
        n_usage = conn.execute('SELECT COUNT(*) FROM usage_event').fetchone()[0]
        n_act = conn.execute('SELECT COUNT(*) FROM activity_event').fetchone()[0]
    finally:
        conn.close()
    return {'tag': tag,
            'aggregate': q['aggregate'], 'daily': q['daily'],
            'by_client': q['by_client'], 'by_project': q['by_project'],
            'sessions': [{k: s[k] for k in ('source', 'session_id', 'events',
                                            'total', 'token_records')}
                         for s in q['sessions']],
            'ledger_state': q['ledger_state'],
            'health_sources': h['sources'],
            'health_status': h['status'],
            'db_usage_rows': n_usage, 'db_activity_rows': n_act,
            'usage_ids': sorted(tuple(x) for x in ids),
            'activity_ids': sorted(tuple(x) for x in acts)}

if mode == 'p1':
    scan = call('POST', '/api/v1/scan', {})
    snap = snapshot('p1')
    print('@@JSON@@' + json.dumps({'scan': scan, 'snap': snap,
                                   'data_root': paths.DATA_ROOT},
                                  ensure_ascii=False))
elif mode in ('p2', 'p3'):
    qdir = os.environ['ZZ_QUARANTINE']
    fxroot = os.path.join(os.environ['LOCALAPPDATA'], 'ZhiZhangFixtures')
    moved = []
    if mode == 'p2':
        # 清退：合成来源移出扫描路径
        for rel in ('wb/sess-1.jsonl', 'codex/rollout-1.jsonl',
                    'dsh-home/sessions/p/s1/session.jsonl.zstd',
                    'cp/conv.jsonl'):
            src = os.path.join(fxroot, rel.replace('/', os.sep))
            if os.path.isfile(src):
                dst = os.path.join(qdir, rel.replace('/', '_'))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                moved.append(rel)
    else:
        # 恢复：从隔离区移回原位（幂等验证前提）
        for rel in ('wb/sess-1.jsonl', 'codex/rollout-1.jsonl',
                    'dsh-home/sessions/p/s1/session.jsonl.zstd',
                    'cp/conv.jsonl'):
            src = os.path.join(qdir, rel.replace('/', '_'))
            dst = os.path.join(fxroot, rel.replace('/', os.sep))
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                moved.append(rel)
    scan = call('POST', '/api/v1/scan', {})
    snap = snapshot(mode)
    print('@@JSON@@' + json.dumps({'scan': scan, 'snap': snap,
                                   'moved': moved}, ensure_ascii=False))
elif mode == 'p4':
    custom = os.environ['ZZ_CUSTOM']
    q = call('POST', '/api/v1/maintenance/quiesce', {})
    plan = call('POST', '/api/v1/maintenance/migrate/plan', {'target': custom})
    exe = call('POST', '/api/v1/maintenance/migrate/execute', {'target': custom})
    call('POST', '/api/v1/maintenance/resume', {})
    old_db = os.path.join(paths.DATA_ROOT, 'usage.db')
    new_db = os.path.join(custom, 'usage.db')
    def counts(p):
        c = sqlite3.connect(p)
        try:
            return {'usage': c.execute('SELECT COUNT(*) FROM usage_event')
                    .fetchone()[0],
                    'activity': c.execute('SELECT COUNT(*) FROM activity_event')
                    .fetchone()[0],
                    'ids': sorted(tuple(x) for x in c.execute(
                        'SELECT client, session_id, event_id FROM usage_event')),
                    'sessions': c.execute(
                        'SELECT COUNT(*) FROM session_registry').fetchone()[0],
                    'projects': c.execute(
                        'SELECT COUNT(*) FROM project_registry').fetchone()[0]}
        finally:
            c.close()
    print('@@JSON@@' + json.dumps({
        'quiesce': q, 'plan': plan, 'execute': exe,
        'old_root': counts(old_db), 'new_root': counts(new_db),
        'custom_root': custom}, ensure_ascii=False))
elif mode == 'p5':
    q = call('GET', '/api/v1/query?range=all&audit=effective')[1]
    h = call('GET', '/api/v1/health')[1]
    conn = sqlite3.connect(os.path.join(paths.DATA_ROOT, 'usage.db'))
    try:
        ids = sorted(tuple(x) for x in conn.execute(
            'SELECT client, session_id, event_id FROM usage_event'))
    finally:
        conn.close()
    print('@@JSON@@' + json.dumps({
        'data_root': paths.DATA_ROOT,
        'aggregate': q['aggregate'], 'by_client': q['by_client'],
        'by_project': q['by_project'], 'usage_ids': ids,
        'health_status': h['status'],
        'health_sources_found': h['sources']['found']},
        ensure_ascii=False))
'''


class ProductCoreLoop(unittest.TestCase):
    """产品核心闭环：扫描 → 清退 → 恢复 → 迁移 → 重新加载。"""

    def setUp(self):
        self.tmp = _safety.sandbox_dir(prefix='ul-coreloop-')
        self.app = os.path.join(self.tmp, 'app')
        self.internal = os.path.join(self.app, '_internal')
        self.la = os.path.join(self.tmp, 'Local')
        self.custom = os.path.join(self.la, 'ZhiZhangLedger-Custom')
        self.quarantine = os.path.join(self.la, 'ZhiZhangQuarantine')
        self.data_root = os.path.join(self.la, 'UsageLedger')
        os.makedirs(self.internal)
        os.makedirs(self.la)
        for f in APP_FILES:
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.internal, f))
        for f in INTERNAL_FILES:
            shutil.copy(os.path.join(ROOT, f), os.path.join(self.internal, f))
        open(os.path.join(self.app, 'UsageLedger.exe'), 'wb').write(b'MZ')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, mode, extra_env=None):
        env = dict(os.environ)
        env.update({
            'LOCALAPPDATA': self.la,
            'USERPROFILE': os.path.join(self.tmp, 'home'),
            'HOME': os.path.join(self.tmp, 'home'),
            'APPDATA': os.path.join(self.tmp, 'Roaming'),
            'ZZ_APP_DIR': self.app,
            'ZZ_MEIPASS': self.internal,
            'ZZ_MODE': mode,
            'ZZ_QUARANTINE': self.quarantine,
            'ZZ_CUSTOM': self.custom,
        })
        for k in ('DSH_HOME', 'USAGE_LEDGER_BACKEND_EXE'):
            env.pop(k, None)
        env.update(extra_env or {})
        r = subprocess.run([PY, '-c', RUNNER], cwd=self.tmp, env=env,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=600)
        if r.returncode != 0:
            raise AssertionError('%s 失败：%s | %s'
                                 % (mode, r.stderr[-800:], r.stdout[-400:]))
        line = [l for l in r.stdout.splitlines()
                if l.startswith('@@JSON@@')][-1]
        return json.loads(line[len('@@JSON@@'):])

    def _seed(self):
        """四种合成来源 + sources.json（与套件同源语义，独立副本）。"""
        os.makedirs(self.data_root, exist_ok=True)
        fx = os.path.join(self.la, 'ZhiZhangFixtures')
        wb = os.path.join(fx, 'wb')
        os.makedirs(wb, exist_ok=True)
        with open(os.path.join(wb, 'sess-1.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({
                    'id': 'wb-%d' % i, 'sessionId': 'wb-sess',
                    'timestamp': 1787000000000 + i * 1000,
                    'type': 'function_call',
                    'providerData': {'model': 'glm-test-model',
                                     'rawUsage': {'prompt_tokens': 1300,
                                                  'completion_tokens': 300,
                                                  'prompt_tokens_details': {
                                                      'cached_tokens': 300}}}},
                    ensure_ascii=False) + '\n')
        cx = os.path.join(fx, 'codex')
        os.makedirs(cx, exist_ok=True)
        with open(os.path.join(cx, 'rollout-1.jsonl'), 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'session_meta', 'payload': {
                'session_id': 'cx-1',
                'cwd': os.path.join(fx, 'cx-proj').replace(os.sep, '/')}}) + '\n')
            f.write(json.dumps({'type': 'token_usage_record',
                                'timestamp': '2026-08-01T10:00:00.000Z',
                                'ordinal': 1,
                                'payload': {'response_id': 'r1',
                                            'usage': {'input_tokens': 1000,
                                                      'output_tokens': 100}}})
                + '\n')
        import zstandard
        dd = os.path.join(fx, 'dsh-home', 'sessions', 'p', 's1')
        os.makedirs(dd, exist_ok=True)
        lines = [json.dumps({'type': 'session', 'id': 'dsh-1',
                             'cwd': os.path.join(fx, 'dsh-proj').replace(os.sep, '/')}),
                 json.dumps({'type': 'assistant/message', 'seq': 1,
                             'time': 1787000000000,
                             'data': {'usage': {'inputTokens': 500,
                                                'outputTokens': 50,
                                                'reasoningTokens': 0,
                                                'cacheReadTokens': 0,
                                                'cacheWriteTokens': 0},
                                      'message': {'source': {
                                          'model': 'deepseek-chat',
                                          'provider': 'deepseek'}}}})]
        with open(os.path.join(dd, 'session.jsonl.zstd'), 'wb') as f:
            f.write(zstandard.ZstdCompressor().compress(
                ''.join(l + '\n' for l in lines).encode('utf-8')))
        cp = os.path.join(fx, 'cp')
        os.makedirs(cp, exist_ok=True)
        with open(os.path.join(cp, 'conv.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(4):
                f.write(json.dumps({'messageId': 'cp-%d' % i,
                                    'conversationId': 'cp-conv',
                                    'type': 'user' if i % 2 == 0 else 'assistant',
                                    'timestamp': '2026-08-01T10:0%d:00.000Z' % i})
                    + '\n')
        with open(os.path.join(self.data_root, 'sources.json'), 'w',
                  encoding='utf-8') as f:
            json.dump({k: os.path.join(fx, v).replace(os.sep, '/')
                       for k, v in (('workbuddy', 'wb'), ('codex', 'codex'),
                                    ('dsh', 'dsh-home'), ('catpaw', 'cp'),
                                    ('zcode', 'none-zcode.sqlite'),
                                    ('traecn', 'none-trae'))}, f)

    # ---- 阶段断言 ----

    def test_core_loop_scan_retain_restore_migrate(self):
        self._seed()
        # ============ P1 扫描入账 ============
        p1 = self._run('p1')
        scan = p1['scan'][1]
        self.assertEqual(scan['status'], 'success', scan)
        self.assertGreaterEqual(scan['sources_found'], 4,
            'P1 发现不足：health detail=%s' % json.dumps(
                p1['snap']['health_sources']['detail'], ensure_ascii=False))
        snap = p1['snap']
        # 口径分离：usage_event 5 条 token（wb3+codex1+dsh1），activity 4 条
        self.assertEqual(snap['db_usage_rows'], 5, snap)
        self.assertEqual(snap['db_activity_rows'], 4, snap)
        self.assertEqual(snap['aggregate']['events'], 9, snap)
        self.assertEqual(snap['aggregate']['token_records'], 5, snap)
        # 按客户端：wb 3 场 3900（input 1000×3 + output 300×3，缓存单列）
        bc = {b['client']: b for b in snap['by_client']}
        self.assertEqual(bc['workbuddy']['events'], 3)
        self.assertEqual(bc['workbuddy']['total'], 3900)
        self.assertEqual(bc['codex']['events'], 1)
        self.assertEqual(bc['codex']['total'], 1100,
                         'codex = input 1000 + output 100（cached 0）')
        self.assertEqual(bc['dsh']['events'], 1)
        self.assertEqual(bc['dsh']['total'], 550)
        self.assertEqual(bc['catpaw']['events'], 4)
        self.assertEqual(bc['catpaw']['token_records'], 0, 'activity 不得计 token')
        # 按日期（真实输入时间戳反推）
        daily = {d['day']: d['total'] for d in snap['daily']}
        self.assertEqual(daily.get('2026-08-01'), 1100, daily)
        self.assertEqual(daily.get('2026-08-18'), 4450, daily)
        # 按项目：dsh-proj / cx-proj 归组；(待整理) 含 wb+catpaw
        bp = {p['display_name']: p for p in snap['by_project']}
        self.assertIn('dsh-proj', bp)
        self.assertIn('cx-proj', bp)   # codex cwd → 项目归组
        # 按会话：4 场，与输入一一对应
        self.assertEqual(len(snap['sessions']), 4, snap['sessions'])

        # ============ P2 来源清退（移出扫描路径）============
        p2 = self._run('p2')
        self.assertEqual(len(p2['moved']), 4, p2['moved'])
        self.assertEqual(p2['scan'][1]['status'], 'partial_failure',
                         '来源清退但账本有留存 → partial（非 success/未丢数据）')
        self.assertEqual(p2['scan'][1]['sources_found'], 0,
                         '清退阶段发现数必须为 0（这正是清退语义）')
        snap2 = p2['snap']
        self.assertEqual(snap2['db_usage_rows'], 5, '清退后记录不得丢失')
        self.assertEqual(snap2['db_activity_rows'], 4, '活动记录不得丢失')
        self.assertEqual(snap2['aggregate']['events'], 9, '统计不得倒退')
        self.assertEqual(snap2['aggregate']['token_records'], 5)
        self.assertEqual(snap2['usage_ids'], snap['usage_ids'], '不得重复入账')
        hs2 = snap2['health_sources']
        purged_n = sum(1 for d in hs2['detail'] if d['state'] == 'not_found'
                       and d['client'] in ('workbuddy', 'codex', 'dsh', 'catpaw'))
        self.assertEqual(purged_n, 4, '四个来源都应标记已清退')
        self.assertGreater(hs2['detail'][0].get('files', 0), -1)

        # ============ P3 来源恢复（幂等）============
        p3 = self._run('p3')
        self.assertIn(p3['scan'][1]['status'], ('success', 'partial_failure'),
                      p3['scan'][1])
        snap3 = p3['snap']
        self.assertEqual(snap3['db_usage_rows'], 5, '恢复后不得重复入账')
        self.assertEqual(snap3['db_activity_rows'], 4)
        self.assertEqual(snap3['usage_ids'], snap['usage_ids'], '事件身份稳定')
        self.assertEqual(snap3['aggregate']['events'], 9, '统计不倒退')
        alive = sum(1 for d in snap3['health_sources']['detail']
                    if d['state'] == 'found'
                    and d['client'] in ('workbuddy', 'codex', 'dsh', 'catpaw'))
        self.assertEqual(alive, 4, '恢复后四来源应重新 alive')

        # ============ P4 账本迁移（桌面壳同款 HTTP 编排）============
        p4 = self._run('p4')
        self.assertEqual(p4['quiesce'][0], 200, p4['quiesce'])
        self.assertEqual(p4['plan'][0], 200, p4['plan'])
        self.assertFalse(p4['plan'][1].get('blocked'), p4['plan'][1])
        self.assertEqual(p4['execute'][0], 200, p4['execute'])
        self.assertTrue(p4['execute'][1].get('ok'), p4['execute'])
        ev = p4['execute'][1].get('evidence', {})
        self.assertEqual(ev.get('counts', {}).get('usage'), 5, ev)
        self.assertEqual(ev.get('counts', {}).get('activity'), 4, ev)
        # 新旧两根：计数与身份完全一致；旧根数据安全保留
        self.assertEqual(p4['new_root']['usage'], 5)
        self.assertEqual(p4['new_root']['activity'], 4)
        self.assertEqual(p4['new_root']['ids'], p4['old_root']['ids'])
        self.assertEqual(p4['new_root']['sessions'], p4['old_root']['sessions'])
        self.assertEqual(p4['new_root']['projects'], p4['old_root']['projects'])
        self.assertTrue(os.path.isfile(
            os.path.join(self.data_root, 'usage.db')), '旧根账本必须保留')

        # ============ P5 重新加载（等价 bootstrap 切换后的重启）============
        # 等价桌面壳迁移后的 bootstrap 切换：后端以新 Data Root 重启
        p5 = self._run('p5', extra_env={'USAGE_LEDGER_HOME': self.custom})
        self.assertEqual(os.path.normcase(p5['data_root']),
                         os.path.normcase(self.custom), p5)
        self.assertEqual(p5['aggregate']['events'], 9, '重载后统计一致')
        self.assertEqual(p5['aggregate']['token_records'], 5)
        self.assertEqual(p5['usage_ids'], snap['usage_ids'], '重载后身份一致')
        self.assertEqual(p5['health_sources_found'], 4,
                         '重载后来源配置（sources.json）随迁移一致')
        bc5 = {b['client']: b for b in p5['by_client']}
        self.assertEqual(bc5['workbuddy']['total'], 3900)
        self.assertEqual(bc5['catpaw']['token_records'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
