#!/usr/bin/env python3
"""discovery.py — Universal Local AI Discovery Engine（V1.1）。

目标：用户不用自己找 AI 工具的数据路径。打开账本 → 引擎检查这台电脑的标准
应用位置 → 给出每个工具的诚实结论。

四层架构：
    LAYER 1  Installed Application Discovery —— 只扫标准用户应用目录
             （home 一级点目录、%APPDATA% / %LOCALAPPDATA% 一级目录、
             HKCU/HKLM uninstall 注册表键），不做全盘递归 C:\\ / D:\\。
    LAYER 2  Candidate Data Discovery —— 在候选工具的数据目录里做有界
             文件发现：深度 / 文件数 / 探测大小 / 超时全部受限，绝不把
             引擎变成磁盘扫描器。
    LAYER 3  Fingerprint Engine —— SQLite 只读 schema 嗅探（table/column
             名）；JSONL 只取最少样本做结构 fingerprint。绝不保存或发送
             prompt / response 正文，只看 schema / key 结构。
    LAYER 4  Adapter Resolution —— Dedicated Adapter（catalog 既有 5 工具）
             → Generic Known Schema（严格契约，见 source-catalog.json）
             → Activity-only → DETECTED_UNSUPPORTED → UNKNOWN。
             未知结构绝不猜 Token 字段：宁可"发现了，但暂时读不出用量"。

四种内部状态（对用户诚实的承诺，见 source-catalog / 产品文档）：
    SUPPORTED            已支持，直接记账（既有 5 个 Adapter）
    GENERIC_SUPPORTED    无专用 Adapter，但数据严格符合已知通用格式，自动解析
    DETECTED_UNSUPPORTED 确认发现 AI 数据源，暂未适配完整用量
    UNKNOWN              发现疑似数据，无法安全判定

隐私边界：
    - 100% 本地，零联网。
    - 结果对外（API / UI）一律脱敏：没有用户绝对路径，只有工具名 / 状态 /
      能力 / 脱敏后的位置提示（ledger.redact_path）。
    - fingerprint 只含 schema / key 结构，不含记录内容。

缓存：discovery-state.json（运行态文件，DATA_ROOT）。应用版本 / catalog
内容 / 标准目录签名变化或用户主动 refresh 时才做完整 discovery；否则
lightweight（只复探已知候选的存在性，毫秒级）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time

if getattr(sys, 'frozen', False):
    BASE = os.environ.get('USAGE_LEDGER_HOME') or os.path.join(
        os.environ.get('LOCALAPPDATA') or
        os.path.join(os.path.expanduser('~'), 'AppData', 'Local'), 'UsageLedger')
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

# 路径契约（V1.1 冻结）：catalog 是**只读程序资源**，frozen 下从打包
# 资源根（PyInstaller _MEIPASS / _internal）读取，随程序版本升级；
# 发现状态（discovery-state / generic-sources）是**可写用户数据**，
# 写 DATA_ROOT。catalog read path != state write path。
STATE_PATH = os.path.join(BASE, 'discovery-state.json')

# 发现状态（产品承诺；不要与 ledger 的数据等级 GRADE_* 混用）
ST_SUPPORTED = 'SUPPORTED'
ST_GENERIC = 'GENERIC_SUPPORTED'
ST_UNANCHORED = 'GENERIC_SUPPORTED_UNANCHORED'
ST_DETECTED = 'DETECTED_UNSUPPORTED'
ST_UNKNOWN = 'UNKNOWN'

# 产品边界（V1.1 冻结）：DISCOVERY != INGESTION。
#   自动发现范围 > 自动记账范围。Generic 来源只有具备**稳定 tool
#   identity anchor**（catalog 规则 / installed app / registry 身份）
#   才允许自动入账；仅靠目录 basename / 备份名 / 归档名 ≠ 工具身份。
#   unanchored：发现、可读，但不入账，等待锚定。
# 事件身份策略（V1.1 冻结）：单个来源生命周期内 pid / content 必须
#   稳定；发现策略将变 → needs_attention 并暂停自动入账，绝不双记。
_GENERIC_ADAPTERS = ('generic-jsonl-openai-usage',
                     'generic-sqlite-openai-usage')
_GENERIC_PID_FIELDS = ('event_id', 'request_id', 'message_id', 'usage_id')

_REDIR = re.compile(r'^([A-Za-z]:)?[\\/]')


# ---------------------------------------------------------------- catalog

def _catalog_resource_path():
    """catalog 的正式读取路径（只读程序资源，随包分发）。"""
    import paths
    return paths.application_resource_path('source-catalog.json')


def load_catalog(path=None):
    """读取 source catalog（只读程序资源）。

    frozen 下永远读打包资源根（随程序版本升级），绝不读 DATA_ROOT 中
    可能存在的旧副本 —— 不创建隐式用户 override。真正缺失时抛出明确
    的打包错误（供诊断），绝不静默。"""
    p = path or _catalog_resource_path()
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            'packaged resource missing: source-catalog.json '
            '(resource root: %s)' % os.path.dirname(p)) from e


def _app_version():
    try:
        with open(os.path.join(BASE, 'VERSION'), encoding='utf-8') as f:
            m = re.search(r'^version\s+(\S+)', f.read(), re.M)
            return m.group(1) if m else 'unknown'
    except OSError:
        return 'unknown'


def _expand_hint(hint, home=None):
    """catalog 路径 token → 本机绝对路径（只做显式 token 展开）。"""
    home = home or os.path.expanduser('~')
    p = hint.replace('\\', '/')
    p = p.replace('%APPDATA%', os.environ.get('APPDATA')
                  or os.path.join(home, 'AppData', 'Roaming'))
    p = p.replace('%LOCALAPPDATA%', os.environ.get('LOCALAPPDATA')
                  or os.path.join(home, 'AppData', 'Local'))
    if p.startswith('~/'):
        p = os.path.join(home, p[2:])
    return os.path.normpath(p)


def _redact(p):
    """结果里绝不出现用户绝对路径；交给 ledger 的统一脱敏（可用时）。"""
    try:
        import ledger
        return ledger.redact_path(p)
    except Exception:
        return _REDIR.sub('…/', (p or '').replace('\\', '/'))


def _slug(name):
    s = re.sub(r'[^a-z0-9_-]+', '-', (name or '').lower()).strip('-')
    return s or 'unknown-tool'


# ---------------------------------------------------------------- LAYER 1

def _home_dotdirs():
    home = os.path.expanduser('~')
    try:
        return sorted(
            e.name for e in os.scandir(home)
            if e.is_dir(follow_symlinks=False) and e.name.startswith('.'))
    except OSError:
        return []


def _std_dir_children(env):
    p = os.environ.get(env) or os.path.join(
        os.path.expanduser('~'), 'AppData',
        'Local' if env == 'LOCALAPPDATA' else 'Roaming')
    try:
        return p, sorted(
            e.name for e in os.scandir(p)
            if e.is_dir(follow_symlinks=False))
    except OSError:
        return p, []


def _registry_installed_names():
    """HKCU/HKLM uninstall 键的 DisplayName（只读，仅作命名提示）。
    非Windows / 注册表不可用 → 空列表，其余层照常工作。"""
    names = []
    try:
        import winreg
    except ImportError:
        return names
    for root, sub in ((winreg.HKEY_CURRENT_USER,
                       r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')):
        try:
            k = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            i = 0
            while i < 512:                       # 有界：最多 512 个键
                try:
                    sk = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                try:
                    vk = winreg.OpenKey(k, sk)
                    name, _t = winreg.QueryValueEx(vk, 'DisplayName')
                    if isinstance(name, str) and name.strip():
                        names.append(name.strip())
                except OSError:
                    continue
        finally:
            winreg.CloseKey(k)
    return names


def _norm_anchor(s):
    """锚匹配的字符串规范化：NFKC + strip + casefold。"""
    import unicodedata
    return unicodedata.normalize('NFKC', str(s or '')).strip().casefold()


def _tool_anchor_names(tool):
    """filesystem basename 严格锚集合：id + install_hints +
    install_aliases（catalog 显式别名），全部 normalized。"""
    names = [tool.get('id')]
    names += list(tool.get('install_hints') or [])
    names += list(tool.get('install_aliases') or [])
    return {_norm_anchor(n) for n in names if n}


def _strict_anchor_id(name, tools):
    """稳定 tool identity 锚（严格）：filesystem basename 的
    normalized exact match 或 catalog install_aliases 显式别名。

    禁止裸 substring：'eta-ai' 绝不命中 'theta-ai'，
    'codex' 绝不命中 'my-codex-backup' —— 除非 catalog 用
    install_aliases 显式声明该名称。宽发现、严锚定。
    """
    n = _norm_anchor(name)
    if not n:
        return None
    for t in tools:
        if n in _tool_anchor_names(t):
            return t['id']
    return None


def _registry_name_matches(display_name, tool):
    """Installed app / registry display name 锚（严格）：
    normalized exact match（catalog display_name 或 install_aliases）。
    不做任意 substring / 词边界碰巧包含。"""
    n = _norm_anchor(display_name)
    if not n:
        return False
    refs = {_norm_anchor(tool.get('display_name'))}
    refs |= {_norm_anchor(a) for a in (tool.get('install_aliases') or [])}
    return n in refs


def _loose_candidate_hit(name, generic_hints):
    """宽松候选判定（仅用于 candidate generation，绝不作为锚）：
    通用 AI 命名提示的词边界匹配 —— '这个目录可能值得检查'。"""
    low = name.lower()
    for h in generic_hints:
        if re.search(r'(?:^|[^a-z])%s(?:[^a-z]|$)' % re.escape(h.lower()), low):
            return True
    return False


def layer1_candidate_roots(catalog, home_override=None, registry_names=None):
    """候选数据根目录（有界清单）：catalog 显式 hints + 标准目录命名命中。
    返回 [{root, via, location_kind, tool_id}]；绝不全盘递归。

    锚语义（V1.1 冻结）：tool_id 只由**严格**匹配赋予（basename
    normalized exact / catalog alias / registry exact alias）；
    宽松命中只生成无名候选（name_heuristic），进 layer2 后一律
    unanchored，不自动入账。
    registry_names：注入的已安装应用名（测试用）；缺省读真实注册表。"""
    limits = catalog.get('limits') or {}
    generic_hints = (catalog.get('generic') or {}).get('name_hints') or []
    tools = catalog.get('tools') or []
    home = home_override or os.path.expanduser('~')
    roots, seen = [], set()

    def add(root, via, kind, tool_id=None):
        root = os.path.normpath(root)
        if root in seen or len(roots) >= int(limits.get('max_candidate_roots', 200)):
            return
        seen.add(root)
        roots.append({'root': root, 'via': via,
                      'location_kind': kind, 'tool_id': tool_id})

    # 1a. catalog 显式 data_path_hints（已知工具的直接路径事实）
    for tool in tools:
        for hint in (tool.get('data_path_hints') or []):
            p = _expand_hint(hint, home=home)
            parent = os.path.dirname(p) if os.path.splitext(p)[1] else p
            add(parent, 'catalog_path', 'hint', tool['id'])

    # 1b. home 一级点目录 / 标准目录一级子目录：严格锚给 tool_id；
    #     宽松命中只生成候选（不锚定）。
    dotdirs = [os.path.join(home, n) for n in _home_dotdirs()]
    for env, kind in (('APPDATA', 'appdata'), ('LOCALAPPDATA', 'localappdata')):
        base, children = _std_dir_children(env)
        for n in children:
            d = os.path.join(base, n)
            hit = _strict_anchor_id(n, tools)
            if hit:
                add(d, 'std_dir_match', kind, hit)
            elif _loose_candidate_hit(n, generic_hints):
                add(d, 'name_heuristic', kind)
    for d in dotdirs:
        n = os.path.basename(d)
        hit = _strict_anchor_id(n, tools)
        if hit:
            add(d, 'std_dir_match', 'home_dotdir', hit)
        elif _loose_candidate_hit(n, generic_hints):
            add(d, 'name_heuristic', 'home_dotdir')

    # 1c. 已安装应用名（注册表）：严格 exact/alias 匹配；只用来给
    # 已知 catalog 工具补 confidence，不展开未知安装位置。
    try:
        reg = registry_names if registry_names is not None \
            else _registry_installed_names()
    except Exception:
        reg = []
    for tool in tools:
        if any(_registry_name_matches(n, tool) for n in reg):
            for hint in (tool.get('data_path_hints') or []):
                add(os.path.dirname(_expand_hint(hint)),
                    'registry_hint', 'registry', tool['id'])
    return roots


# ---------------------------------------------------------------- LAYER 2

def _is_reparse(path):
    """symlink / junction：一律不跟随（防无限递归与越界）。"""
    try:
        st = os.lstat(path)
        if os.path.islink(path):
            return True
        if hasattr(st, 'st_file_attributes'):
            return bool(st.st_file_attributes & 0x400)   # FILE_ATTRIBUTE_REPARSE_POINT
        return False
    except OSError:
        return True    # 不可判定 → 当作危险，跳过


def layer2_find_data_files(root, catalog, deadline):
    """在候选根里做有界数据文件发现。返回 [绝对路径]。"""
    limits = catalog.get('limits') or {}
    max_depth = int(limits.get('data_walk_depth', 3))
    max_files = int(limits.get('max_files_per_root', 400))
    found = []
    interesting_ext = ('.db', '.sqlite', '.sqlite3', '.jsonl', '.ndjson')
    interesting_name = ('history', 'session', 'usage', 'conversation', 'memory')
    base_depth = root.rstrip(os.sep).count(os.sep)
    try:
        it = os.walk(root, topdown=True, followlinks=False,
                     onerror=lambda e: None)
        for dirpath, dirnames, filenames in it:
            if time.monotonic() > deadline or len(found) >= max_files:
                return found
            depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            # junction / symlink 目录一律剪枝
            dirnames[:] = [d for d in dirnames
                           if not _is_reparse(os.path.join(dirpath, d))]
            for fn in filenames:
                low = fn.lower()
                if (low.endswith(interesting_ext)
                        or any(k in low for k in interesting_name)):
                    found.append(os.path.join(dirpath, fn))
                    if len(found) >= max_files:
                        return found
    except OSError:
        return found
    return found


# ---------------------------------------------------------------- LAYER 3

def _fingerprint_sqlite(path, max_tables=8):
    """只读 schema 嗅探。加密 / 损坏 → {'kind':'opaque'}。"""
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'),
                               uri=True, timeout=2)
    except sqlite3.Error:
        return {'kind': 'opaque'}
    try:
        try:
            sql = ("SELECT name FROM sqlite_master WHERE type='table'"
                   " AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
                   " LIMIT " + str(max_tables + 1))
            tables = [r[0] for r in conn.execute(sql)]
        except sqlite3.DatabaseError:
            return {'kind': 'opaque'}
        if not tables:
            return {'kind': 'sqlite', 'tables': {}}
        out = {}
        for t in tables[:max_tables]:
            try:
                cols = [r[1] for r in conn.execute(
                    'PRAGMA table_info(%s)' % json.dumps(t))]
            except sqlite3.DatabaseError:
                return {'kind': 'opaque'}
            out[t] = cols
        return {'kind': 'sqlite', 'tables': out}
    finally:
        conn.close()


def _sample_lines(path, limit, max_bytes):
    """读最少样本行（结构 fingerprint 用），超过 max_bytes 停止。
    只返回 JSON 对象列表；绝不保留行文本。"""
    objs = []
    try:
        with open(path, 'rb') as f:
            buf = f.read(max_bytes)
        text = buf.decode('utf-8', errors='replace')
    except OSError:
        return objs
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict):
            objs.append(o)
        if len(objs) >= limit:
            break
    return objs


def _key_paths(obj, prefix='', depth=0):
    """抽取 key 结构（最多两层嵌套），不含值。"""
    out = []
    for k, v in obj.items():
        p = '%s.%s' % (prefix, k) if prefix else k
        out.append(p)
        if depth < 1 and isinstance(v, dict):
            out.extend(_key_paths(v, p, depth + 1))
    return out


def _fingerprint_jsonl(path, catalog):
    limits = catalog.get('limits') or {}
    objs = _sample_lines(path, int(limits.get('max_sample_lines', 5)),
                         int(limits.get('max_probe_bytes', 262144)))
    keys = []
    for o in objs:
        for kp in _key_paths(o):
            if kp not in keys:
                keys.append(kp)
    if not objs:
        return {'kind': 'unparseable'}
    return {'kind': 'jsonl', 'keys': keys, 'samples': len(objs)}


def fingerprint_file(path, catalog):
    """单一入口：文件 → fingerprint（只含 schema / 结构，无内容）。"""
    low = path.lower()
    if low.endswith(('.db', '.sqlite', '.sqlite3')):
        return _fingerprint_sqlite(path)
    if low.endswith(('.jsonl', '.ndjson')):
        return _fingerprint_jsonl(path, catalog)
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {'kind': 'other', 'bytes': size}


# ---------------------------------------------------------------- LAYER 4

_TS_FIELDS = ('ts_ms', 'timestamp', 'ts', 'created_at', 'created', 'time',
              'started_at')
_SESSION_FIELDS = ('session_id', 'conversation_id', 'session', 'thread_id')
_MODEL_FIELDS = ('model', 'model_name', 'model_id')
_AMBIGUOUS_CACHE = ('cached_tokens', 'cache_read', 'cache_write')
_PROMPT_TOKEN_FIELDS = ('prompt_tokens', 'input_tokens')
_COMPLETION_TOKEN_FIELDS = ('completion_tokens', 'output_tokens')


def _generic_jsonl_match(fp):
    """严格通用契约判定。返回 ('openai_usage', mapping) / ('activity', None)
    / (None, reason)。绝不猜：字段不齐就拒绝。"""
    if fp.get('kind') == 'unparseable':
        return None, '无法解析出 JSON 结构'
    keys = set(fp.get('keys') or [])
    if not keys:
        return None, '样本里没有可识别的字段结构'
    has_model = any(k in keys for k in _MODEL_FIELDS)
    has_prompt = any(k in keys for k in _PROMPT_TOKEN_FIELDS)
    has_completion = any(k in keys for k in _COMPLETION_TOKEN_FIELDS)
    has_ts = any(k in keys for k in _TS_FIELDS)
    has_session = any(k in keys for k in _SESSION_FIELDS)
    has_openai_cache = 'prompt_tokens_details.cached_tokens' in keys
    flat_cache = [k for k in keys if k in _AMBIGUOUS_CACHE]
    structure_hint = any(k.split('.')[0] in
                         ('messages', 'conversation', 'session', 'history',
                          'turns') for k in keys)
    if has_prompt and has_completion and has_model:
        if flat_cache and not has_openai_cache:
            return 'openai_usage_ambiguous', \
                ('存在顶层缓存字段 %s，但无法确认其是否已包含在'
                 ' prompt_tokens 语义内 —— 不猜测，暂不解析' % flat_cache[0])
        if not has_ts:
            return 'openai_usage_ambiguous', \
                ('缺少足够稳定的事件身份（无可解析的时间字段）——'
                 '暂不自动记账')
        if has_openai_cache:
            mapping = {'input': 'prompt-minus-cache', 'cache': 'openai_details'}
        else:
            mapping = {'input': 'prompt', 'cache': None}
        return 'openai_usage', mapping
    if has_session and structure_hint:
        return 'activity', None
    return None, '字段结构不符合已知通用 schema（model/tokens/timestamp 不齐）'


def _generic_sqlite_match(fp):
    if fp.get('kind') != 'sqlite':
        return None, None
    for t, cols in (fp.get('tables') or {}).items():
        cs = set(c.lower() for c in cols)
        if not ({'model', 'prompt_tokens', 'completion_tokens'} <= cs):
            continue
        amb = [c for c in _AMBIGUOUS_CACHE if c in cs]
        if amb:
            return None, ('列 %s 的缓存语义无法确认 —— 不猜测，暂不解析'
                          % amb[0])
        ts = next((c for c in _TS_FIELDS if c in cs), None)
        if not ts:
            # 有用量结构但缺时间列：事件身份缺少稳定区分事实 → 降级，
            # 绝不自动入账（正确性优先于覆盖率）。
            return None, ('缺少足够稳定的事件身份（无可解析的时间字段）——'
                          '暂不自动记账')
        cache_cols = [c for c in ('cache_read_input_tokens',
                                  'cache_creation_input_tokens') if c in cs]
        sess = next((c for c in _SESSION_FIELDS if c in cs), None)
        return 'openai_usage', {'table': t, 'ts': ts, 'session': sess,
                                'cache': cache_cols}
    return None, None


def _resolve_adapter(tool_id, fps, catalog, anchored_generic=None):
    """LAYER 4。fps: [fingerprint]。返回 (status, capability, adapter,
    reason)。dedicated → generic → activity → unsupported → unknown。

    anchored_generic：catalog 规则锚定的 generic adapter 名
    （generic-jsonl-openai-usage / generic-sqlite-openai-usage）。
    此时工具身份已由 catalog 锚定，schema 符合契约即可入账。
    """
    generic = catalog.get('generic') or {}
    for tool in catalog.get('tools') or []:
        if tool['id'] != tool_id:
            continue
        if tool.get('adapter') in _GENERIC_ADAPTERS:
            break                                 # anchored generic：无专用 parser
        fpr = tool.get('fingerprints') or {}
        for fp in fps:
            if fpr.get('kind') == 'opaque' and fp.get('kind') == 'opaque':
                return ST_UNKNOWN, 'UNKNOWN', None, fpr.get('reason', '加密或不可读')
            if fp.get('kind') == 'opaque':
                continue
            if fpr.get('kind') == 'sqlite' and fp.get('kind') == 'sqlite':
                want = fpr.get('tables') or {}
                got = fp.get('tables') or {}
                if all(t in got for t in want):
                    return ST_SUPPORTED, tool['capability'], tool['adapter'], None
            if fpr.get('kind') in ('jsonl', 'zstd-jsonl') and \
                    fp.get('kind') == 'jsonl':
                want = set(fpr.get('keys') or [])
                if not want or want <= set(fp.get('keys') or []):
                    return ST_SUPPORTED, tool['capability'], tool['adapter'], None
        # catalog 已知工具但本次指纹不匹配：仍按 path 事实给 SUPPORTED，
        # 由既有 adapter 在 scan 时如实报告读取结果。
        return ST_SUPPORTED, tool['capability'], tool['adapter'], None

    # ---- 未知工具：通用契约 ----
    if anchored_generic == 'generic-sqlite-openai-usage':
        sql_match = _generic_sqlite_match(
            next((fp for fp in fps if fp.get('kind') == 'sqlite'), {}))
        if sql_match[0] == 'openai_usage':
            return ST_GENERIC, 'TOKEN', anchored_generic, None
        return ST_DETECTED, 'UNKNOWN', None, \
            (sql_match[1] or '已按 catalog 锚定，但数据不符合通用用量契约')
    if anchored_generic == 'generic-jsonl-openai-usage':
        for fp in fps:
            if fp.get('kind') == 'opaque':
                continue
            kind, why = _generic_jsonl_match(fp)
            if kind == 'openai_usage':
                return ST_GENERIC, 'TOKEN', anchored_generic, None
        return ST_DETECTED, 'UNKNOWN', None, \
            '已按 catalog 锚定，但数据不符合通用用量契约'
    reasons = []
    sql_match = _generic_sqlite_match(
        next((fp for fp in fps if fp.get('kind') == 'sqlite'), {}))
    if sql_match[0] is None and sql_match[1]:
        return ST_DETECTED, 'UNKNOWN', None, sql_match[1]
    if sql_match[0] == 'openai_usage':
        return ST_GENERIC, 'TOKEN', 'generic-sqlite-openai-usage', None
    for fp in fps:
        if fp.get('kind') == 'opaque':
            reasons.append('数据库加密或头部不可解析')
            continue
        kind, why = _generic_jsonl_match(fp)
        if kind == 'openai_usage':
            return ST_GENERIC, 'TOKEN', 'generic-jsonl-openai-usage', None
        if kind == 'openai_usage_ambiguous':
            return ST_DETECTED, 'UNKNOWN', None, why
        if kind == 'activity':
            return ST_DETECTED, 'ACTIVITY', None, \
                '只能确认是 AI 会话/活动数据；暂无通用活动 Adapter，不自动入账'
        if why:
            reasons.append(why)
    return ST_UNKNOWN, 'UNKNOWN', None, \
        (reasons[0] if reasons else '无法安全判定数据结构')


# ---------------------------------------------------------------- 结果模型

def _tool_result(tool_id, display_name, status, capability, adapter, via,
                 location_kind, n_sources, last_seen, reason, is_new,
                 needs_attention, path_hint, completeness=None):
    return {
        'tool_id': tool_id,
        'display_name': display_name,
        'status': status,
        'capability': capability,
        'adapter': adapter,
        'detected_via': via,
        'location_kind': location_kind,
        'data_sources_count': n_sources,
        'last_seen': last_seen,
        'reason': reason,
        'is_new': bool(is_new),
        'needs_attention': bool(needs_attention),
        'path_hint': _redact(path_hint) if path_hint else None,
        'completeness': completeness,
    }


# ---------------------------------------------------------------- Codex 历史完整性

def _probe_codex_completeness(spec, max_files=64, max_read_bytes=8 << 20):
    """Codex 历史完整性取证（bounded、只读）。

    口径：rollout 文件含 token_usage_record（新格式）→ 可逐请求计量；
    不含（旧格式）→ 该文件的历史无法证明用量，**不计为 0 token**。
    COMPLETE = 全部 rollout 均可计量；PARTIAL = 存在旧格式历史缺口。
    """
    roots = spec.get('paths') or ([spec['path']] if spec.get('path') else [])
    files = []
    for r in roots:
        if not os.path.isdir(r):
            continue
        for dirpath, _dirs, filenames in os.walk(r):
            files.extend(os.path.join(dirpath, fn)
                         for fn in filenames if fn.startswith('rollout-')
                         and fn.endswith('.jsonl'))
    files = sorted(files)[:max_files]
    if not files:
        return None
    have = 0
    for p in files:
        try:
            with open(p, 'rb') as f:
                have += 1 if b'"token_usage_record"' in \
                    f.read(max_read_bytes) else 0
        except OSError:
            continue
    return 'COMPLETE' if have == len(files) else 'PARTIAL'


# ---------------------------------------------------------------- 状态/缓存

def _load_state(state_dir=None):
    try:
        with open(os.path.join(state_dir or BASE, 'discovery-state.json'),
                  encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st, state_dir=None):
    p = os.path.join(state_dir or BASE, 'discovery-state.json')
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def _root_content_signature(root, catalog):
    """单个候选根的**内容感知**签名：有界 walk（与 LAYER2 同深度/数量
    上限，只看文件元数据，绝不读内容）对数据文件算 (relpath, size,
    mtime) 摘要。候选根内部新增/删除数据文件（如今天多了 backup.db）
    会改变签名 → 缓存失效 → 重新完整发现；不做昂贵全盘内容 hash。"""
    limits = catalog.get('limits') or {}
    max_depth = int(limits.get('data_walk_depth', 3))
    max_files = int(limits.get('max_files_per_root', 400))
    interesting_ext = ('.db', '.sqlite', '.sqlite3', '.jsonl', '.ndjson')
    interesting_name = ('history', 'session', 'usage', 'conversation',
                        'memory')
    items = []
    base_depth = root.rstrip(os.sep).count(os.sep)
    try:
        for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False, onerror=lambda e: None):
            depth = dirpath.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if not _is_reparse(os.path.join(dirpath, d))]
            for fn in filenames:
                low = fn.lower()
                if not (low.endswith(interesting_ext)
                        or any(k in low for k in interesting_name)):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                    rel = os.path.relpath(p, root).replace('\\', '/')
                    items.append('%s|%d|%d' % (rel, st.st_size,
                                               int(st.st_mtime)))
                except OSError:
                    items.append(os.path.relpath(p, root) + '|gone')
                if len(items) >= max_files:
                    break
            if len(items) >= max_files:
                break
    except OSError:
        pass
    if not items:
        return 'empty'
    return hashlib.sha1('\n'.join(sorted(items)).encode('utf-8')).hexdigest()[:16]


def _roots_signature(roots, catalog=None):
    """标准目录签名：一级目录 mtime/ctime + 每个候选根的内容感知摘要。"""
    catalog = catalog or {}
    sig = {}
    for r in roots:
        try:
            st = os.stat(r['root'])
            entry = '%d|%d' % (int(st.st_mtime), int(st.st_ctime))
        except OSError:
            entry = 'gone'
        entry += ':' + _root_content_signature(r['root'], catalog)
        sig[r['root']] = entry
    return sig


def _new_tool_id(name, taken):
    slug = _slug(name)
    tid = slug
    n = 2
    while tid in taken:
        tid = '%s-%d' % (slug, n)
        n += 1
    taken.add(tid)
    return tid


# ---------------------------------------------------------------- 主入口

def run_discovery(full=False, catalog=None, home_override=None, now=None,
                  state_dir=None, registry_names=None):
    """完整 / 轻量 discovery。

    full=False 时先查缓存签名（catalog digest / app version / 标准目录
    签名），无变化 → 直接返回上次完整结果（cached=True）。
    已知 5 工具的候选探测与 resolved-sources 语义完全复用
    ledger.discover_sources()（KEEP，不重写，不破坏既有 Adapter）。
    state_dir：运行态文件（discovery-state.json / generic-sources.json）
    的存放目录；默认 DATA_ROOT。测试用沙盒目录注入。
    """
    t0 = time.monotonic()
    catalog = catalog or load_catalog()
    # digest 按传入 catalog 的内容计算：测试/在线更新替换 catalog 对象
    # 时同样触发缓存失效。
    catalog_dg = hashlib.sha256(json.dumps(
        catalog, sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()[:16]
    version = _app_version()
    state = _load_state(state_dir)

    roots = layer1_candidate_roots(catalog, home_override=home_override,
                                   registry_names=registry_names)
    sig = _roots_signature(roots, catalog)
    if not full and state.get('report') and \
            state.get('catalog_digest') == catalog_dg and \
            state.get('app_version') == version and \
            state.get('roots_signature') == sig:
        rep = dict(state['report'])
        rep['cached'] = True
        rep['mode'] = 'lightweight'
        rep['last_discovery_at'] = state.get('last_discovery_at')
        return rep

    now_s = (now or time.strftime('%Y-%m-%dT%H:%M:%S'))
    limits = catalog.get('limits') or {}
    deadline = time.monotonic() + float(limits.get('probe_timeout_seconds', 20))
    known_by_id = {}
    for r in (state.get('report') or {}).get('tools') or []:
        known_by_id[r['tool_id']] = r

    # ---- 已知工具：复用 ledger.discover_sources（既有 5 Adapter 的事实源）----
    known_results = []
    try:
        import ledger
        lrep = ledger.discover_sources(verbose=False)
        for client, row in (lrep.get('sources') or {}).items():
            state_row = row.get('state')
            spec = ledger.SOURCES.get(client) or {}
            is_generic = bool(spec.get('generic'))
            paused = bool(spec.get('paused'))
            completeness = None
            if client == 'codex' and state_row == 'found':
                completeness = _probe_codex_completeness(spec)
            known_results.append(_tool_result(
                tool_id=client,
                display_name=row.get('label') or client,
                status=ST_GENERIC if is_generic else ST_SUPPORTED,
                capability='TOKEN' if row.get('visibility') == 'token'
                else ('ACTIVITY' if row.get('visibility') == 'activity'
                      else 'UNKNOWN'),
                adapter=('generic-jsonl-openai-usage'
                         if spec.get('kind') == 'generic-jsonl' else
                         ('generic-sqlite-openai-usage'
                          if spec.get('kind') == 'generic-sqlite' else client)),
                via='generic_registry' if is_generic else 'catalog_path',
                location_kind='hint',
                n_sources=1 if state_row == 'found' else 0,
                last_seen=now_s if state_row == 'found'
                else (known_by_id.get(client) or {}).get('last_seen'),
                reason=('事件身份策略发生变化（provider-id ↔ content），'
                        '已暂停自动入账以防双记') if paused
                else (None if state_row == 'found'
                      else '标准候选路径中未找到数据（可在高级设置手动指定）'),
                is_new=client not in known_by_id,
                needs_attention=paused,
                path_hint=row.get('path_hint'),
                completeness=completeness))
        opaque_rows = lrep.get('opaque') or {}
    except Exception as e:                       # 账本模块不可用不阻塞发现
        known_results, opaque_rows = [], {}
        known_results.append(_tool_result(
            'ledger', '内置来源', ST_UNKNOWN, 'UNKNOWN', None, 'catalog_path',
            'hint', 0, None, '内置来源探测失败：%s' % type(e).__name__,
            True, True, None))

    # ---- 不透明存储（UNKNOWN 诚实登记）----
    known_results.extend(_tool_result(
        tool_id=key,
        display_name=row.get('label') or key,
        status=ST_UNKNOWN,
        capability='UNKNOWN',
        adapter=None,
        via='catalog_path',
        location_kind='hint',
        n_sources=1 if row.get('state') == 'found' else 0,
        last_seen=now_s if row.get('state') == 'found' else None,
        reason=row.get('reason'),
        is_new=key not in known_by_id and row.get('state') == 'found',
        needs_attention=False,
        path_hint=row.get('path_hint'))
        for key, row in opaque_rows.items() if row.get('state') == 'found')

    # ---- LAYER 2/3/4：候选根里的未知 AI 数据源 ----
    catalog_tool_hints = {t['id'] for t in catalog.get('tools') or []}
    # 已注册的通用来源：registered path → tool_id（多 physical location）。
    generic_roots = {}
    try:
        for _c, _s in ledger.SOURCES.items():
            if _s.get('generic'):
                for _p in (_s.get('paths') or
                           ([_s['path']] if _s.get('path') else [])):
                    generic_roots[os.path.normpath(_p).lower()] = _c
    except Exception:
        pass
    # generic findings 对账表：tid → 本轮发现的 physical location 集合
    generic_findings = {}
    registered_generic_tids = set(generic_roots.values())
    seen_root = set()
    new_results = []
    sqlite_opened = 0
    jsonl_sampled = 0
    max_sql = int(limits.get('max_sqlite_open', 24))
    taken_ids = {r['tool_id'] for r in known_results}
    slug_map = {}                                 # desired slug → tool_id
    strategies = {}                               # anchored tid → pid|content
    # catalog 锚定的 generic 来源：adapter 是 generic-* 的 catalog 规则。
    # 这是 V1.1 唯一允许「仅凭 schema 契约」自动入账的 generic 形态 ——
    # 工具身份由 catalog 稳定锚定，与目录名无关。
    generic_anchor_tools = {
        t['id']: t for t in (catalog.get('tools') or [])
        if t.get('adapter') in _GENERIC_ADAPTERS}
    for cand in roots:
        if time.monotonic() > deadline:
            break
        tool_hint = cand.get('tool_id')
        if tool_hint and tool_hint in catalog_tool_hints and \
                tool_hint not in generic_anchor_tools:
            continue                              # 已知专用工具走 ledger 事实源
        root = cand['root']
        root_low = os.path.normpath(root).lower()
        if not os.path.isdir(root) or root_low in seen_root:
            continue
        seen_root.add(root_low)
        if _is_reparse(root):
            continue                              # 候选根本身是 junction/symlink：绝不进入
        if root_low in generic_roots:
            # 已注册通用来源的本体位置：确认仍在，不重复发现；
            # 同时重新采样事件身份策略（策略漂移监测）。
            tid_reg = generic_roots[root_low]
            if tid_reg not in strategies:
                fps_reg = [fingerprint_file(fp, catalog)
                           for fp in layer2_find_data_files(
                               root, catalog, deadline)[:4]]
                strategies[tid_reg] = _sample_strategy(fps_reg)
            generic_findings.setdefault(tid_reg, set()).add(root)
            continue
        # 与注册位置存在嵌套关系（父/子任一方向）：跳过 —— 注册根本身
        # 递归发现已覆盖其子树，父目录则避免整棵重复发现。
        if any(p.startswith(root_low + os.sep)
               or root_low.startswith(p + os.sep)
               for p in generic_roots):
            continue
        files = layer2_find_data_files(root, catalog, deadline)
        if not files:
            continue
        fps = []
        for fp in files[:8]:                      # 每个根最多探 8 个文件
            f = fingerprint_file(fp, catalog)
            if f.get('kind') == 'sqlite':
                if sqlite_opened >= max_sql:
                    continue
                sqlite_opened += 1
            elif f.get('kind') == 'jsonl':
                jsonl_sampled += 1
            fps.append(f)
        if not fps:
            continue
        status, capability, adapter, reason = _resolve_adapter(
            _slug(tool_hint or os.path.basename(root)), fps, catalog,
            anchored_generic=(generic_anchor_tools[tool_hint].get('adapter')
                              if tool_hint in generic_anchor_tools else None))
        # 噪音目录：结构完全无法判定且无加密/损坏信号 → 不注册成工具。
        # 加密 / 损坏本身是信号（可能是 AI 工具的私有存储）→ 如实登记 UNKNOWN。
        has_signal = any(fp.get('kind') in ('opaque', 'unparseable')
                         for fp in fps)
        if status == ST_UNKNOWN and not tool_hint and not has_signal:
            continue
        name = os.path.basename(root)
        anchored = tool_hint in generic_anchor_tools
        if status == ST_GENERIC and not anchored:
            # 产品边界：schema 可读但没有稳定 tool identity anchor
            # （只靠目录 basename）→ 发现但绝不自动入账。
            status = ST_UNANCHORED
            capability = 'TOKEN'
        # 锚定 generic 的稳定身份 = catalog id（与目录名无关，
        # live/backup/archive 命中同一 install hint 即归入同一工具）。
        desired = tool_hint if anchored else _slug(tool_hint or name)
        # 同一工具（同名候选）在多个位置：合并到同一结果，只加来源计数
        prev = next((x for x in new_results
                     if slug_map.get(desired) == x['tool_id']), None)
        if prev:
            prev['data_sources_count'] += 1
            if status == ST_GENERIC:
                generic_findings.setdefault(prev['tool_id'], set()).add(root)
            continue
        if status == ST_GENERIC and desired in registered_generic_tids:
            # 已注册通用工具的新 physical location：收编进同一 tool_id
            generic_findings.setdefault(desired, set()).add(root)
            for x in known_results:
                if x['tool_id'] == desired:
                    x['data_sources_count'] += 1
                    break
            continue
        tid = _new_tool_id(desired, taken_ids)
        slug_map[desired] = tid
        result = _tool_result(
            tool_id=tid,
            display_name=(generic_anchor_tools[tool_hint].get('display_name')
                          or name) if anchored else name,
            status=status,
            capability=capability,
            adapter=adapter,
            via=cand['via'],
            location_kind=cand['location_kind'],
            n_sources=1,
            last_seen=now_s,
            reason=reason,
            is_new=False,                         # 下面按「曾经发现过」重算
            needs_attention=status in (ST_DETECTED, ST_UNKNOWN, ST_UNANCHORED),
            path_hint=root)
        new_results.append(result)
        if status == ST_GENERIC:
            generic_findings.setdefault(tid, set()).add(root)
            # 事件身份策略（pid | content）：从 fingerprint 采样判定并
            # 钉入注册；与既有注册不同 → needs_attention + 暂停入账。
            strategies[tid] = _sample_strategy(fps)

    # ---- GENERIC_SUPPORTED（anchored）：把本轮发现的全部 physical
    # location 注册为可采集来源。LOGICAL EVENT != PHYSICAL SOURCE：
    # 多位置全部登记，采集靠稳定 event_id 去重。unanchored 不注册。
    strategy_changed = _reconcile_generic_sources(
        generic_findings, new_results, strategies=strategies,
        base_dir=state_dir)
    if strategy_changed:
        for x in known_results:
            if x['tool_id'] in strategy_changed:
                x['needs_attention'] = True
                x['reason'] = ('事件身份策略发生变化（provider-id ↔ '
                               'content），已暂停自动入账以防双记')

    tools = known_results + new_results
    had_prev = bool(known_by_id)
    for t in tools:
        t['is_new'] = bool(had_prev and t['tool_id'] not in known_by_id)
    groups = {
        'supported': [t['tool_id'] for t in tools
                      if t['status'] == ST_SUPPORTED],
        'generic_supported': [t['tool_id'] for t in tools
                              if t['status'] == ST_GENERIC],
        'unanchored': [t['tool_id'] for t in tools
                       if t['status'] == ST_UNANCHORED],
        'detected_unsupported': [t['tool_id'] for t in tools
                                 if t['status'] == ST_DETECTED],
        'unknown': [t['tool_id'] for t in tools if t['status'] == ST_UNKNOWN],
    }
    rep = {
        'generated_at': now_s,
        'schema_version': 2,
        'catalog_version': catalog.get('catalog_version'),
        'app_version': version,
        'mode': 'full',
        'cached': False,
        'last_discovery_at': now_s,
        'found': len([t for t in tools
                      if t['status'] in (ST_SUPPORTED, ST_GENERIC)
                      and t['data_sources_count'] > 0]),
        'total': len(tools),
        'tools': tools,
        'groups': groups,
        'stats': {
            'candidate_roots': len(roots),
            'sqlite_opened': sqlite_opened,
            'jsonl_sampled': jsonl_sampled,
            'duration_ms': int((time.monotonic() - t0) * 1000),
        },
    }
    _save_state({'last_discovery_at': now_s, 'catalog_digest': catalog_dg,
                 'app_version': version, 'roots_signature': sig,
                 'report': rep}, state_dir)
    return rep


def _sample_strategy(fps):
    """从 fingerprint 采样判定事件身份策略（pid | content）。"""
    if any(
        k in (set(fp.get('keys') or []) |
              {c for cols in (fp.get('tables') or {}).values()
               for c in cols})
        for fp in fps if fp.get('kind') in ('jsonl', 'sqlite')
        for k in _GENERIC_PID_FIELDS):
        return 'pid'
    return 'content'


def _reconcile_generic_sources(generic_findings, new_results,
                               strategies=None, base_dir=None):
    """把本轮发现的 physical location 与注册状态对账（仅 anchored）。

    - 有 findings 的 anchored generic 工具：paths 替换为本轮位置集合
      （新增位置立即收编；消失的位置不再采集）。
    - 本轮无 findings 的已注册工具（目录被清退/契约降级）：保持原注册
      不动，由 scan 如实报告 missing / 解析失败（账本记录保留）。
    - 事件身份策略变化（pid ↔ content）：needs_attention + 暂停自动
      入账（paused），绝不双记。返回发生策略变化的 tool_id 集合。
    """
    changed = set()
    if not generic_findings:
        return changed
    try:
        import ledger
    except Exception:
        return changed
    by_id = {t['tool_id']: t for t in new_results}
    for tid, roots_set in generic_findings.items():
        tool = by_id.get(tid) or {}
        adapter = tool.get('adapter') or ''
        if adapter == 'generic-sqlite-openai-usage':
            kind = 'generic-sqlite'
        elif adapter == 'generic-jsonl-openai-usage':
            kind = 'generic-jsonl'
        else:
            # 已注册工具（不在本轮 new_results 里）：沿用既有注册的 kind
            spec = (ledger.SOURCES.get(tid) or {})
            kind = spec.get('kind') if spec.get('generic') else 'generic-jsonl'
        label = tool.get('display_name') or \
            (ledger.SOURCES.get(tid) or {}).get('label') or tid
        # 策略变化检测：既有注册的 identity_strategy 与本轮采样不同
        existing = ledger.SOURCES.get(tid) or {}
        old_strategy = existing.get('identity_strategy') if \
            existing.get('generic') else None
        new_strategy = (strategies or {}).get(tid)
        if old_strategy and new_strategy and old_strategy != new_strategy:
            changed.add(tid)
            try:
                ledger.set_generic_source_paused(tid, base=base_dir,
                                                 paused=True)
            except Exception:
                pass
            continue
        try:
            ledger.register_generic_source(
                tid, label, sorted(roots_set),
                kind=kind, base=base_dir, replace=True,
                identity_strategy=new_strategy)
        except Exception:
            continue
    return changed


# ---------------------------------------------------------------- 公开载荷

_STATUS_TEXT = {
    ST_SUPPORTED: '已支持，直接记账',
    ST_GENERIC: '符合已知通用格式，自动记账',
    ST_UNANCHORED: '可读取数据 · 工具身份待确认',
    ST_DETECTED: '新发现 · 暂未支持完整用量',
    ST_UNKNOWN: '检测到 AI 数据 · 待识别',
}
_CAP_TEXT = {'TOKEN': '完整用量', 'ACTIVITY': '只能确认使用过',
             'UNKNOWN': '待识别'}
_COMPLETENESS_TEXT = {'COMPLETE': '历史完整', 'PARTIAL': '存在历史缺口'}


def public_payload(rep):
    """API / UI 载荷：无绝对路径、无 schema fingerprint；同时保留 v1
    UI 已消费的 legacy 键（sources / grades / opaque / found），并提供
    V1.1 的新结果模型（tools / groups）。"""
    tools = []
    for t in rep.get('tools') or []:
        tools.append({
            'tool_id': t['tool_id'],
            'display_name': t['display_name'],
            'status': t['status'],
            'status_text': _STATUS_TEXT.get(t['status'], t['status']),
            'capability': t['capability'],
            'capability_text': _CAP_TEXT.get(t['capability'], t['capability']),
            'completeness': t.get('completeness'),
            'completeness_text': _COMPLETENESS_TEXT.get(
                t.get('completeness')),
            'adapter': t['adapter'],
            'detected_via': t['detected_via'],
            'location_kind': t['location_kind'],
            'data_sources_count': t['data_sources_count'],
            'last_seen': t['last_seen'],
            'reason': t['reason'],
            'is_new': t['is_new'],
            'needs_attention': t['needs_attention'],
            'path_hint': t.get('path_hint'),
        })
    payload = {
        'generated_at': rep.get('generated_at'),
        'last_discovery_at': rep.get('last_discovery_at'),
        'catalog_version': rep.get('catalog_version'),
        'app_version': rep.get('app_version'),
        'mode': rep.get('mode'),
        'cached': bool(rep.get('cached')),
        'found': rep.get('found', 0),
        'total': rep.get('total', 0),
        'tools': tools,
        'groups': rep.get('groups') or {},
        'stats': rep.get('stats') or {},
        'privacy': {'local_only': True, 'paths_included': False,
                    'content_sampled': False},
    }
    # ---- legacy 兼容键（v1 UI confidence/first-run 已消费的形状）----
    legacy_sources = []
    for t in tools:
        if t['status'] == ST_SUPPORTED and t['adapter'] and \
                not str(t['adapter']).startswith('generic'):
            grade = 'TOKEN' if t['capability'] == 'TOKEN' else \
                ('ACTIVITY' if t['capability'] == 'ACTIVITY' else 'UNKNOWN')
            legacy_sources.append({
                'client': t['tool_id'], 'label': t['display_name'],
                'state': 'found' if t['data_sources_count'] else 'not_found',
                'data_grade': grade,
                'grade_basis': 'verified' if t['data_sources_count'] else None,
                'grade_provisional': False,
                'grade_records': None, 'files': t['data_sources_count'],
                'capability_text': t['capability_text']})
    counts = {'TOKEN': 0, 'ACTIVITY': 0, 'UNKNOWN': 0}
    for t in tools:
        if t['data_sources_count']:
            counts[t['capability']] = counts.get(t['capability'], 0) + 1
    payload['sources'] = legacy_sources
    payload['grades'] = counts
    payload['opaque'] = [
        {'key': t['tool_id'], 'label': t['display_name'], 'state': 'found',
         'reason': t['reason']}
        for t in tools if t['status'] == ST_UNKNOWN and t['adapter'] is None
        and t['detected_via'] == 'catalog_path']
    return payload
