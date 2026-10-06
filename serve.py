#!/usr/bin/env python3
"""serve.py — 智账（PathOrbit AI Ledger）统一本地服务（Web + Local API）。

一个进程同时提供：
    1. 丝路 Web Dashboard（web/ 静态资源）
    2. Local API v1（/api/v1/*，业务事实 100% 来自 board / health 产物）
    3. usage-board.json 兜底通道
    4. 手动增量扫描（POST /api/v1/scan，唯一写入口）

安全边界（v0）：
    - 只绑定 127.0.0.1，代码层面拒绝其它 host（不做 LAN / 公网暴露）
    - 静态文件白名单在 web/ 内，拒绝目录浏览与 ../ 穿越
    - 不返回 CORS 头；POST scan 校验 Content-Type 与 Origin
    - 扫描互斥锁：并发返回 409；GET 永远只读最近成功的产物文件
    - API 不拥有第二套统计逻辑：不重算成本 / 重放 / 口径

用法：
    python serve.py                 # http://127.0.0.1:8787
    python serve.py --port 8788     # 换端口
    python serve.py --open          # 启动后打开系统浏览器（默认不开）

依赖：仅 Python 标准库（zstandard 是 Core 解析 dsh 的既有依赖，与本文件无关）。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, 'web')

HOST = '127.0.0.1'          # 硬绑定：v0 不提供 --host
DEFAULT_PORT = 8787
SERVER_STATE_PATH = os.path.join(HERE, 'server-state.json')
SERVER_LOG_PATH = os.path.join(HERE, 'server.log')
SERVER_LOG_MAX = 512 * 1024
SCAN_LOCK_STALE_HINT = '删除 usage-ledger-scan.lock 前请先确认锁内 PID 已退出'

SCAN_LOCK = threading.Lock()    # 进程内互斥（跨进程锁由 ledger 扫描锁承担）
SCAN_STATE = {'running': False}

# RC.3 维护静默：桌面壳在真正退出 / 数据迁移前调用 quiesce —— 占用扫描锁、
# 拒绝新写请求，保证收尾期间没有 active scan/write。恢复只经 resume 端点。
MAINTENANCE = {'quiesced': False, 'lock_handle': None}

ALLOWED_ORIGIN_HOSTS = ('127.0.0.1', 'localhost')


# ---------------------------------------------------------------- 安全迁移（RC.3 §29-36）
#
# 原则：源账本永不删除；bootstrap 只在目标完整验证通过后由桌面壳切换；
# 只复制 DURABLE 用户数据，运行态/可再生缓存不搬运成"新环境事实"。

# DURABLE_USER_DATA：账本本体 + 用户配置/状态（§31 重点判断项）
MIGRATION_DURABLE = {
    'usage.db', 'pricing.json', 'pricing.auto.json',
    'sources.json', 'resolved-sources.json',
    'discovery.json', 'discovery-state.json', 'source-catalog.json',
    'replay-state.json', 'auto-schedule.json', 'usage.csv',
}
# usage.db.bak-*：手工归因等操作留下的历史备份，属用户数据
MIGRATION_DURABLE_PREFIX = ('usage.db.bak-',)
# REGENERABLE_CACHE：可由账本重建，但复制后首次体验更平滑
MIGRATION_REGENERABLE = {'usage-board.json', 'usage-board.schema.json', 'report.html'}
# EPHEMERAL_RUNTIME（不复制）：server-state/health/run-state/日志/锁/WAL/SHM/
# 未知文件 —— 一律不搬运，避免把 PID、锁、健康快照变成新环境事实。

_MIGRATION_COUNT_SQL = {
    'usage': 'SELECT COUNT(*) FROM usage_event',
    'activity': 'SELECT COUNT(*) FROM activity_event',
    'projects': 'SELECT COUNT(*) FROM project_registry',
    'sessions': 'SELECT COUNT(*) FROM session_registry',
    'sources': 'SELECT COUNT(*) FROM source_file',
}


def _db_counts(path):
    """只读统计：schema version + 核心表行数（表不存在按 -1 记录）。"""
    out = {}
    conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'),
                           uri=True, timeout=10)
    try:
        out['user_version'] = conn.execute('PRAGMA user_version').fetchone()[0]
        out['quick_check'] = conn.execute('PRAGMA quick_check').fetchone()[0]
        for name, sql in _MIGRATION_COUNT_SQL.items():
            try:
                out[name] = conn.execute(sql).fetchone()[0]
            except sqlite3.Error:
                out[name] = -1
        # 手工归因留痕（§34 关键 registry / manual attribution）
        try:
            out['manual_attribution'] = conn.execute(
                'SELECT COUNT(*) FROM session_registry '
                'WHERE attribution_manual = 1').fetchone()[0]
        except sqlite3.Error:
            out['manual_attribution'] = -1
    finally:
        conn.close()
    return out


def _sha256(path, chunk=1 << 20):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(chunk), b''):
            h.update(blk)
    return h.hexdigest()


def _is_under(child, parent):
    child = os.path.normcase(os.path.abspath(child))
    parent = os.path.normcase(os.path.abspath(parent))
    return child == parent or child.startswith(parent.rstrip('\\/') + os.sep)


def migration_plan(target):
    """目标校验 + 分类清单（§32-33）。blocked=True 时 issues 给出全部原因。"""
    import paths
    issues = []
    cur = os.path.normcase(os.path.normpath(paths.DATA_ROOT))
    tn = os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(target))))

    if tn == cur:
        issues.append({'code': 'same_as_current',
                       'message': '目标位置就是当前账本位置'})
    if _is_under(tn, paths.APP_ROOT) or _is_under(paths.APP_ROOT, tn):
        issues.append({'code': 'install_dir',
                       'message': '不能选择应用安装目录内外的嵌套位置'})
    drive = os.path.splitdrive(tn)[0]
    raw = os.path.splitdrive(os.path.expanduser(target))[1]
    if not drive:
        issues.append({'code': 'bad_path', 'message': '路径无效'})
    elif drive and raw and raw[0] not in ('/', os.sep):
        # "C:foo" 形式：解析结果随进程工作目录漂移
        issues.append({'code': 'bad_path', 'message': '请选择完整的绝对路径（不要使用盘符相对路径）'})
    elif not os.path.isdir(drive + os.sep):
        issues.append({'code': 'drive_missing', 'message': '目标磁盘不可用'})

    # 网络盘 / 可移动盘提醒（RC.3 不承诺支持，§27）
    warnings = []
    if drive and os.path.normcase(drive) != os.path.normcase(
            os.path.splitdrive(os.path.expanduser('~'))[0]):
        try:
            import ctypes
            gtype = ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep)
            if gtype == 4:
                warnings.append('目标位于网络磁盘：建议将智账账本保存在本机固定磁盘。')
            elif gtype == 2:
                warnings.append('目标位于可移动磁盘：换电脑或拔盘后账本将不可用。')
        except Exception:
            pass
    for marker in ('OneDrive', 'Dropbox'):
        if marker.lower() in tn.lower():
            warnings.append('同步盘可能影响本地数据库稳定性，建议使用本机普通文件夹。')
            break

    exists = os.path.isdir(tn)
    has_ledger = False
    nonempty_other = False
    if exists and tn != cur:
        entries = os.listdir(tn)
        if 'usage.db' in entries and os.path.getsize(
                os.path.join(tn, 'usage.db')) > 0:
            has_ledger = True
            issues.append({'code': 'target_has_ledger',
                           'message': '目标位置已有一本智账账本（不允许覆盖；可选择改用它或另选位置）'})
        elif entries:
            nonempty_other = True
            issues.append({'code': 'nonempty_dir',
                           'message': '目标目录包含其它文件（不会覆盖；请选择空文件夹，或使用其下的「智账数据」子目录）'})

    # 可创建/可写验证（目录不存在时试建父级内的探测文件）
    try:
        if not exists:
            os.makedirs(tn, exist_ok=True)
        probe = os.path.join(tn, '.zhizhang-write-test')
        with open(probe, 'w') as f:
            f.write('ok')
        os.remove(probe)
        if not exists:
            os.rmdir(tn)   # 演练不留痕迹
    except OSError as e:
        issues.append({'code': 'not_writable', 'message': '目标位置不可写：%s' % e})

    # 空间预估（持久文件总量 ×2 冗余）
    need = 0
    durable, skipped = [], []
    src_items = []
    try:
        src_items = os.listdir(paths.DATA_ROOT)
    except OSError:
        pass
    for name in src_items:
        p = os.path.join(paths.DATA_ROOT, name)
        if not os.path.isfile(p):
            continue
        size = os.path.getsize(p)
        if name in MIGRATION_DURABLE or name.startswith(MIGRATION_DURABLE_PREFIX):
            durable.append({'name': name, 'bytes': size})
            need += size
        elif name in MIGRATION_REGENERABLE:
            durable.append({'name': name, 'bytes': size, 'class': 'regenerable'})
        else:
            skipped.append({'name': name, 'bytes': size,
                            'class': 'ephemeral_or_unknown'})
    try:
        import shutil
        free = shutil.disk_usage(drive + os.sep if drive else tn).free
        if free < need * 2 + (64 << 20):
            issues.append({'code': 'disk_full',
                           'message': '目标磁盘空间不足（需要约 %.0f MB）' % (need * 2 / 1048576)})
    except OSError:
        pass

    return {
        'target': target,
        'blocked': any(i['code'] in ('same_as_current', 'install_dir', 'bad_path',
                                     'drive_missing', 'not_writable') for i in issues),
        'issues': issues,
        'warnings': warnings,
        'has_ledger': has_ledger,
        'nonempty_dir': nonempty_other,
        'durable': durable,
        'skipped': skipped,
        'total_bytes': need,
        'current_root': paths.DATA_ROOT,
    }


def migration_execute(target):
    """执行复制 + 目标验证（§29 步骤 5-11）。调用方必须已持有扫描锁。

    成功：{'ok': True, 'evidence': {...}} —— bootstrap 仍由桌面壳切换。
    失败：{'ok': False, 'message': ...} —— 源账本未被触碰，原位可用。
    """
    import paths
    plan = migration_plan(target)
    if plan['blocked'] or plan['has_ledger']:
        return {'ok': False,
                'message': '目标校验未通过：%s' % '；'.join(
                    i['message'] for i in plan['issues'])}

    tn = os.path.normpath(os.path.abspath(os.path.expanduser(target)))
    src_db = paths.db_path()
    if not os.path.isfile(src_db):
        return {'ok': False, 'message': '当前账本不存在（usage.db 缺失），中止迁移'}
    try:
        os.makedirs(tn, exist_ok=True)
    except OSError as e:
        return {'ok': False, 'message': '目标目录创建失败：%s' % e}

    # 1) 迁移安全点：源库一致性检查 + backup API 快照复制（§30）
    try:
        src_counts = _db_counts(src_db)
        if src_counts.get('quick_check') != 'ok':
            return {'ok': False,
                    'message': '源账本完整性检查未通过（%s），已保留原状' % src_counts.get('quick_check')}
        dst_db = os.path.join(tn, 'usage.db')
        src = sqlite3.connect(src_db, timeout=30)
        dst = sqlite3.connect(dst_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    except Exception as e:
        return {'ok': False, 'message': '账本快照复制失败：%s' % e}

    # 2) 其余 durable 文件 copy2 + hash
    copied = [{'name': 'usage.db', 'bytes': os.path.getsize(dst_db),
               'sha256': _sha256(dst_db)}]
    for item in plan['durable']:
        name = item['name']
        if name == 'usage.db':
            continue
        sp = os.path.join(paths.DATA_ROOT, name)
        dp = os.path.join(tn, name)
        try:
            import shutil
            shutil.copy2(sp, dp)
            copied.append({'name': name, 'bytes': os.path.getsize(dp),
                           'sha256': _sha256(dp),
                           **({'class': item['class']} if 'class' in item else {})})
        except OSError as e:
            return {'ok': False, 'message': '复制 %s 失败：%s（源账本未受影响）' % (name, e)}

    # 3) 目标验证（§34）：integrity + schema + 计数 + 配置 hash 全等
    try:
        dst_counts = _db_counts(dst_db)
        if dst_counts.get('quick_check') != 'ok':
            return {'ok': False, 'message': '目标账本完整性检查未通过，已保留原状'}
        for k, v in src_counts.items():
            if dst_counts.get(k) != v:
                return {'ok': False,
                        'message': '目标验证失败（%s: %s != %s），已保留原状' % (k, dst_counts.get(k), v)}
        for item in copied:
            # usage.db 经 backup API 快照复制，字节布局可与源文件不同，
            # 由 counts + quick_check 验证；其余 durable 文件逐一比对 hash。
            if item['name'] == 'usage.db':
                continue
            if item['name'] in MIGRATION_DURABLE or item['name'].startswith(MIGRATION_DURABLE_PREFIX):
                sp = os.path.join(paths.DATA_ROOT, item['name'])
                if _sha256(sp) != item['sha256']:
                    return {'ok': False,
                            'message': '目标文件 %s 校验不一致，已保留原状' % item['name']}
    except Exception as e:
        return {'ok': False, 'message': '目标验证异常：%s（源账本未受影响）' % e}

    slog('migration execute：已复制 %d 个文件到 %s（bootstrap 待桌面壳切换）'
         % (len(copied), tn))
    return {'ok': True, 'evidence': {
        'target': tn,
        'copied': copied,
        'skipped': plan['skipped'],
        'counts': src_counts,
    }}


# ---------------------------------------------------------------- server 日志

def slog(line):
    """server.log：服务层痕迹（与 auto.log 的 collector 层分文件）。轮转 512KB。"""
    import datetime as _dt
    try:
        if os.path.isfile(SERVER_LOG_PATH) and \
                os.path.getsize(SERVER_LOG_PATH) > SERVER_LOG_MAX:
            os.replace(SERVER_LOG_PATH, SERVER_LOG_PATH + '.1')
        ts = _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(SERVER_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (ts, line))
    except OSError:
        pass


# ---------------------------------------------------------------- 状态文件

def _load_state():
    try:
        with open(SERVER_STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_state(state):
    import datetime as _dt
    st = {'status': 'running',
          'pid': os.getpid(),
          'started_at': _dt.datetime.now().isoformat(timespec='seconds'),
          'host': HOST,
          'port': int(state['port']),
          'python_executable': sys.executable,
          'version': '1'}
    tmp = SERVER_STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SERVER_STATE_PATH)
    return st


def _clear_state():
    try:
        os.remove(SERVER_STATE_PATH)
    except OSError:
        pass


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return k32.GetLastError() == 5        # 拒绝访问 = 存在，保守视为活
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == 259           # STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _probe_api(port, timeout=1.5):
    """探测 127.0.0.1:port 是否是本产品的 API（health 端点 200/503 都算）。"""
    import http.client
    try:
        c = http.client.HTTPConnection(HOST, int(port), timeout=timeout)
        c.request('GET', '/api/v1/health')
        r = c.getresponse()
        r.read()
        c.close()
        return r.status in (200, 503)
    except Exception:
        return False


def preflight(port):
    """启动前单实例检测。

    返回 ('free', None) 可启动；
         ('ours-running', state) 已有本产品服务（不启动第二实例）；
         ('foreign-busy', None) 端口被其它程序占用（绝不强杀）。
    """
    st = _load_state()
    if st and _pid_alive(st.get('pid')):
        if _probe_api(st.get('port') or port):
            return 'ours-running', st
    if _probe_api(port):
        return 'ours-running', st or {}
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.bind((HOST, int(port)))
        occupied = False
    except OSError:
        occupied = True
    finally:
        s.close()
    if occupied:
        return 'foreign-busy', None
    # state 是 stale 的（pid 已死）→ 自愈清理
    if st:
        slog('stale server-state.json 自愈清理（pid=%s 已不存在）' % st.get('pid'))
        _clear_state()
    return 'free', None


def stop_server():
    """停止本产品服务：只信任 server-state.json 记录的 PID，且必须验证
    该进程命令行确实是本产品 serve.py —— 绝不强杀未知进程。"""
    st = _load_state()
    if not st:
        return 0, '没有找到 server-state.json —— 服务未在运行（或已异常退出）'
    pid = st.get('pid')
    if not _pid_alive(pid):
        _clear_state()
        slog('stop：stale state（pid=%s 已不存在）→ 已清理' % pid)
        return 0, '记录的 PID %s 已不存在 —— 已清理过期状态文件' % pid
    cmdline = _pid_cmdline(pid)
    if cmdline is None or 'serve.py' not in cmdline:
        return 2, ('PID %s 的命令行不是本产品 serve.py —— 拒绝终止未知进程。'
                   '请人工确认后处理。' % pid)
    r = subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    ok = r.returncode == 0
    if ok:
        _clear_state()
        slog('stop：已停止 serve.py（pid=%s）' % pid)
        return 0, '已停止 serve.py（pid=%s）' % pid
    return 2, '停止失败：%s' % (r.stderr or r.stdout or '').strip()[:200]


def _pid_cmdline(pid):
    """读取进程命令行用于身份核验（PowerShell；不可用返回 None → 拒绝终止）。"""
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command',
             '(Get-CimInstance Win32_Process -Filter "ProcessId=%s").CommandLine'
             % int(pid)],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return (r.stdout or '').strip()
        return None
    except Exception:
        return None


# ---------------------------------------------------------------- 产物读取

def _load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def board_path():
    return os.path.join(HERE, 'usage-board.json')


def health_path():
    return os.path.join(HERE, 'health.json')


def get_board():
    """board 是业务事实源。优先读产物文件；缺失时只读现场生成一次。

    build_board() 只 SELECT，不写任何文件、不改账本。
    """
    b = _load_json(board_path())
    if b is not None:
        return b
    if SCAN_STATE['running']:
        return None            # 扫描写窗口内不现场生成，交给 GET 走 503
    try:
        sys.path.insert(0, HERE)
        import ledger
        return ledger.build_board()
    except Exception:
        return None


def get_health():
    h = _load_json(health_path())
    if h is not None:
        return h
    try:
        sys.path.insert(0, HERE)
        import autopilot
        # 纯读推导：依据 discovery/board/账本文件；从未运行过 → unknown（不伪造 healthy）
        return autopilot.build_health({})
    except Exception:
        return None


# ---------------------------------------------------------------- backend identity

# Usage Board Schema v1（ledger.build_board 产出）。改动账本/board 结构时同步升级，
# 桌面壳（main.rs）以此为 Reuse 兼容门槛之一。
BOARD_SCHEMA_VERSION = 1
BACKEND_APP_ID = 'usage-ledger'


def _version_from_file():
    """从 VERSION 提取产品版本号。

    VERSION 是随包 datas：dev 在仓库根（serve.py 同目录），frozen onedir 在
    _internal\（同样是 serve.py 同目录）。因此优先取本文件同目录副本；
    再兜底 paths.app_path('VERSION')。解析失败返回 'unknown'
    （壳会因版本不可比对而拒绝 Reuse，方向安全）。
    """
    import re
    candidates = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'VERSION')]
    try:
        import paths
        candidates.append(paths.app_path('VERSION'))
    except Exception:
        pass
    for p in candidates:
        try:
            with open(p, encoding='utf-8') as f:
                m = re.search(r'^version\s+(\S+)', f.read(), re.M)
                if m:
                    return m.group(1)
        except OSError:
            continue
    return 'unknown'


def identity_payload():
    r"""backend 身份（非敏感）。桌面壳 Reuse 前必须校验，杜绝身份盲复用（R11）。

    data_root_kind 描述【本进程真实读写的账本位置】：
      production  frozen 且未覆盖（%LOCALAPPDATA%\UsageLedger；frozen main() 已换 HERE）
      custom      frozen 且用户显式 USAGE_LEDGER_HOME 覆盖
      repo        dev 源码仓（serve.py 的 HERE/board_path 恒为仓库根，
                  不随 USAGE_LEDGER_HOME 改变——这是 dev 代码的真实行为）
    """
    import paths
    app_version = _version_from_file()
    if paths.is_frozen():
        data_root_kind = ('custom'
                          if os.environ.get('USAGE_LEDGER_HOME') else 'production')
    else:
        data_root_kind = 'repo'
    return {
        'app_id': BACKEND_APP_ID,
        'app_version': app_version,
        'runtime_mode': 'frozen' if paths.is_frozen() else 'dev',
        'data_root_kind': data_root_kind,
        'build_id': 'ulb-v%s-s%d' % (app_version, BOARD_SCHEMA_VERSION),
        'schema_version': BOARD_SCHEMA_VERSION,
        'backend_pid': os.getpid(),
    }


# ---------------------------------------------------------------- 扫描编排

def run_scan_once():
    """一次完整编排：发现 → 增量采集 → board → health → run-state。

    Round 7 隔离设计：以**子进程**运行被服务根目录的 autopilot.py
    （fresh interpreter → 必然加载本根目录的 ledger/autopilot，
    杜绝任何模块缓存把写入引向别的账本）。编排复用 autopilot 全链路：
      - 不做价格同步（--no-pricing，唯一联网层被禁用）
      - trigger='api'（run-state/日志可溯源）
      - 跨进程扫描锁由子进程内的 ledger.acquire_scan_lock 原子获取；
        锁被 scheduled/manual writer 占用时子进程 partial 退出 → 本函数映射为 409

    R10-D.4 桌面 sidecar（frozen）模式：autopilot 已随包打进本进程，
    DATA_ROOT 由 paths.py 唯一确定（桌面形态只有一个账本根，不存在
    Round 7 所防的多根串写问题），因此改为**进程内**调用同一编排，
    结果仍以 DATA_ROOT/run-state.json 为准；扫描锁语义不变。
    """
    if getattr(sys, 'frozen', False):
        import paths
        import autopilot as _ap
        _old_argv = sys.argv
        code = 0
        try:
            sys.argv = ['autopilot', 'auto', '--trigger', 'api', '--no-pricing']
            _ap.main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
        except Exception:
            import traceback as _tb
            slog('scan(frozen) 进程内编排异常：%s' % _tb.format_exc().splitlines()[-1])
            code = 1
        finally:
            sys.argv = _old_argv
        rs = _load_json(paths.run_state_path()) or {}
        slog('scan(frozen) 退出码=%s status=%s inserted=%s'
             % (code, rs.get('status'), rs.get('events_inserted', 0)))
        if rs.get('scan_skipped_lock'):
            return {'ok': False, 'status': rs.get('status'),
                    'scan_in_progress': True,
                    'lock_holder': (rs.get('scan') or {}).get('lock_holder') or {},
                    'errors': (rs.get('errors') or [])[:5]}
        return {
            'ok': code == 0 and rs.get('status') == 'success',
            'status': rs.get('status'),
            'exit_code': code,
            'events_inserted': rs.get('events_inserted', 0),
            'events_updated': rs.get('events_updated', 0),
            'board_written': (rs.get('board') or {}).get('written', False),
            'health': (rs.get('health') or {}).get('status'),
            'errors': (rs.get('errors') or [])[:5],
        }
    exe = sys.executable
    script = os.path.join(HERE, 'autopilot.py')
    if not os.path.isfile(script):
        return {'ok': False, 'error': 'autopilot_missing',
                'message': '被服务目录缺少 autopilot.py'}
    r = subprocess.run(
        [exe, script, 'auto', '--trigger', 'api', '--no-pricing'],
        cwd=HERE, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=900)
    rs = _load_json(os.path.join(HERE, 'run-state.json')) or {}
    slog('scan 子进程退出码=%s status=%s inserted=%s'
         % (r.returncode, rs.get('status'), rs.get('events_inserted', 0)))
    if rs.get('scan_skipped_lock'):
        return {'ok': False, 'status': rs.get('status'),
                'scan_in_progress': True,
                'lock_holder': (rs.get('scan') or {}).get('lock_holder') or {},
                'errors': (rs.get('errors') or [])[:5]}
    return {
        'ok': r.returncode == 0 and rs.get('status') == 'success',
        'status': rs.get('status'),
        'exit_code': r.returncode,
        'events_inserted': rs.get('events_inserted', 0),
        'events_updated': rs.get('events_updated', 0),
        'board_written': (rs.get('board') or {}).get('written', False),
        'health': (rs.get('health') or {}).get('status'),
        'errors': (rs.get('errors') or [])[:5],
    }


# ---------------------------------------------------------------- Handler

class LedgerHandler(BaseHTTPRequestHandler):
    server_version = 'UsageLedger/1.0'
    protocol_version = 'HTTP/1.1'

    # ---- 基础输出 ----
    def _send(self, code, payload, content_type='application/json; charset=utf-8'):
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        if isinstance(obj, list):
            self._send(code, obj)
        else:
            self._send(code, {'ok': True, **obj} if isinstance(obj, dict) and
                       'ok' not in obj else obj)

    def _err(self, code, error, message):
        self._send(code, {'ok': False, 'error': error, 'message': message})

    def log_message(self, fmt, *args):     # 安静日志：一行摘要即可
        sys.stdout.write('[serve] %s\n' % (fmt % args))

    # ---- 路由 ----
    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            slog('GET 内部错误：%s' % traceback.format_exc(limit=8))
            self._err(500, 'internal_error', '服务器内部错误')

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            slog('POST 内部错误：%s' % traceback.format_exc(limit=8))
            self._err(500, 'internal_error', '服务器内部错误')

    def _route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # Functional V1 Light UI 是唯一正式 Runtime UI（源：design/functional-v1-master）。
        # 单壳 SPA：/、/explore、/settings 同一壳，由前端按路径定位视图；
        # 无一级 /analyze。V1.1 起 legacy SPA（index.html）与 Overview V2
        # （overview-v2.html、/v2/*）已退役 —— 页面仅存在于 Git 历史，无运行时入口。
        if path in ('/', '/explore', '/settings'):
            return self._static('shell.html', prefix='v1')
        if path.startswith('/v1/'):
            return self._static(path[len('/v1/'):], prefix='v1')
        if path.startswith('/assets/'):
            return self._static(path[len('/assets/'):], prefix='assets')
        if path == '/usage-board.json':
            # 正式 board 兜底通道：直接回仓库根的真实产物
            b = _load_json(board_path())
            if b is None:
                return self._err(503, 'board_unavailable',
                                 'usage-board.json 不存在且无法安全生成')
            return self._send(200, json.dumps(b, ensure_ascii=False).encode('utf-8'))

        if path == '/api/v1/overview':
            b = get_board()
            if b is None:
                return self._err(503, 'board_unavailable',
                                 '账本看板暂不可用（可能正在扫描或尚未生成）')
            return self._send(200, json.dumps(b, ensure_ascii=False).encode('utf-8'))

        if path == '/api/v1/health':
            h = get_health()
            if h is None:
                return self._err(503, 'health_unavailable', '健康状态暂不可用')
            return self._send(200, json.dumps(h, ensure_ascii=False).encode('utf-8'))

        if path == '/api/v1/runtime':
            return self._runtime()

        # R11：backend 身份（桌面壳 Reuse 前握手）。必须先于 board 门控——
        # board 不可用（503）时身份仍必须可读，否则壳无法区分「本产品 dev」与「无关服务」。
        # 直用 _send：契约字段不含 _json 的 ok 包装。
        if path == '/api/v1/identity':
            return self._send(200, identity_payload())

        # R12 Phase 0：Project / Session 下钻（EFFECTIVE 默认口径）。
        #   /api/v1/projects 与详情读取 board.by_project（唯一事实源，不重算成本）；
        #   sessions / events 读取账本 DB 的注册表与原始行（只读连接；token 求和
        #   不含定价——定价语义仍唯一归属 board）。全部只绑定本机、只读打开。
        if path == '/api/v1/projects':
            b = get_board()
            if b is None:
                return self._err(503, 'board_unavailable', '账本看板暂不可用')
            items = b.get('by_project') or []
            limit, offset = self._page_args(parsed)
            return self._json(200, {
                'total': len(items), 'limit': limit, 'offset': offset,
                'items': items[offset:offset + limit]})

        if path.startswith('/api/v1/projects/') and path.endswith('/sessions'):
            # 项目 → 会话列表（分页；token 求和不涉及定价语义）
            b = get_board()
            if b is None:
                return self._err(503, 'board_unavailable', '账本看板暂不可用')
            pk = unquote(path[len('/api/v1/projects/'):-len('/sessions')])
            entry = next((x for x in (b.get('by_project') or [])
                          if x.get('project_key') == pk), None)
            if entry is None:
                return self._err(404, 'project_not_found', '未知 project_key：%s' % pk)
            limit, offset = self._page_args(parsed)
            db = self._ledger_db_ro()
            if db is None:
                return self._err(503, 'ledger_unavailable', '账本 DB 不存在')
            db.row_factory = sqlite3.Row
            try:
                total = db.execute(
                    'SELECT COUNT(*) c FROM session_registry WHERE project_key=?',
                    (pk,)).fetchone()['c']
                # 排序依据真实事件时间（MAX(usage_event.ts_ms)）。
                # session_registry.last_seen_at 是 upsert/扫描时间戳，
                # 同一轮扫描内全部相同，不能表达「最近使用」。
                rows = db.execute("""
                    SELECT s.source, s.session_id, s.display_name, s.agent,
                           s.first_seen_at, s.last_seen_at,
                           COALESCE(u.events,0) events,
                           COALESCE(u.input,0) input,
                           COALESCE(u.output,0) output,
                           COALESCE(u.cache_read,0) cache_read,
                           COALESCE(u.input+u.output,0) total_tokens,
                           u.last_ts
                    FROM session_registry s
                    LEFT JOIN (
                        SELECT client, session_id, COUNT(*) events,
                               SUM(input_tokens) input, SUM(output_tokens) output,
                               SUM(cache_read_tokens) cache_read,
                               MAX(ts_ms) last_ts
                        FROM usage_event WHERE is_replay=0
                        GROUP BY client, session_id) u
                        ON u.client=s.source AND u.session_id=s.session_id
                    WHERE s.project_key=?
                    ORDER BY COALESCE(u.last_ts, 0) DESC
                    LIMIT ? OFFSET ?""", (pk, limit, offset)).fetchall()
                import datetime as _dt
                items = []
                for x in rows:
                    d = dict(x)
                    la = d.pop('last_ts', None)
                    try:
                        d['last_active'] = _dt.datetime.fromtimestamp(la / 1000.0) \
                            .strftime('%Y-%m-%d %H:%M') if la else None
                    except (OSError, ValueError, TypeError):
                        d['last_active'] = None
                    items.append(d)
                return self._json(200, {
                    'project_key': pk, 'display_name': entry.get('display_name'),
                    'total': total, 'limit': limit, 'offset': offset,
                    'items': items})
            finally:
                db.close()

        if path.startswith('/api/v1/projects/'):
            b = get_board()
            if b is None:
                return self._err(503, 'board_unavailable', '账本看板暂不可用')
            segs = [unquote(x) for x in path.split('/') if x]
            pk = segs[3] if len(segs) > 3 else ''
            entry = next((x for x in (b.get('by_project') or [])
                          if x.get('project_key') == pk), None)
            if entry is None:
                return self._err(404, 'project_not_found', '未知 project_key：%s' % pk)
            return self._json(200, entry)

        # V3：未归属会话（project_key IS NULL）——「首次发现 AI 工具 → 归项目」链路
        if path == '/api/v1/sessions/unassigned':
            db = self._ledger_db_ro()
            if db is None:
                # 空账本（未扫描）= 没有未归属会话，这是事实而非错误
                return self._json(200, {'total': 0, 'items': []})
            db.row_factory = sqlite3.Row
            try:
                cols = {r[1] for r in db.execute('PRAGMA table_info(usage_event)')}
                if 'client' not in cols or 'session_id' not in cols:
                    return self._json(200, {'total': 0, 'items': []})
                rf = ' AND is_replay=0' if 'is_replay' in cols else ''
                pkf = ' AND project_key IS NULL' if 'project_key' in cols else ''
                rows = db.execute("""
                    SELECT client AS source, session_id, COUNT(*) events,
                           SUM(input_tokens) input, SUM(output_tokens) output,
                           SUM(input_tokens+output_tokens) total_tokens,
                           MAX(ts_ms) last_ts, MAX(model) sample_model
                    FROM usage_event WHERE 1=1""" + rf + pkf + """
                    GROUP BY client, session_id
                    ORDER BY last_ts DESC""").fetchall()
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='activity_event'").fetchone():
                    activity_rows = db.execute(
                        'SELECT a.client source,a.session_key session_id,COUNT(*) events,'
                        ' NULL input,NULL output,NULL total_tokens,MAX(a.ts_ms) last_ts,'
                        " '' sample_model FROM activity_event a LEFT JOIN session_registry s"
                        ' ON s.source=a.client AND s.session_id=a.session_key'
                        ' WHERE s.project_key IS NULL GROUP BY a.client,a.session_key').fetchall()
                    rows = sorted(list(rows) + list(activity_rows), key=lambda r: r['last_ts'], reverse=True)
                import datetime as _dt
                items = []
                for x in rows:
                    d = dict(x)
                    la = d.pop('last_ts', None)
                    try:
                        d['last_active'] = _dt.datetime.fromtimestamp(
                            la / 1000.0).strftime('%Y-%m-%d %H:%M') if la else None
                    except (OSError, ValueError, TypeError):
                        d['last_active'] = None
                    items.append(d)
                return self._json(200, {'total': len(items), 'items': items})
            finally:
                db.close()

        # Functional V1：联合筛选查询（筛选后聚合再分页；同范围 total）。
        # 口径：total=input+output；cache 单列；EFFECTIVE 默认剔除 REPLAY。
        if path == '/api/v1/query':
            return _query(self, parse_qs(parsed.query))

        # V1.1 Universal Discovery：真实发现引擎（四层，见 discovery.py）。
        # GET = 轻量/缓存发现（启动、First Run 路径，不做昂贵全扫）。
        # POST /api/v1/discover/refresh = 完整重发现（设置页「重新发现」）。
        # 载荷经 public_payload 脱敏：无绝对路径、无 schema fingerprint；
        # 同时保留 legacy sources/grades/opaque 键供既有 UI 消费。
        if path == '/api/v1/discover':
            try:
                sys.path.insert(0, HERE)
                import discovery as _disc
                rep = _disc.run_discovery(full=False)
                return self._json(200, _disc.public_payload(rep))
            except Exception as exc:
                slog('discover 异常：%s' % type(exc).__name__)
                return self._err(500, 'discover_failed',
                                 '发现失败：%s' % type(exc).__name__)

        if path.startswith('/api/v1/sessions/'):
            segs = [unquote(x) for x in path.split('/') if x]
            if len(segs) < 5:
                return self._err(404, 'not_found', '未知路径：%s' % path)
            source = segs[3]
            session_id = segs[4]
            want_events = len(segs) > 5 and segs[5] == 'events'
            qs = parse_qs(parsed.query)
            view = (qs.get('view') or ['effective'])[0]
            if view not in ('effective', 'raw'):
                return self._err(400, 'bad_request', 'view 只支持 effective|raw')
            limit, offset = self._page_args(parsed)
            db = self._ledger_db_ro()
            if db is None:
                return self._err(503, 'ledger_unavailable', '账本 DB 不存在')
            db.row_factory = sqlite3.Row
            try:
                replay_filter = '' if view == 'raw' else ' AND is_replay=0'
                reg = db.execute(
                    'SELECT * FROM session_registry WHERE source=? AND session_id=?',
                    (source, session_id)).fetchone()
                if reg is None:
                    return self._err(404, 'session_not_found',
                                     '未知会话：%s/%s' % (source, session_id))
                agg = db.execute("""
                    SELECT COUNT(*) events, SUM(input_tokens) input,
                           SUM(output_tokens) output, SUM(cache_read_tokens) cache_read
                    FROM usage_event WHERE client=? AND session_id=?""" + replay_filter,
                    (source, session_id)).fetchone()
                if want_events:
                    total = db.execute(
                        'SELECT COUNT(*) c FROM usage_event WHERE client=? AND session_id=?'
                        + replay_filter, (source, session_id)).fetchone()['c']
                    evs = db.execute("""
                        SELECT ts_ms, model, provider, input_tokens, output_tokens,
                               reasoning_tokens, cache_read_tokens, cache_write_tokens,
                               input_tokens + output_tokens AS total_tokens, event_id
                        FROM usage_event WHERE client=? AND session_id=?""" + replay_filter + """
                        ORDER BY ts_ms DESC, event_id LIMIT ? OFFSET ?""",
                        (source, session_id, limit, offset)).fetchall()
                    return self._json(200, {
                        'source': source, 'session_id': session_id, 'view': view,
                        'total': total, 'limit': limit, 'offset': offset,
                        'items': [dict(x) for x in evs]})
                proj = None
                if reg['project_key']:
                    prow = db.execute(
                        'SELECT project_kind, display_name FROM project_registry WHERE project_key=?',
                        (reg['project_key'],)).fetchone()
                    if prow:
                        proj = {'project_key': reg['project_key'],
                                'project_kind': prow['project_kind'],
                                'display_name': prow['display_name']}
                return self._json(200, {
                    'source': source, 'session_id': session_id,
                    'display_name': reg['display_name'], 'agent': reg['agent'],
                    'first_seen_at': reg['first_seen_at'], 'last_seen_at': reg['last_seen_at'],
                    'project': proj, 'view': view,
                    'events': agg['events'] or 0,
                    'input_tokens': agg['input'] or 0,
                    'output_tokens': agg['output'] or 0,
                    'cache_read_tokens': agg['cache_read'] or 0,
                    'total_tokens': (agg['input'] or 0) + (agg['output'] or 0),
                })
            finally:
                db.close()

        b = get_board()
        if b is None and path.startswith('/api/v1/'):
            return self._err(503, 'board_unavailable', '账本看板暂不可用')

        if path == '/api/v1/sources':
            return self._json(200, b.get('sources', []))
        if path == '/api/v1/clients':
            return self._json(200, b.get('clients', []))
        if path == '/api/v1/models':
            return self._json(200, b.get('models', []))
        if path == '/api/v1/activity':
            return self._json(200, b.get('activity', []))
        if path == '/api/v1/recent-activity':
            return self._recent_activity(parse_qs(parsed.query))

        if path == '/api/v1/usage':
            return self._usage(b, parse_qs(parsed.query))

        return self._err(404, 'not_found', '未知路径：%s' % path)

    @staticmethod
    def _page_args(parsed):
        """分页参数：limit（1..500，默认 50）/ offset（≥0，默认 0）。"""
        qs = parse_qs(parsed.query)
        try:
            limit = int((qs.get('limit') or ['50'])[0])
        except (ValueError, TypeError):
            limit = 50
        try:
            offset = int((qs.get('offset') or ['0'])[0])
        except (ValueError, TypeError):
            offset = 0
        return max(1, min(limit, 500)), max(0, offset)

    @staticmethod
    def _ledger_db_ro():
        """只读打开账本 DB（不存在 → None）。绝不写入。"""
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import paths
        db = paths.db_path()
        if not os.path.isfile(db):
            return None
        return sqlite3.connect('file:' + db.replace('\\', '/') + '?mode=ro', uri=True)

    def _recent_activity(self, qs):
        """最近 AI 活动（R13 首页收编）：真实事件时间（ts_ms）驱动。

        返回：
          items        最近 K 条 EFFECTIVE 事件的脱敏摘要（不含 full path /
                       event id / token 级正文）；项目与会话一律用
                       display_name（注册表缺失时退回稳定键，但不展示路径）。
          active_today 今天（本地时区）有真实使用记录的唯一项目数。
          today        本地日期，供前端核对「今天」的口径。

        只读账本 DB；board 不可用时仍可工作（不依赖 board）。
        """
        import datetime as _dt
        try:
            limit = max(1, min(int((qs.get('limit') or ['8'])[0]), 20))
        except (ValueError, TypeError):
            limit = 8
        db = self._ledger_db_ro()
        if db is None:
            return self._err(503, 'ledger_unavailable', '账本 DB 不存在')
        db.row_factory = sqlite3.Row
        try:
            ucols = {r[1] for r in db.execute('PRAGMA table_info(usage_event)')}
            rf = ' AND u.is_replay = 0' if 'is_replay' in ucols else ''
            rf_plain = ' AND is_replay = 0' if 'is_replay' in ucols else ''
            rows = db.execute("""
                SELECT u.ts_ms, u.model, u.input_tokens, u.output_tokens,
                       u.client AS source, u.session_id, u.project_key,
                       COALESCE(NULLIF(p.display_name, ''), u.project_key) AS project_name,
                       COALESCE(NULLIF(s.display_name, ''), u.session_id) AS session_name,
                       s.agent AS agent
                FROM usage_event u
                LEFT JOIN project_registry p ON p.project_key = u.project_key
                LEFT JOIN session_registry s
                       ON s.source = u.client AND s.session_id = u.session_id
                WHERE 1=1""" + rf + """
                ORDER BY u.ts_ms DESC, u.event_id LIMIT ?""",
                (limit,)).fetchall()
            today = _dt.date.today()
            start_ms = int(_dt.datetime.combine(
                today, _dt.time.min).timestamp() * 1000)
            active_today = db.execute(
                'SELECT COUNT(DISTINCT project_key) c FROM usage_event'
                ' WHERE project_key IS NOT NULL AND ts_ms >= ?' + rf_plain,
                (start_ms,)).fetchone()['c']
            # 每个项目最近一次真实活动（ts_ms 口径）——Work Map 活跃状态的数据源。
            # 与 last_seen（upsert 扫描时间戳）无关。
            project_last = {}
            for r in db.execute(
                    'SELECT project_key, MAX(ts_ms) m FROM usage_event'
                    ' WHERE project_key IS NOT NULL' + rf_plain +
                    ' GROUP BY project_key'):
                try:
                    project_last[r['project_key']] = \
                        _dt.datetime.fromtimestamp(r['m'] / 1000.0) \
                            .strftime('%Y-%m-%d %H:%M')
                except (OSError, ValueError, TypeError):
                    continue
        finally:
            db.close()
        items = []
        for x in rows:
            ts = None
            try:
                ts = _dt.datetime.fromtimestamp(x['ts_ms'] / 1000.0) \
                    .strftime('%Y-%m-%d %H:%M')
            except (OSError, ValueError, TypeError):
                ts = None
            items.append({
                'ts': ts,
                'source': x['source'],
                'model': x['model'] or '',
                'project_key': x['project_key'],
                'project_name': x['project_name'],
                'session_id': x['session_id'],
                'session_name': x['session_name'],
                'agent': x['agent'],
                'tokens': (x['input_tokens'] or 0) + (x['output_tokens'] or 0),
            })
        return self._json(200, {
            'items': items,
            'active_today': active_today,
            'today': today.isoformat(),
            'project_last': project_last,
        })

    def _usage(self, board, qs):
        """board.daily 的合法窗口投影。绝不用 0 补不存在的天数。"""
        daily = board.get('daily') or []
        window = board.get('window_days')
        days = None
        if 'days' in qs:
            try:
                days = int(qs['days'][0])
            except (ValueError, IndexError):
                return self._err(400, 'bad_request', 'days 必须是整数')
            if days < 1 or days > 366:
                return self._err(400, 'bad_request', 'days 取值 1..366')
        rows = daily
        requested = days
        available = len({r.get('day') for r in daily})
        truncated = False
        if days is not None:
            rows = daily[-days:] if days < len(daily) else daily
            truncated = days > available or (window is not None and days > window)
        return self._json(200, {
            'daily': rows,
            'requested_days': requested,
            'available_days': available,
            'window_days': window,
            'truncated': truncated,
        })

    # ---- 静态资源（白名单 web/，防穿越、禁目录列表）----
    def _static(self, rel, prefix=''):
        rel = (rel or '').replace('\\', '/').lstrip('/')
        if not rel:
            return self._err(404, 'not_found', '未知路径')
        target = os.path.normpath(os.path.join(WEB_DIR, prefix, rel))
        web_root = os.path.normpath(WEB_DIR)
        if not target.startswith(web_root + os.sep) and target != web_root:
            return self._err(404, 'not_found', '未知路径')
        if os.path.isdir(target):
            return self._err(404, 'not_found', '目录浏览被禁止')
        if not os.path.isfile(target):
            return self._err(404, 'not_found', '未知路径：%s' % rel)
        ctype = mimetypes.guess_type(target)[0] or 'application/octet-stream'
        with open(target, 'rb') as f:
            self._send(200, f.read(), content_type=ctype)

    # ---- POST：唯一写入口 ----
    def _runtime(self):
        """运行时组合视图：server / scheduler / scan_lock。绝不返回绝对路径。"""
        import datetime as _dt
        st = _load_state() or {}
        sched = None
        try:
            sys.path.insert(0, HERE)
            import autopilot
            sched = autopilot.build_schedule()
            task_ok, task_info = autopilot.query_task()
            sched['task_present'] = bool(task_ok)
        except Exception:
            sched = {'enabled': False, 'note': '调度状态读取失败（不猜测）'}
        lock = None
        try:
            sys.path.insert(0, HERE)
            import ledger
            lock = ledger.scan_lock_status()
        except Exception:
            lock = {'locked': None}      # 无法判定就不编造
        py = st.get('python_executable') or ''
        now = _dt.datetime.now()
        started = st.get('started_at')
        uptime = None
        try:
            if started:
                d0 = _dt.datetime.fromisoformat(started)
                uptime = int((now - d0).total_seconds())
        except Exception:
            pass
        return self._json(200, {
            'server': {'status': 'running' if st else 'stopped',
                       'pid': st.get('pid'),
                       'started_at': started, 'uptime_seconds': uptime,
                       'host': st.get('host'), 'port': st.get('port'),
                       'python': os.path.basename(py) if py else None,
                       'project_venv': ('.venv' in py.replace('\\', '/'))
                       if py else None},
            'scheduler': sched,
            'scan_lock': lock,
        })

    def _json_body(self):
        """读取并解析 JSON body；失败时抛 _ApiError。"""
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            raise _ApiError(400, 'bad_content_type', '只接受 application/json')
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise _ApiError(400, 'bad_request', '非法 Content-Length')
        if length < 0 or length > 1048576:
            raise _ApiError(400, 'bad_request', '请求体长度不合法')
        raw = self.rfile.read(length) if length else b'{}'
        try:
            body = json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            raise _ApiError(400, 'bad_json', '请求体不是合法 JSON')
        if not isinstance(body, dict):
            raise _ApiError(400, 'bad_json', '请求体必须是 JSON 对象')
        return body

    def _check_origin(self):
        origin = self.headers.get('Origin')
        if origin:
            try:
                parsed = urlparse(origin)
                host = parsed.hostname
                port = parsed.port or (80 if parsed.scheme == 'http' else 443)
            except ValueError:
                raise _ApiError(403, 'forbidden_origin', '拒绝非法 Origin')
            if parsed.scheme not in ('http', 'https') or host not in ALLOWED_ORIGIN_HOSTS or port != self.server.server_address[1]:
                raise _ApiError(403, 'forbidden_origin', '拒绝跨站写请求')

    def _write_entry(self):
        """写入口公共闸门：Origin 校验 + 扫描锁互斥。返回需释放的锁或 None。"""
        self._check_origin()
        import ledger as _ledger
        lock = _ledger.scan_lock_status()
        if lock.get('locked'):
            holder = lock.get('holder') or {}
            raise _ApiError(409, 'scan_in_progress',
                            '扫描进行中，写入暂不可用（PID %s）' % holder.get('pid'))
        return _ledger

    def _maintenance_block(self):
        """quiesced 期间拒绝一切新写请求（读路径不受影响）。"""
        if MAINTENANCE['quiesced']:
            raise _ApiError(503, 'maintenance',
                            '账本正在收尾/维护，写入暂不可用')

    def _post_maintenance_quiesce(self):
        """维护静默（RC.3）：占用进程内与跨进程扫描锁并拒绝新写。

        由桌面壳在真正退出 / 数据迁移前调用；锁持续持有直到 resume
        或进程退出（进程被收尾时锁文件由 PID 自愈机制回收）。
        """
        self._check_origin()
        if MAINTENANCE['quiesced']:
            return self._json(200, {'ok': True, 'quiesced': True})
        if SCAN_STATE['running'] or not SCAN_LOCK.acquire(blocking=False):
            return self._err(409, 'scan_in_progress', '已有扫描正在进行，请稍后再试')
        try:
            import ledger as _ledger
            handle = _ledger.acquire_scan_lock(trigger='maintenance')
        except Exception as exc:
            SCAN_LOCK.release()
            slog('maintenance quiesce 失败：%s' % type(exc).__name__)
            return self._err(409, 'scan_in_progress',
                             '扫描锁被其它进程占用，请稍后再试')
        MAINTENANCE['quiesced'] = True
        MAINTENANCE['lock_handle'] = handle
        slog('maintenance quiesce：已暂停扫描与写入（等待桌面壳收尾）')
        return self._json(200, {'ok': True, 'quiesced': True})

    def _post_maintenance_resume(self):
        """恢复写路径（迁移取消/回滚时由桌面壳调用）。"""
        self._check_origin()
        if MAINTENANCE['quiesced']:
            if MAINTENANCE.get('lock_handle'):
                import ledger as _ledger
                _ledger.release_scan_lock(MAINTENANCE['lock_handle'])
            MAINTENANCE['quiesced'] = False
            MAINTENANCE['lock_handle'] = None
            SCAN_LOCK.release()
            slog('maintenance resume：已恢复扫描与写入')
        return self._json(200, {'ok': True, 'quiesced': False})

    # ------------------------------------------------------------ 安全迁移（RC.3 §29-36）

    def _post_maintenance_migrate_plan(self):
        """迁移演练：校验目标 + 文件分类清单，不写任何东西。"""
        self._check_origin()
        body = self._json_body()
        target = body.get('target')
        if not target or not isinstance(target, str):
            return self._err(400, 'bad_request', '缺少 target')
        result = migration_plan(target)
        if result.get('blocked'):
            return self._json(200, result)
        return self._json(200, result)

    def _post_maintenance_migrate_execute(self):
        """执行安全迁移（§29-31）。全程持有扫描锁；源账本永不删除。

        bootstrap 切换由桌面壳在本端点成功返回后原子完成；任何失败
        返回非 2xx + 原因，原账本保持原位可用。
        """
        self._check_origin()
        body = self._json_body()
        target = body.get('target')
        if not target or not isinstance(target, str):
            return self._err(400, 'bad_request', '缺少 target')

        if MAINTENANCE['quiesced'] and MAINTENANCE.get('lock_handle'):
            # 已在 quiesce 态：直接复用已持有的锁（退出/迁移共用同一闸门）。
            evidence = migration_execute(target)
            if evidence.get('ok'):
                return self._json(200, evidence)
            return self._err(500, 'migration_failed', evidence.get('message', '迁移失败'))

        if SCAN_STATE['running'] or not SCAN_LOCK.acquire(blocking=False):
            return self._err(409, 'scan_in_progress', '已有扫描正在进行，请稍后再试')
        try:
            import ledger as _ledger
            handle = _ledger.acquire_scan_lock(trigger='migration')
        except Exception:
            SCAN_LOCK.release()
            return self._err(409, 'scan_in_progress', '扫描锁被其它进程占用，请稍后再试')
        try:
            evidence = migration_execute(target)
        except Exception as exc:
            slog('migration 异常：%s' % type(exc).__name__)
            return self._err(500, 'migration_failed', '迁移失败：%s' % type(exc).__name__)
        finally:
            # 释放跨进程锁 + 进程内锁（bootstrap 未切换前账本必须立即可写）
            import ledger as _ledger2
            _ledger2.release_scan_lock(handle)
            SCAN_LOCK.release()
        if evidence.get('ok'):
            return self._json(200, evidence)
        return self._err(500, 'migration_failed', evidence.get('message', '迁移失败'))

    def _route_post(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == '/api/v1/maintenance/quiesce':
                return self._post_maintenance_quiesce()
            if parsed.path == '/api/v1/maintenance/resume':
                return self._post_maintenance_resume()
            if parsed.path == '/api/v1/maintenance/migrate/plan':
                return self._post_maintenance_migrate_plan()
            if parsed.path == '/api/v1/maintenance/migrate/execute':
                return self._post_maintenance_migrate_execute()
            if parsed.path == '/api/v1/scan':
                return self._post_scan()
            if parsed.path == '/api/v1/discover/refresh':
                return self._post_discover_refresh()
            if parsed.path in ('/api/v1/projects/alias',
                               '/api/v1/projects/assign',
                               '/api/v1/projects/merge'):
                return _post_project_write(self, parsed.path)
            return self._err(404, 'not_found', '未知路径：%s' % parsed.path)
        except _ApiError as e:
            return self._err(e.status, e.error, e.message)
        except ValueError as e:
            return self._err(400, 'bad_request', str(e))
        except Exception as exc:
            slog('write 异常：%s' % type(exc).__name__)
            return self._err(500, 'write_failed',
                             '写入失败：%s' % type(exc).__name__)

    def _post_discover_refresh(self):
        """完整重发现（用户主动触发）。只读本机文件 + 写运行态发现缓存，
        不碰账本，因此不占扫描锁；仍校验 Origin 防跨站写。"""
        self._check_origin()
        self._maintenance_block()
        self._json_body()
        try:
            sys.path.insert(0, HERE)
            import discovery as _disc
            rep = _disc.run_discovery(full=True)
            return self._json(200, _disc.public_payload(rep))
        except Exception as exc:
            slog('discover refresh 异常：%s' % type(exc).__name__)
            return self._err(500, 'discover_failed',
                             '重新发现失败：%s' % type(exc).__name__)

    def _post_scan(self):
        self._check_origin()
        self._maintenance_block()
        body = self._json_body()
        # 严格拒绝危险/未知字段：full / pricing / scheduler 等一律不存在于 v0
        if body.get('full'):
            return self._err(400, 'full_not_allowed',
                             'API v0 只允许普通增量扫描，不支持 --full')
        unknown = set(body) - set()
        if unknown:
            return self._err(400, 'unexpected_field',
                             '不接受字段：%s' % ', '.join(sorted(unknown)))

        if SCAN_STATE['running'] or not SCAN_LOCK.acquire(blocking=False):
            return self._err(409, 'scan_in_progress',
                             '已有扫描正在进行，请稍后再试')
        try:
            SCAN_STATE['running'] = True
            result = run_scan_once()
        except Exception as exc:
            slog('scan 异常：%s' % type(exc).__name__)
            return self._err(500, 'scan_failed', '扫描编排失败：%s' % type(exc).__name__)
        finally:
            SCAN_STATE['running'] = False
            SCAN_LOCK.release()
        if result.get('scan_in_progress'):
            holder = result.get('lock_holder') or {}
            return self._err(409, 'scan_in_progress',
                             '已有扫描正在进行（PID %s）' % holder.get('pid'))
        return self._json(200, result)


def _query(self, qs):
    """联合筛选：时间/项目/工具/模型/审计口径 → 同范围聚合 + 分页记录。
    成本由 ledger 定价引擎按模型组计算（不在前端重算单价）。"""
    import datetime as _dt
    import time as _time
    sys.path.insert(0, HERE)
    import ledger as _ledger

    def one(key, default):
        return (qs.get(key) or [default])[0]

    rng = one('range', '30')
    project = one('project', 'all')
    client = one('client', 'all')
    model = one('model', 'all')
    session = one('session', 'all')
    audit = one('audit', 'effective')
    try:
        page = max(0, int(one('page', '0')))
    except ValueError:
        page = 0
    pagesize = 12

    if audit not in ('effective', 'raw', 'replay'):
        return self._err(400, 'bad_request', 'audit 只支持 effective|raw|replay')
    if rng not in ('1', '7', '30', 'all'):
        return self._err(400, 'bad_request', 'range 只支持 1|7|30|all')

    EMPTY = lambda: self._json(200, {
        'filter': {'range': rng, 'project': project, 'client': client,
                   'model': model, 'audit': audit},
        'aggregate': {'events': 0, 'sessions': 0, 'projects': 0,
                      'input': 0, 'output': 0, 'cache': 0, 'total': 0,
                      'unknown': 0, 'costs': {}, 'reliable_costs': {},
                      'reference_costs': {}, 'unpriced_events': 0,
                      'priced_tokens': 0, 'reliable_priced_tokens': 0,
                      'reference_priced_tokens': 0, 'token_events': 0,
                      'token_records': 0},
        'daily': [], 'models': [], 'by_project': [], 'by_client': [], 'sessions': [],
        'items': [], 'total': 0, 'page': page, 'pagesize': pagesize,
        'window_days': None, 'ledger_state': {'has_records': False, 'has_scan': False}})

    db = self._ledger_db_ro()
    if db is None:
        return EMPTY()
    db.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in db.execute('PRAGMA table_info(usage_event)')}
        if 'client' not in cols or 'session_id' not in cols:
            return EMPTY()
        # One filtered relation for token facts and activity facts. Activity has
        # NULL usage/cost; session_registry supplies only its project identity.
        auto_expr = 'project_key_auto' if 'project_key_auto' in cols else 'project_key'
        relation = ('SELECT client,session_id,event_id,ts_ms,model,provider,'
                    'input_tokens,output_tokens,cache_read_tokens,cache_write_tokens,'
                    'is_replay,project_key,' + auto_expr + ' project_key_auto,'
                    "'token' record_kind FROM usage_event")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'activity_event' in tables and 'session_registry' in tables:
            relation += (" UNION ALL SELECT a.client,a.session_key,a.event_id,a.ts_ms,'','',"
                         'NULL,NULL,NULL,NULL,0,s.project_key,NULL,'
                         "'activity' FROM activity_event a LEFT JOIN session_registry s"
                         ' ON s.source=a.client AND s.session_id=a.session_key')
        prefix = 'WITH ledger_records AS (' + relation + ') '
        def select(sql, params=()):
            return db.execute(prefix + sql, params)
        token_records_sql = " SUM(CASE WHEN record_kind='token' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN 1 ELSE 0 END) token_records,"
        ledger_state = {
            'has_records': bool(select('SELECT EXISTS(SELECT 1 FROM ledger_records)').fetchone()[0]),
            'has_scan': bool(db.execute('SELECT EXISTS(SELECT 1 FROM scan_run)').fetchone()[0]) if 'scan_run' in tables else False}
        where = []
        args = []
        if 'is_replay' in cols:
            if audit == 'effective':
                where.append('is_replay=0')
            elif audit == 'replay':
                where.append('is_replay=1')
        if rng != 'all':
            if rng == '1':
                start = _dt.datetime.combine(_dt.date.today(), _dt.time.min)
                where.append('ts_ms >= ?')
                args.append(int(start.timestamp() * 1000))
            else:
                where.append('ts_ms >= ?')
                args.append(int((_time.time() - int(rng) * 86400) * 1000))
        if project == 'unassigned':
            where.append('project_key IS NULL')
        elif project != 'all':
            where.append('project_key = ?')
            args.append(project)
        if session != 'all':
            # 会话下钻：session = "<source>|<session_id>"
            if '|' in session:
                ssrc, ssid = session.split('|', 1)
                where.append('client = ?')
                args.append(ssrc)
                where.append('session_id = ?')
                args.append(ssid)
            else:
                return self._err(400, 'bad_request', 'session 必须含 source|session_id')
            if not ssrc or not ssid:
                return self._err(400, 'bad_request', 'session identity 不能为空')
        if client != 'all':
            where.append('client = ?')
            args.append(client)
        if model != 'all':
            where.append('model = ?')
            args.append(model)
        W = (' WHERE ' + ' AND '.join(where)) if where else ''

        agg = select(
            'SELECT COUNT(*) events,'
            " COUNT(DISTINCT client || char(31) || session_id) sessions,"
            ' COUNT(DISTINCT project_key) projects,'
            + token_records_sql +
            ' COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,'
            ' COALESCE(SUM(cache_read_tokens),0) cr,'
            ' COALESCE(SUM(input_tokens+output_tokens),0) total,'
            " SUM(CASE WHEN model='' AND input_tokens+output_tokens"
            '+cache_read_tokens=0 THEN 1 ELSE 0 END) unknown,'
            " SUM(CASE WHEN record_kind='token' AND input_tokens+output_tokens>0 THEN 1 ELSE 0 END) token_events,"
            ' MIN(ts_ms) first_ts, MAX(ts_ms) last_ts'
            ' FROM ledger_records' + W, args).fetchone()

        # 模型组 → 定价引擎（组数有限，成本不在前端重算）
        mrows = select(
            'SELECT model, COUNT(*) events, SUM(input_tokens) i,'
            ' SUM(output_tokens) o, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw,'
            " SUM(CASE WHEN record_kind='token' AND input_tokens+output_tokens>0 THEN 1 ELSE 0 END) token_events,"
            ' SUM(input_tokens+output_tokens) total'
            ' FROM ledger_records' + W + ' GROUP BY model', args).fetchall()
        layers = _ledger.load_pricing_layers()
        # 模型 → 唯一可靠 provider 身份（混合 / 不可知 → None，绝不绑定官方价）
        _mpid = {}
        try:
            for r in select("SELECT model, COUNT(DISTINCT provider) n,"
                            " MIN(provider) lo, MAX(provider) hi"
                            " FROM ledger_records GROUP BY model"):
                mm = r['model'] or ''
                if r['n'] == 1:
                    _mpid[mm] = _ledger._provider_identity(r['lo'])
        except Exception:
            pass

        def _qcost(mm, i, o, cr, cw):
            rec = _ledger.resolve_price_record(layers, mm,
                                               provider=_mpid.get(mm))
            return rec, _ledger.record_cost(rec, i, o, cr, cw)

        costs = {}
        reliable_costs = {}
        reference_costs = {}
        unpriced_events = 0
        priced_tokens = 0
        reliable_priced_tokens = 0
        reference_priced_tokens = 0
        token_events = 0
        model_cost = {}
        models = []
        for m in mrows:
            mm = m['model'] or ''
            i, o = m['i'] or 0, m['o'] or 0
            cr = m['cr'] or 0
            tot = m['total'] or 0
            if tot > 0:
                token_events += m['token_events'] or 0
            rec, res = _qcost(mm, i, o, cr, m['cw'] or 0)
            ptype = (rec or {}).get('pricing_type') or _ledger.PT_UNAVAILABLE
            mc = {'model': mm, 'events': m['events'], 'total': tot,
                  'priced': bool(res and res['available']),
                  'pricing_type': ptype,
                  'pricing_applicability': (rec or {}).get('applicability')}
            if res and res['available']:
                cur = res['currency']
                costs[cur] = round(costs.get(cur, 0.0) + res['amount'], 4)
                bucket = (reliable_costs if _ledger.is_reliable_record(rec)
                          else reference_costs)
                bucket[cur] = round(bucket.get(cur, 0.0) + res['amount'], 4)
                priced_tokens += res['covered_io_tokens']
                if _ledger.is_reliable_record(rec):
                    reliable_priced_tokens += res['covered_io_tokens']
                else:
                    reference_priced_tokens += res['covered_io_tokens']
                if res['partial']:
                    unpriced_events += m['token_events'] or 0
                mc['partial'] = res['partial']
                mc['cost'] = round(res['amount'], 4)
                mc['currency'] = cur
            else:
                if tot > 0:
                    unpriced_events += m['token_events'] or 0
                mc['cost'] = None
                mc['currency'] = None
            model_cost[mm] = mc
            models.append(mc)
        # Missing rates affect only records that actually use those components,
        # rather than every record of the same model.
        unpriced_events = 0
        component_columns = {'input': 'input_tokens', 'output': 'output_tokens',
                             'cache_read': 'cache_read_tokens', 'cache_write': 'cache_write_tokens'}
        proj_unpriced = {}
        sess_unpriced = {}
        for m in mrows:
            mm = m['model'] or ''
            _rec, res = _qcost(mm, m['i'] or 0, m['o'] or 0,
                               m['cr'] or 0, m['cw'] or 0)
            if not res or not res['missing_components']:
                continue
            condition = ' OR '.join(component_columns[c] + '>0' for c in res['missing_components'])
            extra = (' AND ' if W else ' WHERE ') + "model=? AND record_kind='token' AND (" + condition + ')'
            for r in select('SELECT project_key,client,session_id,COUNT(*) n FROM ledger_records' + W + extra +
                            ' GROUP BY project_key,client,session_id', args + [mm]):
                unpriced_events += r['n']
                pk, sk = r['project_key'], (r['client'], r['session_id'])
                proj_unpriced[pk] = proj_unpriced.get(pk, 0) + r['n']
                sess_unpriced[sk] = sess_unpriced.get(sk, 0) + r['n']
        models.sort(key=lambda x: -x['total'])

        # 每日用量（本地时区日界）
        daily = []
        if 'ts_ms' in cols:
            daily = [{'day': r['day'], 'total': r['total'] or 0} for r in select(
                "SELECT date(ts_ms/1000,'unixepoch','localtime') day,"
                ' SUM(input_tokens+output_tokens) total'
                ' FROM ledger_records' + W +
                ' GROUP BY day ORDER BY day', args)]

        # 项目分组（含成本）
        proj_rows = select(
            'SELECT project_key, COUNT(*) events,'
            " COUNT(DISTINCT client || char(31) || session_id) sessions,"
            + token_records_sql +
            ' SUM(input_tokens+output_tokens) total, MAX(ts_ms) last_ts'
            ' FROM ledger_records' + W + ' GROUP BY project_key', args).fetchall()
        pm_rows = select(
            'SELECT project_key, model, COUNT(*) events, SUM(input_tokens) i,'
            ' SUM(output_tokens) o, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw,'
            ' SUM(input_tokens+output_tokens) total'
            ' FROM ledger_records' + W + ' GROUP BY project_key, model', args).fetchall()
        proj_cost = {}
        for r in pm_rows:
            k = r['project_key']
            mm = r['model'] or ''
            if not ((r['total'] or 0) + (r['cr'] or 0) + (r['cw'] or 0)):
                continue
            _rec, res = _qcost(mm, r['i'] or 0, r['o'] or 0,
                               r['cr'] or 0, r['cw'] or 0)
            if res and res['available']:
                bucket = proj_cost.setdefault(k, {})
                bucket[res['currency']] = round(
                    bucket.get(res['currency'], 0.0) + res['amount'], 4)
        by_client = []
        try:
            src_label = {}
            for ckey, conf in (_ledger.SOURCES or {}).items():
                src_label[ckey] = (conf or {}).get('label', ckey)
            for r in select(
                    'SELECT client, COUNT(*) events,'
                    + token_records_sql +
                    ' SUM(input_tokens+output_tokens) total'
                    ' FROM ledger_records' + W + ' GROUP BY client', args):
                by_client.append({'client': r['client'],
                                  'label': src_label.get(r['client'], r['client']),
                                  'events': r['events'],
                                  'token_records': r['token_records'] or 0,
                                  'total': r['total'] or 0})
        except Exception:
            by_client = []
        by_client.sort(key=lambda x: -x['total'])
        # 会话分组（含按模型分摊的成本桶）
        sess_rows = select(
            'SELECT client, session_id, project_key, COUNT(*) events,'
            + token_records_sql +
            ' SUM(input_tokens) i, SUM(output_tokens) o,'
            ' SUM(input_tokens+output_tokens) total, MIN(ts_ms) first_ts, MAX(ts_ms) last_ts'
            ' FROM ledger_records' + W + ' GROUP BY client, session_id ORDER BY last_ts DESC', args).fetchall()
        sm_rows = select(
            'SELECT client, session_id, model, COUNT(*) events, SUM(input_tokens) i,'
            ' SUM(output_tokens) o, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw,'
            ' SUM(input_tokens+output_tokens) total'
            ' FROM ledger_records' + W + ' GROUP BY client, session_id, model', args).fetchall()
        sess_cost = {}
        for r in sm_rows:
            k = (r['client'], r['session_id'])
            mm = r['model'] or ''
            if not ((r['total'] or 0) + (r['cr'] or 0) + (r['cw'] or 0)):
                continue
            _rec, res = _qcost(mm, r['i'] or 0, r['o'] or 0,
                               r['cr'] or 0, r['cw'] or 0)
            if res and res['available']:
                bucket = sess_cost.setdefault(k, {})
                bucket[res['currency']] = round(
                    bucket.get(res['currency'], 0.0) + res['amount'], 4)
        reg_name = {}
        reg_agent = {}
        try:
            for r in db.execute('SELECT source, session_id, display_name, agent'
                                ' FROM session_registry'):
                reg_name[(r['source'], r['session_id'])] = r['display_name']
                reg_agent[(r['source'], r['session_id'])] = r['agent']
        except Exception:
            pass
        sessions = []
        for r in sess_rows:
            k = (r['client'], r['session_id'])
            sessions.append({
                'source': r['client'], 'session_id': r['session_id'],
                'project_key': r['project_key'],
                'title': reg_name.get(k) or r['session_id'],
                'agent': reg_agent.get(k),
                'events': r['events'], 'total': r['total'] or 0,
                'token_records': r['token_records'] or 0,
                'last_ts': r['last_ts'],
                'first_ts': r['first_ts'],
                'unpriced_events': sess_unpriced.get(k, 0),
                'estimated_cost_by_currency': sess_cost.get(k) or None})
        names = {}
        for r in db.execute(
                'SELECT project_key, display_name FROM project_registry'):
            names[r['project_key']] = r['display_name']
        by_project = []
        for r in proj_rows:
            k = r['project_key']
            by_project.append({
                'project_key': k,
                'display_name': names.get(k) or '(待整理)',
                'events': r['events'], 'sessions': r['sessions'],
                'token_records': r['token_records'] or 0,
                'total': r['total'] or 0,
                'last_ts': r['last_ts'],
                'unpriced_events': proj_unpriced.get(k, 0),
                'estimated_cost_by_currency': proj_cost.get(k) or None})

        total = agg['events']
        items = []
        if total:
            off = min(page * pagesize, total)
            rows = select(
                'SELECT client, session_id, event_id, ts_ms, model, provider,'
                ' input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,'
                ' input_tokens + output_tokens AS total_tokens, is_replay,'
                ' project_key, project_key_auto, record_kind'
                ' FROM ledger_records' + W +
                ' ORDER BY ts_ms DESC, client, session_id, event_id LIMIT ? OFFSET ?',
                args + [pagesize, off]).fetchall()
            sess_names = {}
            try:
                for r in db.execute('SELECT source, session_id, display_name'
                                    ' FROM session_registry'):
                    sess_names[(r['source'], r['session_id'])] = r['display_name']
            except Exception:
                pass
            for r in rows:
                mm = r['model'] or ''
                mc = model_cost.get(mm) or {}
                ev_cost = None
                ev_cur = None
                cost_status = 'unavailable'
                ev_ptype = mc.get('pricing_type') or _ledger.PT_UNAVAILABLE
                if r['record_kind'] == 'token':
                    _prec, price = _qcost(mm,
                        r['input_tokens'] or 0, r['output_tokens'] or 0,
                        r['cache_read_tokens'] or 0,
                        r['cache_write_tokens'] or 0)
                    if price and price['available']:
                        ev_cost = round(price['amount'], 6)
                        ev_cur = price['currency']
                        cost_status = 'partial' if price['partial'] else 'available'
                        ev_ptype = price['pricing_type']
                items.append({
                    'client': r['client'],
                    'session_id': r['session_id'],
                    'event_id': r['event_id'],
                    'session_name': sess_names.get(
                        (r['client'], r['session_id']), r['session_id']),
                    'ts_ms': r['ts_ms'],
                    'model': r['model'] or None,
                    'provider': r['provider'] or None,
                    'input': r['input_tokens'],
                    'output': r['output_tokens'],
                    'cache_read': r['cache_read_tokens'],
                    'cache_write': r['cache_write_tokens'],
                    'total': r['total_tokens'],
                    'replay': bool(r['is_replay']) if 'is_replay' in cols else False,
                    'project_key': r['project_key'],
                    'project_key_auto': r['project_key_auto'],
                    'record_kind': r['record_kind'],
                    'project_name': names.get(r['project_key']),
                    'cost': ev_cost,
                    'cost_status': cost_status,
                    'pricing_type': ev_ptype,
                    'currency': ev_cur})
        return self._json(200, {
            'filter': {'range': rng, 'project': project, 'client': client,
                       'model': model, 'session': session, 'audit': audit},
            'aggregate': {
                'events': agg['events'], 'sessions': agg['sessions'],
                'projects': agg['projects'],
                'input': agg['i'], 'output': agg['o'], 'cache': agg['cr'],
                'total': agg['total'], 'unknown': agg['unknown'] or 0,
                'first_ts': agg['first_ts'], 'last_ts': agg['last_ts'],
                'costs': costs,
                'reliable_costs': reliable_costs,
                'reference_costs': reference_costs,
                'unpriced_events': unpriced_events,
                'priced_tokens': priced_tokens,
                'reliable_priced_tokens': reliable_priced_tokens,
                'reference_priced_tokens': reference_priced_tokens,
                'token_events': token_events,
                'token_records': agg['token_records'] or 0},
            'daily': daily, 'models': models, 'by_project': by_project,
            'by_client': by_client, 'sessions': sessions,
            'items': items, 'total': total, 'page': page,
            'pagesize': pagesize,
            'window_days': None, 'ledger_state': ledger_state})
    finally:
        db.close()


class _ApiError(Exception):
    """写入口结构化错误 → _err JSON 响应。"""

    def __init__(self, status, error, message):
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message


def _post_project_write(self, path):
    """V3 写入能力：alias / assign / merge（同一闸门：Origin + 扫描锁互斥）。"""
    self._maintenance_block()
    if not SCAN_LOCK.acquire(blocking=False):
        raise _ApiError(409, 'scan_in_progress', '已有更新或整理正在进行')
    import ledger as _ledger
    handle = None
    try:
        self._write_entry()
        try:
            handle = _ledger.acquire_scan_lock(trigger='project-write')
        except _ledger.ScanLockBusy:
            raise _ApiError(409, 'scan_in_progress', '已有更新或整理正在进行')
        result = _post_project_write_locked(self, path)
    finally:
        if handle is not None:
            _ledger.release_scan_lock(handle)
        SCAN_LOCK.release()
    return self._json(200, result)


def _post_project_write_locked(self, path):
    body = self._json_body()
    import ledger as _ledger
    if path == '/api/v1/projects/alias':
        unknown = set(body) - {'alias', 'project_key', 'display_name'}
        if unknown:
            raise _ApiError(400, 'unexpected_field',
                            '不接受字段：%s' % ', '.join(sorted(unknown)))
        alias = body.get('alias')
        if not isinstance(alias, str) or not alias.strip():
            raise _ApiError(400, 'bad_request', 'alias 必须是非空字符串')
        pk = body.get('project_key')
        if pk is not None and (not isinstance(pk, str) or not pk.strip()):
            raise _ApiError(400, 'bad_request', 'project_key 必须是非空字符串')
        dn = body.get('display_name')
        if dn is not None and not isinstance(dn, str):
            raise _ApiError(400, 'bad_request', 'display_name 必须是字符串')
        result = _ledger.project_alias_add(
            alias, project_key=(pk or '').strip() or None,
            display_name=(dn or '').strip() or None)
        return result

    if path == '/api/v1/projects/assign':
        unknown = set(body) - {'project_key', 'sessions', 'display_name'}
        if unknown:
            raise _ApiError(400, 'unexpected_field',
                            '不接受字段：%s' % ', '.join(sorted(unknown)))
        pk = body.get('project_key')
        if not isinstance(pk, str) or not pk.strip():
            raise _ApiError(400, 'bad_request', 'project_key 必须是非空字符串')
        sessions = body.get('sessions')
        if not isinstance(sessions, list):
            raise _ApiError(400, 'bad_request', 'sessions 必须是数组')
        for s in sessions:
            if (not isinstance(s, dict) or
                not isinstance(s.get('source'), str) or not s['source'].strip() or
                not isinstance(s.get('session_id'), str) or not s['session_id'].strip() or
                set(s) - {'source', 'session_id'}):
                raise _ApiError(400, 'bad_request',
                                'sessions 元素必须含 source 与 session_id')
        dn = body.get('display_name')
        if dn is not None and not isinstance(dn, str):
            raise _ApiError(400, 'bad_request', 'display_name 必须是字符串')
        result = _ledger.project_assign_sessions(
            pk.strip(), sessions, display_name=(dn or '').strip() or None)
        return result

    # merge
    unknown = set(body) - {'from', 'to'}
    if unknown:
        raise _ApiError(400, 'unexpected_field',
                        '不接受字段：%s' % ', '.join(sorted(unknown)))
    fk = body.get('from')
    tk = body.get('to')
    for v in (fk, tk):
        if not isinstance(v, str) or not v.strip():
            raise _ApiError(400, 'bad_request', 'from/to 必须是非空字符串')
    result = _ledger.project_merge(fk.strip(), tk.strip())
    return result


def make_server(port=DEFAULT_PORT):
    """仅供 127.0.0.1。其它 host 在代码层面拒绝。"""
    if port is None:
        port = DEFAULT_PORT
    return ThreadingHTTPServer((HOST, int(port)), LedgerHandler)


def _out(msg):
    """pythonw 下 sys.stdout 为 None —— 输出必须容错。"""
    out = sys.stdout
    if out is None:
        return
    try:
        print(msg)
    except Exception:
        pass


def _redirect_none_streams():
    """pythonw 下 sys.stdout/stderr 为 None：未经保护的输出会直接杀死
    进程。重定向到 server.log —— 既修复崩溃，又把异常栈留档诊断。"""
    import datetime as _dt
    if sys.stdout is None or sys.stderr is None:
        try:
            f = open(SERVER_LOG_PATH, 'a', encoding='utf-8', buffering=1)
            if sys.stdout is None:
                sys.stdout = f
            if sys.stderr is None:
                sys.stderr = f
            banner = ('[%s] === pythonw session start (stdout/stderr -> server.log) ===' % _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            f.write(banner + chr(10))
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description='智账 · 统一本地服务（Web + API）')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT,
                    help='默认 %d' % DEFAULT_PORT)
    ap.add_argument('--open', action='store_true', help='启动后打开系统浏览器')
    ap.add_argument('--no-open', dest='no_open', action='store_true',
                    help='禁止打开浏览器（桌面壳 sidecar 模式，R10-D）')
    ap.add_argument('--check-only', action='store_true',
                    help='只做单实例检测并输出结论，不启动服务')
    ap.add_argument('--stop', action='store_true',
                    help='停止 server-state.json 记录的本产品服务')
    args = ap.parse_args()

    # Round 10：frozen 模式下自动确保 DATA_ROOT 并默认打开浏览器
    if getattr(sys, 'frozen', False):
        import paths
        paths.ensure_data_root()
        global HERE
        HERE = paths.DATA_ROOT
        if not args.stop and not args.check_only and not args.no_open:
            args.open = True  # 打包版默认打开浏览器（桌面壳以 --no-open 抑制）
    _redirect_none_streams()

    if args.stop:
        code, msg = stop_server()
        _out('  %s' % msg)
        slog('stop 调用：%s' % msg)
        return code

    verdict, st = preflight(args.port)
    if args.check_only:
        if verdict == 'ours-running':
            _out('  already running：本产品服务已在运行（PID %s，端口 %s）'
                  % (st.get('pid'), st.get('port')))
            return 2
        if verdict == 'foreign-busy':
            _out('  port_in_use：端口 %d 已被其它程序占用（不会强杀）。'
                  '可用 --port 换端口。' % args.port)
            return 3
        _out('  ok：端口 %d 可用，可以启动。' % args.port)
        return 0

    if verdict == 'ours-running':
        _out('  already running：本产品服务已在运行（PID %s，端口 %s），'
              '不启动第二实例。' % (st.get('pid'), st.get('port')))
        slog('启动拒绝：已有本产品实例在运行（PID %s）' % st.get('pid'))
        return 2
    if verdict == 'foreign-busy':
        _out('  port_in_use：端口 %d 已被其它程序占用（本工具不会强杀未知进程）。'
              '可用 --port 8788 换端口，或释放该端口后重试。' % args.port)
        slog('启动失败：端口 %d 被外部程序占用' % args.port)
        return 3

    try:
        httpd = make_server(args.port)
    except OSError as e:
        _out('  port_in_use：端口 %d 绑定失败（%s）。可用 --port 换端口。'
              % (args.port, e.strerror or e))
        slog('绑定失败：端口 %d（%s）' % (args.port, e.strerror or e))
        return 3

    state = _write_state({'port': httpd.server_address[1]})
    url = 'http://%s:%d' % (HOST, httpd.server_address[1])
    _out('')
    _out('  智账 · PathOrbit AI Ledger Local Server')
    _out('  %s' % url)
    _out('  Web Dashboard  →  %s/' % url)
    _out('  Local API v1   →  %s/api/v1/overview' % url)
    _out('  只绑定 127.0.0.1 ｜ board 是业务事实源 ｜ Ctrl+C 退出')
    _out('')
    slog('server start：pid=%s port=%s python=%s'
         % (state['pid'], state['port'], os.path.basename(sys.executable)))
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _out('\n  已退出。')
    finally:
        _clear_state()
        slog('server stop：pid=%s' % state['pid'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
