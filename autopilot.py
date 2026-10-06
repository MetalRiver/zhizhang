#!/usr/bin/env python3
"""autopilot.py — Usage Ledger 的编排层：自动发现 → 自动采集 → 自动产出。

职责边界：
    本文件**不进账本核心**。它只负责按顺序调用 ledger.py 的能力，并把每次运行
    的结果写成可观察的状态文件。账本核心（parser / SQLite / retention）仍在 ledger.py。

设计原则：
    - 失败绝不静默：每次运行都写 run-state.json 与 auto.log，后台任务没有控制台也一样可诊断。
    - 默认离线：只有显式带 --pricing 才联网（价格同步是唯一的联网层）。
    - 系统任务不偷偷注册：install-auto 必须由用户主动执行一次。

每次运行都产出可观察状态（后台任务没有控制台也能诊断）：
    discovery.json   自动发现的每个数据源、路径脱敏提示、数据等级（TOKEN/ACTIVITY/UNKNOWN）
    run-state.json   最近一次运行的完整状态与计数
    health.json      独立产品健康状态（healthy/partial/failed/unknown），Dashboard 直接消费
    auto.log         逐次运行的日志（超过 512KB 自动轮转）

退出码 / 状态：
    0 = success              全链路正常
    1 = failed               核心链路整体不可用（异常中断 / 账本不可写 / board 未写出）
    2 = partial_failure      跑完了但有环节失败（价格同步失败、个别源解析异常等）
    3 = no_supported_sources 未发现任何受支持数据源，且账本为空

用法：
    python autopilot.py auto                      # 跑一次：发现 → 采集 → 出 board
    python autopilot.py auto --pricing            # 额外做一次价格同步（联网）
    python autopilot.py auto --no-pricing         # 显式关闭联网
    python autopilot.py watch --interval 1800     # 常驻，每 30 分钟一次
    python autopilot.py install-auto --minutes 30 # 注册 Windows 计划任务（一次授权）
    python autopilot.py install-auto --print-only # 只输出将要执行的命令，不改动任何东西
    python autopilot.py task-info                 # 只读查询计划任务真实状态
    python autopilot.py uninstall-auto
    python autopilot.py status [--json]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ledger  # noqa: E402

RUN_STATE_PATH = os.path.join(HERE, 'run-state.json')
AUTO_LOG_PATH = os.path.join(HERE, 'auto.log')
# 自动同步配置：install-auto 注册成功后写入，卸载时删除。
# Dashboard 靠它回答"自动同步有没有开启 / 下次什么时候跑"——
# 没有这个文件就只能如实说"未配置"，不许在前端编一个绿灯出来。
SCHEDULE_PATH = os.path.join(HERE, 'auto-schedule.json')
LOG_MAX_BYTES = 512 * 1024
TASK_NAME = 'UsageLedger-Auto'
EXE = sys.executable or 'python'

STATUS_OK = 'success'
STATUS_FAILED = 'failed'
STATUS_PARTIAL = 'partial_failure'
STATUS_NO_SOURCES = 'no_supported_sources'
EXIT_BY_STATUS = {
    STATUS_OK: 0,
    STATUS_FAILED: 1,        # 核心链路整体不可用（异常中断 / 账本不可写 / board 未写出）
    STATUS_PARTIAL: 2,       # 跑完了但有环节失败（价格同步失败、个别源解析异常等）
    STATUS_NO_SOURCES: 3,    # 未发现任何受支持数据源，且账本为空
}
ALL_STATUSES = (STATUS_OK, STATUS_FAILED, STATUS_PARTIAL, STATUS_NO_SOURCES)


# ---------------------------------------------------------------- 日志

def _rotate_log():
    try:
        if os.path.isfile(AUTO_LOG_PATH) and os.path.getsize(AUTO_LOG_PATH) > LOG_MAX_BYTES:
            os.replace(AUTO_LOG_PATH, AUTO_LOG_PATH + '.1')
    except OSError:
        pass


def log(line):
    """追加一行到 auto.log（带时间戳）。后台运行时这是唯一的诊断线索。"""
    _rotate_log()
    ts = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        with open(AUTO_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('[%s] %s\n' % (ts, line))
    except OSError:
        pass


class _Tee(io.TextIOBase):
    """把子命令的 stdout 同时送到控制台（若有）和 auto.log。"""

    def __init__(self, buf):
        self.buf = buf

    def write(self, s):
        self.buf.write(s)
        try:
            _rotate_log()
            with open(AUTO_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(s)
        except OSError:
            pass
        return len(s)

    def flush(self):
        return None


def _ns(**kw):
    return argparse.Namespace(**kw)


# ---------------------------------------------------------------- 首次运行向导

def cmd_setup(args):
    """全新环境的首次运行向导：发现 → 展示 → **等你确认** → 首次扫描 → 出 board。

    刻意做成"必须确认"：工具不会自己决定开始采集，更不会自己去注册后台任务。
    这里只做一次性采集，注册计划任务仍然是独立的 `install-auto` 显式动作。
    """
    print()
    print('=' * 66)
    print('  智账 · 首次运行向导')
    print('=' * 66)
    print()
    print('  工作目录：%s' % HERE)
    for f in ('sources.json', 'usage.db', 'discovery.json', 'run-state.json',
              'pricing.auto.json', 'pricing.json'):
        p = os.path.join(HERE, f)
        print('    %-20s %s' % (f, ('已存在，将沿用' if os.path.isfile(p) else '（无，全新）')))
    print()

    print('  [1/4] 自动发现受支持的数据源…')
    rep = ledger.discover_sources(verbose=True)
    ledger.regrade_sources(rep)
    ledger.write_discovery(rep)
    print()
    print('  [2/4] 发现结果与数据等级')
    print('    %-10s %-24s %-10s %-9s %s' % ('客户端', '名称', '状态', '数据等级', '文件'))
    print('    ' + '-' * 74)
    for c, r in (rep.get('sources') or {}).items():
        print('    %-10s %-24s %-10s %-9s %s'
              % (c, (r.get('label') or '')[:22], r.get('state'),
                 r.get('data_grade') or '?', r.get('files')))
    op = rep.get('opaque') or {}
    if op:
        print()
        print('    不透明存储（认得出、读不出，登记为 UNKNOWN）：')
        for k, r in op.items():
            print('      %-16s %-9s %s' % (k, r['state'], r['reason']))
    g = rep.get('grades') or {}
    print()
    print('    等级统计：TOKEN %s ｜ ACTIVITY %s ｜ UNKNOWN %s'
          % (g.get('TOKEN', 0), g.get('ACTIVITY', 0), g.get('UNKNOWN', 0)))
    print()
    print('    明细：%s' % ledger.DISCOVERY_PATH)

    if rep['found'] == 0:
        print()
        print('  [3/4] × 没有发现任何受支持的数据源，无法继续。')
        print()
        print('    可以做的事：')
        print('      · 确认你用的客户端在本机跑过至少一次（没跑过就没有本地记录）')
        print('      · 看 discovery.json 里每个候选路径的探测结果')
        print('      · 若数据不在默认位置（例如 dsh 的桌面打包版），用 sources.json 指定：')
        print('          { "dsh": "D:/path/to/dsh-home" }')
        print()
        log('setup：0 个数据源，终止（未做任何采集）')
        return ledger.EXIT_NO_SOURCES

    print()
    print('  [3/4] 即将执行（本次只做一次采集，不会注册任何后台任务）：')
    print('    · 扫描上述 %d 个数据源，写入本地账本 usage.db' % rep['found'])
    print('    · 生成 usage-board.json（供 Dashboard 读取）')
    print('    · 写 discovery.json / run-state.json / health.json / auto.log')
    print('    · 全程离线；价格同步需要另加 --pricing 才会联网')

    if not getattr(args, 'yes', False):
        print()
        try:
            ans = input('  确认开始？[y/N] ').strip().lower()
        except EOFError:
            ans = ''
        if ans not in ('y', 'yes'):
            print()
            print('  已取消，未做任何改动。想跑的时候执行：python autopilot.py setup --yes')
            log('setup：用户取消，未采集')
            return ledger.EXIT_OK

    print()
    print('  [4/4] 开始采集…')
    print('-' * 66)
    code = cmd_auto(argparse.Namespace(pricing=False, no_pricing=True, source='all',
                                       trigger='manual'))
    print('-' * 66)
    print()
    if code == 0:
        print('  ✅ 完成。打开 Dashboard 查看：')
    else:
        print('  ⚠ 采集未完全成功（退出码 %d，见上面的错误）。' % code)
        print('    仍然可以打开 Dashboard 看看已经拿到的数据：')
    print()
    print('     python -m http.server 8000')
    print('     # 然后浏览器打开 http://127.0.0.1:8000/usage-dashboard.html')
    print()
    print('     （直接双击 usage-dashboard.html 也行，只是浏览器不允许 file:// 读同目录')
    print('      的 usage-board.json，页面会退化为内嵌演示数据并提示你手动载入 board）')
    print()
    print('  想让它在后台定时跑：python autopilot.py install-auto --minutes 30')
    print('  查看健康状态：      python autopilot.py health')
    print()
    return code


# ---------------------------------------------------------------- 单次运行

def run_once(do_pricing=False, pricing_source='all', quiet=False, trigger='manual'):
    """执行一次 发现 → 采集 → board（→ 价格），返回 (status, run_state)。

    trigger：`manual`（人跑的）或 `scheduled`（计划任务 TR 里带的
    `--trigger scheduled`）。有了它，health.json 才能证明「调度器真的在工作」，
    而不是只有人手动跑过。

    run-state.json 的字段契约（自动化"有声运行"的最低要求）：
        started_at / finished_at / sources_found / sources_scanned
        events_seen / events_inserted / events_updated / errors
    另外附带 status / exit_code / trigger / discovery / scan / pricing / board / grades 便于排查。
    """
    started = dt.datetime.now()
    state = {
        'schema_version': 2,
        'status': STATUS_OK,
        'trigger': trigger,
        'started_at': started.isoformat(timespec='seconds'),
        'finished_at': None,
        'duration_ms': None,
        # ---- 契约字段 ----
        'sources_found': 0,
        'sources_scanned': 0,
        'events_seen': 0,
        'events_inserted': 0,
        'events_updated': 0,
        'errors': [],
        # ---- 诊断字段 ----
        'sources_total': len(ledger.SOURCES),
        'discovered_sources': 0,          # sources_found 的别名（兼容旧消费者）
        'discovery': {},
        'grades': {},
        'opaque_stores': [],
        'scan': {},
        'pricing': {'attempted': bool(do_pricing), 'status': 'skipped', 'errors': []},
        'board': {'written': False},
        'exit_code': 0,
    }
    fatal = []
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(_Tee(buf)):
            print('=' * 66)
            print('autopilot 开始：%s' % started.strftime('%Y-%m-%d %H:%M:%S'))

            # 1) 自动发现
            rep = ledger.discover_sources(verbose=True)
            ledger.regrade_sources(rep)
            state['sources_found'] = rep['found']
            state['discovered_sources'] = rep['found']
            state['discovery'] = {
                c: {'state': r['state'], 'mode': r['mode'], 'files': r['files'],
                    'data_grade': r.get('data_grade'), 'grade_basis': r.get('grade_basis')}
                for c, r in rep['sources'].items()
            }
            state['grades'] = dict(rep.get('grades') or {})
            state['opaque_stores'] = [k for k, r in (rep.get('opaque') or {}).items()
                                      if r['state'] == 'found']
            print('发现结果：%d / %d 个可采集数据源 ｜ 等级 TOKEN %d / ACTIVITY %d / UNKNOWN %d'
                  % (rep['found'], rep['total'], state['grades'].get('TOKEN', 0),
                     state['grades'].get('ACTIVITY', 0), state['grades'].get('UNKNOWN', 0)))
            if state['opaque_stores']:
                print('不透明存储（认得出、读不出用量）：%s' % ', '.join(state['opaque_stores']))

            # 1b) V1.1 Universal Discovery：发现既有 5 来源之外的本机 AI 工具
            #     （轻量缓存优先；通用契约通过的新工具已在引擎内注册为可采集源）。
            #     只增强、不替代上面的内置来源事实源；失败不阻塞采集。
            try:
                import discovery as _disc
                _drep = _disc.run_discovery(full=False)
                _dnew = [t['tool_id'] for t in (_drep.get('tools') or [])
                         if t.get('is_new')]
                state['universal_discovery'] = {
                    'mode': _drep.get('mode'),
                    'tools': _drep.get('total', 0),
                    'new_tools': _dnew,
                    'groups': _drep.get('groups') or {},
                }
                print('Universal Discovery：%d 个工具（%s）｜ 新发现：%s'
                      % (_drep.get('total', 0), _drep.get('mode'),
                         ', '.join(_dnew) if _dnew else '无'))
            except Exception as _de:
                print('! Universal Discovery 跳过（不阻塞采集）：%s' % type(_de).__name__)

            # 2) 自动采集（离线）
            retained = _ledger_record_count()
            if rep['found'] == 0:
                # 关键区分：源文件全被清退 ≠ 首次就没有数据源。
                # 前者账本里有留存记录，属于「本次没能刷新」的部分失败，不该报成配置类错误。
                if retained > 0:
                    state['status'] = STATUS_PARTIAL
                    state['errors'].append(
                        '本次未发现任何数据源（可能已被客户端清退）；'
                        '账本仍有 %d 条留存记录，本次未采集到新数据' % retained)
                    print('未发现数据源，但账本仍有 %d 条留存记录 —— 判定为部分失败（数据未丢）。'
                          % retained)
                    # 仍跑一次扫描：让此前见过的源文件被正确标记为「已清退」
                    scan_code = ledger.cmd_scan(_ns(full=False))
                    _absorb_scan_stats(state)
                else:
                    state['status'] = STATUS_NO_SOURCES
                    print('未发现任何受支持数据源，且账本为空 —— 跳过采集。')
                    state['errors'].append(
                        'no_supported_sources：0 / %d 个内置数据源被探测到' % rep['total'])
            else:
                scan_code = ledger.cmd_scan(_ns(full=False))
                _absorb_scan_stats(state)
                state['scan']['exit_code'] = scan_code
                n_fail = state['scan'].get('sources_failed', 0)
                n_ok = state['scan'].get('sources_ok', 0)
                if scan_code == ledger.EXIT_NO_SOURCES:
                    state['status'] = STATUS_NO_SOURCES
                    if n_fail:
                        state['errors'].append(
                            '所有已发现的数据源都读取失败（%d 个）—— 等价于没有可用数据源'
                            % n_fail)
                elif scan_code == ledger.EXIT_PARTIAL:
                    state['status'] = STATUS_PARTIAL
                    state['errors'].append(
                        '有 %d / %d 个数据源读取失败（其余 %d 个正常采集）'
                        % (n_fail, n_fail + n_ok, n_ok))
                elif scan_code != 0:
                    state['status'] = STATUS_PARTIAL
                    state['errors'].append('scan 返回 %s' % scan_code)
                # 具体的失败来源一并写进 run-state / auto.log，不留哑谜
                for e in (state['scan'].get('source_errors') or [])[:10]:
                    state['errors'].append('数据源读取失败 → %s' % e)

            # 2b) 采集后重算数据等级并落盘 discovery.json
            #     这样「找到了存储但解析不出记录」会立刻体现为 UNKNOWN，而不是继续假装 TOKEN
            try:
                ledger.regrade_sources(rep)
                ledger.write_discovery(rep)
                state['grades'] = dict(rep.get('grades') or {})
                for c, row in (rep.get('sources') or {}).items():
                    if c in state['discovery']:
                        state['discovery'][c]['data_grade'] = row.get('data_grade')
                        state['discovery'][c]['grade_basis'] = row.get('grade_basis')
            except Exception as e:
                state['errors'].append('discovery.json 写入失败：%s' % e)
                print('! discovery.json 写入失败：%s' % e)

            # 3) 产出 board（即使没数据源也写，避免完全没有诊断产物）
            try:
                board = ledger.build_board()
                _write_board(board)
                s = board['summary']
                state['board'] = {
                    'written': True,
                    'file': os.path.basename(ledger.BOARD_PATH),
                    'usage_events': s['usage_events'],
                    'activity_events': sum(a['records'] for a in board.get('activity', [])),
                    'total_tokens': s['total_tokens'],
                    'estimated_cost_usd': s['estimated_cost_usd_top50_models'],
                    'priced_models': s['priced_models'],
                    'unpriced_models': s['unpriced_models'],
                }
                print('已写入 %s' % ledger.BOARD_PATH)
            except Exception as e:
                # board 是唯一对外产物，写不出来属于致命失败
                fatal.append('board 生成失败：%s' % e)
                state['errors'].append(fatal[-1])
                print('! board 生成失败：%s' % e)

            # 4) 可选价格同步（唯一联网层）
            if do_pricing:
                try:
                    code = ledger.cmd_pricing_sync(_ns(source=pricing_source))
                except Exception as e:
                    code = ledger.EXIT_FAILED
                    state['pricing']['errors'].append(str(e))
                if code == 0:
                    state['pricing']['status'] = 'ok'
                    try:
                        with open(ledger.PRICING_AUTO_PATH, encoding='utf-8') as f:
                            raw = json.load(f)
                        state['pricing']['models'] = len(
                            [k for k in raw if not k.startswith('_')])
                        state['pricing']['source'] = (raw.get('_meta') or {}).get('source')
                        state['pricing']['retrieved_at'] = (raw.get('_meta') or {}).get('retrieved_at')
                    except Exception:
                        pass
                else:
                    state['pricing']['status'] = 'failed'
                    state['errors'].append('pricing-sync 返回 %s（旧缓存已保留）' % code)
                    if state['status'] == STATUS_OK:
                        state['status'] = STATUS_PARTIAL
                # 价格变了要重出 board
                try:
                    _write_board(ledger.build_board())
                except Exception as e:
                    state['errors'].append('价格同步后重出 board 失败：%s' % e)
            else:
                state['pricing']['status'] = 'skipped'
                print('价格同步：已跳过（默认离线，加 --pricing 才联网）')

    except Exception as e:
        fatal.append('未捕获异常：%s' % e)
        state['errors'].append(fatal[-1])
        with contextlib.redirect_stdout(_Tee(buf)):
            import traceback
            traceback.print_exc()
            print('! 运行中断：%s' % e)

    # 致命失败优先于一切其它状态
    if fatal:
        state['status'] = STATUS_FAILED

    finished = dt.datetime.now()
    state['finished_at'] = finished.isoformat(timespec='seconds')
    state['duration_ms'] = int((finished - started).total_seconds() * 1000)
    state['exit_code'] = EXIT_BY_STATUS[state['status']]

    # 把 run-state 的关键结论同步进 board（board 先前用的可能是中间 status）
    try:
        with open(ledger.BOARD_PATH, encoding='utf-8') as f:
            board = json.load(f)
        board['automation'] = {
            'status': state['status'],
            'exit_code': state['exit_code'],
            'last_run_at': state['started_at'],
            'finished_at': state['finished_at'],
            'sources_found': state['sources_found'],
            'sources_scanned': state['sources_scanned'],
            'sources_total': state['sources_total'],
            'events_seen': state['events_seen'],
            'events_inserted': state['events_inserted'],
            'events_updated': state['events_updated'],
            'grades': state['grades'],
            'pricing_status': state['pricing']['status'],
            'board_written': state['board'].get('written', False),
            'errors_count': len(state['errors']),
            'errors': state['errors'][:5],
            'run_state_file': os.path.basename(RUN_STATE_PATH),
            'log_file': os.path.basename(AUTO_LOG_PATH),
            # 自动同步状态：Dashboard 的"是否开启 / 多久一次 / 下次何时"全靠它。
            # 没配过就如实返回 enabled=False，前端不许显示绿灯。
            'schedule': build_schedule(),
        }
        _write_board(board)
    except Exception:
        pass

    # ---- health.json：独立产品的健康状态，Dashboard 直接消费 ----
    try:
        health = build_health(state)
        state['health'] = {'status': health['status'], 'reason': health['reason']}
        write_health(health)
    except Exception as e:
        state['errors'].append('health.json 生成失败：%s' % e)
        print('! health.json 生成失败：%s' % e, file=sys.stderr)
        health = None

    # 把 health 注入 board，Dashboard 不必额外请求第二个文件
    if health:
        try:
            with open(ledger.BOARD_PATH, encoding='utf-8') as f:
                board = json.load(f)
            board['health'] = health
            _write_board(board)
        except Exception as e:
            state['errors'].append('health 注入 board 失败：%s' % e)

    try:
        tmp = RUN_STATE_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RUN_STATE_PATH)
    except Exception as e:
        print('! run-state.json 写入失败：%s' % e, file=sys.stderr)

    summary = ('status=%s  exit=%d  trigger=%s  数据源 发现%d/扫描%d/共%d  '
               '事件 seen=%s +%s ~%s  错误=%d'
               % (state['status'], state['exit_code'], state.get('trigger'),
                  state['sources_found'], state['sources_scanned'], state['sources_total'],
                  state['events_seen'], state['events_inserted'], state['events_updated'],
                  len(state['errors'])))
    log(summary)
    for e in state['errors']:
        log('  error: %s' % e)

    if not quiet:
        print()
        print('  %s' % summary)
        if health:
            print('  健康：%s（%s）' % (health['status'], health['reason']))
        if state['status'] == STATUS_FAILED:
            print('  × 运行失败：核心链路不可用，见上面的错误。')
        elif state['status'] == STATUS_NO_SOURCES:
            print('  ⚠ 未发现受支持数据源。看 discovery.json，或在 sources.json 指定路径。')
        for e in state['errors']:
            print('  ! %s' % e)
        print('  状态：%s' % RUN_STATE_PATH)
        print('  健康：%s' % HEALTH_PATH)
        print('  日志：%s' % AUTO_LOG_PATH)
    return state['status'], state


# ------------------------------------------------------- 自动同步配置（schedule）

def load_schedule():
    """读自动同步配置。文件不存在 = 没配过，返回 enabled=False（如实，不猜）。"""
    try:
        with open(SCHEDULE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def save_schedule(cfg):
    with open(SCHEDULE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def build_schedule():
    """把配置翻成 Dashboard 能直接显示的状态，含"下次运行"的推算与依据。

    下次运行**必须带 basis**（推算依据）：
    Windows 的 MINUTE 计划任务从**注册时刻**起每 N 分钟触发，
    所以 next = installed_at + ceil((now-installed_at)/N)*N。
    查不到注册时间就老老实实返回 None，前端显示"未知"，不许编。
    """
    cfg = load_schedule()
    if not cfg or not cfg.get('enabled'):
        return {'enabled': False,
                'interval_minutes': None,
                'installed_at': None,
                'next_run_at': None,
                'next_run_basis': 'not_configured',
                'method': None,
                'note': '未检测到自动同步配置。运行 install-auto 开启每 30 分钟自动扫描。'}

    out = {'enabled': True,
           'interval_minutes': cfg.get('interval_minutes'),
           'installed_at': cfg.get('installed_at'),
           'method': cfg.get('method'),
           'task_name': cfg.get('task_name') or TASK_NAME,
           'note': ''}

    iv = cfg.get('interval_minutes')
    inst = cfg.get('installed_at')
    if isinstance(iv, int) and iv > 0 and inst:
        try:
            t0 = dt.datetime.fromisoformat(inst)
            now = dt.datetime.now()
            elapsed = (now - t0).total_seconds()
            k = int(elapsed // (iv * 60)) + 1
            nxt = t0 + dt.timedelta(minutes=iv * k)
            out['next_run_at'] = nxt.isoformat(timespec='seconds')
            out['next_run_basis'] = 'estimated_from_install_time'
            out['next_run_in_seconds'] = int((nxt - now).total_seconds())
        except Exception:
            out['next_run_at'] = None
            out['next_run_basis'] = 'unparseable_install_time'
    else:
        out['next_run_at'] = None
        out['next_run_basis'] = 'missing_interval_or_install_time'
    return out


# ---------------------------------------------------------------- health.json

HEALTH_PATH = os.path.join(HERE, 'health.json')
HEALTH_STATES = ('healthy', 'partial', 'failed', 'unknown')


def _iso_age_seconds(iso):
    if not iso:
        return None
    try:
        d = dt.datetime.fromisoformat(iso)
        return int((dt.datetime.now() - d).total_seconds())
    except Exception:
        return None


def build_health(state):
    """把 run-state / ledger / board / discovery 汇成一份健康状态。

    状态定义（四态，不许含糊）：
        unknown  从未运行过 —— 没有任何依据可判断，不能假装健康
        failed   核心链路不可用：本次运行失败、账本读不出来、board 没写出或过期
        partial  能跑但降级：部分环节失败、有源文件被清退、board 落后于账本
        healthy  全链路正常且数据新鲜
    """
    now = dt.datetime.now()
    reasons = []

    # ---- sources ----
    disc = {}
    if os.path.isfile(ledger.DISCOVERY_PATH):
        try:
            with open(ledger.DISCOVERY_PATH, encoding='utf-8') as f:
                disc = json.load(f)
        except Exception as e:
            reasons.append('discovery.json 不可读：%s' % e)
    src_rows = disc.get('sources') or {}
    found = sum(1 for r in src_rows.values() if r.get('state') == 'found')
    detail = [{
        'client': c,
        'label': r.get('label'),
        'state': r.get('state'),
        'data_grade': r.get('data_grade'),
        'grade_basis': r.get('grade_basis'),
        'files': r.get('files'),
    } for c, r in sorted(src_rows.items())]
    opaque = [k for k, r in (disc.get('opaque') or {}).items() if r.get('state') == 'found']
    sources_block = {
        'found': found,
        'total': disc.get('total', len(src_rows)),
        'scanned': state.get('sources_scanned', 0),
        'grades': disc.get('grades') or state.get('grades') or {},
        'opaque_stores': opaque,
        'detail': detail,
    }

    # ---- ledger ----
    ledger_block = {
        'path': os.path.basename(ledger.DB_PATH),
        'exists': os.path.isfile(ledger.DB_PATH),
        'writable': os.access(os.path.dirname(ledger.DB_PATH), os.W_OK),
        'size_bytes': os.path.getsize(ledger.DB_PATH) if os.path.isfile(ledger.DB_PATH) else 0,
        'usage_events': 0,
        'activity_events': 0,
        'source_files_alive': 0,
        'source_files_purged': 0,
        'scan_runs': 0,
        'last_scan_at': None,
        'first_day': None,
        'last_day': None,
        'read_error': None,
    }
    try:
        c = ledger.connect()
        ledger_block['usage_events'] = c.execute(
            'SELECT COUNT(*) n FROM usage_event').fetchone()['n'] or 0
        try:
            ledger_block['activity_events'] = c.execute(
                'SELECT COUNT(*) n FROM activity_event').fetchone()['n'] or 0
        except Exception:
            pass
        try:
            ledger_block['source_files_alive'] = c.execute(
                'SELECT COUNT(*) n FROM source_file WHERE missing_since IS NULL'
            ).fetchone()['n'] or 0
            ledger_block['source_files_purged'] = c.execute(
                'SELECT COUNT(*) n FROM source_file WHERE missing_since IS NOT NULL'
            ).fetchone()['n'] or 0
        except Exception:
            pass
        try:
            row = c.execute('SELECT COUNT(*) n, MAX(started_at) t FROM scan_run').fetchone()
            ledger_block['scan_runs'] = row['n'] or 0
            ledger_block['last_scan_at'] = row['t']
        except Exception:
            pass
        rng = c.execute('SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM usage_event').fetchone()
        if rng and rng['a']:
            ledger_block['first_day'] = ledger.day_of(rng['a'])
            ledger_block['last_day'] = ledger.day_of(rng['b'])
        c.close()
    except Exception as e:
        ledger_block['read_error'] = str(e)
        reasons.append('账本不可读：%s' % e)

    # ---- board ----
    board_block = {
        'path': os.path.basename(ledger.BOARD_PATH),
        'exists': os.path.isfile(ledger.BOARD_PATH),
        'written': bool((state.get('board') or {}).get('written')),
        'generated_at': None,
        'age_seconds': None,
        'schema_version': None,
        'usage_events': None,
        'total_tokens': None,
        'estimated_cost_usd': None,
        'priced_models': None,
        'unpriced_models': None,
        'read_error': None,
    }
    if board_block['exists']:
        try:
            with open(ledger.BOARD_PATH, encoding='utf-8') as f:
                b = json.load(f)
            s = b.get('summary') or {}
            board_block.update({
                'generated_at': b.get('generated_at'),
                'age_seconds': _iso_age_seconds(b.get('generated_at')),
                'schema_version': b.get('schema_version'),
                'usage_events': s.get('usage_events'),
                'total_tokens': s.get('total_tokens'),
                'estimated_cost_usd': s.get('estimated_cost_usd_top50_models'),
                'priced_models': s.get('priced_models'),
                'unpriced_models': s.get('unpriced_models'),
            })
        except Exception as e:
            board_block['read_error'] = str(e)
            reasons.append('board 不可读：%s' % e)

    # ---- 状态判定（顺序即优先级）----
    run_status = state.get('status')
    triggered = state.get('trigger', 'manual')
    board_fresh = bool(board_block['generated_at']
                       and board_block['generated_at'] >= (state.get('started_at') or ''))

    # 1) 从未运行
    if not run_status:
        status = 'unknown'
        reasons.insert(0, '尚未运行过任何一次采集')
    # 2) 核心链路不可用
    elif run_status == STATUS_FAILED:
        status = 'failed'
        reasons.insert(0, '本次运行失败（failed）')
    elif run_status == STATUS_NO_SOURCES:
        status = 'failed'
        reasons.insert(0, '未发现任何受支持数据源，产品无法采集（no_supported_sources）')
    elif ledger_block['read_error']:
        status = 'failed'
    elif not board_block['exists'] or board_block['read_error']:
        status = 'failed'
        reasons.insert(0, 'board 不存在或不可读')
    elif not board_fresh:
        status = 'failed'
        reasons.insert(0, 'board 未随本次运行更新（可能没写成功）')
    # 3) 降级
    elif run_status == STATUS_PARTIAL:
        status = 'partial'
        reasons.insert(0, '本次运行部分失败（partial_failure）')
    elif ledger_block['source_files_purged'] > 0:
        status = 'partial'
        reasons.insert(0, '有 %d 个源文件已被客户端清退（记录仍留存）'
                      % ledger_block['source_files_purged'])
    elif state.get('pricing', {}).get('status') == 'failed':
        status = 'partial'
        reasons.insert(0, '价格同步失败（旧缓存已保留）')
    elif (board_block['unpriced_models'] or 0) > 0:
        # 成本是这个产品的核心产出之一；有模型没单价，成本数就是残的。
        # 不管本次有没有跑价格同步，都要如实降级并说清原因。
        status = 'partial'
        pstat = state.get('pricing', {}).get('status')
        reasons.insert(0, '有 %d 个模型未配置单价，成本不完整（本次价格同步：%s）'
                      % (board_block['unpriced_models'], pstat))
    else:
        status = 'healthy'

    if not reasons:
        reasons.append('全链路正常')

    # 状态定完之后再补一条"数据完整性"说明：即使整体是 partial，
    # 也要把"有源文件被清退"这件事明确写出来，而不是只留一句笼统的失败。
    if ledger_block['source_files_purged'] > 0:
        msg = '有 %d 个源文件已被客户端清退（记录仍留存）' % ledger_block['source_files_purged']
        if not any('清退' in r for r in reasons):
            reasons.append(msg)
    if (board_block['unpriced_models'] or 0) > 0:
        if not any('未配置单价' in r for r in reasons):
            reasons.append('有 %d 个模型未配置单价，成本不完整'
                           % board_block['unpriced_models'])

    return {
        'schema_version': 1,
        'generated_at': now.isoformat(timespec='seconds'),
        'status': status,
        'reason': '；'.join(reasons),
        'last_run': {
            'started_at': state.get('started_at'),
            'finished_at': state.get('finished_at'),
            'duration_ms': state.get('duration_ms'),
            'result': run_status,
            'exit_code': state.get('exit_code'),
            'trigger': triggered,
            'errors': state.get('errors') or [],
        },
        'sources': sources_block,
        'ledger': ledger_block,
        'board': board_block,
        'run_state_file': os.path.basename(RUN_STATE_PATH),
        'log_file': os.path.basename(AUTO_LOG_PATH),
        'health_file': os.path.basename(HEALTH_PATH),
    }


def write_health(health):
    tmp = HEALTH_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(health, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HEALTH_PATH)


def cmd_health(args):
    if not os.path.isfile(HEALTH_PATH):
        if getattr(args, 'json', False):
            print(json.dumps({'schema_version': 1, 'status': 'unknown',
                              'reason': 'health.json 不存在：还没有运行过',
                              'last_run': None, 'sources': None,
                              'ledger': None, 'board': None},
                             ensure_ascii=False, indent=2))
            return ledger.EXIT_PARTIAL
        print()
        print('  还没有 health.json。先跑 `python autopilot.py auto`。')
        print()
        return ledger.EXIT_PARTIAL
    with open(HEALTH_PATH, encoding='utf-8') as f:
        h = json.load(f)
    if getattr(args, 'json', False):
        print(json.dumps(h, ensure_ascii=False, indent=2))
        return ledger.EXIT_OK

    mark = {'healthy': 'OK ', 'partial': '~~ ', 'failed': 'XX ', 'unknown': '?? '}
    print()
    print('  %s健康：%s' % (mark.get(h['status'], '   '), h['status']))
    print('      原因：%s' % h['reason'])
    print('      生成：%s' % h['generated_at'])
    lr = h['last_run']
    print()
    print('  last_run   结果 %s（exit %s，trigger=%s）'
          % (lr['result'], lr['exit_code'], lr['trigger']))
    print('             %s → %s' % (lr['started_at'], lr['finished_at']))
    s = h['sources']
    g = s.get('grades') or {}
    print('  sources    发现 %s / 共 %s ｜ 扫描 %s ｜ TOKEN %s / ACTIVITY %s / UNKNOWN %s'
          % (s['found'], s['total'], s['scanned'],
             g.get('TOKEN', 0), g.get('ACTIVITY', 0), g.get('UNKNOWN', 0)))
    if s['opaque_stores']:
        print('             不透明存储：%s' % ', '.join(s['opaque_stores']))
    for d in s['detail']:
        print('               %s%-10s %-9s %-9s 文件 %s'
              % ('OK ' if d['state'] == 'found' else '-- ', d['client'],
                 d['state'], d['data_grade'] or '?', d['files']))
    l = h['ledger']
    print('  ledger     %s（%.1f MB）｜ 用量 %s ｜ 活动量 %s ｜ 扫描轮次 %s'
          % ('可读' if not l['read_error'] else '不可读',
             l['size_bytes'] / 1048576, f"{l['usage_events']:,}",
             f"{l['activity_events']:,}", l['scan_runs']))
    print('             源文件 现存 %s / 已清退 %s ｜ 跨度 %s ~ %s'
          % (l['source_files_alive'], l['source_files_purged'],
             l['first_day'], l['last_day']))
    b = h['board']
    print('  board      %s ｜ 生成于 %s（%s 秒前）｜ schema v%s'
          % ('存在' if b['exists'] else '缺失', b['generated_at'], b['age_seconds'],
             b['schema_version']))
    print('             用量事件 %s ｜ token %s ｜ 成本 %s'
          % (f"{b['usage_events']:,}" if b['usage_events'] is not None else '-',
             f"{b['total_tokens']:,}" if b['total_tokens'] is not None else '-',
             '未配置' if b['estimated_cost_usd'] is None else '$%.4f' % b['estimated_cost_usd']))
    print()
    print('  文件：%s' % HEALTH_PATH)
    print()
    return ledger.EXIT_OK


def _ledger_record_count():
    """账本里现存的记录总数（用量 + 活动量）。账本不可读时返回 0。"""
    try:
        c = ledger.connect()
        n = c.execute('SELECT COUNT(*) n FROM usage_event').fetchone()['n'] or 0
        try:
            n += c.execute('SELECT COUNT(*) n FROM activity_event').fetchone()['n'] or 0
        except Exception:
            pass
        c.close()
        return n
    except Exception:
        return 0


def _absorb_scan_stats(state):
    """把 ledger.cmd_scan 的统计吸收进 run-state，让后台任务可诊断。"""
    st = dict(getattr(ledger, 'LAST_SCAN_STATS', None) or {})
    state['scan'] = st or {}
    state['sources_scanned'] = st.get('sources_scanned', 0)
    state['events_seen'] = st.get('events_seen', 0)
    state['events_inserted'] = st.get('events_inserted', 0)
    state['events_updated'] = st.get('events_updated', 0)
    if st.get('scan_skipped_lock'):
        # Round 7：跨进程锁被其它 writer 持有 —— 明确跳过并留痕，绝不静默
        holder = st.get('lock_holder') or {}
        state['errors'].append(
            '已有扫描正在进行（PID %s，trigger=%s）—— 本次调度跳过，未采集'
            % (holder.get('pid'), holder.get('trigger')))
        state['scan_skipped_lock'] = True
    return st


def _write_board(board):
    out = ledger.BOARD_PATH
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(board, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)


def cmd_auto(args):
    do_pricing = args.pricing and not args.no_pricing
    status, _ = run_once(do_pricing=do_pricing, pricing_source=args.source,
                         trigger=getattr(args, 'trigger', 'manual'))
    return EXIT_BY_STATUS[status]


# ---------------------------------------------------------------- 常驻

def cmd_watch(args):
    interval = max(60, int(args.interval))
    print('watch 启动：每 %d 秒执行一次（Ctrl+C 退出）' % interval)
    log('watch 启动，间隔 %d 秒' % interval)
    while True:
        try:
            run_once(do_pricing=args.pricing and not args.no_pricing,
                     pricing_source=args.source)
        except KeyboardInterrupt:
            print('\nwatch 已停止')
            log('watch 停止（KeyboardInterrupt）')
            return ledger.EXIT_OK
        except Exception as e:
            log('watch 轮次异常：%s' % e)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print('\nwatch 已停止')
            log('watch 停止（KeyboardInterrupt）')
            return ledger.EXIT_OK


# ---------------------------------------------------------------- Windows 计划任务

def _task_command():
    return '"%s" "%s" auto' % (EXE, os.path.join(HERE, 'autopilot.py'))


class _Result:
    def __init__(self, code, out='', err=''):
        self.returncode, self.stdout, self.stderr = code, out, err


def _run(cmd):
    """跑外部命令。环境可能禁掉 schtasks 之类，因此绝不抛出，统一返回结果对象。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', shell=isinstance(cmd, str))
    except PermissionError as e:
        return _Result(126, '', '环境拒绝启动该程序（%s）：%s' % (cmd[0] if cmd else '?', e))
    except FileNotFoundError as e:
        return _Result(127, '', '找不到可执行文件：%s' % e)
    except OSError as e:
        return _Result(125, '', '执行失败：%s' % e)


def _pythonw():
    """优先用 pythonw.exe，避免后台任务弹控制台窗口。"""
    if EXE.lower().endswith('python.exe'):
        cand = EXE[:-len('python.exe')] + 'pythonw.exe'
        if os.path.isfile(cand):
            return cand
    return EXE


def venv_runtime():
    """正式 Runtime：本仓库 .venv 的 pythonw.exe（Round 7 冻结）。

    缺失时返回 None —— 调用方必须明确报错，禁止悄悄退回系统 Python。
    """
    cand = os.path.join(HERE, '.venv', 'Scripts', 'pythonw.exe')
    return cand if os.path.isfile(cand) else None


def _require_venv_runtime():
    exe = venv_runtime()
    if not exe:
        raise RuntimeError(
            '未找到 .venv\\Scripts\\pythonw.exe —— 正式调度禁止使用系统 Python。'
            '请先按 docs/RUNTIME.md 创建 .venv 并安装 requirements-core.txt')
    return exe


def task_command():
    """计划任务实际会执行的命令行（TR）。这是"任务能不能跑通"的唯一真相来源。

    Round 7 起 TR 强制指向项目 .venv 的 pythonw.exe；缺失时抛 RuntimeError。
    带 --trigger scheduled，这样 run-state/health 能区分「计划任务真的起来了」
    和「我自己手动跑的」—— 否则没法证明调度器在工作。
    """
    exe = _require_venv_runtime()
    return '"%s" "%s" auto --trigger scheduled' % (exe, os.path.join(HERE, 'autopilot.py'))


def register_commands(minutes=30):
    """返回两条注册路径的**精确命令**，供需要手动执行的场景复制粘贴。

    默认走 schtasks（系统自带、无需额外模块）。某些受管控的机器会把 schtasks.exe
    列入程序黑名单（进程起不来，报 WinError 5 / 拒绝访问）——这时可以用 PowerShell 的
    ScheduledTasks 模块注册，效果等价。两条路径都是**显式授权**动作，不会静默执行。
    Runtime 强制项目 .venv pythonw.exe（缺失即抛错，绝不退回系统 Python）。
    """
    exe = _require_venv_runtime()
    tr = task_command()
    # schtasks 的 /TR 里含空格路径时必须整段加引号，内层引号用 \" 转义
    sch = ['schtasks', '/Create', '/F', '/SC', 'MINUTE', '/MO', str(minutes),
           '/TN', TASK_NAME, '/TR', tr.replace('"', '\\"')]
    ps = ('$a = New-ScheduledTaskAction -Execute "%s" -Argument \'"%s" auto --trigger scheduled\'\n'
          '$t = New-ScheduledTaskTrigger -Once -At (Get-Date) '
          '-RepetitionInterval (New-TimeSpan -Minutes %d)\n'
          'Register-ScheduledTask -TaskName "%s" -Action $a -Trigger $t -Force'
          % (exe, os.path.join(HERE, 'autopilot.py'), minutes, TASK_NAME))
    return tr, sch, ps


def manual_block(reason=''):
    """受管控环境下给出可直接复制的完整手动流程。"""
    tr, sch, ps = register_commands()
    lines = []
    if reason:
        lines.append('  ! 自动注册未能完成：%s' % reason)
    lines.append('')
    lines.append('  计划任务实际会执行的命令（TR）：')
    lines.append('    %s' % tr)
    lines.append('')
    lines.append('  在**你自己的终端**里执行以下任意一条即可完成注册：')
    lines.append('')
    lines.append('  [方式一 · schtasks]')
    lines.append('    ' + ' '.join('"%s"' % x if ' ' in x else x for x in sch))
    lines.append('')
    lines.append('  [方式二 · PowerShell ScheduledTasks 模块]（schtasks 被策略拦截时用这个）')
    for l in ps.splitlines():
        lines.append('    ' + l)
    lines.append('')
    lines.append('  注册后确认：')
    lines.append('    schtasks /Query /TN %s /V /FO LIST' % TASK_NAME)
    lines.append('    或（只读查询，无需 schtasks）')
    lines.append('    Get-ScheduledTask -TaskName "%s" | Format-List; '
                 'Get-ScheduledTaskInfo -TaskName "%s"' % (TASK_NAME, TASK_NAME))
    lines.append('')
    lines.append('  手动触发一次：')
    lines.append('    schtasks /Run /TN %s' % TASK_NAME)
    lines.append('  说明：本工具不会绕过任何系统安全策略。若 schtasks 被程序黑名单拦截，')
    lines.append('       请在「安全中心 → 命令安全 → 程序黑名单」里放行，或用上面方式二。')
    return '\n'.join(lines)


def cmd_install_auto(args):
    minutes = max(1, int(args.minutes))

    if os.name != 'nt':
        print('install-auto 目前只支持 Windows（当前系统：%s）。' % os.name)
        print('其他系统请用 `python autopilot.py watch --interval %d` 或自建 systemd/cron。'
              % (minutes * 60))
        log('install-auto 跳过：非 Windows')
        return ledger.EXIT_PARTIAL

    # Round 7：调度 Runtime 冻结为项目 .venv —— 缺失即明确失败，绝不退回系统 Python
    try:
        runtime_exe = _require_venv_runtime()
    except RuntimeError as e:
        print('  ✗ %s' % e)
        log('install-auto 失败：%s' % e)
        return ledger.EXIT_FAILED

    tr, sch, ps = register_commands(minutes)

    print('即将注册 Windows 计划任务（这是你本次显式授权）：')
    print('  任务名：%s' % TASK_NAME)
    print('  频率  ：每 %d 分钟' % minutes)
    print('  命令  ：%s' % tr)
    print('  Runtime：%s（项目 .venv）' % runtime_exe)
    print()

    if getattr(args, 'print_only', False):
        print('--print-only：只输出将要执行的命令，不做任何改动。')
        print(manual_block())
        log('install-auto --print-only：输出注册命令，未执行')
        return ledger.EXIT_OK

    via = getattr(args, 'via', 'schtasks') or 'schtasks'
    if via == 'powershell':
        script = ps
        res = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script])
        label = 'PowerShell Register-ScheduledTask'
    else:
        res = _run(sch)
        label = 'schtasks /Create'

    ok = res.returncode == 0
    if ok:
        print(res.stdout or '')
        log('已注册计划任务 %s，每 %d 分钟（%s）' % (TASK_NAME, minutes, label))
        # 记下配置：Dashboard 要据此回答"自动同步开没开 / 下次什么时候跑"
        try:
            save_schedule({
                'schema_version': 2,
                'enabled': True,
                'interval_minutes': minutes,
                'installed_at': dt.datetime.now().isoformat(timespec='seconds'),
                'method': 'powershell' if via == 'powershell' else 'schtasks',
                'task_name': TASK_NAME,
                # Round 7：Runtime 与脚本固化进配置 —— 任务 TR 的唯一事实源
                'runtime_executable': runtime_exe,
                'script': os.path.join(HERE, 'autopilot.py'),
                'args': 'auto --trigger scheduled',
            })
            print('已写入 %s（自动同步状态将显示在 Dashboard）'
                  % os.path.basename(SCHEDULE_PATH))
        except Exception as e:
            print('! 配置写入失败：%s' % e, file=sys.stderr)
        print('已注册（%s）。立即做一次自检运行…' % label)
        code = cmd_auto(argparse.Namespace(pricing=False, no_pricing=True, source='all'))
        print()
        print('自检退出码 %d（0=成功 / 1=失败 / 2=部分失败 / 3=无数据源）' % code)
        print('查看任务：schtasks /Query /TN %s /V /FO LIST' % TASK_NAME)
        return ledger.EXIT_OK

    # 失败：绝不静默，给出完整可执行的手动流程
    reason = (res.stderr or res.stdout or '').strip().splitlines()
    reason = reason[0][:200] if reason else '未知原因（returncode=%s）' % res.returncode
    print('注册失败（%s）。' % label, file=sys.stderr)
    print(manual_block(reason))
    log('install-auto 失败（%s）：%s' % (label, reason))
    return ledger.EXIT_PARTIAL


def cmd_uninstall_auto(args):
    if os.name != 'nt':
        print('非 Windows，无需卸载。')
        return ledger.EXIT_OK
    via = getattr(args, 'via', 'schtasks') or 'schtasks'

    # Round 7：--dry-run 安全预览 —— 只报告将做什么，绝不触碰系统
    if getattr(args, 'dry_run', False):
        ok, info = query_task()
        print()
        print('  卸载预览（--dry-run，未做任何改动）：')
        print('    将删除计划任务：%s（当前%s）'
              % (TASK_NAME, '存在' if ok else '不存在'))
        print('    将删除配置文件：%s'
              % (os.path.basename(SCHEDULE_PATH)
                 if os.path.isfile(SCHEDULE_PATH) else '（不存在）'))
        print('    不会触碰：usage.db / usage-board.json / server / 仓库 / 其它计划任务')
        print()
        return ledger.EXIT_OK

    if via == 'powershell':
        res = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                    'Unregister-ScheduledTask -TaskName "%s" -Confirm:$false' % TASK_NAME])
        label = 'PowerShell Unregister-ScheduledTask'
    else:
        res = _run(['schtasks', '/Delete', '/F', '/TN', TASK_NAME])
        label = 'schtasks /Delete'
    ok = res.returncode == 0
    (print if ok else (lambda s: print(s, file=sys.stderr)))(res.stdout or res.stderr)
    if not ok:
        print()
        print('  可手动删除：')
        print('    schtasks /Delete /F /TN %s' % TASK_NAME)
        print('    或 Unregister-ScheduledTask -TaskName "%s" -Confirm:$false' % TASK_NAME)
    if ok:
        # 任务没了，配置也要跟着消失——否则 Dashboard 会继续显示"已开启"
        try:
            if os.path.isfile(SCHEDULE_PATH):
                os.remove(SCHEDULE_PATH)
                print('已移除 %s' % os.path.basename(SCHEDULE_PATH))
        except Exception as e:
            print('! 移除配置失败：%s' % e, file=sys.stderr)
    log('uninstall-auto %s（%s）' % ('成功' if ok else '失败', label))
    return ledger.EXIT_OK if ok else ledger.EXIT_PARTIAL


# ---------------------------------------------------------------- 任务状态查询

_PS_QUERY = (
    '$e = $ErrorActionPreference; $ErrorActionPreference = "SilentlyContinue";\n'
    '$t = Get-ScheduledTask -TaskName "%s";\n'
    'if ($null -eq $t) { "TASK_NOT_FOUND"; exit 0 }\n'
    '$i = Get-ScheduledTaskInfo -TaskName "%s";\n'
    'function F($k,$v){ "$k=$v" }\n'
    'F "TASK_NAME" $t.TaskName\n'
    'F "STATE" $t.State\n'
    'F "PATH" $t.TaskPath\n'
    'F "AUTHOR" $t.Author\n'
    'F "LAST_RUN" $i.LastRunTime\n'
    'F "LAST_RESULT" $i.LastTaskResult\n'
    'F "NEXT_RUN" $i.NextRunTime\n'
    'F "MISSED_RUNS" $i.NumberOfMissedRuns\n'
    'foreach ($a in $t.Actions) { F "ACTION_EXEC" $a.Execute; F "ACTION_ARGS" $a.Arguments }\n'
    'foreach ($g in $t.Triggers) { F "TRIGGER" $g.CimClass.CimClassName; '
    'F "REPETITION_INTERVAL" $g.Repetition.Interval; F "TRIGGER_ENABLED" $g.Enabled }\n'
    '$ErrorActionPreference = $e' % (TASK_NAME, TASK_NAME))


def query_task():
    """只读查询计划任务真实状态，返回 (ok, dict_or_reason)。

    用 PowerShell 的 ScheduledTasks 模块而不是 schtasks.exe：
    某些受管控环境把 schtasks.exe 列入程序黑名单，连查询都起不来，
    而 ScheduledTasks 是微软推荐的现代接口，且这里只做**只读**查询。
    """
    if os.name != 'nt':
        return False, {'reason': 'non_windows'}
    res = _run(['powershell', '-NoProfile', '-NonInteractive', '-Command', _PS_QUERY])
    if res.returncode != 0:
        return False, {'reason': 'query_failed',
                       'stderr': (res.stderr or res.stdout or '').strip()[:300],
                       'returncode': res.returncode}
    out = (res.stdout or '').strip()
    if 'TASK_NOT_FOUND' in out:
        return False, {'reason': 'task_not_found'}
    data = {}
    for line in out.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            data[k.strip()] = v.strip()
    if not data:
        return False, {'reason': 'empty_response', 'raw': out[:300]}
    return True, data


def cmd_task_info(args):
    ok, info = query_task()
    if getattr(args, 'json', False):
        print(json.dumps({'ok': ok, **info}, ensure_ascii=False, indent=2))
        return ledger.EXIT_OK if ok else ledger.EXIT_PARTIAL
    print()
    if not ok:
        r = info.get('reason')
        if r == 'task_not_found':
            print('  计划任务 %s 不存在。' % TASK_NAME)
            print('  注册：python autopilot.py install-auto --minutes 30')
            print('  （若 schtasks 被安全策略拦截：python autopilot.py '
                  'install-auto --print-only 会给出可复制的手动命令）')
        elif r == 'non_windows':
            print('  非 Windows，无计划任务。')
        else:
            print('  查询失败：%s' % json.dumps(info, ensure_ascii=False))
        print()
        return ledger.EXIT_PARTIAL
    print('  计划任务真实状态（只读查询，ScheduledTasks 模块）')
    label = {'TASK_NAME': '任务名', 'STATE': '状态', 'PATH': '路径', 'AUTHOR': '作者',
             'LAST_RUN': '上次运行', 'LAST_RESULT': '上次结果码', 'NEXT_RUN': '下次运行',
             'MISSED_RUNS': '错过次数', 'ACTION_EXEC': '执行程序', 'ACTION_ARGS': '参数',
             'TRIGGER': '触发器', 'REPETITION_INTERVAL': '重复间隔',
             'TRIGGER_ENABLED': '触发器启用'}
    for k, v in info.items():
        print('    %-20s %s' % (label.get(k, k), v))
    print()
    print('  期望的 TR  ：%s' % task_command())
    print()
    return ledger.EXIT_OK


def cmd_task_status(args):
    """`status` 里展示计划任务状态。

    先试 schtasks（输出最贴合习惯）；被安全策略拦截时**自动降级**到只读的
    ScheduledTasks 查询，保证 status 在任何环境下都不会只剩一句"查不了"。
    """
    if os.name != 'nt':
        print('    非 Windows，无计划任务。')
        return ledger.EXIT_OK
    res = _run(['schtasks', '/Query', '/TN', TASK_NAME, '/V', '/FO', 'LIST'])
    if res.returncode == 0:
        keep = ('TaskName', 'Status', 'Last Run Time', 'Last Result',
                'Next Run Time', 'Task To Run', 'Schedule Type')
        for line in (res.stdout or '').splitlines():
            if any(line.strip().startswith(k) for k in keep):
                print('    ' + line.strip())
        return ledger.EXIT_OK

    blocked = res.returncode in (125, 126, 127) or 'WinError 5' in (res.stderr or '') \
        or '拒绝访问' in (res.stderr or '')
    ok2, info = query_task()
    if ok2:
        print('    （schtasks 不可用，已降级为 ScheduledTasks 只读查询）')
        for k in ('TASK_NAME', 'STATE', 'LAST_RUN', 'LAST_RESULT', 'NEXT_RUN',
                  'MISSED_RUNS', 'ACTION_ARGS'):
            if k in info:
                print('    %-22s %s' % (k, info[k]))
        return ledger.EXIT_OK
    if info.get('reason') == 'task_not_found':
        print('    计划任务 %s 不存在。' % TASK_NAME)
        print('    注册：python autopilot.py install-auto --minutes 30')
        print('    受管控环境：python autopilot.py install-auto --print-only 给出手动命令')
        return ledger.EXIT_PARTIAL
    if blocked:
        print('    schtasks 被安全策略拦截：%s' % (res.stderr or '').strip()[:160])
        print('    手动确认：schtasks /Query /TN %s /V /FO LIST' % TASK_NAME)
    else:
        print('    未找到计划任务 %s。' % TASK_NAME)
        print('    注册：python autopilot.py install-auto --minutes 30')
    return ledger.EXIT_PARTIAL


# ---------------------------------------------------------------- status

def cmd_status(args):
    if getattr(args, 'json', False):
        if not os.path.isfile(RUN_STATE_PATH):
            print(json.dumps({'status': 'never_run', 'run_state_file': RUN_STATE_PATH},
                             ensure_ascii=False, indent=2))
            return ledger.EXIT_PARTIAL
        with open(RUN_STATE_PATH, encoding='utf-8') as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
        return ledger.EXIT_OK

    print()
    if not os.path.isfile(RUN_STATE_PATH):
        print('  还没有运行记录。先跑 `python autopilot.py auto`。')
        print('  预计状态文件：%s' % RUN_STATE_PATH)
        return ledger.EXIT_PARTIAL
    with open(RUN_STATE_PATH, encoding='utf-8') as f:
        st = json.load(f)
    print('  最近一次自动运行')
    print('    状态      ：%s（退出码 %s）' % (st.get('status'), st.get('exit_code')))
    print('    开始/结束 ：%s → %s（%.1f 秒）'
          % (st.get('started_at'), st.get('finished_at'),
             (st.get('duration_ms') or 0) / 1000.0))
    print('    数据源    ：发现 %s ｜ 扫描 %s ｜ 内置共 %s'
          % (st.get('sources_found', st.get('discovered_sources')),
             st.get('sources_scanned'), st.get('sources_total')))
    print('    事件      ：seen %s ｜ 新增 %s ｜ 更新 %s'
          % (f"{st.get('events_seen', 0):,}", f"{st.get('events_inserted', 0):,}",
             f"{st.get('events_updated', 0):,}"))
    g = st.get('grades') or {}
    if g:
        print('    数据等级  ：TOKEN %s ｜ ACTIVITY %s ｜ UNKNOWN %s'
              % (g.get('TOKEN', 0), g.get('ACTIVITY', 0), g.get('UNKNOWN', 0)))
    if st.get('opaque_stores'):
        print('    不透明存储：%s' % ', '.join(st['opaque_stores']))
    for c, row in (st.get('discovery') or {}).items():
        mark = 'OK ' if row.get('state') == 'found' else '-- '
        print('      %s%-9s %-8s %-9s 文件 %-4s %s'
              % (mark, c, row.get('mode'), row.get('data_grade', '?'),
                 row.get('files'), row.get('grade_basis', '')))
    b = st.get('board') or {}
    print('    Board     ：%s' % ('已写入 %s' % b.get('file') if b.get('written') else '未写入'))
    if b.get('written'):
        print('      请求 %s ｜ 活动量 %s ｜ token %s ｜ 估算成本 %s'
              % (f"{b.get('usage_events', 0):,}", f"{b.get('activity_events', 0):,}",
                 f"{b.get('total_tokens', 0):,}",
                 ('未配置' if b.get('estimated_cost_usd') is None
                  else '$%.4f' % b['estimated_cost_usd'])))
        print('      模型 %s 个有单价 ｜ %s 个未配置'
              % (b.get('priced_models'), b.get('unpriced_models')))
    p = st.get('pricing') or {}
    print('    价格同步  ：%s%s%s'
          % (p.get('status'),
             ('（%s）' % p.get('source')) if p.get('source') else '',
             ('  取回于 %s' % p.get('retrieved_at')) if p.get('retrieved_at') else ''))
    errs = st.get('errors') or []
    if errs:
        print('    错误 %d 条：' % len(errs))
        for e in errs:
            print('      ! %s' % e)
    else:
        print('    错误      ：无')
    print()
    print('  run-state：%s' % RUN_STATE_PATH)
    print('  auto.log ：%s' % AUTO_LOG_PATH)
    print('  discovery：%s' % ledger.DISCOVERY_PATH)
    print()
    print('  Windows 计划任务：')
    cmd_task_status(args)
    return EXIT_BY_STATUS.get(st.get('status'), ledger.EXIT_PARTIAL)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description='智账 · 自动采集编排层')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def add_common(p):
        p.add_argument('--pricing', action='store_true', help='额外做一次联网价格同步')
        p.add_argument('--no-pricing', action='store_true', help='显式关闭联网（默认关）')
        p.add_argument('--source', default='all', choices=['litellm', 'openrouter', 'all'])
        p.add_argument('--trigger', default='manual',
                       choices=['manual', 'scheduled', 'api'],
                       help='本次运行的触发者；计划任务 TR 带 scheduled，API 带 api')

    p = sub.add_parser('auto', help='跑一次：发现 → 采集 → 出 board')
    add_common(p)
    p.set_defaults(func=cmd_auto)

    p = sub.add_parser('watch', help='常驻，按间隔重复执行')
    add_common(p)
    p.add_argument('--interval', type=int, default=1800, help='间隔秒数，默认 1800')
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser('setup', help='首次运行向导：发现 → 展示 → 确认 → 采集 → 出 board')
    p.add_argument('--yes', action='store_true', help='跳过交互确认')
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser('health', help='查看独立产品健康状态（health.json）')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_health)

    p = sub.add_parser('install-auto', help='注册 Windows 计划任务（一次显式授权）')
    p.add_argument('--minutes', type=int, default=30)
    p.add_argument('--via', choices=['schtasks', 'powershell'], default='schtasks',
                   help='注册通道；schtasks 被安全策略拦截时用 powershell')
    p.add_argument('--print-only', action='store_true',
                   help='只输出将要执行的命令与手动流程，不做任何改动')
    p.set_defaults(func=cmd_install_auto)

    p = sub.add_parser('uninstall-auto', help='删除 Windows 计划任务')
    p.add_argument('--via', choices=['schtasks', 'powershell'], default='schtasks')
    p.add_argument('--dry-run', action='store_true',
                   help='只预览将删除什么，不做任何改动')
    p.set_defaults(func=cmd_uninstall_auto)

    p = sub.add_parser('task-info', help='查询计划任务状态（只读，不依赖 schtasks）')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=cmd_task_info)

    p = sub.add_parser('status', help='查看最近一次自动运行状态')
    p.add_argument('--json', action='store_true', help='以 JSON 输出（便于机器读取）')
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
