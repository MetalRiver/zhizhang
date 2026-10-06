#!/usr/bin/env python3
"""verify_fresh_install.py — P2 全新环境安装体验（冷启动）验收。

在一个完全隔离的临时目录 + 隔离 HOME 里，从**零状态**跑完整条链路：

    启动 → 自动发现 → 显示发现结果 → 数据等级判定 → 用户确认
         → 首次扫描 → 生成 board → Dashboard 正常显示

并证明：
    · 没有 sources.json / 旧 usage.db / discovery.json / run-state.json / 价格缓存
    · 不读取机器上已有的 Usage Ledger 状态（新账本只含沙盒 fixture 的数据）
    · 不接受确认就绝不写任何东西
    · 全程不依赖 PathOrbit

产出 docs/FRESH_INSTALL_EVIDENCE.md。
"""
from __future__ import annotations

import argparse
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
DOCS = os.path.join(HERE, 'docs')
REPORT = os.path.join(DOCS, 'FRESH_INSTALL_EVIDENCE.md')

# 只发布 Core，绝不带 integrations/
CORE_FILES = ('ledger.py', 'autopilot.py', 'usage-dashboard.html',
              'usage-board.schema.json', 'README.md', 'verify_retention.py')

# 冷启动前必须不存在的状态文件
FORBIDDEN_STATE = ('sources.json', 'usage.db', 'discovery.json', 'run-state.json',
                   'health.json', 'pricing.auto.json', 'pricing.json',
                   'usage-board.json', 'auto.log', 'resolved-sources.json')

STEPS = []


def now():
    return dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def sha(p, n=16):
    if not os.path.isfile(p):
        return '-'
    with open(p, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:n]


def step(sid, title, ok, verdict, evidence=''):
    STEPS.append({'id': sid, 'title': title, 'ok': bool(ok), 'verdict': verdict,
                  'evidence': evidence})
    print('  [%s] %-48s %s' % ('PASS' if ok else 'FAIL', title, verdict))
    return ok


def run(args, cwd, env=None, timeout=600, stdin_text=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run([PY] + args, cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', env=e, timeout=timeout,
                       input=stdin_text)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


# ---------------------------------------------------------------- fixture

def build_fake_home(home):
    """在隔离 HOME 里造出"这台机器用过 3 个客户端"的样子。"""
    # ZCode：真实结构的 sqlite
    zc = os.path.join(home, '.zcode', 'cli', 'db')
    os.makedirs(zc, exist_ok=True)
    db = os.path.join(zc, 'db.sqlite')
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE model_usage (
        id INTEGER PRIMARY KEY, session_id TEXT, model_id TEXT, provider_id TEXT,
        started_at INTEGER, input_tokens INTEGER, output_tokens INTEGER,
        reasoning_tokens INTEGER, cache_read_input_tokens INTEGER,
        cache_creation_input_tokens INTEGER)""")
    for i in range(4):
        c.execute("INSERT INTO model_usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (i + 1, 'zc-sess-1', 'glm-fresh-test', 'zhipu',
                   1787100000000 + i * 60000, 1000 + i, 200 + i, 30 + i, 500 + i, 0))
    c.commit()
    c.close()

    # WorkBuddy：jsonl
    wb = os.path.join(home, '.workbuddy', 'projects', 'proj-a')
    os.makedirs(wb, exist_ok=True)
    with open(os.path.join(wb, 's1.jsonl'), 'w', encoding='utf-8') as f:
        for i in range(3):
            f.write(json.dumps({
                'id': 'wb-%d' % i, 'sessionId': 's1',
                'timestamp': 1787100000000 + i * 1000,
                'providerData': {'model': 'glm-fresh-test', 'requestModelName': 'Auto',
                                 'rawUsage': {'prompt_tokens': 800,
                                              'completion_tokens': 120,
                                              'prompt_tokens_details': {'cached_tokens': 300}}},
            }) + '\n')

    # CatPaw：转录 jsonl（只有活动量）
    cp = os.path.join(home, '.catpaw', 'projects', 'conv-x')
    os.makedirs(cp, exist_ok=True)
    with open(os.path.join(cp, 'c1.jsonl'), 'w', encoding='utf-8') as f:
        for i in range(5):
            f.write(json.dumps({
                'messageId': 'cp-%d' % i, 'conversationId': 'conv-x',
                'type': 'user' if i % 2 == 0 else 'assistant',
                'timestamp': '2026-08-02T09:0%d:00.000Z' % i}) + '\n')

    return {'zcode_events': 4, 'workbuddy_events': 3, 'catpaw_records': 5}


def find_browser():
    for c in (os.path.expanduser(r'~\AppData\Local\Google\Chrome\Application\chrome.exe'),
              r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'):
        if os.path.isfile(c):
            return c
    return None


def dashboard_dom(root, board):
    """在**沙盒目录**里起服务、跑真实浏览器，检查渲染结果。

    不只看源码里有没有字符串 —— 要确认数据真的被画进了 DOM。
    """
    chrome = find_browser()
    if not chrome:
        return False, '环境无浏览器，跳过 DOM 核验', ''
    port = '8941'
    srv = subprocess.Popen([PY, '-m', 'http.server', port, '--bind', '127.0.0.1'],
                           cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        r = subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                            '--virtual-time-budget=10000', '--dump-dom',
                            'http://127.0.0.1:%s/usage-dashboard.html' % port],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=180)
        dom = r.stdout or ''
        s = board.get('summary') or {}
        ts = board.get('generated_at') or ''
        first = (s.get('usage_first_day') or '')[:10]

        def node(nid):
            """取渲染后某个元素的内容（dump-dom 拿到的是 JS 执行完的 DOM）。"""
            m = re.search(r'id="%s"[^>]*>(.*?)</' % nid, dom, re.S)
            return (m.group(1).strip() if m else '')

        def cls(nid):
            m = re.search(r'<(?:span|div)[^>]*id="%s"[^>]*class="([^"]*)"' % nid, dom)
            if m:
                return m.group(1)
            m = re.search(r'<(?:span|div)[^>]*class="([^"]*)"[^>]*id="%s"' % nid, dom)
            return m.group(1) if m else ''

        hero = node('heroTokens')
        rng = node('heroRange')
        pill_cls = cls('srcPill')
        banner_cls = cls('banner')

        checks = {
            'DOM 非空': len(dom) > 20000,
            '总 token 已渲染（非空非 0）': bool(hero) and hero not in ('0', ''),
            '数据跨度已渲染': bool(first) and first in rng,
            '数据源标记为 usage-board.json（未退化演示）': 'ok' in pill_cls and 'warn' not in pill_cls,
            '演示提示条未显示（banner 无 show）': 'show' not in banner_cls,
        }
        ok = all(checks.values())
        detail = '\n'.join('  %-42s %s   %s' % (k, '✓' if v else '✗',
                                                {'总 token 已渲染（非空非 0）': hero,
                                                 '数据跨度已渲染': rng,
                                                 '数据源标记为 usage-board.json（未退化演示）': pill_cls,
                                                 '演示提示条未显示（banner 无 show）': banner_cls,
                                                 }.get(k, ''))
                         for k, v in checks.items())
        summary = '%d/%d 项通过' % (sum(checks.values()), len(checks))
        evidence = (
            '无头浏览器执行页面 JS 后 dump DOM，核验**渲染结果**'
            '（不是查源码里有没有字符串）：\n%s\n\n'
            '期望：generated_at=%s ｜ 起始日=%s ｜ 全部展示的 token=%s\n'
            'DOM 长度 = %d 字符\n'
            '说明：dump-dom 拿到的是 JS 跑完之后的 DOM，所以 heroTokens 里是真实数字、'
            'banner 的 class 反映的是真实状态；脚本源码里的"演示数据"字样不会被误当成'
            '页面正在展示演示数据。'
            % (detail, ts, first, s.get('total_tokens'), len(dom)))
        return ok, summary, evidence
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--print', action='store_true')
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix='ul-fresh-')
    home = os.path.join(root, 'fakehome')
    print('P2 冷启动验收 %s' % now())
    print('  隔离根目录：%s' % root)
    print('  隔离 HOME ：%s' % home)

    try:
        # ---- 0) 铺环境：只放 Core，不放任何状态文件 ----
        os.makedirs(home, exist_ok=True)
        copied = []
        for f in CORE_FILES:
            src = os.path.join(HERE, f)
            if os.path.isfile(src):
                shutil.copy(src, os.path.join(root, f))
                copied.append(f)
        present = sorted(os.listdir(root))
        leftovers = [f for f in FORBIDDEN_STATE if f in present]
        integ = [f for f in present if 'pathorbit' in f.lower() or f == 'integrations']
        step('F0', '0. 全新目录：只有 Core 文件，零状态文件',
             not leftovers and not integ,
             '复制 %d 个文件；状态文件残留 %s；integrations 残留 %s'
             % (len(copied), leftovers or '无', integ or '无'),
             '发布内容：%s\n目录实际内容：%s' % (', '.join(copied), present))

        fx = build_fake_home(home)
        env = {'HOME': home, 'USERPROFILE': home,
               'APPDATA': os.path.join(home, 'AppData', 'Roaming'),
               'LOCALAPPDATA': os.path.join(home, 'AppData', 'Local'),
               'DSH_HOME': os.path.join(home, '.dsh')}
        step('F0b', '0b. 隔离 HOME 里预置 3 个客户端（但**没有** sources.json）',
             True,
             'zcode %d 条 / workbuddy %d 条 / catpaw %d 条'
             % (fx['zcode_events'], fx['workbuddy_events'], fx['catpaw_records']),
             '完全靠自动发现，没有手写任何路径。\n'
             'fakehome 结构：\n  .zcode/cli/db/db.sqlite\n'
             '  .workbuddy/projects/proj-a/s1.jsonl\n  .catpaw/projects/conv-x/c1.jsonl')

        # ---- 1) 启动：还没有任何运行记录 ----
        code, out = run(['autopilot.py', 'status'], root, env)
        has_no_record = '还没有运行记录' in out
        step('F1', '1. 启动：识别出这是全新环境', has_no_record,
             '退出码 %d，提示"还没有运行记录"=%s' % (code, has_no_record),
             '$ python autopilot.py status\n退出码 = %d\n\n%s' % (code, out.strip()[:900]))

        # ---- 2/3) 自动发现 + 数据等级，且**拒绝确认时不得写入** ----
        code, out = run(['autopilot.py', 'setup'], root, env, stdin_text='n\n')
        found_line = [l for l in out.splitlines() if '发现结果与数据等级' in l]
        wrote = [f for f in ('usage.db', 'usage-board.json', 'run-state.json', 'health.json')
                 if os.path.isfile(os.path.join(root, f))]
        declined_clean = (not wrote) and ('已取消' in out)
        step('F2', '2. 自动发现：无需任何手工路径',
             'zcode' in out and 'workbuddy' in out and 'catpaw' in out,
             '发现 3 个客户端（zcode / workbuddy / catpaw）',
             '$ python autopilot.py setup   （在提示处输入 n）\n退出码 = %d\n\n%s'
             % (code, out.strip()[:2400]))
        d0 = json.load(open(os.path.join(root, 'discovery.json'), encoding='utf-8'))
        grades = {c: r.get('data_grade') for c, r in (d0.get('sources') or {}).items()}
        allowed = {'TOKEN', 'ACTIVITY', 'UNKNOWN'}
        step('F3', '3. 数据等级判定：三态机制生效且判定有据',
             all(v in allowed for v in grades.values()) and bool(d0.get('grades')),
             '等级分布 %s' % json.dumps(d0.get('grades'), ensure_ascii=False),
             'discovery.json 里每个客户端都带 data_grade 与 grade_basis（判定依据）：\n%s\n\n'
             '本次沙盒里 UNKNOWN 计数为 0，是因为 UNKNOWN 只在两种情况出现：\n'
             '  ① 存储找到了但解析不出记录（found_but_unparsed）\n'
             '  ② 不透明存储（unreadable_store）\n'
             '这两条分别在 P1 演练 B（来源不可读）与真实机器的 Trae CN 加密库上被验证。\n'
             '未安装的客户端不算 UNKNOWN，只算 not_found —— 没装过不等于读不出来。'
             % json.dumps(grades, ensure_ascii=False, indent=2))
        step('F4', '4. 用户未确认 → 绝不写入任何东西', declined_clean,
             '未确认时写入的文件：%s' % (wrote or '无'),
             '这是"用户确认自动运行"的硬证据：拒绝之后目录里没有 usage.db / board / '
             'run-state / health。\n\n实际存在的文件：%s\n\nsetup 的收尾输出：\n%s'
             % (sorted(os.listdir(root)),
                '\n'.join(out.strip().splitlines()[-4:])))

        # ---- 5) 确认并首次采集 ----
        code, out = run(['autopilot.py', 'setup', '--yes'], root, env)
        step('F5', '5. 用户确认 → 首次扫描 + 生成 board', code == 0,
             '退出码 %d' % code,
             '$ python autopilot.py setup --yes\n退出码 = %d\n\n%s' % (code, out.strip()[:2600]))

        disc = json.load(open(os.path.join(root, 'discovery.json'), encoding='utf-8'))
        board = json.load(open(os.path.join(root, 'usage-board.json'), encoding='utf-8'))
        state = json.load(open(os.path.join(root, 'run-state.json'), encoding='utf-8'))
        health = json.load(open(os.path.join(root, 'health.json'), encoding='utf-8'))
        s = board.get('summary') or {}

        step('F6', '6. 账本只含沙盒数据（未读到机器已有账本）',
             s.get('usage_events') == fx['zcode_events'] + fx['workbuddy_events'],
             'board.usage_events = %s（期望 %d）'
             % (s.get('usage_events'), fx['zcode_events'] + fx['workbuddy_events']),
             'board.summary：\n%s\n\n如果误读了机器上已有的账本，这里会是 3 万多条；'
             '现在是 %s 条，说明全新的 usage.db 从零开始，没有串到旧状态。'
             % (json.dumps(s, ensure_ascii=False, indent=2)[:1400], s.get('usage_events')))

        step('F6b', '6b. Token 与活动量严格分栏',
             s.get('usage_events') == 7 and len(board.get('activity') or []) == 1
             and (board['activity'][0].get('records') == fx['catpaw_records']),
             'token 事件 %s ｜ 活动量客户端 %s 个，%s 条'
             % (s.get('usage_events'), len(board.get('activity') or []),
                (board.get('activity') or [{}])[0].get('records')),
             'board.activity = %s\n（CatPaw 只有活动量，一条都不进 token 统计）'
             % json.dumps(board.get('activity'), ensure_ascii=False))

        step('F7', '7. 未有价格缓存 → 成本为 null 而不是 0',
             s.get('estimated_cost_usd_top50_models') is None,
             'estimated_cost_usd = %s，未配置单价模型 %s 个'
             % (s.get('estimated_cost_usd_top50_models'), s.get('unpriced_models')),
             'board.summary.pricing = %s\n'
             'board.summary.estimated_cost_usd_top50_models = %s\n'
             '→ 全新环境没联网、没有价格缓存，成本必须留空，不能假装是 $0。'
             % (json.dumps(s.get('pricing'), ensure_ascii=False),
                s.get('estimated_cost_usd_top50_models')))

        step('F8', '8. health.json 可从零生成', bool(health.get('status')),
             'status=%s（%s）' % (health.get('status'), health.get('reason')),
             'health.json：\n%s' % json.dumps(health, ensure_ascii=False, indent=2)[:1800])

        # ---- 9) Dashboard 正常显示 ----
        ok, verdict, ev = dashboard_dom(root, board)
        step('F9', '9. Dashboard 正常显示（真实浏览器 DOM 核验）', ok, verdict, ev)

        # ---- 10) 不依赖 PathOrbit ----
        # 运行时文件必须零引用。README.md 是文档，它提到 PathOrbit 是为了**声明解耦**，
        # 不是依赖，单独列出来说明。
        bad_words = ('PathOrbit', 'pathorbit', 'PATHORBIT', '途有引力', '知识库看板',
                     '04-领域', 'ai-usage', 'ai_usage')
        hits, doc_hits = {}, {}
        for f in sorted(os.listdir(root)):
            p = os.path.join(root, f)
            if not os.path.isfile(p) or os.path.getsize(p) > 5 * 1024 * 1024:
                continue
            try:
                t = open(p, encoding='utf-8', errors='replace').read()
            except Exception:
                continue
            h = [w for w in bad_words if w in t]
            if not h:
                continue
            if f.lower().endswith('.md'):
                doc_hits[f] = h
            else:
                hits[f] = h
        step('F10', '10. 冷启动流程完全不依赖 PathOrbit（运行时文件零引用）',
             not hits,
             '运行时文件零引用' if not hits else '命中 %s' % hits,
             '对隔离目录内全部**运行时**文件做关键字扫描：\n%s\n\n'
             '文档文件（允许提及，用于声明解耦，不是依赖）：\n%s\n\n'
             '目录内不存在 integrations/ 或任何适配器文件，Core 也不需要它们：\n%s'
             % (json.dumps(hits, ensure_ascii=False) if hits else '  无命中',
                json.dumps(doc_hits, ensure_ascii=False) if doc_hits
                else '  无',
                sorted(os.listdir(root))))

        ok_n = len([s for s in STEPS if s['ok']])
        print('\n%s' % ('=' * 66))
        print('  检查项 %d ｜ 通过 %d ｜ 未通过 %d'
              % (len(STEPS), ok_n, len(STEPS) - ok_n))
        print('=' * 66)

        if not args.print:
            os.makedirs(DOCS, exist_ok=True)
            with open(REPORT, 'w', encoding='utf-8') as f:
                f.write(build_report(root, disc, board, state, health))
            print('\n  报告已写入：%s' % REPORT)
        return 0 if ok_n == len(STEPS) else 2
    finally:
        shutil.rmtree(root, ignore_errors=True)


def build_report(root, disc, board, state, health):
    ok = len([s for s in STEPS if s['ok']])
    tot = len(STEPS)
    L = ['# Usage Ledger · 全新环境冷启动验收证据', '']
    L.append('生成时间：%s  ' % now())
    L.append('方式：隔离临时目录 + 隔离 HOME（`HOME`/`USERPROFILE`/`APPDATA`/'
             '`LOCALAPPDATA`/`DSH_HOME` 全部指向临时目录），只放入 Core 文件，'
             '预置 3 个客户端的本地数据，**不放任何状态文件**。')
    L.append('')
    L.append('---')
    L.append('')
    L.append('## 结论')
    L.append('')
    L.append('| | |')
    L.append('|---|---|')
    L.append('| 检查项 | **%d** |' % tot)
    L.append('| 通过 | **%d** |' % ok)
    L.append('| 未通过 | **%d** |' % (tot - ok))
    L.append('')
    L.append('要求的流程逐段对应：')
    L.append('')
    L.append('| 要求 | 对应检查 |')
    L.append('|---|---|')
    L.append('| 启动 | `F1` |')
    L.append('| 自动发现 | `F2` |')
    L.append('| 显示发现结果 | `F2`（setup 的发现表） |')
    L.append('| 数据等级判定 | `F3` |')
    L.append('| 用户确认自动运行 | `F4`（拒绝时零写入）+ `F5`（确认后执行） |')
    L.append('| 首次扫描 | `F5` / `F6` |')
    L.append('| 生成 board | `F5` / `F6b` / `F7` |')
    L.append('| Dashboard 正常显示 | `F9` |')
    L.append('| 不依赖 PathOrbit | `F10` |')
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
        if s['evidence']:
            L.append('```')
            L.append(s['evidence'])
            L.append('```')
            L.append('')
    L.append('---')
    L.append('')
    L.append('## 冷启动后的真实产物')
    L.append('')
    L.append('`discovery.json`（数据等级与判定依据）：')
    L.append('')
    L.append('```json')
    L.append(json.dumps({c: {k: r.get(k) for k in
                             ('state', 'data_grade', 'grade_basis', 'files')}
                         for c, r in (disc.get('sources') or {}).items()},
                        ensure_ascii=False, indent=2))
    L.append('```')
    L.append('')
    L.append('`run-state.json`（运行契约字段）：')
    L.append('')
    L.append('```json')
    L.append(json.dumps({k: state.get(k) for k in
                         ('status', 'exit_code', 'trigger', 'started_at', 'finished_at',
                          'sources_found', 'sources_scanned', 'events_seen',
                          'events_inserted', 'events_updated', 'errors')},
                        ensure_ascii=False, indent=2))
    L.append('```')
    L.append('')
    L.append('`health.json`：')
    L.append('')
    L.append('```json')
    L.append(json.dumps(health, ensure_ascii=False, indent=2)[:2200])
    L.append('```')
    L.append('')
    L.append('## 复现')
    L.append('')
    L.append('```')
    L.append('python verify_fresh_install.py')
    L.append('```')
    L.append('')
    return '\n'.join(L)


if __name__ == '__main__':
    sys.exit(main())
