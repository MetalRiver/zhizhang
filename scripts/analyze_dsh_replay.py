#!/usr/bin/env python3
"""analyze_dsh_replay.py — dsh 重放血统取证分析器（只读，R3 研究工具）。

背景（Round 3 取证结论，见 docs/architecture/R3_DEDUP_DESIGN.md）：
    dsh 的 fork/subagent 会话把父会话历史重放进新文件。Round 1 的 status 用
    「timestamp+tokens 指纹」量化出约 5,118 条疑似重复，但指纹是启发式，
    不能作为删除依据。本工具基于**血统证据**做权威判定：

取证已证明的结构事实：
    1. 每个会话文件首行是 type=session 事件，顶层字段含
       id / parentSession / seedLength / delegationDepth / createdAt。
    2. seq 是**血统树内**的单调事件计数器：fork 子树共享、延续；
       不同树之间独立，可能碰撞（实测同项目跨树同 seq 异指纹 33 例）。
    3. fork 子会话（有 seedLength）文件内 seq <= seedLength 的内容是父链
       历史重放；seq > seedLength 是子会话自有工作。重放事件保留原始
       seq / time / usage（父子逐条比对 316/316、367/367 完全一致）。
    4. depth=1 的 subagent（parentSession 有、seedLength 无）内容独立
       （实测与父 0% 重合），绝不参与去重。
    5. session/end-seed 事件是种子批次边界标记，随内容一起被复制。

权威判定规则（指纹只作旁证，不作依据）：
    no_parent           文件无 parentSession —— 全部为原始事件
    subagent_no_seed    depth=1 或无 seedLength —— 全部为原始事件（独立工作）
    normal_own          seq > seedLength —— 子会话自有工作
    parent_seed_exact   seq <= seedLength 且最近祖先文件中存在同 seq 同指纹
    ancestor_seed_exact 最近父不存在，但更远祖先可验证
    ambiguous           树内同 seq 但指纹不一致 —— 保留，不判定为重放
    broken_parent       父链全部缺失或无法验证 —— 保留（宁可漏杀不可误删）

安全边界：
    - 只读：绝不修改任何 dsh 源文件 / usage.db / 任何账本产物。
    - 跨树 / 跨项目的 seq 碰撞是独立事件（实测存在），绝不因 seq 相同去重。
    - 指纹启发式（timestamp+tokens）只输出对照数字。

用法：
    .venv/Scripts/python scripts/analyze_dsh_replay.py [--json OUT] [--sessions-dir DIR]
    不带 --sessions-dir 时，通过 ledger.py 的数据源解析取得 dsh sessions 目录。
    输出 JSON 含 per-session replay seq 清单，供影子库标记使用（--json - 输出 stdout）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------- 文件读取

def _iter_lines(path):
    """跨帧解压 .zstd（容忍截断尾部），或直接读纯 .jsonl。"""
    if path.endswith('.zstd'):
        try:
            import zstandard
        except ImportError:
            raise RuntimeError('解析 dsh 会话需要 zstandard：'
                               '使用 .venv/Scripts/python 或 pip install zstandard')
        dec = zstandard.ZstdDecompressor()
        obj = dec.decompressobj(read_across_frames=True)
        buf = b''
        tail_err = None
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                try:
                    buf += obj.decompress(chunk)
                except Exception as e:      # 尾部截断：保留已解出内容
                    tail_err = str(e)
                    break
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    yield line
        if buf:
            yield buf
        if tail_err:
            global LAST_TAIL_ERROR
            LAST_TAIL_ERROR = tail_err
    else:
        with open(path, 'rb') as f:
            for line in f:
                yield line


LAST_TAIL_ERROR = None


def scan_session_file(path):
    """解析单个会话文件：返回 (header, usage_events, end_seeds)。

    usage_events: [{seq, time, input, output, cache_read, model}]
    不读取、不输出任何用户消息正文 —— 结构元数据 only。
    """
    header = {}
    usage = []
    end_seeds = []
    for raw in _iter_lines(path):
        if b'"assistant/message"' not in raw and b'"session"' not in raw \
                and b'end-seed' not in raw:
            continue
        try:
            o = json.loads(raw)
        except Exception:
            continue
        t = o.get('type')
        if t == 'session' and not header:
            header = {k: o.get(k) for k in
                      ('id', 'parentSession', 'seedLength', 'delegationDepth', 'createdAt')}
        elif t == 'session/end-seed':
            end_seeds.append(o.get('seq'))
        elif t == 'assistant/message':
            u = (o.get('data') or {}).get('usage')
            if isinstance(u, dict):
                src = ((o.get('data') or {}).get('message') or {}).get('source') or {}
                reply = (src.get('replayState') or {}).get('response') or {}
                usage.append({
                    'seq': o.get('seq'),
                    'time': o.get('time'),
                    'input': u.get('inputTokens') or 0,
                    'output': u.get('outputTokens') or 0,
                    'cache_read': u.get('cacheReadTokens') or 0,
                    'model': (reply.get('responseModel') or src.get('model') or ''),
                })
    return header, usage, end_seeds


# ---------------------------------------------------------------- 血统与判定
# Round 4 起：判定逻辑的唯一权威实现在 ledger.py（DshLineage），
# 本工具只做事件归一化与结果呈现 —— ingest / replay-mark / analyzer 三者共用，
# 杜绝算法漂移。

def _normalize(events):
    """analyzer 抓取的原始 usage 事件 → ledger 判定器需要的归一化形状。"""
    out = []
    for e in events:
        out.append({'event_id': 'seq%s' % e['seq'],
                    'ts_ms': e['time'],
                    'input_tokens': e['input'],
                    'output_tokens': e['output'],
                    'cache_read_tokens': e['cache_read'],
                    'model': e['model']})
    return out


def classify(sessions, sessions_dir=None):
    """对 collect_sessions 的结果做血统判定（委托 ledger.DshLineage）。

    sessions_dir 给定时：祖先验证按需只读解析磁盘文件（生产语义）。
    未给定时（测试/复算）：用 sessions 里已解析的事件预置祖先索引，纯内存。
    返回 (verdicts, lineage)，verdicts: {sid: [(seq, reason), …]}。
    """
    sys.path.insert(0, ROOT)
    import ledger

    lin = ledger.DshLineage()
    if sessions_dir:
        lin.load({os.path.join(sessions_dir, s['file']) for s in sessions.values()})
    # 预置头部与事件索引（磁盘模式也会命中缓存，避免重复解析）
    for sid, s in sessions.items():
        lin.headers[sid] = s['header']
        idx = {}
        for ev in s['usage']:
            try:
                seq = int(str(ev['seq']))
            except (ValueError, TypeError):
                continue
            idx.setdefault(seq, (ev['time'], ev['input'], ev['output'],
                                 ev['cache_read'], ev['model']))
        lin.indexes[sid] = idx
        if not sessions_dir:
            lin.files[sid] = None

    verdicts = {}
    for sid, s in sessions.items():
        h = s['header']
        parent = h.get('parentSession')
        sl = h.get('seedLength')
        if not parent:
            file_reason = 'no_parent'
        elif sl is None:
            file_reason = 'subagent_no_seed'
        else:
            file_reason = None
        rows = []
        if file_reason:
            rows = [(e['seq'], file_reason) for e in s['usage']]
        else:
            vmap = lin.classify_file(sid, _normalize(s['usage']))
            for e in s['usage']:
                seq = e['seq']
                v = vmap.get(seq)
                if not v:
                    rows.append((seq, 'normal_own'))
                elif v[3] == 'replay':
                    rows.append((seq, v[1]))
                elif v[3] == 'own':
                    rows.append((seq, 'normal_own'))
                elif v[3] == 'broken':
                    rows.append((seq, 'broken_parent'))
                elif v[3] == 'ambiguous':
                    rows.append((seq, 'ambiguous'))
                else:
                    rows.append((seq, 'normal_own'))
        verdicts[sid] = rows

    # 血统元数据（树根 / 祖先链），仅用于呈现
    roots = {}

    def root_of(sid):
        path = []
        cur = sid
        while True:
            if cur in roots:
                r = roots[cur]
                break
            h = (sessions.get(cur) or {}).get('header') or {}
            p = h.get('parentSession')
            if not p or p not in sessions or p in path:
                r = cur if (not p or p not in sessions) else p
                break
            path.append(cur)
            cur = p
        for s2 in path:
            roots[s2] = r
        roots[sid] = r
        return r

    lineage = {'roots': {sid: root_of(sid) for sid in sessions}}
    lineage['chains'] = {}
    for sid in sessions:
        chain = []
        cur = (sessions[sid]['header'] or {}).get('parentSession')
        seen = {sid}
        while cur and cur in sessions and cur not in seen:
            chain.append(cur)
            seen.add(cur)
            cur = (sessions[cur]['header'] or {}).get('parentSession')
        lineage['chains'][sid] = chain
    return verdicts, lineage


# ---------------------------------------------------------------- 汇总与入口

REPLAY_REASONS = ('parent_seed_exact', 'ancestor_seed_exact')


def collect_sessions(sessions_dir):
    """扫描目录下全部会话文件，返回 classify() 需要的 sessions 字典。"""
    files = {}
    for p in sorted(set(glob.glob(os.path.join(sessions_dir, '**', 'session.jsonl.zstd'),
                                  recursive=True))
                    | set(glob.glob(os.path.join(sessions_dir, '**', 'session.jsonl'),
                                    recursive=True))):
        sid = os.path.basename(os.path.dirname(p))
        files[sid] = p
    sessions = {}
    parse_errors = 0
    for sid, p in files.items():
        try:
            header, usage, end_seeds = scan_session_file(p)
        except Exception:
            parse_errors += 1
            continue
        project = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(p))))
        sessions[sid] = {'header': header, 'usage': usage, 'end_seeds': end_seeds,
                         'file': os.path.relpath(p, sessions_dir), 'project': project}
    return sessions, parse_errors


def heuristic_crosscheck(sessions):
    """status 同款启发式（timestamp+tokens+model 跨会话重合）—— 仅作对照。"""
    from collections import Counter
    fp_count = Counter()
    for s in sessions.values():
        for ev in s['usage']:
            fp_count[(ev['time'], ev['input'], ev['output'],
                      ev['cache_read'], ev['model'])] += 1
    groups = sum(1 for c in fp_count.values() if c > 1)
    extra = sum(c - 1 for c in fp_count.values() if c > 1)
    return {'groups': groups, 'extra_events': extra}


def main():
    ap = argparse.ArgumentParser(description='dsh 重放血统取证（只读）')
    ap.add_argument('--sessions-dir', help='dsh sessions 目录（默认经 ledger 解析）')
    ap.add_argument('--json', dest='json_out', help='输出机器可读 JSON 到文件（- 为 stdout）')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    sessions_dir = args.sessions_dir
    if not sessions_dir:
        sys.path.insert(0, ROOT)
        import ledger                                  # 只为路径解析，不触碰账本
        sessions_dir = ledger.SOURCES['dsh']['path']
    if not os.path.isdir(sessions_dir):
        print('✗ sessions 目录不存在：%s' % sessions_dir, file=sys.stderr)
        return 2

    sessions, parse_errors = collect_sessions(sessions_dir)

    verdicts, lineage = classify(sessions, sessions_dir)

    # ---- 汇总 ----
    total = sum(len(s['usage']) for s in sessions.values())
    reason_count = {}
    for vs in verdicts.values():
        for _, r in vs:
            reason_count[r] = reason_count.get(r, 0) + 1
    confirmed = sum(reason_count.get(r, 0) for r in REPLAY_REASONS)
    n_parent = sum(1 for s in sessions.values() if s['header'].get('parentSession'))
    n_broken = sum(1 for sid, s in sessions.items()
                   if s['header'].get('parentSession')
                   and s['header']['parentSession'] not in sessions)
    n_edges = sum(1 for s in sessions.values() if s['header'].get('parentSession'))
    multi_level = sum(1 for sid, s in sessions.items()
                      if len(lineage['chains'][sid]) >= 2)
    unresolved = reason_count.get('ambiguous', 0) + reason_count.get('broken_parent', 0)

    result = {
        'schema': 'dsh-replay-analysis/1',
        'sessions_dir': sessions_dir,
        'sessions': len(sessions),
        'parse_errors': parse_errors,
        'tail_errors': 1 if LAST_TAIL_ERROR else 0,
        'usage_events_total': total,
        'reasons': reason_count,
        'confirmed_replay': confirmed,
        'unresolved': unresolved,
        'normal': total - confirmed - unresolved,
        'lineage': {'with_parent': n_parent, 'edges': n_edges,
                    'broken_parent_links': n_broken, 'multi_level_chains': multi_level,
                    'trees': len(set(lineage['roots'].values()))},
        'heuristic_crosscheck': heuristic_crosscheck(sessions),
        'per_session': {sid: {
            'file': sessions[sid]['file'],
            'project': sessions[sid]['project'],
            'parent': sessions[sid]['header'].get('parentSession'),
            'seedLength': sessions[sid]['header'].get('seedLength'),
            'delegationDepth': sessions[sid]['header'].get('delegationDepth'),
            'usage': len(sessions[sid]['usage']),
            'replay_seqs': [seq for seq, r in verdicts[sid] if r in REPLAY_REASONS],
            'reasons': {r: sum(1 for _, x in verdicts[sid] if x == r)
                        for r in set(x for _, x in verdicts[sid])},
        } for sid in sorted(sessions)},
    }

    if args.json_out:
        payload = json.dumps(result, ensure_ascii=False, indent=1)
        if args.json_out == '-':
            print(payload)
        else:
            with open(args.json_out, 'w', encoding='utf-8') as f:
                f.write(payload)
    if not args.quiet or not args.json_out:
        print('dsh 重放血统取证（只读）')
        print('  会话 %d ｜ usage 事件 %d ｜ 解析失败 %d ｜ 截断尾部 %s'
              % (result['sessions'], total, parse_errors, '有' if result['tail_errors'] else '无'))
        g = result['lineage']
        print('  血统：有父 %d ｜ 边 %d ｜ 断链 %d ｜ 多层链 %d ｜ 树 %d'
              % (g['with_parent'], g['edges'], g['broken_parent_links'],
                 g['multi_level_chains'], g['trees']))
        print('  判定：confirmed %d ｜ ambiguous %d ｜ broken_parent %d ｜ normal %d'
              % (confirmed, reason_count.get('ambiguous', 0),
                 reason_count.get('broken_parent', 0), result['normal']))
        rc = result['reasons']
        print('    明细：parent_seed_exact %d ｜ ancestor_seed_exact %d ｜ normal_own %d ｜ '
              'no_parent %d ｜ subagent_no_seed %d'
              % (rc.get('parent_seed_exact', 0), rc.get('ancestor_seed_exact', 0),
                 rc.get('normal_own', 0), rc.get('no_parent', 0),
                 rc.get('subagent_no_seed', 0)))
        hc = result['heuristic_crosscheck']
        print('  启发式对照（仅参考）：%d 组 / 约 %d 条' % (hc['groups'], hc['extra_events']))
        print('  减少比例：%.1f%%' % (confirmed * 100.0 / total if total else 0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
