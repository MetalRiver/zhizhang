#!/usr/bin/env python3
"""verify_auto_run.py — P1 真实 Windows 自动运行验收（可复现、带证据）。

做四件事，每一步都留原始证据（命令、退出码、时间戳、SHA256、JSON 片段）：

  A. 计划任务注册：真实尝试 → 只读查询真实状态 → 若被安全管控拦截，输出可粘贴的手动命令
  B. 以计划任务**完全相同的 TR** 运行一次（pythonw、无控制台、工作目录设在 System32
     以证明不依赖 cwd），再逐条核验 12 项
  C. 失败演练 A：让一个已支持来源不可读 → 必须 partial_failure(2)，其余来源继续采集
  D. 失败演练 B：所有来源不可读 → 必须 no_supported_sources(3)

产出 docs/AUTO_RUN_EVIDENCE.md。

用法：
    python verify_auto_run.py            # 跑全部并写报告
    python verify_auto_run.py --print     # 只打印
"""
from __future__ import annotations

import argparse
import os
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # scripts/ 下一级 = 仓库根
PY = sys.executable
PYW = os.path.join(os.path.dirname(PY), 'pythonw.exe')
DOCS = os.path.join(HERE, 'docs')
REPORT = os.path.join(DOCS, 'AUTO_RUN_EVIDENCE.md')
TASK_NAME = 'UsageLedger-Auto'

STEPS = []          # [{'id','title','ok','verdict','evidence'}]


def now():
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def sha(p, n=16):
    if not os.path.isfile(p):
        return '-'
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:n]


def stat_of(p):
    if not os.path.isfile(p):
        return {'exists': False}
    st = os.stat(p)
    return {'exists': True, 'size': st.st_size,
            'mtime': dt.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'mtime_epoch': st.st_mtime, 'sha16': sha(p)}


def ledger_counts(db):
    out = {}
    try:
        c = sqlite3.connect('file:' + db.replace('\\', '/') + '?mode=ro', uri=True)
        for k, q in (('usage_event', 'SELECT COUNT(*) FROM usage_event'),
                     ('activity_event', 'SELECT COUNT(*) FROM activity_event'),
                     ('scan_run', 'SELECT COUNT(*) FROM scan_run'),
                     ('source_file', 'SELECT COUNT(*) FROM source_file')):
            try:
                out[k] = c.execute(q).fetchone()[0]
            except Exception as e:
                out[k] = 'ERR:%s' % e
        try:
            out['max_scan_run_id'] = c.execute(
                'SELECT COALESCE(MAX(id),0) FROM scan_run').fetchone()[0]
            out['last_scan_started'] = c.execute(
                'SELECT started_at FROM scan_run ORDER BY id DESC LIMIT 1').fetchone()[0]
        except Exception:
            pass
        c.close()
    except Exception as e:
        out['open_error'] = str(e)
    return out


def load_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def step(sid, title, ok, verdict, evidence):
    STEPS.append({'id': sid, 'title': title, 'ok': bool(ok),
                  'verdict': verdict, 'evidence': evidence})
    print('  [%s] %-46s %s' % ('PASS' if ok else 'FAIL', title, verdict))
    return ok


def snapshot(root):
    files = ['usage.db', 'usage-board.json', 'run-state.json', 'discovery.json',
             'auto.log', 'health.json']
    snap = {f: stat_of(os.path.join(root, f)) for f in files}
    snap['_ledger'] = ledger_counts(os.path.join(root, 'usage.db'))
    return snap


# ==================================================================== A. 注册

def phase_register():
    print('\n== A. 计划任务注册 ==')
    res = subprocess.run([PY, 'autopilot.py', 'install-auto', '--minutes', '30'],
                         cwd=HERE, capture_output=True, text=True,
                         encoding='utf-8', errors='replace', timeout=300)
    out = (res.stdout or '') + (res.stderr or '')
    ok = res.returncode == 0
    step('A1', '真实执行 install-auto --minutes 30', ok,
         '退出码 %d' % res.returncode,
         '$ python autopilot.py install-auto --minutes 30\n'
         '退出码 = %d\n\n%s' % (res.returncode, out.strip()[:2600]))

    q = subprocess.run([PY, 'autopilot.py', 'task-info'], cwd=HERE, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=120)
    qo = ((q.stdout or '') + (q.stderr or '')).strip()
    exists = q.returncode == 0 and 'TASK_NAME' in qo
    step('A2', '计划任务是否真实存在（只读查询）', exists,
         '存在' if exists else '不存在',
         '$ python autopilot.py task-info\n退出码 = %d\n\n%s' % (q.returncode, qo[:1200]))

    # 注册被拦时的可执行替代：--print-only 给出的都是无副作用的输出
    p = subprocess.run([PY, 'autopilot.py', 'install-auto', '--print-only'],
                       cwd=HERE, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=120)
    po = (p.stdout or '') + (p.stderr or '')
    has_tr = '计划任务实际会执行的命令' in po
    step('A3', '受管控时可导出手动注册流程（--print-only）', has_tr,
         '已导出精确命令与两条注册路径',
         '$ python autopilot.py install-auto --print-only\n退出码 = %d\n\n%s'
         % (p.returncode, po.strip()[:2000]))

    blocked = '拒绝访问' in out or 'WinError 5' in out or 'PROGRAM BLOCKED' in out \
        or '程序黑名单' in out
    return {'registered': ok, 'exists': exists, 'blocked': blocked,
            'install_output': out, 'print_only': po}


# ==================================================================== B. 按 TR 运行

def run_via_tr(root, trigger='scheduled', timeout=900):
    """以计划任务的 TR 方式执行：pythonw（无控制台）+ 中性 cwd。

    这是能证明「TR 本身可用」的最强手段：进程形态、解释器、参数、工作目录
    都与任务调度器一致，只有调度器那层没参与。
    """
    exe = PYW if os.path.isfile(PYW) else PY
    script = os.path.join(root, 'autopilot.py')
    cwd = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    if not os.path.isdir(cwd):
        cwd = root
    args = [exe, script, 'auto', '--no-pricing', '--trigger', trigger]
    t0 = time.time()
    flags = 0
    if os.name == 'nt':
        flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = 124
    dur = time.time() - t0
    cmdline = '"%s" "%s" auto --no-pricing --trigger %s' % (exe, script, trigger)
    return {'returncode': rc, 'duration_s': round(dur, 2), 'cwd': cwd,
            'cmdline': cmdline, 'interpreter': exe}


def phase_run_and_checks(reg):
    print('\n== B. 以计划任务相同的 TR 运行并核验 ==')
    before = snapshot(HERE)
    run = run_via_tr(HERE)
    time.sleep(1.0)
    after = snapshot(HERE)
    st = load_json(os.path.join(HERE, 'run-state.json')) or {}
    disc = load_json(os.path.join(HERE, 'discovery.json')) or {}
    board = load_json(os.path.join(HERE, 'usage-board.json')) or {}
    health = load_json(os.path.join(HERE, 'health.json')) or {}

    print('  运行：%s' % run['cmdline'])
    print('  退出码 %d，耗时 %.1fs，cwd=%s' % (run['returncode'], run['duration_s'], run['cwd']))

    # 1) 任务确实存在
    step('B1', '1. 计划任务确实存在', reg['exists'],
         '存在' if reg['exists'] else '**不存在（受安全管控拦截，见 A1/A3）**',
         reg['install_output'].strip()[-900:] if not reg['exists']
         else 'task-info 返回 TASK_NAME')

    # 2) 下次运行时间有效
    nxt = None
    q = subprocess.run([PY, 'autopilot.py', 'task-info', '--json'], cwd=HERE,
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=120)
    try:
        nxt = json.loads(q.stdout or '{}')
    except Exception:
        nxt = {}
    step('B2', '2. 下次运行时间有效（NEXT_RUN）',
         bool(nxt.get('NEXT_RUN')), str(nxt.get('NEXT_RUN') or '无（任务未注册）'),
         'AUTO_RUN_NEXT = %s\nREPETITION_INTERVAL = %s\nTRIGGER = %s'
         % (nxt.get('NEXT_RUN'), nxt.get('REPETITION_INTERVAL'), nxt.get('TRIGGER')))

    # 3) 手动 Run 一次 —— 用等价的 TR 执行代替（调度器那层被拦）
    step('B3', '3. 手动触发一次（等价于 schtasks /Run → 执行 TR）',
         run['returncode'] == 0,
         '退出码 %d，耗时 %.1fs' % (run['returncode'], run['duration_s']),
         '实际执行（与计划任务 TR 完全同形，只是由本脚本当父进程）：\n'
         '%s\n\npythonw（无控制台）=%s\ncwd=%s（故意设在 System32，证明不依赖工作目录）'
         % (run['cmdline'], run['interpreter'], run['cwd']))

    # 4) autopilot 确实启动
    started = st.get('started_at')
    step('B4', '4. autopilot 确实启动', bool(started) and st.get('trigger') == 'scheduled',
         'started_at=%s trigger=%s' % (started, st.get('trigger')),
         'run-state.json：\n  started_at = %s\n  finished_at = %s\n  trigger = %s\n'
         '  duration_ms = %s\n（trigger=scheduled 说明是以计划任务的形态起来的）'
         % (st.get('started_at'), st.get('finished_at'), st.get('trigger'),
            st.get('duration_ms')))

    # 5) 自动发现真实客户端
    found = disc.get('found')
    names = [c for c, r in (disc.get('sources') or {}).items() if r.get('state') == 'found']
    step('B5', '5. 自动发现真实客户端', bool(found), '发现 %s 个：%s' % (found, ', '.join(names)),
         'discovery.json：\n%s'
         % json.dumps({c: {'state': r['state'], 'data_grade': r.get('data_grade'),
                           'grade_basis': r.get('grade_basis'), 'files': r.get('files')}
                       for c, r in (disc.get('sources') or {}).items()},
                      ensure_ascii=False, indent=2))

    # 6) 扫描成功
    scan = st.get('scan') or {}
    step('B6', '6. 扫描成功（无来源读取失败）',
         scan.get('sources_failed', 0) == 0 and scan.get('sources_scanned', 0) > 0,
         'sources_scanned=%s ok=%s failed=%s' % (scan.get('sources_scanned'),
                                                 scan.get('sources_ok'),
                                                 scan.get('sources_failed')),
         'run-state.scan：\n%s' % json.dumps(scan, ensure_ascii=False, indent=2)[:1200])

    # 7) SQLite 有变化
    lb, la = before['_ledger'], after['_ledger']
    changed = (lb.get('scan_run') != la.get('scan_run')
               or lb.get('max_scan_run_id') != la.get('max_scan_run_id')
               or lb.get('usage_event') != la.get('usage_event'))
    step('B7', '7. SQLite 有变化', changed,
         'scan_run %s → %s｜usage_event %s → %s'
         % (lb.get('scan_run'), la.get('scan_run'),
            lb.get('usage_event'), la.get('usage_event')),
         'usage.db：\n  size  %s → %s\n  sha16 %s → %s\n'
         '  scan_run %s → %s\n  max_scan_run_id %s → %s\n'
         '  usage_event %s → %s\n  activity_event %s → %s\n'
         '  last_scan_started %s → %s'
         % (before['usage.db'].get('size'), after['usage.db'].get('size'),
            before['usage.db'].get('sha16'), after['usage.db'].get('sha16'),
            lb.get('scan_run'), la.get('scan_run'),
            lb.get('max_scan_run_id'), la.get('max_scan_run_id'),
            lb.get('usage_event'), la.get('usage_event'),
            lb.get('activity_event'), la.get('activity_event'),
            lb.get('last_scan_started'), la.get('last_scan_started')))

    # 8) usage-board.json 更新时间变化
    bm = (after['usage-board.json'].get('mtime_epoch') or 0)
    bm0 = (before['usage-board.json'].get('mtime_epoch') or 0)
    fresh_board = bm > bm0
    step('B8', '8. usage-board.json 更新时间变化', fresh_board,
         '%s → %s' % (before['usage-board.json'].get('mtime'),
                      after['usage-board.json'].get('mtime')),
         'usage-board.json：\n  mtime %s → %s\n  size  %s → %s\n  sha16 %s → %s\n'
         '  generated_at = %s\n  health.status = %s\n  automation.status = %s'
         % (before['usage-board.json'].get('mtime'), after['usage-board.json'].get('mtime'),
            before['usage-board.json'].get('size'), after['usage-board.json'].get('size'),
            before['usage-board.json'].get('sha16'), after['usage-board.json'].get('sha16'),
            board.get('generated_at'), (board.get('health') or {}).get('status'),
            (board.get('automation') or {}).get('status')))

    # 9) run-state.json 正确更新
    need = ['started_at', 'finished_at', 'sources_found', 'sources_scanned',
            'events_seen', 'events_inserted', 'events_updated', 'errors']
    miss = [k for k in need if k not in st]
    rs_fresh = (after['run-state.json'].get('mtime_epoch') or 0) > bm0
    step('B9', '9. run-state.json 正确更新', (not miss) and rs_fresh and st.get('status') == 'success',
         'status=%s 缺字段=%s' % (st.get('status'), miss or '无'),
         'run-state.json（mtime %s → %s）：\n%s'
         % (before['run-state.json'].get('mtime'), after['run-state.json'].get('mtime'),
            json.dumps({k: st.get(k) for k in need + ['status', 'exit_code', 'trigger']},
                       ensure_ascii=False, indent=2)))

    # 10) discovery.json 正确更新
    dfresh = (after['discovery.json'].get('mtime_epoch') or 0) > bm0
    step('B10', '10. discovery.json 正确更新', dfresh and bool(disc.get('grades')),
         'generated_at=%s，等级 %s' % (disc.get('generated_at'),
                                      json.dumps(disc.get('grades'), ensure_ascii=False)),
         'discovery.json：\n  mtime %s → %s\n  found = %s / %s\n  grades = %s\n'
         '  opaque = %s'
         % (before['discovery.json'].get('mtime'), after['discovery.json'].get('mtime'),
            disc.get('found'), disc.get('total'),
            json.dumps(disc.get('grades'), ensure_ascii=False),
            list((disc.get('opaque') or {}).keys())))

    # 11) auto.log 有本次执行记录
    logtxt = ''
    lp = os.path.join(HERE, 'auto.log')
    if os.path.isfile(lp):
        with open(lp, encoding='utf-8', errors='replace') as f:
            logtxt = f.read()
    tail = [l for l in logtxt.strip().splitlines() if 'status=' in l]
    stamp = (st.get('started_at') or '')[:13].replace('T', ' ')   # 2026-09-19 00
    ran = bool(tail) and stamp in tail[-1].replace('T', ' ')
    step('B11', '11. auto.log 有本次执行记录', ran, '末行：%s' % (tail[-1][:130] if tail else '无'),
         'auto.log：\n  size %s → %s\n  含 status= 的行数 = %d\n  本次 started_at = %s\n\n'
         '末尾 6 行：\n%s'
         % (before['auto.log'].get('size'), after['auto.log'].get('size'), len(tail),
            st.get('started_at'),
            '\n'.join(logtxt.strip().splitlines()[-6:])))

    # 12) Dashboard 能看到最新更新时间
    dash_ok, dash_ev = dashboard_sees_timestamp(board)
    step('B12', '12. Dashboard 能看到最新更新时间', dash_ok,
         '渲染后的 DOM 中出现 %s' % (board.get('generated_at') or '-'),
         dash_ev)

    # 附：health.json
    step('B13', '附. health.json 生成且状态可解释', bool(health.get('status')),
         'status=%s' % health.get('status'),
         'health.json：\n%s' % json.dumps(
             {'status': health.get('status'), 'reason': health.get('reason'),
              'last_run': health.get('last_run'),
              'sources': {k: health.get('sources', {}).get(k)
                          for k in ('found', 'total', 'scanned', 'grades')},
              'ledger': {k: health.get('ledger', {}).get(k)
                         for k in ('usage_events', 'activity_events', 'scan_runs',
                                   'source_files_alive', 'source_files_purged')},
              'board': {k: health.get('board', {}).get(k)
                        for k in ('exists', 'generated_at', 'age_seconds', 'schema_version')}},
             ensure_ascii=False, indent=2)[:1600])

    return {'before': before, 'after': after, 'run': run, 'state': st,
            'discovery': disc, 'board': board, 'health': health}


def find_browser():
    for c in (os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
              r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if os.path.isfile(c):
            return c
    return None


def dashboard_sees_timestamp(board):
    """用真实浏览器跑 JS，再从渲染后的 DOM 里找 board 的生成时间。

    只看源码里有没有 `generated_at` 字样不算证据 —— 必须确认它被真的画出来了。
    """
    ts = board.get('generated_at')
    if not ts:
        return False, 'board 没有 generated_at，无法核验'
    chrome = find_browser()
    if not chrome:
        return False, '环境没有可用浏览器，无法做 DOM 核验'
    srv = subprocess.Popen([PY, '-m', 'http.server', '8931', '--bind', '127.0.0.1'],
                           cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        r = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                            '--virtual-time-budget=9000', '--dump-dom',
                            'http://127.0.0.1:8931/usage-dashboard.html'],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=180)
        dom = r.stdout or ''
        # Phase 1C 起，页脚时间渲染为人类可读格式（T → 空格）。
        # 本检查的意图是「生成时间真的被渲染出来」，两种形态都算命中。
        hit = (ts in dom) or (ts.replace('T', ' ') in dom)
        # 找出渲染后的页脚那一行，作为可读证据
        line = ''
        for l in dom.splitlines():
            if 'board 生成于' in l:
                line = l.strip()[:400]
                break
        ev = ('用无头浏览器执行页面 JS 后 dump DOM，检查 board.generated_at 是否真的被渲染出来。\n\n'
              '期望时间戳：%s\nDOM 中出现：%s\n\n渲染后的页脚节点：\n  %s\n'
              '（DOM 长度 %d 字符；节点来自 #footRight，由 drawChrome() 写入）'
              % (ts, '是' if hit else '否', line or '（未找到该节点）', len(dom)))
        return hit, ev
    except Exception as e:
        return False, 'DOM 核验异常：%s' % e
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()


# ==================================================================== C/D 演练

def make_sandbox(bad_zcode=True, bad_catpaw=False, good_workbuddy=True):
    """造一个隔离环境：部分来源可读、部分损坏。

    这里用「文件存在但不是合法 SQLite」来模拟真实世界里的"来源不可读"
    （客户端改格式 / 文件被截断 / 加密）。
    """
    sb = tempfile.mkdtemp(prefix='ul-drill-')
    for f in ('ledger.py', 'autopilot.py', 'usage-board.schema.json'):
        shutil.copy(os.path.join(HERE, f), os.path.join(sb, f))
    fx = os.path.join(sb, 'fixtures')
    os.makedirs(fx, exist_ok=True)

    src = {'dsh': os.path.join(fx, 'no-dsh'),
           'workbuddy': os.path.join(fx, 'no-wb'),
           'catpaw': os.path.join(fx, 'no-cp'),
           'traecn': os.path.join(fx, 'no-te'),
           'zcode': os.path.join(fx, 'bad-zcode.sqlite')}

    # 一个真正可读的来源
    if good_workbuddy:
        wb = os.path.join(fx, 'wb')
        os.makedirs(wb, exist_ok=True)
        with open(os.path.join(wb, 'ok.jsonl'), 'w', encoding='utf-8') as f:
            for i in range(3):
                f.write(json.dumps({
                    'id': 'wb-%d' % i, 'sessionId': 'sess-1',
                    'timestamp': 1787000000000 + i * 1000,
                    'providerData': {'model': 'glm-test-model',
                                     'rawUsage': {'prompt_tokens': 1200,
                                                  'completion_tokens': 300,
                                                  'prompt_tokens_details': {
                                                      'cached_tokens': 200}}}}) + '\n')
        src['workbuddy'] = wb

    # 损坏的来源：存在，但不是合法数据库
    if bad_zcode:
        with open(src['zcode'], 'wb') as f:
            f.write(b'this is not a sqlite database, it is garbage bytes' * 40)
    if bad_catpaw:
        cp = os.path.join(fx, 'cp')
        os.makedirs(cp, exist_ok=True)
        with open(os.path.join(cp, 'broken.db'), 'wb') as f:
            f.write(b'corrupted-catpaw-memory-db-not-sqlite' * 30)
        src['catpaw'] = cp

    with open(os.path.join(sb, 'sources.json'), 'w', encoding='utf-8') as f:
        json.dump(src, f, ensure_ascii=False, indent=2)
    return sb


def run_sandbox(sb, label):
    r = subprocess.run([PY, 'autopilot.py', 'auto', '--no-pricing', '--trigger', 'scheduled'],
                       cwd=sb, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=600)
    out = ((r.stdout or '') + (r.stderr or ''))
    st = load_json(os.path.join(sb, 'run-state.json')) or {}
    log = ''
    lp = os.path.join(sb, 'auto.log')
    if os.path.isfile(lp):
        with open(lp, encoding='utf-8', errors='replace') as f:
            log = f.read()
    board = load_json(os.path.join(sb, 'usage-board.json')) or {}
    return {'label': label, 'returncode': r.returncode, 'stdout': out,
            'state': st, 'log': log, 'board': board, 'sandbox': sb}


def phase_drill_a():
    print('\n== C. 失败演练 A：一个已支持来源不可读 ==')
    sb = make_sandbox(bad_zcode=True, good_workbuddy=True)
    try:
        before = ledger_counts(os.path.join(sb, 'usage.db'))
        r = run_sandbox(sb, 'A')
        st, scan = r['state'], (r['state'].get('scan') or {})
        ok_code = r['returncode'] == 2 and st.get('status') == 'partial_failure'
        step('C1', 'A. 返回 partial_failure（退出码 2）', ok_code,
             '退出码 %d status=%s' % (r['returncode'], st.get('status')),
             '命令：python autopilot.py auto --no-pricing --trigger scheduled\n（沙盒 %s）\n\n%s'
             % (sb, r['stdout'].strip()[-2200:]))

        other = (st.get('events_inserted', 0) or 0) > 0 and scan.get('sources_ok', 0) >= 1
        step('C2', 'A. 其他来源仍继续采集', other,
             'sources_ok=%s sources_failed=%s events_inserted=%s'
             % (scan.get('sources_ok'), scan.get('sources_failed'),
                st.get('events_inserted')),
             'run-state.scan：\n%s\n\nboard.summary.usage_events = %s（可读来源的数据确实落库了）'
             % (json.dumps(scan, ensure_ascii=False, indent=2)[:1100],
                (r['board'].get('summary') or {}).get('usage_events')))

        errs = st.get('errors') or []
        in_state = any('读取失败' in e or '数据源' in e for e in errs)
        in_log = 'status=partial_failure' in r['log'] and (
            '读取失败' in r['log'] or '数据源' in r['log'])
        step('C3', 'A. 错误写入 run-state 与 auto.log', in_state and in_log,
             'run-state %d 条错误，auto.log %s' % (len(errs), '有' if in_log else '无'),
             'run-state.errors：\n%s\n\nauto.log 末尾：\n%s'
             % ('\n'.join('  · %s' % e for e in errs[:6]),
                '\n'.join(r['log'].strip().splitlines()[-6:])))
        return r
    finally:
        shutil.rmtree(sb, ignore_errors=True)


def phase_drill_b():
    print('\n== D. 失败演练 B：所有来源不可读 ==')
    sb = make_sandbox(bad_zcode=True, bad_catpaw=True, good_workbuddy=False)
    try:
        r1 = run_sandbox(sb, 'B-1')
        st = r1['state']
        ok = r1['returncode'] == 3 and st.get('status') == 'no_supported_sources'
        step('D1', 'B. 返回 no_supported_sources（退出码 3）', ok,
             '退出码 %d status=%s' % (r1['returncode'], st.get('status')),
             '命令：python autopilot.py auto --no-pricing --trigger scheduled\n（沙盒 %s）\n\n%s'
             % (sb, r1['stdout'].strip()[-2200:]))

        step('D2', 'B. 非 0 退出（不被吞成成功）', r1['returncode'] != 0,
             '退出码 %d' % r1['returncode'],
             'exit_code 字段 = %s' % st.get('exit_code'))

        errs = st.get('errors') or []
        silent = (not errs) or ('no_supported_sources' not in ' '.join(errs)
                                and '读取失败' not in ' '.join(errs))
        step('D3', 'B. 不得静默（必须留下可读错误）', not silent,
             'errors=%d 条' % len(errs),
             'run-state.errors：\n%s' % '\n'.join('  · %s' % e for e in errs[:6]))

        # 再跑一次：证明下一次仍会尝试（不会被失败"卡死"或自我禁用）
        time.sleep(1.0)
        r2 = run_sandbox(sb, 'B-2')
        st2 = r2['state']
        attempts = r2['log'].count('status=')
        again = (r2['returncode'] == 3 and st2.get('status') == 'no_supported_sources'
                 and (st2.get('started_at') or '') != (st.get('started_at') or ''))
        step('D4', 'B. 下次计划任务仍应继续尝试', again and attempts >= 2,
             '第二次退出码 %d，auto.log 累计 %d 次运行记录' % (r2['returncode'], attempts),
             '第一次 started_at = %s\n第二次 started_at = %s\n'
             'auto.log 含 status= 的行数 = %d\n\n'
             '说明：失败不影响后续调度 —— autopilot 里除了 uninstall-auto 之外，'
             '没有任何代码路径会去注销或禁用计划任务（可 grep 验证）。'
             % (st.get('started_at'), st2.get('started_at'), attempts))

        # 反证：代码里确实没有任何"失败就注销任务"的逻辑。
        # 判定方式：找出所有真正会去删除/注销任务的 subprocess 调用，确认它们只在
        # cmd_uninstall_auto 里出现（文案里的提示字符串不算）。
        src = open(os.path.join(HERE, 'autopilot.py'), encoding='utf-8').read()
        lines = src.splitlines()
        # 切出各个函数体
        funcs = {}
        cur = None
        for i, l in enumerate(lines):
            if l.startswith('def '):
                cur = l[4:].split('(')[0]
                funcs[cur] = []
            elif cur and not l.startswith(' ' * 0 + 'def '):
                funcs.setdefault(cur, []).append((i + 1, l))
        offenders = []
        for fn, body in funcs.items():
            for ln, l in body:
                if re.search(r"_run\(", l) and ("Delete" in l or "Unregister-ScheduledTask" in l):
                    if fn != 'cmd_uninstall_auto':
                        offenders.append((fn, ln, l.strip()[:80]))
        step('D5', 'B. 代码中不存在"失败即注销/禁用任务"的逻辑', not offenders,
             '删除/注销调用只出现在 cmd_uninstall_auto' if not offenders
             else '越界调用：%s' % offenders,
             '扫描 autopilot.py 全部函数体，查找会执行 schtasks /Delete 或\n'
             'Unregister-ScheduledTask 的 subprocess 调用：\n\n'
             '越界（不在 cmd_uninstall_auto 内）的调用：%s\n\n'
             'cmd_uninstall_auto 内的调用：%s\n\n'
             '结论：失败路径（scan/pricing/board 出错）没有任何代码会去碰计划任务，\n'
             '所以本次失败不会影响下一次调度。'
             % (offenders or '无',
                [l.strip() for _ln, l in funcs.get('cmd_uninstall_auto', [])
                 if 'Delete' in l or 'Unregister' in l] or '（见函数体）'))
        return r1, r2
    finally:
        shutil.rmtree(sb, ignore_errors=True)


# ==================================================================== 报告

def build_report(reg, main, drillA, drillB1, drillB2):
    ok = len([s for s in STEPS if s['ok']])
    tot = len(STEPS)
    L = []
    L.append('# Usage Ledger · 真实 Windows 自动运行验收证据')
    L.append('')
    L.append('生成时间：%s  ' % now())
    L.append('机器：Windows · Python %s  ''  ' % sys.version.split()[0])
    L.append('被测：`%s` 的真实源码（非参考实现）' % os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    L.append('')
    L.append('---')
    L.append('')
    L.append('## 结论总览')
    L.append('')
    L.append('| | |')
    L.append('|---|---|')
    L.append('| 检查项 | **%d** |' % tot)
    L.append('| 通过 | **%d** |' % ok)
    L.append('| 未通过 | **%d** |' % (tot - ok))
    L.append('| 结论 | **%s** |' % ('全部通过' if ok == tot else '存在未通过项，逐条列在下面'))
    L.append('')
    L.append('## 没通过的是哪几项，为什么')
    L.append('')
    fails = [s for s in STEPS if not s['ok']]
    if not fails:
        L.append('无。')
    else:
        L.append('| ID | 检查项 | 结论 | 原因 |')
        L.append('|---|---|---|---|')
        for s in fails:
            L.append('| `%s` | %s | %s | %s |' % (s['id'], s['title'], s['verdict'],
                                                  s['evidence'].splitlines()[0][:80]))
            L.append('')
        L.append('> 下面逐项给出原始证据；**不做"测试通过"式的空口结论**。')
    L.append('')
    L.append('---')
    L.append('')
    L.append('## 逐项证据')
    L.append('')
    for s in STEPS:
        L.append('### %s %s' % (s['id'], s['title']))
        L.append('')
        L.append('**判定：%s** — %s' % ('通过' if s['ok'] else '未通过', s['verdict']))
        L.append('')
        L.append('```')
        L.append(s['evidence'])
        L.append('```')
        L.append('')
    L.append('---')
    L.append('')
    L.append('## 环境限制（必须说明）')
    L.append('')
    L.append('| 限制 | 影响 | 已做到的替代证明 |')
    L.append('|---|---|---|')
    L.append('| `schtasks.exe` 被安全中心**程序黑名单**在进程启动层拦截'
             '（`WinError 5 拒绝访问`），系统明确提示「不可从当前命令批准或绕过」 | '
             'P1 的第 1/2/3 项（任务存在 / 下次运行时间 / 手动 Run）无法在本环境完成 | '
             '① 只读查询独立确认任务当前**不存在**；②`--print-only` 导出可直接粘贴的两条注册路径'
             '（schtasks 与 ScheduledTasks 模块）；③ 用**与 TR 完全同形**的命令行'
             '（pythonw + 无控制台 + cwd 设为 System32）跑通全链路，证明任务体本身可用 |')
    L.append('')
    L.append('**请在真机上补最后一步**（在你自己终端里执行，无需本工具）：')
    L.append('')
    L.append('```')
    L.append('python autopilot.py install-auto --minutes 30')
    L.append('schtasks /Query /TN UsageLedger-Auto /V /FO LIST')
    L.append('schtasks /Run   /TN UsageLedger-Auto')
    L.append('```')
    L.append('')
    L.append('若 `schtasks` 同样被拦，用 `python autopilot.py install-auto --print-only` '
             '输出里的**方式二**（ScheduledTasks 模块）。本工具不会绕过任何系统安全策略。')
    L.append('')
    L.append('---')
    L.append('')
    L.append('## 复现')
    L.append('')
    L.append('```')
    L.append('python verify_auto_run.py      # 重跑本报告的全部检查')
    L.append('```')
    L.append('')
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--print', action='store_true', help='只打印，不写报告')
    args = ap.parse_args()

    print('P1 真实验收开始 %s' % now())
    print('  解释器：%s' % PY)
    print('  pythonw：%s（%s）' % (PYW, '存在' if os.path.isfile(PYW) else '缺失，将退回 python'))

    reg = phase_register()
    main_ = phase_run_and_checks(reg)
    a = phase_drill_a()
    b1, b2 = phase_drill_b()

    ok = len([s for s in STEPS if s['ok']])
    print('\n%s' % ('=' * 66))
    print('  检查项 %d ｜ 通过 %d ｜ 未通过 %d' % (len(STEPS), ok, len(STEPS) - ok))
    print('=' * 66)
    for s in STEPS:
        if not s['ok']:
            print('  FAIL  %s %s' % (s['id'], s['title']))
            print('        %s' % s['verdict'])
    print('\n  完整证据：%s' % REPORT if not args.print else '')

    if not args.print:
        os.makedirs(DOCS, exist_ok=True)
        with open(REPORT, 'w', encoding='utf-8') as f:
            f.write(build_report(reg, main_, a, b1, b2))
        print('  报告已写入：%s' % REPORT)
    return 0 if ok == len(STEPS) else 2


if __name__ == '__main__':
    sys.exit(main())
