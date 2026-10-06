#!/usr/bin/env python3
"""智账 · PathOrbit AI Ledger（内部项目名 usage-ledger）

为什么要自己做一个：
    现成的用量统计工具（tokscale / tokens 等）每次运行都是「重新扫描磁盘上的会话文件」，
    扫到什么就报什么。一旦客户端自己清理了历史会话，那部分用量就从报表里永久消失了。
    它们的官方建议是「去把关掉客户端的历史清理」——那是让你迁就工具，不是工具替你保管。

本工具反过来：扫描一次就把每条用量记录**落进自己的 SQLite 账本**，之后再扫是幂等 upsert。
    客户端清退历史文件之后，账本里的记录依然在。源文件消失了，数据还在。

用法：
    python ledger.py scan                 # 扫描所有数据源（增量，已扫过且未变的文件会跳过）
    python ledger.py scan --full          # 强制全量重扫
    python ledger.py report               # 按天汇总
    python ledger.py report --by model    # 按模型汇总
    python ledger.py report --by client
    python ledger.py status               # 数据源健康：哪些文件在、哪些已被清退（数据是否留存）
    python ledger.py pricing-template     # 按账本里出现过的模型生成 pricing.json 模板
    python ledger.py export --out usage.csv

依赖：只用标准库 + zstandard（仅解析 dsh 会话需要）。
"""

import argparse
import csv
import datetime as dt
import glob
import hashlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.request

if getattr(sys, 'frozen', False):
    _local = os.environ.get('LOCALAPPDATA') or os.path.join(
        os.path.expanduser('~'), 'AppData', 'Local')
    BASE = os.environ.get('USAGE_LEDGER_HOME') or os.path.join(_local, 'UsageLedger')
    os.makedirs(BASE, exist_ok=True)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, 'usage.db')
PRICING_PATH = os.path.join(BASE, 'pricing.json')          # 手工覆盖，优先级最高
PRICING_AUTO_PATH = os.path.join(BASE, 'pricing.auto.json')  # 自动同步产物
DISCOVERY_PATH = os.path.join(BASE, 'discovery.json')
BOARD_PATH = os.path.join(BASE, 'usage-board.json')
REPLAY_STATE_PATH = os.path.join(BASE, 'replay-state.json')
HOME = os.path.expanduser('~')

# 最近一次 cmd_scan 的统计，供编排层（autopilot）写进 run-state.json。
LAST_SCAN_STATS = {}

# CLI 退出码约定（与 autopilot 保持一致）
EXIT_OK = 0
EXIT_FAILED = 1        # 致命失败：账本不可写 / 核心步骤整体不可用
EXIT_PARTIAL = 2       # 部分失败：跑完了但有的环节失败（含价格同步失败、board 未写出）
EXIT_NO_SOURCES = 3    # 未发现任何受支持数据源（且账本为空）

# 数据等级 —— 自动发现的结论，不是配置项。
#   TOKEN    : 该客户端本地写了真实 token 用量，能进 token/成本统计
#   ACTIVITY : 只有活动痕迹（消息/记忆条数），无 token/cost 字段
#   UNKNOWN  : 找到了本地存储，但读不出用量（加密/私有格式），或暂未解析出任何记录
GRADE_TOKEN = 'TOKEN'
GRADE_ACTIVITY = 'ACTIVITY'
GRADE_UNKNOWN = 'UNKNOWN'
GRADES = (GRADE_TOKEN, GRADE_ACTIVITY, GRADE_UNKNOWN)

# ---------------------------------------------------------------- 数据源定义

DSH_HOME = os.environ.get('DSH_HOME') or os.path.join(HOME, '.dsh')
ROAMING = os.environ.get('APPDATA') or os.path.join(HOME, 'AppData', 'Roaming')
LOCALAPPDATA = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')

# 每个客户端自带「候选路径清单」——自动发现只查这些明确候选，不做全盘模糊匹配。
# visibility 对应数据可见性模型：token / activity / unknown
SOURCES = {
    # ---- 有 token 用量，进 usage_event ----
    'zcode': {
        'label': 'ZCode',
        'mode': 'usage',
        'visibility': 'token',
        'kind': 'sqlite',
        'path': os.path.join(HOME, '.zcode', 'cli', 'db', 'db.sqlite'),
        'candidates': [
            os.path.join(HOME, '.zcode', 'cli', 'db', 'db.sqlite'),
        ],
        'probe': 'file',
        'hint': '',
    },
    'dsh': {
        'label': 'dsh (DeepSeek Harness)',
        'mode': 'usage',
        'visibility': 'token',
        'kind': 'zstd-jsonl',
        'path': os.path.join(DSH_HOME, 'sessions'),
        'candidates': None,   # 下面按 DSH_HOME / sources.json 的 _search_roots 动态生成
        'probe': 'dir',
        'hint': ('设 DSH_HOME 环境变量指向 dsh 的 home 目录（桌面打包版通常不在 ~/.dsh），'
                 '或在 sources.json 里覆盖这个路径 / 用 _search_roots 给几个候选根目录'),
    },
    'workbuddy': {
        'label': 'WorkBuddy',
        'mode': 'usage',
        'grade': GRADE_TOKEN,
        'visibility': 'token',
        'kind': 'jsonl',
        'path': os.path.join(HOME, '.workbuddy', 'projects'),
        'candidates': [
            os.path.join(HOME, '.workbuddy', 'projects'),
            os.path.join(HOME, '.workbuddy-ai', 'projects'),
        ],
        'probe': 'dir',
        'hint': '',
    },
    # ---- 本地不记 token，只进 activity_event（活动量）----
    'catpaw': {
        'label': 'CatPaw',
        'mode': 'activity',
        'grade': GRADE_ACTIVITY,
        'visibility': 'activity',
        'kind': 'jsonl',
        'path': os.path.join(HOME, '.catpaw', 'projects'),
        'alt': os.path.join(ROAMING, 'catpaw-moon'),   # 转录被清退时的兜底（记忆库）
        'candidates': [
            os.path.join(HOME, '.catpaw', 'projects'),
            os.path.join(ROAMING, 'catpaw-moon'),
            os.path.join(HOME, '.meituan-catpaw'),
        ],
        'probe': 'dir',
        'hint': '本地只写会话转录与记忆库，没有 token/cost 字段，因此只统计活动量',
    },
    'traecn': {
        'label': 'Trae CN',
        'mode': 'activity',
        'grade': GRADE_ACTIVITY,
        'visibility': 'activity',
        'kind': 'jsonl',
        'path': os.path.join(HOME, '.trae-cn', 'memory', 'projects'),
        'candidates': [
            os.path.join(HOME, '.trae-cn', 'memory', 'projects'),
        ],
        'probe': 'dir',
        'hint': ('Trae CN 的 ai-agent 数据库是加密格式（sqlite 打不开），'
                 '只有记忆摘要可读，因此只统计活动量'),
    },
    # ---- Codex（OpenAI 桌面/CLI）：rollout 会话日志含逐请求 token 记录 ----
    # 语义取证结论（2026-10-03，本机实测；见 parse_codex_file 注释）：
    # token_usage_record.payload.usage 是**逐请求**新鲜用量（turn 累计
    # = Σ usage 已数值验证）；input_tokens 含 cached_input_tokens
    # （OpenAI 口径，同 workbuddy）。旧格式文件无该记录 → 不入账。
    'codex': {
        'label': 'Codex',
        'mode': 'usage',
        'visibility': 'token',
        'kind': 'jsonl',
        'path': os.path.join(HOME, '.codex', 'sessions'),
        'candidates': [
            os.path.join(HOME, '.codex', 'sessions'),
            os.path.join(HOME, '.codex', 'archived_sessions'),
        ],
        'probe': 'dir',
        'hint': '',
    },
}

# 「不透明存储」——我们确认它存在、但读不出用量。它们不参与采集，只在自动发现里
# 被登记为 UNKNOWN，这样用户能看到「这个客户端我认出来了，但它的数据我拿不到」，
# 而不是被静默忽略掉。
OPAQUE_STORES = {
    'traecn_agentdb': {
        'label': 'Trae CN · ai-agent 数据库',
        'path': os.path.join(ROAMING, 'Trae CN', 'ModularData', 'ai-agent', 'database.db'),
        'candidates': [
            os.path.join(ROAMING, 'Trae CN', 'ModularData', 'ai-agent', 'database.db'),
            os.path.join(ROAMING, 'TRAE SOLO CN', 'ModularData', 'ai-agent', 'database.db'),
        ],
        'probe': 'file',
        'reason': '加密存储：SQLite 头部不可解析（file is not a database），本地无法读取用量',
    },
}

# 可选：在同目录放一个 sources.json 覆盖数据源路径 / 提供额外的候选根目录。
#   {
#     "dsh": "D:/codeTRAE Buddy/dsh-desktop-pack/data/dsh-home",
#     "_search_roots": ["D:/codeTRAE Buddy", "D:/apps"]
#   }
SEARCH_ROOTS = []
SOURCES_OVERRIDE = os.path.join(BASE, 'sources.json')
if os.path.isfile(SOURCES_OVERRIDE):
    try:
        with open(SOURCES_OVERRIDE, encoding='utf-8') as _f:
            _ov = json.load(_f)
        for _root in (_ov.get('_search_roots') or []):
            if isinstance(_root, str) and _root.strip():
                SEARCH_ROOTS.append(os.path.normpath(os.path.expanduser(_root.strip())))
        for _k, _v in _ov.items():
            if _k.startswith('_') or _k not in SOURCES:
                continue
            if not isinstance(_v, str) or not _v.strip():
                continue
            _v = os.path.normpath(os.path.expanduser(_v.strip()))
            # dsh 的路径既可以给 home 目录，也可以直接给 sessions 目录
            if _k == 'dsh' and not os.path.basename(_v) == 'sessions':
                _v = os.path.join(_v, 'sessions')
            SOURCES[_k]['path'] = _v
    except Exception as _e:
        print('  ! sources.json 读取失败：%s' % _e, file=sys.stderr)

# dsh 候选：DSH_HOME + 默认 home + 用户给的搜索根下的常见打包位置（仍然是有界清单）
_dsh_cands = [os.path.join(DSH_HOME, 'sessions'),
              os.path.join(HOME, '.dsh', 'sessions')]
for _r in SEARCH_ROOTS:
    _dsh_cands.append(os.path.join(_r, 'data', 'dsh-home', 'sessions'))
    _dsh_cands.append(os.path.join(_r, 'dsh-home', 'sessions'))
SOURCES['dsh']['candidates'] = _dsh_cands

# 自动发现的结果落在这里（本机运行态文件，含真实路径，因此不进 usage-board.json）。
# 这样 `discover` 之后单跑 `scan` 也能用上发现到的路径，不必重复发现。
RESOLVED_PATH = os.path.join(BASE, 'resolved-sources.json')
_explicit_paths = set()
if os.path.isfile(SOURCES_OVERRIDE):
    try:
        with open(SOURCES_OVERRIDE, encoding='utf-8') as _f:
            _explicit_paths = {k for k, v in json.load(_f).items()
                               if not k.startswith('_') and isinstance(v, str) and v.strip()}
    except Exception:
        pass
if os.path.isfile(RESOLVED_PATH):
    try:
        with open(RESOLVED_PATH, encoding='utf-8') as _f:
            for _k, _v in (json.load(_f) or {}).items():
                if _k in SOURCES and _k not in _explicit_paths and isinstance(_v, str) and _v.strip():
                    SOURCES[_k]['path'] = os.path.normpath(_v)
    except Exception as _e:
        print('  ! resolved-sources.json 读取失败：%s' % _e, file=sys.stderr)


def redact_path(p):
    """脱敏路径：只保留末两段 + 短哈希，用于 discovery.json / usage-board.json。

    绝不输出盘符、用户名或完整路径——隐私约定要求这些文件可以直接公开。
    """
    if not p:
        return ''
    h = hashlib.sha256(str(p).encode('utf-8', 'replace')).hexdigest()[:8]
    parts = [x for x in os.path.normpath(str(p)).replace('\\', '/').split('/') if x]
    who = os.path.basename(HOME).lower()
    tail = [x for x in parts[-3:] if x.lower() != who and not x.endswith(':')]
    return '…/%s#%s' % ('/'.join(tail[-2:]) or '?', h)


# ---------------------------------------------------------------- 账本结构

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_event (
    client         TEXT    NOT NULL,
    session_id     TEXT    NOT NULL,
    event_id       TEXT    NOT NULL,
    ts_ms          INTEGER NOT NULL,
    model          TEXT    NOT NULL DEFAULT '',
    provider       TEXT    NOT NULL DEFAULT '',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    source_file    TEXT    NOT NULL DEFAULT '',
    first_seen     TEXT    NOT NULL,
    last_seen      TEXT    NOT NULL,
    -- v1 重放血统标记（Round 4）。raw 永远保留；标记只增不改原字段。
    --   is_replay            1 = 已验证的血统重放副本；0 = 原始/未决/保守保留
    --   replay_reason        parent_seed_exact | ancestor_seed_exact | NULL
    --   replay_source_session 被追溯到的父/祖先 session id（不含路径），未验证为 NULL
    is_replay INTEGER NOT NULL DEFAULT 0,
    replay_reason TEXT,
    replay_source_session TEXT,
    -- v2（Phase 0）：项目归因。稳定哈希键，绝不保存完整本地路径；NULL = 暂无法归属。
    project_key TEXT,
    PRIMARY KEY (client, session_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_usage_ts    ON usage_event(ts_ms);
CREATE INDEX IF NOT EXISTS idx_usage_model ON usage_event(model);
CREATE INDEX IF NOT EXISTS idx_usage_client ON usage_event(client);
-- idx_usage_project 由 migrate_schema 创建（v1 → v2 加列后才能建）。

-- v2（Phase 0）：项目注册表。project_key = 归一化路径 / 原生 id 的稳定哈希，
-- 绝不保存完整本地路径；display_name 只含安全段名。first_seen 只降不升。
CREATE TABLE IF NOT EXISTS project_registry (
    project_key  TEXT PRIMARY KEY,
    project_kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    first_seen_at TEXT,
    last_seen_at  TEXT,
    metadata_json TEXT
);

-- v2（Phase 0）：会话注册表。source = 采集通道（client）；session_id 与 usage_event 同键。
-- 归因写入本表后即永久保留，不再依赖原始客户端库存活。
CREATE TABLE IF NOT EXISTS session_registry (
    source        TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    project_key   TEXT,
    display_name  TEXT,
    first_seen_at TEXT,
    last_seen_at  TEXT,
    agent         TEXT,
    PRIMARY KEY(source, session_id)
);
CREATE INDEX IF NOT EXISTS idx_session_project ON session_registry(project_key);

CREATE TABLE IF NOT EXISTS source_file (
    client       TEXT NOT NULL,
    path         TEXT NOT NULL,
    size         INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    events       INTEGER NOT NULL DEFAULT 0,
    first_seen   TEXT    NOT NULL,
    last_seen    TEXT    NOT NULL,
    missing_since TEXT,
    -- 上次读取失败的原因。非空意味着这个文件"上次没读成功"，
    -- 增量跳过必须忽略它 —— 否则读到一半失败的文件会被当成"未变化"永久跳过，
    -- 数据一直缺、报表一直说成功。
    last_error    TEXT,
    PRIMARY KEY (client, path)
);

CREATE TABLE IF NOT EXISTS scan_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    events_seen      INTEGER NOT NULL DEFAULT 0,
    events_inserted  INTEGER NOT NULL DEFAULT 0,
    events_updated   INTEGER NOT NULL DEFAULT 0,
    files_scanned    INTEGER NOT NULL DEFAULT 0,
    files_skipped    INTEGER NOT NULL DEFAULT 0
);

-- 活动量：给那些本地不记 token 的客户端（CatPaw / Trae CN）用。
-- 只回答「哪天用了、用了几轮、开了几个会话」，不参与任何 token 与成本计算。
CREATE TABLE IF NOT EXISTS activity_event (
    client      TEXT    NOT NULL,
    session_key TEXT    NOT NULL,
    event_id    TEXT    NOT NULL,
    ts_ms       INTEGER NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'message',
    source_file TEXT    NOT NULL DEFAULT '',
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    PRIMARY KEY (client, session_key, event_id)
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_event(ts_ms);
"""


def now_iso():
    return dt.datetime.now().isoformat(timespec='seconds')


def norm_path(p):
    """归一化路径，作为 source_file 的主键。

    Windows 上 glob 会按根路径的分隔符风格返回路径（根用正斜杠时混出
    `D:/a/sessions\\b\\c` 这种），不归一化的话同一个文件会被当成两个，
    增量跳过会整体失效、每次都全量重解压。
    """
    return os.path.normcase(os.path.abspath(os.path.normpath(str(p))))


# ---------------------------------------------------------------- 项目归因键（Phase 0）

# project_kind 常量：project=代码/仓库目录；workspace=客户端默认工作区；
# work_item=WorkBuddy 工作项；unknown=只有不可逆的本地标识。
PROJECT_KIND_PROJECT = 'project'
PROJECT_KIND_WORKSPACE = 'workspace'
PROJECT_KIND_WORK_ITEM = 'work_item'
PROJECT_KIND_UNKNOWN = 'unknown'


def normalize_project_path(p):
    """项目路径归一化：跨客户端合并同目录、拒绝把完整路径当键。

    处理：Windows 大小写（normcase）、slash/backslash、尾分隔符、重复分隔符、
    `..` 折叠（normpath）。不展开符号链接（避免把用户可见的同一挂载点拆开）。
    """
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    np = os.path.normcase(os.path.normpath(s)).replace('\\', '/')
    return np or None


def project_key_from_path(p):
    """归一化路径 → 稳定 project_key（哈希），同时返回归一化路径供展示层取段名。"""
    np = normalize_project_path(p)
    if not np:
        return None, None
    key = 'p' + hashlib.sha256(np.encode('utf-8', 'replace')).hexdigest()[:16]
    return key, np


def project_key_from_native(source, native_id):
    """无路径但有稳定原生 id → hash(source 命名空间 + native id)。"""
    if not native_id:
        return None
    key = 'n' + hashlib.sha256(('%s|%s' % (source, str(native_id).strip()))
                               .encode('utf-8', 'replace')).hexdigest()[:16]
    return key


def path_display_name(p):
    """安全显示名：只取末段并保留原始大小写（键才做 normcase），绝不返回完整路径。"""
    if p is None:
        return None
    s = str(p).strip()
    if not s:
        return None
    base = os.path.basename(os.path.normpath(s).rstrip('\\/'))
    return base or None


def _execute_schema(conn, script):
    """Execute DDL without executescript's implicit transaction commit."""
    statement = ''
    for line in script.splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ''


def migrate_schema(conn):
    """把已有账本迁移到当前版本（探测式，幂等，可重复调用）。

    v0 → v1（Round 4）：usage_event 增加重放血统标记三列。
    v1 → v2（Phase 0）：usage_event 增加 project_key 列；
         新建 project_registry / session_registry（additive，不改历史行）。
    ALTER 全部成功后才提交并设置版本号；任一失败整体回滚。
    """
    conn.execute('SAVEPOINT schema_migration')
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(source_file)')}
        if 'last_error' not in cols:
            conn.execute('ALTER TABLE source_file ADD COLUMN last_error TEXT')

        ucols = {r[1] for r in conn.execute('PRAGMA table_info(usage_event)')}
        for col, ddl in (('is_replay', 'INTEGER NOT NULL DEFAULT 0'),
                         ('replay_reason', 'TEXT'),
                         ('replay_source_session', 'TEXT')):
            if col not in ucols:
                conn.execute('ALTER TABLE usage_event ADD COLUMN %s %s' % (col, ddl))

        if 'project_key' not in ucols:
            conn.execute('ALTER TABLE usage_event ADD COLUMN project_key TEXT')
        _execute_schema(conn, """
            CREATE TABLE IF NOT EXISTS project_registry (
                project_key  TEXT PRIMARY KEY,
                project_kind TEXT NOT NULL,
                display_name TEXT NOT NULL,
                first_seen_at TEXT,
                last_seen_at  TEXT,
                metadata_json TEXT
            );
            CREATE TABLE IF NOT EXISTS session_registry (
                source        TEXT NOT NULL,
                session_id    TEXT NOT NULL,
                project_key   TEXT,
                display_name  TEXT,
                first_seen_at TEXT,
                last_seen_at  TEXT,
                agent         TEXT,
                PRIMARY KEY(source, session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_event(project_key);
            CREATE INDEX IF NOT EXISTS idx_session_project ON session_registry(project_key);
        """)
        # v2 -> v3（Functional V1）：手工归因优先于自动归因。
        #   usage_event.project_key_manual  用户 Move/Merge 指定后置 1，重扫不再覆盖
        #   usage_event.project_key_auto    被手工覆盖时仍保留 adapter 原始归因（provenance）
        #   session_registry.attribution_manual  会话级手工归因标记
        #   project_registry.display_name_manual 用户改名后，重扫不回退自动识别名
        # 探测式幂等：列已存在（已是 v3）则跳过，重复 connect 不报错。
        ucols3 = {r[1] for r in conn.execute('PRAGMA table_info(usage_event)')}
        if 'project_key_manual' not in ucols3:
            conn.execute('ALTER TABLE usage_event ADD COLUMN project_key_manual INTEGER NOT NULL DEFAULT 0')
        if 'project_key_auto' not in ucols3:
            conn.execute('ALTER TABLE usage_event ADD COLUMN project_key_auto TEXT')
        scols3 = {r[1] for r in conn.execute('PRAGMA table_info(session_registry)')}
        if 'attribution_manual' not in scols3:
            conn.execute('ALTER TABLE session_registry ADD COLUMN attribution_manual INTEGER NOT NULL DEFAULT 0')
        prow3 = {r[1] for r in conn.execute('PRAGMA table_info(project_registry)')}
        if 'display_name_manual' not in prow3:
            conn.execute('ALTER TABLE project_registry ADD COLUMN display_name_manual INTEGER NOT NULL DEFAULT 0')
        uv = conn.execute('PRAGMA user_version').fetchone()[0] or 0
        if uv < 1:
            conn.execute('PRAGMA user_version = 1')
        if uv < 2:
            conn.execute('PRAGMA user_version = 2')
        if uv < 3:
            conn.execute('PRAGMA user_version = 3')
        conn.execute('RELEASE schema_migration')
    except Exception:
        conn.execute('ROLLBACK TO schema_migration')
        conn.execute('RELEASE schema_migration')
        raise


def migrate_source_paths(conn):
    """把历史遗留的非归一化 source_file.path 就地改成归一化形式。"""
    rows = conn.execute("""
        SELECT client, path, size, mtime, events, first_seen, last_seen, missing_since
        FROM source_file
    """).fetchall()
    touched = False
    seen = set()
    for r in rows:
        old = r['path']
        new = norm_path(old)
        key = (r['client'], new)
        if new == old:
            seen.add(key)
            continue
        touched = True
        if key in seen:
            conn.execute('DELETE FROM source_file WHERE client=? AND path=?', (r['client'], old))
            continue
        conn.execute("""
            UPDATE OR REPLACE source_file
            SET path=?, size=?, mtime=?, events=?, first_seen=?, last_seen=?, missing_since=?
            WHERE client=? AND path=?
        """, (new, r['size'], r['mtime'], r['events'], r['first_seen'],
              r['last_seen'], r['missing_since'], r['client'], old))
        seen.add(key)
    if touched:
        conn.commit()


def connect(create=False):
    """连接账本。

    create=False（默认）时**绝不在磁盘上留下文件**：账本还不存在就用内存库，
    读出来自然是 0 条。这样 `status` / `discover` / `board` 这类"只是看一眼"的操作
    不会凭空造出一个 usage.db —— 否则"用户还没确认，文件已经出现了"。
    只有真正的采集入口（cmd_scan）才用 create=True 落盘。

    打开已有文件库时自动做探测式 schema 迁移（v0 → v2，幂等）。
    """
    if os.path.isfile(DB_PATH) or create:
        conn = sqlite3.connect(DB_PATH)
    else:
        conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('SAVEPOINT ledger_schema')
    try:
        _execute_schema(conn, SCHEMA)
        migrate_schema(conn)
        conn.execute('RELEASE ledger_schema')
    except Exception:
        conn.execute('ROLLBACK TO ledger_schema')
        conn.execute('RELEASE ledger_schema')
        conn.close()
        raise
    return conn


def day_of(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d')


# ---------------------------------------------------------------- 解析器
# 每个解析器产出统一的 dict：
#   client / session_id / event_id / ts_ms / model / provider
#   / input_tokens / output_tokens / reasoning_tokens
#   / cache_read_tokens / cache_write_tokens / source_file
#
# 记账口径（已实测确认，不同客户端不一致，这里统一成「input 不含 cache」）：
#   zcode     : raw_usage_json = {inputTokens, outputTokens, totalTokens, cacheReadTokens, cacheWriteTokens}
#               且 totalTokens == inputTokens + outputTokens  -> input 不含 cache
#   dsh       : data.usage = {inputTokens, outputTokens, reasoningTokens, cacheReadTokens, cacheWriteTokens}
#               独立字段 -> input 不含 cache
#   workbuddy : rawUsage.prompt_tokens 是**含** cache 的总输入（OpenAI 口径）
#               -> 非缓存输入 = prompt_tokens - 缓存命中


ZCODE_SQL = """
    SELECT m.id, m.session_id, m.model_id, m.provider_id, m.started_at,
           m.input_tokens, m.output_tokens, m.reasoning_tokens,
           m.cache_read_input_tokens, m.cache_creation_input_tokens,
           m.agent,
           s.project_id AS s_project_id,
           s.directory  AS s_directory,
           s.title      AS s_title
    FROM model_usage m
    LEFT JOIN session s ON m.session_id = s.id
    WHERE m.started_at IS NOT NULL
"""


def list_files(client, root=None):
    """列出某个数据源当前的源文件。按文件粒度枚举，才能让增量跳过真正生效。

    root 可显式指定（自动发现时要逐个候选路径试），默认用 SOURCES 里已解析的路径。
    """
    if client == 'zcode':
        p = root or SOURCES['zcode']['path']
        return [p] if os.path.isfile(p) else []
    if client == 'dsh':
        r = root or SOURCES['dsh']['path']
        if not os.path.isdir(r):
            return []
        fs = glob.glob(os.path.join(r, '**', 'session.jsonl.zstd'), recursive=True)
        fs += glob.glob(os.path.join(r, '**', 'session.jsonl'), recursive=True)
        return sorted(set(fs))
    if client == 'workbuddy':
        r = root or SOURCES['workbuddy']['path']
        if not os.path.isdir(r):
            return []
        return sorted(glob.glob(os.path.join(r, '**', '*.jsonl'), recursive=True))
    if client == 'catpaw':
        r = root or SOURCES['catpaw']['path']
        if os.path.isdir(r):
            fs = sorted(glob.glob(os.path.join(r, '**', '*.jsonl'), recursive=True))
            if fs:
                return fs
            # 该候选本身就是记忆库目录，或里面有 *.db
            dbs = sorted(glob.glob(os.path.join(r, '*.db'))) + \
                sorted(glob.glob(os.path.join(r, '**', '*.db'), recursive=True))
            if dbs:
                return sorted(set(dbs))
        # 转录被清退时的兜底：应用自己的记忆库（两者消息 ID 不同源，不能同时用，否则重复计数）
        #
        # 但兜底只在「路径由自动发现决定」时才允许：用户在 sources.json 里显式指定了
        # catpaw 的路径，就必须只读那个路径——否则你以为在测沙盒，实际把真实机器的
        # 数据读进来了。显式配置优先于任何兜底猜测。
        if root is not None or 'catpaw' in _explicit_paths:
            return []
        alt = SOURCES['catpaw'].get('alt')
        if alt and os.path.normpath(alt) != os.path.normpath(str(r)) and os.path.isdir(alt):
            return sorted(glob.glob(os.path.join(alt, '*.db')))
        return []
    if client == 'traecn':
        r = root or SOURCES['traecn']['path']
        if not os.path.isdir(r):
            return []
        return sorted(glob.glob(os.path.join(r, '**', '*.jsonl'), recursive=True))
    if client == 'codex':
        r = root or SOURCES['codex']['path']
        if not os.path.isdir(r):
            return []
        fs = glob.glob(os.path.join(r, '**', 'rollout-*.jsonl'),
                       recursive=True)
        return sorted(set(fs))[:_GENERIC_LIMIT_FILES]
    # V1.1 Universal Discovery 注册的通用来源：多 physical location，
    # 统一目录形态（sqlite 在目录内有界发现 *.db/*.sqlite）。
    # LOGICAL EVENT != PHYSICAL SOURCE：跨位置/副本靠稳定 event_id 去重。
    spec = SOURCES.get(client) or {}
    if spec.get('kind') in ('generic-jsonl', 'generic-sqlite'):
        roots = spec.get('paths') or ([spec['path']] if spec.get('path')
                                      else [])
        out = []
        for r in roots:
            if not os.path.isdir(r):
                continue
            if spec['kind'] == 'generic-jsonl':
                out += glob.glob(os.path.join(r, '**', '*.jsonl'),
                                 recursive=True)
                out += glob.glob(os.path.join(r, '**', '*.ndjson'),
                                 recursive=True)
            else:
                for ext in _GENERIC_SQLITE_EXTS:
                    out += glob.glob(os.path.join(r, '**', '*' + ext),
                                     recursive=True)
        return sorted(set(out))[:_GENERIC_LIMIT_FILES]
    return []


def parse_zcode_file(path):
    conn = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for r in conn.execute(ZCODE_SQL):
            # 项目归因（Phase 0）：join session 表取 project_id / directory / title。
            # project_key 由 directory（真实工作目录）生成；session 行缺失（原始库
            # 清理/活库波动）→ 归因留空，绝不猜测。
            pk = pkind = pdisp = None
            if r['s_directory']:
                pk, _np = project_key_from_path(r['s_directory'])
                pkind = PROJECT_KIND_PROJECT
                pdisp = path_display_name(r['s_directory'])
            yield {
                'client': 'zcode',
                'session_id': str(r['session_id'] or ''),
                'event_id': str(r['id']),
                'ts_ms': int(r['started_at']),
                'model': (r['model_id'] or '').strip(),
                'provider': (r['provider_id'] or '').strip(),
                'input_tokens': r['input_tokens'] or 0,
                'output_tokens': r['output_tokens'] or 0,
                'reasoning_tokens': r['reasoning_tokens'] or 0,
                'cache_read_tokens': r['cache_read_input_tokens'] or 0,
                'cache_write_tokens': r['cache_creation_input_tokens'] or 0,
                'source_file': path,
                'project_key': pk,
                'project_kind': pkind,
                'project_display': pdisp,
                'session_title': (r['s_title'] or '').strip() or None,
                'agent': (r['agent'] or '').strip() or None,
            }
    finally:
        conn.close()


def _zstd_lines(path):
    """dsh 的 session.jsonl.zstd 是追加写的多帧 zstd，必须跨帧解压、分块读。"""
    try:
        import zstandard
    except ImportError:
        raise RuntimeError('解析 dsh 会话需要 zstandard 模块：pip install zstandard')
    d = zstandard.ZstdDecompressor()
    with open(path, 'rb') as f:
        if os.path.getsize(path) == 0:
            return
        obj = d.decompressobj(read_across_frames=True)
        buf = b''
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            buf += obj.decompress(chunk)
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                yield line
        if buf:
            yield buf


def _plain_lines(path):
    """纯 jsonl 会话文件（list_files 本就会收集 session.jsonl）。"""
    with open(path, 'rb') as f:
        for line in f:
            yield line


def parse_dsh_file(path):
    """读单个 dsh 会话文件里 assistant/message 的 data.usage。

    项目/会话归因（Phase 0）：`session` 事件带顶层 cwd / id / agentPreset，
    `session/title` 事件带标题。两者与用量无强绑定顺序，因此物化一遍解压流，
    先取会话级归因，再输出用量事件。cwd 是项目身份（归一化哈希为 project_key）；
    容器 slug 有损（CJK 转义+大小写不保），绝不单独作为 project_key。

    关于分叉会话：dsh 的 forked / subagent 会话会在新文件里带上父会话的历史
    （`session` 事件的 `parentSession` + `seedLength`）。实测 `createdAt` 晚于文件内最早事件的时间，
    所以**不能**拿它当"父历史重放"的边界来过滤——那样会误删真实数据。这里按会话如实记账，
    重叠由 `status` / HTML 报告量化；血统标记见 dsh_replay_verdicts（Round 4）。
    """
    sess = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
    if os.path.getsize(path) == 0:
        return
    it = _plain_lines(path) if not path.endswith('.zstd') else _zstd_lines(path)
    materialized = []
    got = 0
    while True:
        try:
            raw = next(it)
        except StopIteration:
            break
        except RuntimeError:
            raise
        except Exception as e:
            if got == 0:
                raise
            print('  ~ [dsh] %s 尾部不完整（已解析 %d 行，忽略尾部）：%s'
                  % (sess[:24], got, e))
            break
        got += 1
        materialized.append(raw)

    # ---- 会话级归因（cwd / agentPreset / title）----
    cwd = None
    agent_preset = None
    s_title = None
    parsed = []
    for raw in materialized:
        s = raw.decode('utf-8', 'replace')
        try:
            if '"assistant/message"' in s:
                o = json.loads(s)
            elif '"type":"session",' in s or '"type": "session",' in s or s.startswith('{"type":"session"'):
                o = json.loads(s)
            elif '"session/title"' in s:
                o = json.loads(s)
            else:
                continue
        except Exception:
            continue
        if not isinstance(o, dict):
            continue
        t = o.get('type')
        if t == 'session':
            cwd = o.get('cwd') or cwd
            agent_preset = (o.get('agentPreset') or '').strip() or agent_preset
        elif t == 'session/title':
            dd = o.get('data') or {}
            tt = dd.get('title') if isinstance(dd, dict) else None
            if tt:
                s_title = str(tt).strip() or s_title
        elif t == 'assistant/message':
            parsed.append(o)

    pk = pkind = pdisp = None
    if cwd:
        pk, _np = project_key_from_path(cwd)
        pkind = PROJECT_KIND_PROJECT
        pdisp = path_display_name(cwd)

    for o in parsed:
        d = o.get('data') or {}
        u = d.get('usage')
        if not isinstance(u, dict):
            continue
        src = ((d.get('message') or {}).get('source')) or {}
        reply = ((src.get('replayState') or {}).get('response') or {})
        yield {
            'client': 'dsh',
            'session_id': sess,
            'event_id': 'seq%s' % o.get('seq'),
            'ts_ms': int(o.get('time') or 0),
            'model': (reply.get('responseModel') or src.get('model') or '').strip(),
            'provider': (src.get('provider') or '').strip(),
            'input_tokens': u.get('inputTokens') or 0,
            'output_tokens': u.get('outputTokens') or 0,
            'reasoning_tokens': u.get('reasoningTokens') or 0,
            'cache_read_tokens': u.get('cacheReadTokens') or 0,
            'cache_write_tokens': u.get('cacheWriteTokens') or 0,
            'source_file': path,
            'project_key': pk,
            'project_kind': pkind,
            'project_display': pdisp,
            'session_title': s_title,
            'agent': agent_preset,
        }


def parse_workbuddy_file(path):
    sid = os.path.splitext(os.path.basename(path))[0]
    # 会话级归因（Phase 0）：cwd 每行都有且恒定；ai-title 提供会话显示名。
    # 两者都被旧 parser 丢弃 —— 这里先扫一遍拿归因，再输出 rawUsage 事件。
    ai_title = None
    cwd = None
    lines = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            lines.append(line)
            try:
                if '"ai-title"' in line and line.lstrip().startswith('{'):
                    o = json.loads(line)
                    if o.get('type') == 'ai-title':
                        if not ai_title:
                            ai_title = (o.get('aiTitle') or '').strip() or None
                        cwd = o.get('cwd') or cwd
                        continue
                if cwd is None and '"cwd"' in line and line.lstrip().startswith('{'):
                    o = json.loads(line)
                    if isinstance(o, dict) and o.get('cwd'):
                        cwd = o['cwd']
            except Exception:
                continue
    pk = pkind = pdisp = None
    if cwd:
        pk, _np = project_key_from_path(cwd)
        pkind = PROJECT_KIND_WORK_ITEM
        pdisp = path_display_name(cwd)
    for line in lines:
        if 'rawUsage' not in line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        pd = o.get('providerData') or {}
        u = pd.get('rawUsage')
        if not isinstance(u, dict):
            continue
        ts = o.get('timestamp')
        if not isinstance(ts, (int, float)):
            continue
        prompt = u.get('prompt_tokens') or 0
        cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens') or 0
        if not cached:
            cached = u.get('prompt_cache_hit_tokens') or 0
        cached = min(cached, prompt)  # prompt_tokens 含缓存命中，减掉才是新增输入
        yield {
            'client': 'workbuddy',
            'session_id': str(o.get('sessionId') or sid),
            'event_id': str(o.get('id') or ''),
            'ts_ms': int(ts),
            'model': (pd.get('model') or pd.get('requestModelId') or '').strip(),
            'provider': (pd.get('requestModelName') or '').strip(),
            'input_tokens': max(prompt - cached, 0),
            'output_tokens': u.get('completion_tokens') or 0,
            'reasoning_tokens': ((u.get('completion_tokens_details') or {})
                                 .get('reasoning_tokens') or 0),
            'cache_read_tokens': cached,
            'cache_write_tokens': u.get('cache_creation_input_tokens') or 0,
            'source_file': path,
            'project_key': pk,
            'project_kind': pkind,
            'project_display': pdisp,
            'session_title': ai_title,
        }


def parse_codex_file(path):
    """Codex（OpenAI 桌面/CLI）rollout 会话日志。

    语义取证结论（2026-10-03，本机实测，全部数值验证）：
      - session_meta（首行）payload.session_id 是稳定会话身份；
        payload.cwd 是真实工作目录 → 项目归因。
      - turn_context.payload.model 是该轮请求的模型名。
      - **只入账 token_usage_record**：payload.usage 是**逐请求**新鲜
        用量（验证：turn_token_usage 累计值 = Σ usage，逐 turn 相等）。
        event_msg/token_count 里的 total_token_usage 是**会话内累计
        值**（单调递增），入账它必然重复计数 → 明确跳过。
      - input_tokens 含 cached_input_tokens（OpenAI 口径：total_tokens
        = input + output，cached ⊆ input；与 workbuddy 同口径）→
        input = max(input_tokens − cached_input_tokens, 0)。
      - 事件身份 = canonical fingerprint（client/session/response_id/
        ordinal/ts/model/token 五元组）：ordinal 是 producer 写入的
        记录序号（实测全文件唯一），属于记录内容；文件改名/副本/
        archived 迁移均不改变身份。旧格式文件（无 token_usage_record）
        没有可证明的逐请求用量 → 0 条，不猜测。

    只读打开；绝不执行/上传任何日志内容。
    """
    session_id = None
    cwd = None
    model = ''
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            if '"token_usage_record"' not in line and \
                    '"session_meta"' not in line and \
                    '"turn_context"' not in line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            t = o.get('type')
            if t == 'session_meta':
                pl = o.get('payload') or {}
                session_id = str(pl.get('session_id') or pl.get('id') or '')
                cwd = pl.get('cwd') or None
                continue
            if t == 'turn_context':
                model = str((o.get('payload') or {}).get('model') or '')
                continue
            if t != 'token_usage_record':
                continue
            pl = o.get('payload') or {}
            u = pl.get('usage')
            if not isinstance(u, dict):
                continue
            inp = _generic_int(u.get('input_tokens'))
            out = _generic_int(u.get('output_tokens'))
            if inp is None or out is None:
                continue
            cached = _generic_int(u.get('cached_input_tokens')) or 0
            cached = min(cached, inp)
            ts = _iso_ms(o.get('timestamp'))
            if ts is None:
                continue
            fresh_input = max(inp - cached, 0)
            reasoning = _generic_int(u.get('reasoning_output_tokens')) or 0
            cache_write = _generic_int(u.get('cache_write_input_tokens')) or 0
            pk = pkind = pdisp = None
            if cwd:
                pk, _np = project_key_from_path(cwd)
                pkind = PROJECT_KIND_PROJECT
                pdisp = path_display_name(cwd)
            yield {
                'client': 'codex',
                'session_id': session_id or '',
                'event_id': _generic_event_identity(
                    'codex', session_id or '', ts, model,
                    fresh_input, out, reasoning, cached, cache_write,
                    provider_id=pl.get('response_id'),
                    extra={'ordinal': _generic_int(o.get('ordinal')) or 0}),
                'ts_ms': ts,
                'model': model,
                'provider': 'openai',
                'input_tokens': fresh_input,
                'output_tokens': out,
                'reasoning_tokens': reasoning,
                'cache_read_tokens': cached,
                'cache_write_tokens': cache_write,
                'source_file': path,
                'project_key': pk,
                'project_kind': pkind,
                'project_display': pdisp,
                'session_title': None,
            }


PARSE_FILE = {
    'zcode': parse_zcode_file,
    'dsh': parse_dsh_file,
    'workbuddy': parse_workbuddy_file,
    'codex': parse_codex_file,
    'catpaw': None,
    'traecn': None,
}


# ---------------------------------------------------------------- 通用 Schema Adapter（V1.1 Universal Discovery）
# Discovery 引擎（discovery.py）对未知工具做严格 schema fingerprint，只有
# 完全符合 source-catalog.json 里的通用契约时才注册到这里。解析层仍逐条
# 再验：字段不齐 / 语义不明（如顶层 cached_tokens）的记录一律跳过，绝不猜。
#
# 事件身份（correctness gate）：
#   PHYSICAL SOURCE != LOGICAL EVENT。event_id 是**确定性稳定身份**，
#   绝不依赖 rowid / 绝对路径 / 文件名 / 读取顺序 / mtime —— 因此
#   VACUUM、表重建、文件改名/移动、备份副本、重新发现都不会把同一
#   条真实 Usage 变成新事件，也不会把不同位置的重复观察记两次。
#   优先使用 provider 自带的稳定 id（event_id / request_id /
#   message_id / usage_id；JSONL 额外接受裸 'id'，SQLite 不接受裸
#   'id' —— 它通常是 rowid 别名）；不存在时用 canonical content
#   fingerprint（规范化字段 + 固定键序的 SHA256）。规范化契约见
#   _generic_event_identity 与 source-catalog.json → generic.identity。

GENERIC_SOURCES_NAME = 'generic-sources.json'
GENERIC_IDENTITY_SCHEMA_VERSION = 1
_GENERIC_MODEL_FIELDS = ('model', 'model_name', 'model_id')
_GENERIC_TS_FIELDS = ('ts_ms', 'timestamp', 'ts', 'created_at', 'created',
                      'time', 'started_at')
_GENERIC_SESSION_FIELDS = ('session_id', 'conversation_id', 'session',
                           'thread_id')
_GENERIC_AMBIGUOUS_CACHE = ('cached_tokens', 'cache_read', 'cache_write')
# provider 稳定事件 id 字段。SQLite 额外要求**排除**裸 'id'：
# SQLite 的 id 几乎总是 INTEGER PRIMARY KEY（rowid 别名），VACUUM /
# 重建后不稳定，不能做长期身份。
_GENERIC_PID_FIELDS_SQL = ('event_id', 'request_id', 'message_id',
                           'usage_id')
_GENERIC_PID_FIELDS_JSONL = _GENERIC_PID_FIELDS_SQL + ('id',)
_GENERIC_LIMIT_FILES = 500      # 单个通用来源最多列出的文件数
_GENERIC_SQLITE_EXTS = ('.db', '.sqlite', '.sqlite3')


def _norm_text(v):
    """字符串规范化：NFKC + strip。确定性、无环境依赖。"""
    import unicodedata
    if v is None:
        return ''
    return unicodedata.normalize('NFKC', str(v)).strip()


def _generic_event_identity(client, session, ts_ms, model,
                            input_tokens, output_tokens, reasoning_tokens,
                            cache_read_tokens, cache_write_tokens,
                            provider_id=None, extra=None):
    """Generic 来源的稳定事件身份（canonical fingerprint）。

    组成（固定键序 JSON → SHA256，截 32 hex）：
      schema 版本 / 身份种类（pid | content）/ 归一化 tool identity /
      归一化 session / provider id（若有）/ 归一化时间戳（epoch ms）/
      归一化 model / 归一化 token 五元组 (input, output, reasoning,
      cache_read, cache_write) / 额外稳定字段（extra，如 producer
      自带的 ordinal —— 属于记录内容，不是读取顺序）。

    明确**不**进入身份：绝对路径、rowid、文件名、mtime。
    NULL → ''；数值一律 int；字符串 NFKC+strip；时间戳一律 epoch ms。
    同一条真实 Usage：VACUUM / 表重建 / 文件改名 / 备份副本 / 重新扫描
    → 身份不变；不同真实事件（可区分字段不同）→ 身份必不同。
    """
    basis = {
        'schema': GENERIC_IDENTITY_SCHEMA_VERSION,
        'kind': 'pid' if provider_id not in (None, '') else 'content',
        'client': _norm_text(client),
        'session': _norm_text(session),
        'pid': _norm_text(provider_id),
        'ts_ms': int(ts_ms),
        'model': _norm_text(model),
        'tokens': [int(input_tokens), int(output_tokens),
                   int(reasoning_tokens), int(cache_read_tokens),
                   int(cache_write_tokens)],
    }
    for k in sorted(extra or {}):
        basis[k] = extra[k]
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]


def _generic_tool_id(tool_id):
    """client id 清洗：小写 slug，且不得与内置来源冲突。"""
    s = re.sub(r'[^a-z0-9_-]+', '-', (tool_id or '').lower()).strip('-') or 'tool'
    if s in SOURCES and not (SOURCES[s].get('generic')):
        s = s + '-generic'
        n = 2
        while s in SOURCES and not SOURCES[s].get('generic'):
            s = re.sub(r'-\d+$', '', s) + '-%d' % n
            n += 1
    return s


def register_generic_source(tool_id, label, paths, kind, base=None,
                            persist=True, replace=False,
                            identity_strategy=None):
    """把 Discovery 引擎确认的通用 schema 来源注册为可采集数据源。

    kind: 'generic-jsonl' | 'generic-sqlite'。paths 是**数据根目录清单**
    （两种 kind 统一为目录：sqlite 在目录内有界发现 *.db/*.sqlite）。
    一个工具可以有多个 physical location（live / backup / archive）——
    全部登记进 'paths'，采集时逐一扫描；同一逻辑事件靠稳定 event_id
    在 usage_event 里去重（ON CONFLICT UPDATE），只入账一次。

    replace=False（默认）：追加新位置，保留既有位置；
    replace=True（discovery 对账）：paths 整体替换为本轮发现集合。
    identity_strategy：'pid' | 'content' —— 事件身份策略在来源生命
    周期内必须稳定（V1.1 冻结）：解析器按钉住的策略生成身份，策略
    漂移由 discovery 对账检测并暂停入账（见 set_generic_source_paused）。
    注册同时原子写 generic-sources.json（运行态文件，本机发现事实，
    不入库；路径留在本机，与 resolved-sources 同等隐私级别）。
    """
    tool_id = _generic_tool_id(tool_id)
    if isinstance(paths, str):
        paths = [paths]
    norm = []
    for p in paths or []:
        p = os.path.normpath(p)
        if p not in norm:
            norm.append(p)
    spec = SOURCES.get(tool_id) if tool_id in SOURCES and \
        SOURCES[tool_id].get('generic') else None
    if spec:
        if replace:
            spec['paths'] = norm
        else:
            for p in norm:                   # 追加新位置，不丢已知位置
                if p not in spec['paths']:
                    spec['paths'].append(p)
        spec['candidates'] = list(spec['paths'])
        spec['path'] = spec['paths'][0] if spec['paths'] else ''
        spec['label'] = label or spec['label']
        spec['kind'] = kind
        if identity_strategy:
            spec['identity_strategy'] = identity_strategy
    else:
        spec = {
            'label': label or tool_id,
            'mode': 'usage',
            'visibility': 'token',
            'kind': kind,
            'paths': norm,
            'path': norm[0] if norm else '',
            'candidates': list(norm),
            'probe': 'dir',
            'hint': 'Universal Discovery 自动接入（通用用量 schema）',
            'generic': True,
        }
        if identity_strategy:
            spec['identity_strategy'] = identity_strategy
        SOURCES[tool_id] = spec
    PARSE_FILE[tool_id] = (_make_generic_sqlite_parser(tool_id)
                           if kind == 'generic-sqlite'
                           else _make_generic_jsonl_parser(tool_id))
    if persist:
        _persist_generic_sources(base)
    return tool_id


def set_generic_source_paused(tool_id, base=None, paused=True):
    """暂停 / 恢复一个通用来源的自动入账（身份策略漂移保护）。"""
    spec = SOURCES.get(_generic_tool_id(tool_id)) or {}
    if not spec.get('generic'):
        return False
    spec['paused'] = bool(paused)
    _persist_generic_sources(base)
    return True


def _persist_generic_sources(base=None):
    base = base or BASE
    data = {}
    for client, spec in SOURCES.items():
        if spec.get('generic'):
            data[client] = {'label': spec.get('label'),
                            'paths': list(spec.get('paths') or []),
                            'kind': spec['kind'],
                            'identity_strategy': spec.get('identity_strategy'),
                            'paused': bool(spec.get('paused'))}
    try:
        p = os.path.join(base, GENERIC_SOURCES_NAME)
        os.makedirs(base, exist_ok=True)
        tmp = p + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError as e:
        print('  ! generic-sources.json 写入失败：%s' % e, file=sys.stderr)


def _generic_ts_ms(o):
    """从记录里取时间戳 → epoch ms。字段值语义不定时拒绝（返回 None）。"""
    for k in _GENERIC_TS_FIELDS:
        v = o.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and v > 0:
            return int(v if v >= 10**12 else v * 1000)
        if isinstance(v, str) and v.strip():
            ms = _iso_ms(v) or _local_ms(v)
            if ms:
                return ms
    return None


def _generic_int(v):
    """非负整数（接受整值 float，如 JSON 123.0 / SQLite REAL）。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if not isinstance(v, int) or v < 0:
        return None
    return v


def _parse_generic_jsonl(path, client):
    """OpenAI 风格 usage JSONL（契约见 source-catalog.json → generic）。

    逐条校验：model 非空、prompt/completion 为非负整数、时间可解析。
    顶层出现 cached_tokens 等扁平缓存字段 → 语义不明，整条跳过（绝不把
    含缓存的输入错记成非缓存输入）。缓存只认 prompt_tokens_details.
    cached_tokens（OpenAI 文档语义：prompt_tokens 含缓存命中）。

    记录缺 session 字段 → session_id = ''（常量桶）：会话归属不依赖
    文件名，文件改名/移动/复制不影响事件身份与数量。

    身份策略冻结（V1.1）：来源注册时钉住 'pid' | 'content'；生命周期
    内不得漂移（'content' 时忽略 provider id；'pid' 时缺 id 的记录
    跳过），防止两种策略互不认账造成双记。paused 来源不入账。
    """
    spec = SOURCES.get(client) or {}
    if spec.get('paused'):
        return
    pinned = spec.get('identity_strategy')
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if not isinstance(o, dict):
                continue
            model = next((str(o[k]).strip() for k in _GENERIC_MODEL_FIELDS
                          if o.get(k)), '')
            if not model:
                continue
            pt = _generic_int(o.get('prompt_tokens'))
            ct = _generic_int(o.get('completion_tokens'))
            if pt is None or ct is None:
                continue
            if any(k in o for k in _GENERIC_AMBIGUOUS_CACHE):
                continue
            details = o.get('prompt_tokens_details')
            cached = 0
            if isinstance(details, dict):
                cached = _generic_int(details.get('cached_tokens')) or 0
            cached = min(cached, pt)
            ts = _generic_ts_ms(o)
            if ts is None:
                continue
            sid = next((str(o[k]) for k in _GENERIC_SESSION_FIELDS
                        if o.get(k)), '')
            pid = next((str(o[k]) for k in _GENERIC_PID_FIELDS_JSONL
                        if o.get(k)), None)
            if pinned == 'content':
                pid = None
            elif pinned == 'pid' and not pid:
                continue                     # 策略已钉为 pid：缺 id 不入账
            cd = o.get('completion_tokens_details')
            reasoning = (_generic_int(cd.get('reasoning_tokens')) or 0) \
                if isinstance(cd, dict) else 0
            inp = max(pt - cached, 0)
            yield {
                'client': client,
                'session_id': sid,
                'event_id': _generic_event_identity(
                    client, sid, ts, model, inp, ct, reasoning, cached, 0,
                    provider_id=pid),
                'ts_ms': ts,
                'model': model,
                'provider': '',
                'input_tokens': inp,
                'output_tokens': ct,
                'reasoning_tokens': reasoning,
                'cache_read_tokens': cached,
                'cache_write_tokens': 0,
                'source_file': path,
                'project_key': None,
                'project_kind': None,
                'project_display': None,
                'session_title': None,
            }


def _parse_generic_sqlite(path, client):
    """OpenAI 风格 usage SQLite。只读打开；contract 列集（与 discovery
    fingerprint 同一契约）在运行时重新确认，schema 变了就拒绝解析。

    事件身份绝不使用 rowid / 文件路径：provider 稳定 id 列
    （event_id/request_id/message_id/usage_id，**不含**裸 id —— 那是
    rowid 别名）优先；否则 canonical content fingerprint
    （session/ts/model/token 五元组）。VACUUM / 表重建 / 改名 / 副本
    均不改变身份。身份策略冻结与 paused 语义同 generic JSONL。
    """
    spec = SOURCES.get(client) or {}
    if spec.get('paused'):
        return
    pinned = spec.get('identity_strategy')
    conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'),
                           uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        target = _cols = None
        try:
            sql = ("SELECT name FROM sqlite_master WHERE type='table'"
                   " AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")
            tables = [r[0] for r in conn.execute(sql)]
        except sqlite3.DatabaseError:
            return
        for t in tables:
            cols = [r[1] for r in conn.execute('PRAGMA table_info(%s)' % t)]
            cs = set(c.lower() for c in cols)
            if not ({'model', 'prompt_tokens', 'completion_tokens'} <= cs):
                continue
            if any(c in cs for c in _GENERIC_AMBIGUOUS_CACHE):
                continue
            ts = next((c for c in _GENERIC_TS_FIELDS if c in cs), None)
            if not ts:
                continue
            target, _cols = t, cs
            break
        if not target:
            return
        sess_col = next((c for c in _GENERIC_SESSION_FIELDS if c in _cols),
                        None)
        pid_col = next((c for c in _GENERIC_PID_FIELDS_SQL if c in _cols),
                       None)
        cache_read = ('cache_read_input_tokens' in _cols
                      and 'cache_read_input_tokens') or None
        cache_write = ('cache_creation_input_tokens' in _cols
                       and 'cache_creation_input_tokens') or None
        for r in conn.execute('SELECT * FROM "%s"' % target):
            o = {k: r[k] for k in r.keys()}
            model = next((str(o[k]).strip() for k in _GENERIC_MODEL_FIELDS
                          if o.get(k)), '')
            if not model:
                continue
            pt = _generic_int(o.get('prompt_tokens'))
            ct = _generic_int(o.get('completion_tokens'))
            if pt is None or ct is None:
                continue
            ts = _generic_ts_ms(o)
            if ts is None:
                continue
            raw_cached = _generic_int(o.get(cache_read)) if cache_read else None
            cached = min(raw_cached or 0, pt)
            sid = str(o[sess_col]) if sess_col and o.get(sess_col) else ''
            pid = str(o[pid_col]) if pid_col and o.get(pid_col) else None
            if pinned == 'content':
                pid = None
            elif pinned == 'pid' and not pid:
                continue                     # 策略已钉为 pid：缺 id 不入账
            inp = max(pt - cached, 0)
            yield {
                'client': client,
                'session_id': sid,
                'event_id': _generic_event_identity(
                    client, sid, ts, model, inp, ct, 0, cached,
                    (_generic_int(o.get(cache_write)) or 0) if cache_write
                    else 0,
                    provider_id=pid),
                'ts_ms': ts,
                'model': model,
                'provider': '',
                'input_tokens': inp,
                'output_tokens': ct,
                'reasoning_tokens': 0,
                'cache_read_tokens': cached,
                'cache_write_tokens':
                    (_generic_int(o.get(cache_write)) or 0) if cache_write else 0,
                'source_file': path,
                'project_key': None,
                'project_kind': None,
                'project_display': None,
                'session_title': None,
            }
    finally:
        conn.close()


def _make_generic_jsonl_parser(client):
    def _parser(path):
        yield from _parse_generic_jsonl(path, client)
    return _parser


def _make_generic_sqlite_parser(client):
    def _parser(path):
        yield from _parse_generic_sqlite(path, client)
    return _parser


# Discovery 引擎此前注册过的通用来源：新进程加载即恢复，scan 不必先发现。
if os.path.isfile(os.path.join(BASE, GENERIC_SOURCES_NAME)):
    try:
        with open(os.path.join(BASE, GENERIC_SOURCES_NAME),
                  encoding='utf-8') as _gsf:
            for _gid, _gs in (json.load(_gsf) or {}).items():
                if not isinstance(_gs, dict) or \
                        _gs.get('kind') not in ('generic-jsonl',
                                                'generic-sqlite'):
                    continue
                _paths = _gs.get('paths') or \
                    ([_gs['path']] if _gs.get('path') else [])
                if _paths:
                    _tid = register_generic_source(_gid, _gs.get('label'),
                                                   _paths, _gs['kind'],
                                                   persist=False)
                    if _gs.get('identity_strategy'):
                        SOURCES[_tid]['identity_strategy'] = \
                            _gs['identity_strategy']
                    if _gs.get('paused'):
                        SOURCES[_tid]['paused'] = True
    except Exception as _e:
        print('  ! generic-sources.json 读取失败：%s' % _e, file=sys.stderr)


# ---------------------------------------------------------------- 活动量解析
# CatPaw / Trae CN 本地都不写 token 用量（Trae CN 的 ai-agent 库还是加密的），
# 所以只产生活动事件：哪天用了、开了哪些会话、有多少轮。
# 这些事件写进 activity_event，永远不参与 token 与成本计算。


def _iso_ms(s):
    """'2026-07-26T11:22:57.157Z' -> epoch ms"""
    if not isinstance(s, str):
        return None
    try:
        d = dt.datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp() * 1000)
    except Exception:
        return None


def _local_ms(s):
    """'2026-08-02 15:52:18' -> epoch ms（按本地时区解释）"""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        return int(dt.datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S').timestamp() * 1000)
    except Exception:
        return None


def parse_catpaw_file(path):
    """CatPaw：会话转录 jsonl（messageId / conversationId / timestamp / type / cwd）。

    活动归因（Phase 0）：行内 cwd + 路径容器可给出项目键；绝不产生 token/cost。
    """
    if path.lower().endswith('.db'):
        yield from _parse_catpaw_db(path)
        return
    stem = os.path.splitext(os.path.basename(path))[0]
    cwd = None
    lines = []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            lines.append(line)
            if cwd is None and '"cwd"' in line:
                try:
                    o = json.loads(line)
                    if isinstance(o, dict) and o.get('cwd'):
                        cwd = o['cwd']
                except Exception:
                    continue
    pk = pkind = pdisp = None
    if cwd:
        pk, _np = project_key_from_path(cwd)
        pkind = PROJECT_KIND_WORKSPACE
        pdisp = path_display_name(cwd)
    for line in lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        mid = o.get('messageId') or (o.get('message') or {}).get('messageId')
        ts = _iso_ms(o.get('timestamp'))
        if not mid or ts is None:
            continue
        yield {
            'client': 'catpaw',
            'session_key': str(o.get('conversationId') or stem),
            'event_id': str(mid),
            'ts_ms': ts,
            'kind': 'message',
            'source_file': path,
            'project_key': pk,
            'project_kind': pkind,
            'project_display': pdisp,
        }


def _parse_catpaw_db(path):
    """兜底：应用自己的记忆库。消息 ID 与转录不同源，所以只在上面的转录不存在时才用。

    注意：这里**不再吞异常**。以前无论打开失败还是查询失败都静默 return，
    结果是"来源读不出来"被当成"这个来源没数据"，整次运行照样报成功。
    """
    conn = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True)
    try:
        for cid, mid, ts, role in conn.execute(
                'SELECT conversation_id, message_id, created_at_ms, role '
                'FROM ui_sdk_messages WHERE created_at_ms IS NOT NULL'):
            if not mid or not ts:
                continue
            yield {
                'client': 'catpaw',
                'session_key': str(cid or path),
                'event_id': str(mid),
                'ts_ms': int(ts),
                'kind': 'message',
                'source_file': path,
                'meta': role or '',
            }
    finally:
        conn.close()


def parse_traecn_file(path):
    """Trae CN：只有记忆摘要可读（intent/actions/outcome/learned + message_summary_time）。

    活动归因（Phase 0）：项目身份取自路径层 `<mangled 项目目录>/<yyyymmdd>/文件` ——
    目录名是不可逆的本地标识（稳定 native id），哈希为 project_key（kind=project）；
    显示名做 best-effort 解码，绝不输出完整路径。
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    mproj = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path))))
    pk = project_key_from_native('traecn', mproj) if mproj else None
    disp = None
    if mproj:
        try:
            import urllib.parse as _up
            dec = _up.unquote(mproj.replace('~', '%'))
            disp = re.sub(r'-{2,}', ' ', dec).strip(' -') or None
        except Exception:
            disp = None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:
                continue
            mid = o.get('message_id')
            ts = _local_ms(o.get('message_summary_time'))
            if not mid or ts is None:
                continue
            yield {
                'client': 'traecn',
                'session_key': stem,
                'event_id': str(mid),
                'ts_ms': ts,
                'kind': 'memory',
                'source_file': path,
                'project_key': pk,
                'project_kind': PROJECT_KIND_PROJECT if pk else None,
                'project_display': disp,
            }


PARSE_FILE['catpaw'] = parse_catpaw_file
PARSE_FILE['traecn'] = parse_traecn_file


# ---------------------------------------------------------------- dsh 血统/重放判定
# 权威实现（Round 4）。scripts/analyze_dsh_replay.py 与 replay-mark 复用同一套逻辑，
# 避免算法漂移。取证结论（Round 3，见 docs/architecture/R3_DEDUP_DESIGN.md）：
#   seq 是血统树内单调计数器；seedLength = 重放边界（本文件 seq 下界）；
#   重放保留原 seq/time/usage；depth=1 subagent 无种子、内容独立；
#   跨树 seq 碰撞是独立事件；断链/模糊一律保守保留（is_replay=0）。
# 原则：raw 永远保留；指纹只在血统链内做副本验证，绝不跨会话判定。

DSH_REASON_EXACT = 'parent_seed_exact'
DSH_REASON_ANCESTOR = 'ancestor_seed_exact'


def dsh_read_header(path):
    """只读会话文件首行 type=session 的顶层血统字段。"""
    try:
        it = _plain_lines(path) if not path.endswith('.zstd') else _zstd_lines(path)
        for raw in it:
            s = raw.decode('utf-8', 'replace')
            if '"session"' not in s:
                return {}
            o = json.loads(s)
            if o.get('type') == 'session':
                return {k: o.get(k) for k in ('id', 'parentSession',
                                              'seedLength', 'delegationDepth',
                                              'createdAt')}
            return {}
    except Exception:
        return {}
    return {}


def dsh_fp(ev):
    """重放副本一致性指纹（归一化事件）。仅用于血统链内验证。"""
    return (ev['ts_ms'], ev['input_tokens'], ev['output_tokens'],
            ev['cache_read_tokens'], ev['model'])


class DshLineage:
    """一次运行内的 dsh 血统上下文：按需只读解析祖先文件，进程内缓存。

    设计要点（Round 3 阶段 4 要求）：不依赖"本轮 parent 必须被重新扫描"——
    祖先文件即使因增量未变被跳过、或本轮不在扫描清单里，也能按需只读解析。
    """

    def __init__(self):
        self.files = {}      # sid -> path
        self.headers = {}    # sid -> header
        self.indexes = {}    # sid -> {seq: fp}

    def load(self, paths):
        self.files = {os.path.basename(os.path.dirname(p)): p for p in paths}

    def header(self, sid):
        if sid not in self.headers:
            p = self.files.get(sid)
            self.headers[sid] = dsh_read_header(p) if p else {}
        return self.headers[sid]

    def index(self, sid):
        """祖先文件的 usage 事件 seq -> fp 索引（只读，缓存）。"""
        if sid not in self.indexes:
            p = self.files.get(sid)
            idx = {}
            if p:
                try:
                    for ev in parse_dsh_file(p):
                        try:
                            seq = int(str(ev['event_id'])[3:])
                        except (ValueError, TypeError, KeyError):
                            continue
                        idx.setdefault(seq, dsh_fp(ev))
                except Exception:
                    pass        # 祖先读不出 → 视为缺失，由判定保守保留
            self.indexes[sid] = idx
        return self.indexes[sid]

    def classify_file(self, sid, events):
        """对单个会话文件的 usage 事件做血统判定（权威实现）。

        返回 {seq: (is_replay, replay_reason, replay_source_session, status)}；
        status ∈ replay / own / broken / ambiguous；未列出的 seq 一律
        (0, None, None, 'own')。无父/无 seedLength 的文件返回 {}（文件级
        状态由调用方按 no_parent / subagent_no_seed 归类）。
        """
        h = self.header(sid)
        parent = h.get('parentSession')
        sl = h.get('seedLength')
        if not parent or sl is None:
            return {}       # 无父（原始）或 subagent（独立工作）→ 全部保守 0
        chain = []
        seen = {sid}
        cur = parent
        while cur and cur in self.files and cur not in seen:
            chain.append(cur)
            seen.add(cur)
            cur = self.header(cur).get('parentSession')
        out = {}
        for ev in events:
            try:
                seq = int(str(ev['event_id'])[3:])
            except (ValueError, TypeError, KeyError):
                continue
            if seq > sl:
                out[seq] = (0, None, None, 'own')       # 子会话自有工作
                continue
            fp = dsh_fp(ev)
            verified = None
            verified_hop = -1
            ambiguous = False
            for hop, anc in enumerate(chain):
                idx = self.index(anc)
                if seq in idx:
                    if idx[seq] == fp:
                        verified, verified_hop = anc, hop
                    else:
                        ambiguous = True        # 同树同 seq 异指纹 → 不判定
                    break
            if ambiguous:
                out[seq] = (0, None, None, 'ambiguous')
            elif verified is None:
                out[seq] = (0, None, None, 'broken')
            else:
                out[seq] = (1,
                            DSH_REASON_EXACT if verified_hop == 0
                            else DSH_REASON_ANCESTOR,
                            verified, 'replay')
        return out


ACTIVITY_UPSERT = """
INSERT INTO activity_event (
    client, session_key, event_id, ts_ms, kind, source_file, first_seen, last_seen)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(client, session_key, event_id) DO UPDATE SET
    ts_ms = excluded.ts_ms,
    kind = excluded.kind,
    source_file = excluded.source_file,
    last_seen = excluded.last_seen
"""


# ---------------------------------------------------------------- 跨进程扫描锁
# Round 7：CLI scan / scheduled scan / API scan 三条写入路径共享同一把
# 文件锁（O_CREAT|O_EXCL 原子创建），杜绝两个 writer 并发进入 cmd_scan。
# 锁内容只含 PID / 时间 / trigger —— 不含任何路径或用户数据。
# stale 判定：PID 已死 → 回收自愈；PID 存活（含权限无法判定）→ 保守拒绝。

SCAN_LOCK_PATH = os.path.join(BASE, 'usage-ledger-scan.lock')
SCAN_LOCK_STALE_NOTE = ('删除 %s 后重试，或确认持有进程（见锁内 pid）已退出' %
                        os.path.basename(SCAN_LOCK_PATH))


class ScanLockBusy(Exception):
    """扫描锁被其它进程持有时抛出；info 为锁文件内容。"""

    def __init__(self, info):
        super().__init__('scan lock busy')
        self.info = info or {}


def _read_lock_info():
    try:
        with open(SCAN_LOCK_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pid_alive(pid):
    """PID 存活检查（仅标准库）。Windows 走 OpenProcess —— 禁用 os.kill(pid, 0)
    （在 Windows 上语义危险）。权限不足导致无法判定时保守视为存活。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return k32.GetLastError() == ERROR_ACCESS_DENIED   # 无法判定 → 保守
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def scan_lock_status():
    """锁现状（供 API / 诊断）。locked=False 表示无锁。"""
    info = _read_lock_info()
    if not info:
        return {'locked': False}
    return {'locked': True, 'pid': info.get('pid'),
            'trigger': info.get('trigger'), 'started_at': info.get('started_at'),
            'holder_alive': _pid_alive(info.get('pid'))}


_SCAN_THREAD_LOCK = threading.Lock()
_SCAN_REENTRANCY = {'count': 0}      # 本进程内的锁重入深度（serve→run_once→cmd_scan 链）


def acquire_scan_lock(trigger='unknown'):
    """原子获取扫描锁；成功返回 handle，被其它进程占用抛 ScanLockBusy。

    同 PID 重入：进程内引用计数（serve→autopilot.run_once→ledger.cmd_scan
    的编排链安全）；跨线程并发由调用方（serve 的 SCAN_LOCK）顺序化。
    """
    with _SCAN_THREAD_LOCK:
        if _SCAN_REENTRANCY['count'] > 0:
            _SCAN_REENTRANCY['count'] += 1
            return {'pid': os.getpid(), 'trigger': trigger, 'reentrant': True}
        last = {}
        for _ in range(2):                   # stale 回收后重试一次
            try:
                fd = os.open(SCAN_LOCK_PATH,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                info = {'pid': os.getpid(), 'started_at': now_iso(),
                        'trigger': trigger}
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(info, f)
                _SCAN_REENTRANCY['count'] = 1
                return dict(info, reentrant=False)
            except FileExistsError:
                last = _read_lock_info()
                if last.get('pid') == os.getpid():
                    # 本进程持有但计数归零（异常路径残留）→ 重建计数
                    _SCAN_REENTRANCY['count'] = 1
                    return dict(last, reentrant=True, trigger=trigger)
                if _pid_alive(last.get('pid')):
                    raise ScanLockBusy(last)
                try:
                    os.unlink(SCAN_LOCK_PATH)   # 进程已死：stale 自愈
                except OSError:
                    pass
        raise ScanLockBusy(last or {})


def release_scan_lock(handle):
    """释放本进程持有的锁：引用计数归零才真正删除文件。"""
    with _SCAN_THREAD_LOCK:
        if not handle:
            return
        if _SCAN_REENTRANCY['count'] > 0:
            _SCAN_REENTRANCY['count'] -= 1
            if _SCAN_REENTRANCY['count'] == 0:
                info = _read_lock_info()
                if info and info.get('pid') == os.getpid():
                    try:
                        os.unlink(SCAN_LOCK_PATH)
                    except OSError:
                        pass
            return
        # 未走 acquire 计数的直接释放：仅当锁确实属于本进程才删除
        info = _read_lock_info()
        if info and info.get('pid') == os.getpid():
            try:
                os.unlink(SCAN_LOCK_PATH)
            except OSError:
                pass


# ---------------------------------------------------------------- scan

UPSERT = """
INSERT INTO usage_event (
    client, session_id, event_id, ts_ms, model, provider,
    input_tokens, output_tokens, reasoning_tokens,
    cache_read_tokens, cache_write_tokens, source_file, first_seen, last_seen,
    is_replay, replay_reason, replay_source_session, project_key)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(client, session_id, event_id) DO UPDATE SET
    ts_ms = excluded.ts_ms,
    model = excluded.model,
    provider = excluded.provider,
    input_tokens = excluded.input_tokens,
    output_tokens = excluded.output_tokens,
    reasoning_tokens = excluded.reasoning_tokens,
    cache_read_tokens = excluded.cache_read_tokens,
    cache_write_tokens = excluded.cache_write_tokens,
    source_file = excluded.source_file,
    last_seen = excluded.last_seen,
    is_replay = excluded.is_replay,
    replay_reason = excluded.replay_reason,
    replay_source_session = excluded.replay_source_session,
    -- MANUAL USER ATTRIBUTION > AUTOMATIC ADAPTER ATTRIBUTION：
    --   手工 Move/Merge 过的行（project_key_manual=1）永不被重扫覆盖；
    --   adapter 本轮派生值仍记入 project_key_auto（原始归因可追溯）。
    --   未手工处理时：归因只增不丢（读不出时保留既有值）。
    project_key = CASE WHEN usage_event.project_key_manual = 1
                       THEN usage_event.project_key
                       ELSE COALESCE(excluded.project_key,
                                     usage_event.project_key) END,
    project_key_auto = CASE WHEN usage_event.project_key_manual = 1
                            THEN COALESCE(excluded.project_key,
                                          usage_event.project_key_auto)
                            ELSE excluded.project_key END
"""

PROJECT_UPSERT = """
INSERT INTO project_registry (
    project_key, project_kind, display_name, first_seen_at, last_seen_at)
VALUES (?,?,?,?,?)
ON CONFLICT(project_key) DO UPDATE SET
    project_kind = excluded.project_kind,
    -- 手工改名（display_name_manual=1）优先；自动识别名只在用户未改名时生效
    display_name = CASE WHEN project_registry.display_name_manual = 1
                        THEN project_registry.display_name
                        ELSE COALESCE(NULLIF(excluded.display_name, ''),
                                      project_registry.display_name) END,
    last_seen_at = excluded.last_seen_at,
    first_seen_at = CASE
        WHEN project_registry.first_seen_at IS NULL
             OR project_registry.first_seen_at > excluded.first_seen_at
        THEN excluded.first_seen_at
        ELSE project_registry.first_seen_at END
"""

SESSION_UPSERT = """
INSERT INTO session_registry (
    source, session_id, project_key, display_name, first_seen_at, last_seen_at, agent)
VALUES (?,?,?,?,?,?,?)
ON CONFLICT(source, session_id) DO UPDATE SET
    -- 手工归因（attribution_manual=1）优先；自动归因只在未手工指定时生效
    project_key = CASE WHEN session_registry.attribution_manual = 1
                       THEN session_registry.project_key
                       ELSE COALESCE(excluded.project_key,
                                     session_registry.project_key) END,
    display_name = COALESCE(excluded.display_name, session_registry.display_name),
    agent = COALESCE(excluded.agent, session_registry.agent),
    last_seen_at = excluded.last_seen_at,
    first_seen_at = CASE
        WHEN session_registry.first_seen_at IS NULL
             OR session_registry.first_seen_at > excluded.first_seen_at
        THEN excluded.first_seen_at
        ELSE session_registry.first_seen_at END
"""


def resolve_merged_key(conn, key, _depth=0):
    """跟随 merge 留痕：adapter 再次派生已合并的旧 key 时，改写到目标项目。
    防止 merge 后的新事件重新产生重复项目归属。带回环保护。"""
    seen = set()
    while key:
        if key in seen:
            raise ValueError('项目合并关系存在循环')
        seen.add(key)
        row = conn.execute(
            'SELECT metadata_json FROM project_registry WHERE project_key=?',
            (key,)).fetchone()
        if row is None or not row[0]:
            return key
        try:
            meta = json.loads(row[0])
        except Exception:
            return key
        nxt = (meta or {}).get('merged_into')
        if not nxt:
            return key
        key = nxt
    return key


def _upsert_project_and_session(conn, ev, ts):
    """把事件携带的项目/会话归因写入注册表（缺失键静默跳过，不猜测）。
    已被用户 Merge 掉的 key 会在入口处改写到合并目标。"""
    auto = ev.get('project_key')
    sid = ev.get('session_id') or ev.get('session_key') or ''
    session = conn.execute(
        'SELECT project_key, attribution_manual FROM session_registry'
        ' WHERE source=? AND session_id=?', (ev['client'], sid)).fetchone()
    manual = bool(session and session['attribution_manual'])
    pk = resolve_merged_key(conn, session['project_key'] if manual else auto)
    # Called immediately after the event UPSERT in the real scan path.
    # New events inherit the user's session decision; auto remains provenance.
    if ev.get('session_id') and ev.get('event_id') is not None:
        conn.execute(
            'UPDATE usage_event SET project_key_auto=COALESCE(?,project_key_auto),'
            ' project_key=CASE WHEN project_key_manual=1 THEN project_key'
            ' ELSE COALESCE(?,project_key) END,'
            ' project_key_manual=CASE WHEN ? THEN 1 ELSE project_key_manual END'
            ' WHERE client=? AND session_id=? AND event_id=?',
            (auto, pk, manual or (auto is not None and pk != auto),
             ev['client'], sid, ev['event_id']))
    if not pk:
        return
    if manual or pk != auto:
        conn.execute('UPDATE project_registry SET last_seen_at=? WHERE project_key=?', (ts, pk))
    else:
        conn.execute(PROJECT_UPSERT, (
            pk, ev.get('project_kind') or PROJECT_KIND_UNKNOWN,
            ev.get('project_display') or pk, ts, ts))
    conn.execute(SESSION_UPSERT, (
        ev['client'], sid,
        pk, ev.get('session_title'), ts, ts, ev.get('agent')))


def cmd_scan(args):
    """扫描入口（CLI / scheduled / API 共用）。

    Round 7：跨进程扫描锁 —— 任何时刻只允许一个 writer 进入采集。
    锁被其它进程持有时：明确跳过（partial），绝不排队等待、绝不覆盖写。
    """
    try:
        lock = acquire_scan_lock(trigger='cli')
    except ScanLockBusy as e:
        info = e.info or {}
        print()
        print('  ⚠ 已有扫描正在进行（PID %s，trigger=%s，自 %s）—— 本次跳过，未采集。'
              % (info.get('pid'), info.get('trigger'), info.get('started_at')))
        print('    如确认持有进程已退出：%s' % SCAN_LOCK_STALE_NOTE)
        LAST_SCAN_STATS.clear()
        LAST_SCAN_STATS.update({'scan_skipped_lock': True,
                                'lock_holder': {'pid': info.get('pid'),
                                                'trigger': info.get('trigger'),
                                                'started_at': info.get('started_at')}})
        return EXIT_PARTIAL
    try:
        return _cmd_scan_locked(args)
    finally:
        release_scan_lock(lock)


def _cmd_scan_locked(args):
    conn = connect(create=True)
    migrate_schema(conn)
    migrate_source_paths(conn)
    ts = now_iso()
    cur = conn.execute('INSERT INTO scan_run (started_at) VALUES (?)', (ts,))
    run_id = cur.lastrowid

    total_seen = total_ins = total_upd = n_files = n_skip = 0
    n_sources = 0
    n_ok = n_failed = 0
    n_replay = 0
    n_reobserved_cross_file = 0   # 同一逻辑事件在其它物理文件被再次观察到
    source_errors = []          # [(client, file, error)] —— 必须一路传到 run-state/auto.log
    lineage = None              # dsh 血统上下文（惰性创建，仅 dsh 分支使用）

    for client, spec in SOURCES.items():
        parser = PARSE_FILE[client]
        files = list_files(client)

        known = {}
        for row in conn.execute(
                'SELECT path, size, mtime, last_error FROM source_file WHERE client = ?',
                (client,)):
            known[norm_path(row['path'])] = {
                'size': row['size'], 'mtime': row['mtime'], 'err': row['last_error']}

        if not files:
            # 源文件全没了：必须把此前见过的文件标记为已清退，否则留存状态失真。
            # 这一段以前放在「有文件」的分支里，导致客户端一清空历史就再也标不上 missing_since。
            marked = 0
            for key in known:
                cur = conn.execute(
                    'SELECT missing_since FROM source_file WHERE client=? AND path=?',
                    (client, key)).fetchone()
                if cur and not cur['missing_since']:
                    marked += 1
                conn.execute("""
                    UPDATE source_file SET missing_since = COALESCE(missing_since, ?)
                    WHERE client = ? AND path = ?
                """, (ts, client, key))
            conn.commit()
            if known:
                print('  [%-9s] 源文件已全部清退（%d 个，其中 %d 个本次新标记）—— 账本记录保留'
                      % (client, len(known), marked))
            else:
                print('  [%-9s] 跳过：没找到源文件  %s' % (client, redact_path(spec['path'])))
                if spec.get('hint'):
                    print('             提示：%s' % spec['hint'])
            continue
        n_sources += 1

        n_events = inserted = updated = 0
        src_failed = False
        failed_files = {}
        is_activity = spec.get('mode') == 'activity'
        for fp in files:
            key = norm_path(fp)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            prev = known.get(key) or {}
            unchanged = (prev.get('size') == int(st.st_size)
                         and prev.get('mtime') == float(st.st_mtime))
            # 上次读失败过的文件永远不跳过 —— 否则失败会被"未变化"永久掩盖
            if not args.full and unchanged and not prev.get('err'):
                n_skip += 1          # 文件未变，整文件跳过，连解压都不做
                continue
            if fp.endswith('.zstd') and st.st_size == 0:
                continue
            is_activity = spec.get('mode') == 'activity'
            try:
                file_events = list(parser(fp))
                replay_map = {}
                if client == 'dsh' and not is_activity:
                    # 血统重放判定（Round 4）：祖先文件按需只读解析，
                    # 不依赖"本轮父文件必须被重新扫描"。
                    if lineage is None:
                        lineage = DshLineage()
                        lineage.load(files)
                    sid = os.path.basename(os.path.dirname(fp)) or os.path.basename(fp)
                    replay_map = lineage.classify_file(sid, file_events)
                for ev in file_events:
                    n_events += 1
                    total_seen += 1
                    if is_activity:
                        had = conn.execute(
                            'SELECT 1 FROM activity_event WHERE client=? AND session_key=? AND event_id=?',
                            (ev['client'], ev['session_key'], ev['event_id'])).fetchone()
                        conn.execute(ACTIVITY_UPSERT, (
                            ev['client'], ev['session_key'], ev['event_id'], ev['ts_ms'],
                            ev.get('kind', 'message'), ev.get('source_file', ''), ts, ts))
                        _upsert_project_and_session(conn, ev, ts)
                        if had:
                            updated += 1
                            total_upd += 1
                        else:
                            inserted += 1
                            total_ins += 1
                        continue
                    # Provenance（最小实现）：同一逻辑事件从**不同物理文件**
                    # 再次观察到 → 计数留痕（source_file 由 UPSERT 更新为
                    # 最新观察位；不新增行、不重复入账）。
                    before = conn.execute(
                        'SELECT source_file FROM usage_event WHERE client=? AND session_id=? AND event_id=?',
                        (ev['client'], ev['session_id'], ev['event_id'])).fetchone()
                    prev_source_file = before['source_file'] if before else None
                    ir, ir_reason, ir_src = (0, None, None)
                    if replay_map:
                        try:
                            v = replay_map.get(int(str(ev['event_id'])[3:]))
                            if v:
                                ir, ir_reason, ir_src = v[0], v[1], v[2]
                        except (ValueError, TypeError):
                            pass
                    if ir:
                        n_replay += 1
                    conn.execute(UPSERT, (
                        ev['client'], ev['session_id'], ev['event_id'], ev['ts_ms'],
                        ev['model'], ev['provider'],
                        ev['input_tokens'], ev['output_tokens'], ev['reasoning_tokens'],
                        ev['cache_read_tokens'], ev['cache_write_tokens'],
                        key, ts, ts,
                        ir, ir_reason, ir_src,
                        ev.get('project_key')))
                    _upsert_project_and_session(conn, ev, ts)
                    if before:
                        updated += 1
                        total_upd += 1
                        if prev_source_file and \
                                norm_path(prev_source_file) != key:
                            n_reobserved_cross_file += 1
                    else:
                        inserted += 1
                        total_ins += 1
            except Exception as e:
                # 读不出来 ≠ 可以当作没看见。记下来，让整次运行降级为 partial/failed。
                src_failed = True
                failed_files[key] = '%s: %s' % (type(e).__name__, str(e)[:240])
                msg = '%s：%s' % (os.path.basename(os.path.dirname(fp)) or fp, e)
                source_errors.append(('%s' % client, fp, str(e)))
                print('  ! [%-9s] 读取失败：%s' % (client, msg))

        # 记录源文件状态：出现过的更新 last_seen；此前见过但这次没出现的标记 missing_since
        seen = {norm_path(f) for f in files}
        for fp in files:
            key = norm_path(fp)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            conn.execute("""
                INSERT INTO source_file (client, path, size, mtime, events, first_seen, last_seen, missing_since, last_error)
                VALUES (?,?,?,?,?,?,?,NULL,?)
                ON CONFLICT(client, path) DO UPDATE SET
                    size = excluded.size, mtime = excluded.mtime,
                    last_seen = excluded.last_seen, missing_since = NULL,
                    last_error = excluded.last_error
            """, (client, key, int(st.st_size), float(st.st_mtime), 0, ts, ts,
                  failed_files.get(key)))
        for key in known:
            if key not in seen:
                conn.execute("""
                    UPDATE source_file SET missing_since = COALESCE(missing_since, ?)
                    WHERE client = ? AND path = ?
                """, (ts, client, key))

        n_files += len(files)
        if src_failed:
            n_failed += 1
        else:
            n_ok += 1
        print('  [%-9s] %-22s 文件 %3d  %s %6d  新增 %5d  更新 %5d%s'
              % (client, spec['label'], len(files),
                 '活动量' if is_activity else '用量  ', n_events, inserted, updated,
                 '   ⚠ 读取失败' if src_failed else ''))
        if n_replay and client == 'dsh':
            print('             其中血统重放标记 %d 条（raw 保留，聚合默认剔除）' % n_replay)
        conn.commit()

    conn.execute("""
        UPDATE scan_run SET finished_at=?, events_seen=?, events_inserted=?,
               events_updated=?, files_scanned=?, files_skipped=? WHERE id=?
    """, (now_iso(), total_seen, total_ins, total_upd, n_files, n_skip, run_id))
    conn.commit()

    rows = conn.execute('SELECT COUNT(*) c FROM usage_event').fetchone()['c']
    print()
    print('  本次扫描：事件 %d（新增 %d / 更新 %d）  整文件跳过 %d 个'
          % (total_seen, total_ins, total_upd, n_skip))
    print('  账本累计事件：%d 条' % rows)
    print('  账本位置：%s' % DB_PATH)
    conn.close()

    # 暴露给编排层（autopilot 要把这些写进 run-state.json，让后台任务可诊断）
    LAST_SCAN_STATS.clear()
    LAST_SCAN_STATS.update({
        'sources_scanned': n_sources,
        'sources_ok': n_ok,
        'sources_failed': n_failed,
        'source_error_count': len(source_errors),
        'source_errors': ['%s | %s | %s' % (c, os.path.basename(f), e)
                          for c, f, e in source_errors[:20]],
        'files_scanned': n_files,
        'files_skipped': n_skip,
        'events_seen': total_seen,
        'events_inserted': total_ins,
        'events_updated': total_upd,
        'replay_marked': n_replay,
        'events_reobserved_cross_file': n_reobserved_cross_file,
        'ledger_usage_events': rows,
    })

    if n_sources == 0:
        print()
        print('  ⚠ 未发现任何受支持数据源（0 / %d）。' % len(SOURCES))
        print('    先跑 `python ledger.py discover` 看候选探测结果，或在 sources.json 里指定路径。')
        return EXIT_NO_SOURCES

    # 有源读不出来 → 绝不当成成功。全部源都读不出来 → 等价于"没有可用数据源"。
    if n_failed and not n_ok:
        print()
        print('  ⚠ 所有已发现的数据源都读取失败（%d 个）—— 等价于没有可用数据源。' % n_failed)
        for c, f, e in source_errors[:5]:
            print('    ! [%s] %s：%s' % (c, os.path.basename(f), e))
        return EXIT_NO_SOURCES
    if n_failed:
        print()
        print('  ⚠ 有 %d / %d 个数据源读取失败（其余 %d 个正常采集）—— 判定为部分失败。'
              % (n_failed, n_sources, n_ok))
        for c, f, e in source_errors[:5]:
            print('    ! [%s] %s：%s' % (c, os.path.basename(f), e))
        return EXIT_PARTIAL
    return EXIT_OK


# ---------------------------------------------------------------- report

BY = {
    'day':      ('date(ts_ms/1000, "unixepoch", "localtime")', '日期'),
    'model':    ('model', '模型'),
    'client':   ('client', '客户端'),
    'provider': ('provider', '提供方'),
}


def _read_pricing_file(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    for k, v in raw.items():
        if k.startswith('_') or not isinstance(v, dict):
            continue
        # 定价匹配不区分大小写，但候选清单要能让用户**原样复制**键名，
        # 所以把原始大小写留在 _key 里。
        entry = dict(v, _key=k.strip())
        # Round 8-C 币种：缺失 -> USD（legacy 兼容规则，仅适用于既有数据）；
        # 不支持的币种 -> 明确跳过，绝不静默当 USD。
        cur = _entry_currency(entry)
        if cur is None:
            print('  ! %s：模型 %s 的 currency 不受支持（%s）—— 条目已跳过'
                  % (os.path.basename(path), k, entry.get('currency')),
                  file=sys.stderr)
            continue
        entry['currency'] = cur
        key_lc = k.strip().lower()
        prev = out.get(key_lc)
        if prev is not None:
            # 大小写变体键碰撞（如 GLM-5.3-Flash 与 glm-5.3-flash）：
            # 1) entry 自身无正数单价 -> 整条继承 prev（含币种与元数据）
            # 2) 双方都有正数单价且币种冲突 -> 保留先到者并大声告警
            # 3) 否则 null 字段从 prev 补全（null 不覆盖有值）
            entry_own_has = any(isinstance(entry.get(f), (int, float))
                                and entry[f] > 0
                                for f in PRICE_FIELDS)
            for f in PRICE_FIELDS:
                if entry.get(f) is None and prev.get(f) is not None:
                    entry[f] = prev[f]
            if not entry_own_has and prev.get('currency'):
                entry['currency'] = prev['currency']
            prev_has = any(isinstance(prev.get(f), (int, float)) and prev[f] > 0
                           for f in PRICE_FIELDS)
            cur_has = any(isinstance(entry.get(f), (int, float)) and entry[f] > 0
                          for f in PRICE_FIELDS)
            if (prev.get('currency') != entry.get('currency')
                    and prev_has and cur_has):
                print('  ! %s：大小写变体 %s 币种冲突（%s vs %s）—— '
                      '保留先到者，请人工核对 pricing 文件'
                      % (os.path.basename(path), k, entry.get('currency'),
                         prev.get('currency')), file=sys.stderr)
                entry['currency'] = prev['currency']
                for f in PRICE_FIELDS:
                    entry[f] = prev.get(f)
        out[key_lc] = entry
    return out


SUPPORTED_CURRENCIES = ('USD', 'CNY')
DEFAULT_CURRENCY = 'USD'          # legacy 兼容：无 currency 字段的既有数据


def _entry_currency(entry):
    """解析条目币种。缺失 -> USD（legacy 规则）；不支持 -> None（调用方跳过）。"""
    cur = (entry.get('currency') or '').strip().upper()
    if not cur:
        return DEFAULT_CURRENCY
    return cur if cur in SUPPORTED_CURRENCIES else None


def load_pricing():
    """优先级：pricing.json（手工覆盖） > pricing.auto.json（自动同步），按字段合并。"""
    return load_pricing_with_sources()[0]


def load_pricing_with_sources():
    """同 load_pricing，并返回每个模型的定价来源：
    'manual_verified'（人工确认）/ 'auto_reference'（目录候选，未人工确认）/
    'unpriced'（手工模板无价）。"""
    auto = _read_pricing_file(PRICING_AUTO_PATH)
    manual = _read_pricing_file(PRICING_PATH)
    merged = {k: dict(v) for k, v in auto.items()}
    sources = {k: 'auto_reference' for k in merged}
    for k, v in manual.items():
        contributed = any(float(v.get(f) or 0) > 0 for f in PRICE_FIELDS)             or bool(v.get('verified_zero'))
        merged[k] = _merge_price_entry(merged.get(k), v)
        # 来源语义（Round 8-C）：只有手工条目**实际写入过正数单价或验证零价**
        # 才算 manual_verified；仅存在全 null 模板的键仍按 auto_reference。
        if contributed:
            sources[k] = 'manual_verified'
        elif k not in sources:
            sources[k] = 'unpriced'
    return merged, sources


def _merge_price_entry(base, over):
    """字段级合并：只让手工文件里**真正填了正数**的字段覆盖自动价格。

    如果整条覆盖，pricing-template 生成的全 null 手工条目会把自动同步到的价格抹掉。
    Round 8：人工证据确认的 verified_zero 标记同样由手工层带入。
    """
    out = dict(base or {})
    for f in ('input', 'output', 'cache_read', 'cache_write'):
        try:
            v = float((over or {}).get(f))
        except (TypeError, ValueError):
            continue
        if v > 0:
            out[f] = v
    if (over or {}).get('verified_zero'):
        out['verified_zero'] = True
    # Round 8-C：元数据（currency/unit/evidence_id/verified_at…）随手工层透传——
    # 手工条目是事实源，币种语义不允许在合并时丢失。base 的元数据保留为缺省。
    for k2, v2 in (over or {}).items():
        if k2.startswith('_') or k2 in ('input', 'output', 'cache_read',
                                        'cache_write', 'verified_zero'):
            continue
        out[k2] = v2
    return out


def pricing_meta():
    """价格来源元数据，进 board 的 sources / summary，便于说明估算依据。"""
    meta = {'manual': os.path.isfile(PRICING_PATH),
            'auto': os.path.isfile(PRICING_AUTO_PATH)}
    if os.path.isfile(PRICING_AUTO_PATH):
        try:
            with open(PRICING_AUTO_PATH, encoding='utf-8') as f:
                raw = json.load(f)
            m = raw.get('_meta') or {}
            meta.update({'source': m.get('source'), 'source_id': m.get('source_id'),
                         'retrieved_at': m.get('retrieved_at'),
                         'models': len([k for k in raw if not k.startswith('_')])})
        except Exception:
            pass
    return meta


def cost_of(pricing, model, i, o, cr, cw):
    """返回 (成本, 是否有可用单价)。单价单位：美元 / 百万 token。

    只有至少存在一个**正数**单价才算「已配置」。pricing.json 模板里全是 null，
    若不这样判定就会把「没匹配到价格」算成真实成本 $0——规格明确禁止。

    Round 8：`verified_zero` 例外 —— 模型经官方证据确认免费（free /
    zero-priced tier / 用户合同价 0）时，人工在条目里写 "verified_zero": true，
    视为已配置、成本 0。没有该标记的 0/全 null 仍然 = 未配置。
    """
    p = pricing.get((model or '').strip().lower())
    if not p:
        return 0.0, False
    if p.get('verified_zero'):
        return 0.0, True

    def rate(name):
        try:
            f = float(p.get(name))
        except (TypeError, ValueError):
            return 0.0
        return f / 1_000_000 if f > 0 else 0.0

    r_in, r_out = rate('input'), rate('output')
    r_cr, r_cw = rate('cache_read'), rate('cache_write')
    if not (r_in or r_out or r_cr or r_cw):
        return 0.0, False
    return (i * r_in + o * r_out + cr * r_cr + cw * r_cw), True


def cost_of_with_currency(pricing, model, i, o, cr, cw, source=None):
    """Round 8-C：cost_of 的币种感知包装层（底层数学不变）。

    返回 {'amount', 'currency', 'priced', 'source'}：
      amount    估算金额（币种见 currency 字段；不同币种绝不 sum）
      currency  条目币种（缺失 → USD legacy 规则）
      priced    是否已配置单价
      source    'manual_verified' | 'auto_reference' | None（调用方未传时）
    同一模型的一组 input/output/cache rate 共享条目级单一币种；
    跨币种混合属于配置错误，由 _read_pricing_file 在加载期拒绝。
    """
    amount, priced = cost_of(pricing, model, i, o, cr, cw)
    entry = pricing.get((model or '').strip().lower()) or {}
    cur = entry.get('currency') or DEFAULT_CURRENCY
    units = {'input': i, 'output': o, 'cache_read': cr, 'cache_write': cw}
    def known(name):
        if entry.get('verified_zero'):
            return True
        try:
            return float(entry.get(name)) > 0
        except (TypeError, ValueError):
            return False
    missing = [name for name, count in units.items() if count and not known(name)]
    # A rate for an unused component cannot make unknown usage look free.
    available = priced and (not any(units.values()) or any(
        count and known(name) for name, count in units.items()))
    return {'amount': amount if available else 0.0, 'currency': cur,
            'priced': priced, 'source': source, 'partial': priced and bool(missing),
            'available': available,
            'missing_components': missing,
            'covered_io_tokens': (i if known('input') else 0) + (o if known('output') else 0)}


# ---------------------------------------------------------------- 价格溯源与四级回退链（V1.1）
# 冻结优先级：USER_CHANNEL > OFFICIAL > THIRD_PARTY_REFERENCE > UNAVAILABLE。
#
# 产品承诺（每个显示出来的价格都要能说明）：
#   从哪里来 / 是谁的价格 / 是否适用于当前 provider+model /
#   官方·用户·第三方 / 什么时候取得 / 只是 API 等价值估算。
# 官方价必须 provider + model 双重可靠对应才允许使用；provider 身份
# 不可知（如路由模式串 'Auto' / UUID）时绝不绑定官方价。
# 无可靠价格 = null（UI：暂无可靠价格），绝不 0 / 免费 / 猜测。
# token × API 价格永远只是 API 等价值估算；actual_spend 独立，
# 没有账单 / 订阅 / 充值 / 人工录入数据时 = unknown，不得由 token 反推。

PT_USER_CHANNEL = 'user_channel'
PT_OFFICIAL = 'official'
PT_THIRD_PARTY_REFERENCE = 'third_party_reference'
PT_UNAVAILABLE = 'unavailable'
PRICING_TYPES = (PT_USER_CHANNEL, PT_OFFICIAL, PT_THIRD_PARTY_REFERENCE,
                 PT_UNAVAILABLE)
RELIABLE_PRICING_TYPES = (PT_USER_CHANNEL, PT_OFFICIAL)

# 官方价绑定的 provider 身份别名表：usage provider 与价格条目 provider
# 都要归一化到同一身份才允许匹配。不在这个表（或条目 provider_aliases）
# 里的 provider 值（如 'Auto'、UUID、路由模式串）→ 身份不可知 → 绝不
# 绑定官方价。
PROVIDER_ALIASES = {
    'openai': 'openai',
    'deepseek': 'deepseek',
    'zhipu': 'zhipu',
    'bigmodel': 'zhipu',
    'zai': 'zhipu',
    'zai-standard-api': 'zhipu',
    'anthropic': 'anthropic',
    'moonshot': 'moonshot',
    'kimi': 'moonshot',
    'minimax': 'minimax',
    'qwen': 'qwen',
    'alibaba': 'qwen',
    'bailian': 'qwen',
    'doubao': 'volcengine',
    'volcengine': 'volcengine',
    'siliconflow': 'siliconflow',
}


def _provider_identity(provider):
    """provider 值 → 可靠身份；不可识别 → None（绝不猜）。"""
    p = (provider or '').strip().lower()
    if not p:
        return None
    if p in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[p]
    # 条目 / 用量侧都可声明 'provider_aliases'（显式别名，不做模糊包含）
    return None


_PRICING_LAYERS_CACHE = {'key': None, 'layers': None}


def _pricing_layers_cache_key(user_path, auto_path):
    user_path = user_path or PRICING_PATH
    auto_path = auto_path or PRICING_AUTO_PATH
    stat = lambda p: (os.path.getmtime(p), os.path.getsize(p))         if os.path.isfile(p) else None
    return (user_path, auto_path, stat(user_path), stat(auto_path))


def load_pricing_layers(user_path=None, auto_path=None):
    """带 mtime 失效的进程内缓存：定价文件未变化时不再重复读盘/解析
    （每次 /api/v1/query 都会解析两层定价文件，实测 ~12ms/次）。
    文件任何变化（含 pricing sync 写入）→ 自动失效重读。语义不变。"""
    key = _pricing_layers_cache_key(user_path, auto_path)
    if _PRICING_LAYERS_CACHE['key'] == key and             _PRICING_LAYERS_CACHE['layers'] is not None:
        return _PRICING_LAYERS_CACHE['layers']
    layers = _load_pricing_layers_uncached(user_path, auto_path)
    _PRICING_LAYERS_CACHE['key'] = key
    _PRICING_LAYERS_CACHE['layers'] = layers
    return layers


def _load_pricing_layers_uncached(user_path=None, auto_path=None):
    """按定价类型分层加载（V1.1 溯源契约）。

    返回 {'user': {...}, 'official': {...}, 'reference': {...},
          'ref_meta': {...}}：
      user     pricing.json 中实际写入过正数单价（或 verified_zero）的
               条目 → user_channel（用户也可显式声明 pricing_type，
               'official' + provider 字段的条目进 official 层）。
      official pricing.json 中 pricing_type='official' 且带 provider
               字段的条目 → official；applicability=exact。
      reference pricing.auto.json 全部条目 → third_party_reference，
               applicability=reference_only，source/source_id/retrieved_at
               取自 _meta；多来源按字段合并时先到者优先
               （litellm → openrouter，确定性顺序，绝不随机选）。
    """
    user_path = user_path or PRICING_PATH
    auto_path = auto_path or PRICING_AUTO_PATH
    manual = _read_pricing_file(user_path)
    user, official = {}, {}
    for key, entry in manual.items():
        contributed = any(float(entry.get(f) or 0) > 0 for f in PRICE_FIELDS) \
            or bool(entry.get('verified_zero'))
        if not contributed:
            continue
        ptype = str(entry.get('pricing_type') or PT_USER_CHANNEL).strip()
        if ptype == PT_OFFICIAL:
            if not entry.get('provider'):
                # 官方价必须声明 provider 身份；缺失 → 降级为用户价，
                # 绝不静默冒充官方。
                entry = dict(entry, _provenance_note='official 条目缺 provider，按用户渠道价处理')
                user[key] = entry
            else:
                official[key] = entry
        else:
            user[key] = entry
    auto_raw = {}
    ref_meta = {}
    if os.path.isfile(auto_path):
        try:
            with open(auto_path, encoding='utf-8') as f:
                raw = json.load(f)
            ref_meta = dict(raw.get('_meta') or {})
            auto_raw = {k: v for k, v in raw.items()
                        if not k.startswith('_') and isinstance(v, dict)}
        except Exception:
            auto_raw, ref_meta = {}, {}
    reference = {}
    for key, entry in auto_raw.items():
        e = dict(entry)
        e['_key'] = entry.get('_key', key)
        e['pricing_type'] = PT_THIRD_PARTY_REFERENCE
        e['source'] = ref_meta.get('source')
        e['source_id'] = ref_meta.get('source_id')
        e['retrieved_at'] = ref_meta.get('retrieved_at')
        e['applicability'] = 'reference_only'
        # 币种缺省：第三方目录价几乎都是 USD；显式声明则尊重声明。
        e.setdefault('currency', 'USD')
        reference[key.strip().lower()] = e
    return {'user': user, 'official': official, 'reference': reference,
            'ref_meta': ref_meta}


def resolve_price_record(layers, model, provider=None):
    """四级回退链：返回价格溯源记录或 None（UNAVAILABLE）。

    顺序（冻结）：user_channel → official → third_party_reference → None。
    溯源是**字段级**的：input / output / cache_read / cache_write 每个费率
    独立回退（用户价 → 官方价 → 第三方参考价），rate_sources 记录每个
    字段来自哪一层 —— 用户只填一个字段时不会丢掉其余费率，也不会让
    第三方费率伪装成用户渠道价。

    记录级语义：
      pricing_type      贡献费率的最高层（user_channel > official >
                        third_party_reference）
      applicability     所有费率都来自可靠层 = exact；任一费率来自
                        第三方 = reference_only（保守：混合记录不得
                        被呈现为纯官方/纯渠道价）
      has_reference_rates=True 的记录永远进不了 reliable 成本口径。

    - user_channel：模型键 exact（用户自己的渠道价，优先于一切）；
      条目声明了 provider 且用量侧 provider 身份可靠且不同 → 不适用。
    - official：条目 provider 与用量 provider 必须归一化到同一身份
      （PROVIDER_ALIASES / 条目 provider_aliases）；任一侧不可知 →
      整层不适用（绝不只因为模型名相同就套官方价）。
    - third_party_reference：模型键 exact；多来源在加载期确定性合并
      （litellm 先、openrouter 补缺），来源随记录透传 —— 绝不随机。
    """
    key = (model or '').strip().lower()
    if not key:
        return None
    usage_pid = _provider_identity(provider)

    u = layers.get('user', {}).get(key)
    if u is not None:
        entry_provider = u.get('provider')
        if entry_provider and usage_pid and \
                _provider_identity(entry_provider) != usage_pid:
            u = None                              # 用户价声明了别的渠道

    o = layers.get('official', {}).get(key)
    if o is not None:
        entry_pid = _provider_identity(o.get('provider'))
        entry_aliases = {_provider_identity(a)
                         for a in (o.get('provider_aliases') or [])}
        entry_aliases.discard(None)
        if usage_pid is None or not (
                entry_pid == usage_pid or usage_pid in entry_aliases):
            o = None                              # 官方价：provider 必须可靠对应

    r = layers.get('reference', {}).get(key)

    rates, rate_sources = {}, {}
    for field in PRICE_FIELDS:
        if u is not None and _entry_rate(u, field) is not None:
            rates[field], rate_sources[field] = _entry_rate(u, field), 'user'
        elif o is not None and _entry_rate(o, field) is not None:
            rates[field], rate_sources[field] = _entry_rate(o, field), 'official'
        elif r is not None and _entry_rate(r, field) is not None:
            rates[field], rate_sources[field] = _entry_rate(r, field), 'reference'
    if not any(v is not None for v in rates.values()):
        return None

    has_user = any(v == 'user' for v in rate_sources.values())
    has_official = any(v == 'official' for v in rate_sources.values())
    has_reference = any(v == 'reference' for v in rate_sources.values())
    if has_user:
        base = dict(u or {})
        ptype = PT_USER_CHANNEL
        src = 'user'
    elif has_official:
        base = dict(o or {})
        ptype = PT_OFFICIAL
        src = 'official'
    else:
        base = dict(r or {})
        ptype = PT_THIRD_PARTY_REFERENCE
        src = base.get('source')
    base.update(rates)
    base['pricing_type'] = ptype
    base['rate_sources'] = rate_sources
    base['has_reference_rates'] = has_reference
    base['applicability'] = 'exact' if not has_reference else 'reference_only'
    base.setdefault('source', src)
    base.setdefault('unit', 'per_1m_tokens')
    if ptype == PT_THIRD_PARTY_REFERENCE:
        base.setdefault('applicability', 'reference_only')
    return base


def _entry_rate(entry, field):
    try:
        v = float(entry.get(field))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def is_reliable_record(rec):
    """可靠价格口径：user_channel / official，且**没有任何费率字段**
    来自第三方参考（字段级溯源下混合记录保守归入参考口径）。"""
    return bool(rec) and rec.get('pricing_type') in RELIABLE_PRICING_TYPES \
        and not rec.get('has_reference_rates')


def record_cost(record, i, o, cr, cw):
    """按价格溯源记录计算 API 等价值估算（数学与 cost_of 一致）。

    返回 None 表示 UNAVAILABLE（调用方必须呈现「暂无可靠价格」，
    绝不折算 0 / 免费）。partial 语义同 cost_of_with_currency。
    """
    if not record:
        return None
    entry = record
    if record.get('verified_zero'):
        return {'amount': 0.0, 'currency': entry.get('currency') or DEFAULT_CURRENCY,
                'available': True, 'partial': False, 'missing_components': [],
                'covered_io_tokens': i + o, 'pricing_type': record.get('pricing_type'),
                'applicability': record.get('applicability'),
                'rate_sources': record.get('rate_sources'),
                'has_reference_rates': bool(record.get('has_reference_rates')),
                'source': record.get('source'), 'source_id': record.get('source_id'),
                'retrieved_at': record.get('retrieved_at'), 'unit': record.get('unit')}

    def rate(name):
        try:
            f = float(entry.get(name))
        except (TypeError, ValueError):
            return 0.0
        return f / 1_000_000 if f > 0 else 0.0

    r_in, r_out = rate('input'), rate('output')
    r_cr, r_cw = rate('cache_read'), rate('cache_write')
    if not (r_in or r_out or r_cr or r_cw):
        return None
    units = {'input': i, 'output': o, 'cache_read': cr, 'cache_write': cw}

    def known(name):
        try:
            return float(entry.get(name)) > 0
        except (TypeError, ValueError):
            return False
    missing = [name for name, count in units.items() if count and not known(name)]
    available = not any(units.values()) or any(
        count and known(name) for name, count in units.items())
    return {'amount': (i * r_in + o * r_out + cr * r_cr + cw * r_cw)
            if available else 0.0,
            'currency': entry.get('currency') or DEFAULT_CURRENCY,
            'available': available, 'partial': bool(missing),
            'missing_components': missing,
            'covered_io_tokens': (i if known('input') else 0)
            + (o if known('output') else 0),
            'pricing_type': record.get('pricing_type'),
            'applicability': record.get('applicability'),
            'rate_sources': record.get('rate_sources'),
            'has_reference_rates': bool(record.get('has_reference_rates')),
            'source': record.get('source'), 'source_id': record.get('source_id'),
            'retrieved_at': record.get('retrieved_at'),
            'unit': record.get('unit')}


def cmd_report(args):
    conn = connect()
    view = getattr(args, 'view', 'raw') or 'raw'
    # 口径：raw = 全部账本（默认，向后兼容）；effective = 剔除已验证血统重放。
    # 未定价模型在两种口径下都保持 null/未计入，绝不折算成 $0。
    view_filter = 'is_replay = 0' if view == 'effective' else ''
    expr, title = BY[args.by]
    conds = []
    params = []
    if args.days:
        cutoff = int((dt.datetime.now() - dt.timedelta(days=args.days)).timestamp() * 1000)
        conds.append('ts_ms >= ?')
        params.append(cutoff)
    if view_filter:
        conds.append(view_filter)
    where = ('WHERE ' + ' AND '.join(conds)) if conds else ''

    sql = """
        SELECT {e} AS k,
               COUNT(*) AS events,
               SUM(input_tokens) AS i,
               SUM(output_tokens) AS o,
               SUM(reasoning_tokens) AS r,
               SUM(cache_read_tokens) AS cr,
               SUM(cache_write_tokens) AS cw
        FROM usage_event {w}
        GROUP BY k ORDER BY k
    """.format(e=expr, w=where)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print('  账本里还没有数据，先跑 `python ledger.py scan`')
        return

    pricing, pricing_sources = load_pricing_with_sources()

    # 成本必须按模型算，所以单独取一次模型维度的合计（Round 8-C：币种感知，
    # 不同币种分别聚合，绝不相加）
    mrows = conn.execute("""
        SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
        FROM usage_event {w} GROUP BY model
    """.format(w=where), params).fetchall()
    cost_by_currency = {}
    currency_of = {}
    unpriced_models = []
    for r in mrows:
        res = cost_of_with_currency(pricing, r['model'], r['i'] or 0,
                                    r['o'] or 0, r['cr'] or 0, r['cw'] or 0,
                                    source=pricing_sources.get(
                                        (r['model'] or '').strip().lower()))
        if res['priced']:
            cost_by_currency[res['currency']] =                 cost_by_currency.get(res['currency'], 0.0) + res['amount']
            currency_of[r['model']] = res['currency']
        elif (r['model'] or '').strip():
            unpriced_models.append((r['model'], (r['i'] or 0) + (r['cr'] or 0)))
    total_cost = cost_by_currency.get('USD', 0.0)   # legacy usd 字段 = USD 桶

    show_cost = (args.by == 'model')
    head = '  %-20s %8s %14s %12s %12s %16s' % (
        title, '请求', '新输入', '输出', '推理', '缓存读取')
    if show_cost:
        head += ' %12s' % '成本 $'
    print()
    print(head)
    print('  ' + '-' * (104 if show_cost else 90))
    if view == 'effective':
        n_replay = conn.execute(
            'SELECT COUNT(*) c FROM usage_event WHERE is_replay = 1'
        ).fetchone()['c']
        print('  口径：有效（产品默认；已剔除 %s 条已验证血统重放，--view raw 查看完整账本）'
              % f'{n_replay:,}')
    else:
        n_replay = conn.execute(
            'SELECT COUNT(*) c FROM usage_event WHERE is_replay = 1'
        ).fetchone()['c']
        print('  口径：完整账本 raw（含 %s 条已验证血统重放；产品默认口径为 effective）'
              % f'{n_replay:,}')

    ti = to = tr = tcr = tcw = 0
    for r in rows:
        ti += r['i'] or 0
        to += r['o'] or 0
        tr += r['r'] or 0
        tcr += r['cr'] or 0
        tcw += r['cw'] or 0
        line = '  %-20s %8d %14s %12s %12s %16s' % (
            str(r['k'])[:20], r['events'],
            f"{r['i'] or 0:,}", f"{r['o'] or 0:,}",
            f"{r['r'] or 0:,}", f"{r['cr'] or 0:,}")
        if show_cost:
            res = cost_of_with_currency(pricing, r['k'], r['i'] or 0,
                                        r['o'] or 0, r['cr'] or 0,
                                        r['cw'] or 0,
                                        source=pricing_sources.get(
                                            (r['k'] or '').strip().lower()))
            cell = ('%.4f' % res['amount']) if res['priced'] else '—'
            if res['priced'] and res['currency'] != 'USD':
                cell += ' ' + res['currency']
            line += ' %12s' % cell
        print(line)

    print('  ' + '-' * (104 if show_cost else 90))
    total_events = sum(r['events'] for r in rows)
    foot = '  %-20s %8d %14s %12s %12s %16s' % (
        '合计', total_events, f'{ti:,}', f'{to:,}', f'{tr:,}', f'{tcr:,}')
    if show_cost:
        foot += ' %12s' % ('%.4f' % total_cost if total_cost else '0')
    print(foot)
    print()
    print('  口径：新输入不含缓存读取；缓存读取单列 —— 通常它才是成本大头。')
    print('  成本为「已定价成本 / API 等价估算」：只累计配置了单价的模型，'
          '未定价绝不折算成 $0。')
    if cost_by_currency:
        sym = {'USD': '$', 'CNY': '¥'}
        for cur in sorted(cost_by_currency):
            print('  已定价成本合计 %s：%s%.4f'
                  % (cur, sym.get(cur, cur + ' '), cost_by_currency[cur]))
        if len(cost_by_currency) > 1:
            print('  （多币种：未做汇率换算，不可直接相加）')
    if unpriced_models:
        print('  ⚠ %d 个模型没有单价，成本未计入：' % len(unpriced_models))
        for m, tok in sorted(unpriced_models, key=lambda x: -x[1])[:8]:
            print('      %-34s %s token' % (m, f'{tok:,}'))
        print('    跑 `python ledger.py pricing-template` 生成模板，填价后重跑。')
    conn.close()


CLIENT_LABEL = {c: s['label'] for c, s in SOURCES.items()}


def cmd_activity(args):
    """活动量报表：给那些本地不记 token 的客户端（CatPaw / Trae CN）。"""
    conn = connect()
    rows = conn.execute("""
        SELECT client,
               COUNT(DISTINCT date(ts_ms/1000,'unixepoch','localtime')) days,
               COUNT(DISTINCT session_key) sessions,
               COUNT(*) events,
               MIN(ts_ms) a, MAX(ts_ms) b
        FROM activity_event GROUP BY client ORDER BY events DESC
    """).fetchall()
    if not rows:
        print('  账本里还没有活动量记录。先跑 `python ledger.py scan`。')
        print('  （CatPaw / Trae CN 本地不记 token，只会写活动量）')
        conn.close()
        return

    print()
    print('  活动量 —— 这两个客户端本地不写 token/cost，所以只回答「哪天用了、用了几轮」')
    print('  %-14s %8s %8s %8s   %s' % ('客户端', '活跃天', '会话数', '记录数', '跨度'))
    print('  ' + '-' * 74)
    for r in rows:
        span = '%s ~ %s' % (day_of(r['a']), day_of(r['b'])) if r['a'] else ''
        print('  %-14s %8d %8d %8d   %s'
              % (CLIENT_LABEL.get(r['client'], r['client']),
                 r['days'], r['sessions'], r['events'], span))

    if args.by == 'day':
        days = args.days or 30
        cutoff = int((dt.datetime.now() - dt.timedelta(days=days)).timestamp() * 1000)
        dr = conn.execute("""
            SELECT date(ts_ms/1000,'unixepoch','localtime') d, client,
                   COUNT(DISTINCT session_key) sessions, COUNT(*) n
            FROM activity_event WHERE ts_ms >= ?
            GROUP BY d, client ORDER BY d
        """, (cutoff,)).fetchall()
        print()
        print('  按天明细（最近 %d 天）：' % days)
        print('  %-12s %-14s %8s %8s' % ('日期', '客户端', '会话数', '记录数'))
        print('  ' + '-' * 48)
        for r in dr:
            print('  %-12s %-14s %8d %8d'
                  % (r['d'], CLIENT_LABEL.get(r['client'], r['client']),
                     r['sessions'], r['n']))

    print()
    print('  注：CatPaw 与 Trae CN 的取数口径不同 ——')
    print('      CatPaw  取会话转录里的每条消息（转录被清退时退回应用记忆库）')
    print('      Trae CN 取记忆摘要条数（其 ai-agent 数据库为加密格式，无法读取）')
    print('  这些数字**不并入也不影响** token / 成本统计。')
    conn.close()


def cmd_status(args):
    conn = connect()
    print()
    print('  账本：%s' % DB_PATH)
    if not os.path.isfile(DB_PATH):
        print('  （账本还没创建，先跑 scan）')
        return
    tot = conn.execute('SELECT COUNT(*) c FROM usage_event').fetchone()['c']
    act = conn.execute('SELECT COUNT(*) c FROM activity_event').fetchone()['c']
    rng = conn.execute('SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM usage_event').fetchone()
    print('  用量记录：%s 条' % f'{tot:,}')
    print('  活动量记录：%s 条' % f'{act:,}')
    if rng['a']:
        print('  用量数据跨度：%s ~ %s'
              % (day_of(rng['a']), day_of(rng['b'])))
    print()
    print('  数据源健康：')
    for client, spec in SOURCES.items():
        rows = conn.execute("""
            SELECT path, missing_since, last_seen FROM source_file
            WHERE client = ? ORDER BY path
        """, (client,)).fetchall()
        alive = [r for r in rows if not r['missing_since']]
        gone = [r for r in rows if r['missing_since']]
        if spec.get('mode') == 'activity':
            ev = conn.execute('SELECT COUNT(*) c FROM activity_event WHERE client=?',
                              (client,)).fetchone()['c']
            unit = '活动量'
        else:
            ev = conn.execute('SELECT COUNT(*) c FROM usage_event WHERE client=?',
                              (client,)).fetchone()['c']
            unit = '用量'
        exists = os.path.exists(spec['path'])
        print('    [%-9s] 源目录%s  %s %s 条  现存文件 %d / 已消失 %d'
              % (client, '在' if exists else '不在', unit, f'{ev:,}', len(alive), len(gone)))
        for r in gone[:5]:
            print('        · 已清退：%s（数据已留存）'
                  % os.path.basename(os.path.dirname(r['path'])) or r['path'])
        if len(gone) > 5:
            print('        · …另有 %d 个' % (len(gone) - 5))
    print()
    print('  结论：源文件被客户端清退后，账本里的用量记录不受影响。')

    # dsh 的 forked/subagent 会话可能重放父会话历史，这里量化跨会话的重叠，避免总数被高估。
    dup = conn.execute("""
        SELECT COUNT(*) c, COALESCE(SUM(n - 1), 0) extra FROM (
            SELECT COUNT(*) n FROM usage_event
            WHERE client = 'dsh'
            GROUP BY ts_ms, input_tokens, output_tokens, cache_read_tokens, model
            HAVING COUNT(DISTINCT session_id) > 1
        )
    """).fetchone()
    if dup and dup['c']:
        print()
        print('  ⚠ dsh 有 %d 组用量在多个会话里重复出现，可能多算约 %s 条'
              % (dup['c'], f"{dup['extra']:,}"))
        print('     （forked/subagent 会话重放父历史所致；看报告时对 dsh 数字保留一点折扣）')
    conn.close()


def cmd_pricing_template(args):
    conn = connect()
    rows = conn.execute("""
        SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr
        FROM usage_event GROUP BY model ORDER BY (SUM(input_tokens)+SUM(cache_read_tokens)) DESC
    """).fetchall()
    tpl = {
        '_note': '单价单位：美元 / 每 100 万 token。把 null 换成实际单价；不填则该模型成本不计入。',
        '_fields': ['input', 'output', 'cache_read', 'cache_write'],
    }
    for r in rows:
        m = (r['model'] or '').strip()
        if not m:
            continue
        tpl[m] = {'input': None, 'output': None, 'cache_read': None, 'cache_write': None}
    with open(PRICING_PATH, 'w', encoding='utf-8') as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    print('  已生成 %s' % PRICING_PATH)
    print('  包含 %d 个模型，填入单价后重新跑 report 就会显示成本。' % (len(tpl) - 2))
    conn.close()


def cmd_export(args):
    conn = connect()
    out = args.out or os.path.join(BASE, 'usage.csv')
    rows = conn.execute("""
        SELECT client, date(ts_ms/1000,'unixepoch','localtime') AS day, model, provider,
               COUNT(*) events, SUM(input_tokens) input, SUM(output_tokens) output,
               SUM(reasoning_tokens) reasoning, SUM(cache_read_tokens) cache_read,
               SUM(cache_write_tokens) cache_write
        FROM usage_event GROUP BY client, day, model, provider ORDER BY day, client, model
    """).fetchall()
    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(['client', 'day', 'model', 'provider', 'events',
                    'input', 'output', 'reasoning', 'cache_read', 'cache_write'])
        for r in rows:
            w.writerow(list(r))
    print('  已导出 %d 行到 %s' % (len(rows), out))
    conn.close()


# ---------------------------------------------------------------- 自动发现

def _ledger_has_scan(conn):
    try:
        return (conn.execute('SELECT COUNT(*) c FROM scan_run').fetchone()['c'] or 0) > 0
    except Exception:
        return False


def grade_of(client, spec, found, conn=None):
    """自动判断某个客户端的数据等级，返回 (grade, basis, records, provisional)。

    等级不是照抄配置：声明只作为**上界**，最终结论必须由账本里的真实记录背书。
    这样「客户端升级后改了存储格式」这种事故会自己冒出来变成 UNKNOWN，
    而不是继续假装是 TOKEN 然后悄悄少算。

        TOKEN    : 本地写了真实 token 用量
        ACTIVITY : 只有活动痕迹，没有 token/cost 字段
        UNKNOWN  : 存储存在但读不出用量，或尚未解析出任何记录
    """
    declared = spec.get('grade') or (
        GRADE_TOKEN if spec.get('mode') == 'usage' else GRADE_ACTIVITY)
    if not found:
        return GRADE_UNKNOWN, 'not_found', 0, False

    own = None
    try:
        own = conn or connect()
        table = 'usage_event' if declared == GRADE_TOKEN else 'activity_event'
        n = own.execute('SELECT COUNT(*) c FROM %s WHERE client=?' % table,
                        (client,)).fetchone()['c'] or 0
        scanned = _ledger_has_scan(own)
    except Exception:
        return declared, 'declared_unverified', 0, True
    finally:
        if conn is None and own is not None:
            try:
                own.close()
            except Exception:
                pass

    unit = '用量' if declared == GRADE_TOKEN else '活动量'
    if n > 0:
        return declared, 'verified', n, False
    if not scanned:
        # 还没扫过账本：声明可信，但结论是暂定的
        return declared, 'declared_provisional', 0, True
    # 已扫过却一条都没有 -> 存储认识、但内容读不出来，老实降级为 UNKNOWN
    return GRADE_UNKNOWN, 'found_but_unparsed', 0, False


def regrade_sources(report):
    """按账本里的真实记录重算 report 里每个数据源的数据等级。"""
    try:
        conn = connect()
    except Exception:
        return report
    try:
        for client, row in (report.get('sources') or {}).items():
            spec = SOURCES.get(client) or {}
            found = row.get('state') == 'found'
            g, basis, n, prov = grade_of(client, spec, found, conn=conn)
            row['data_grade'] = g
            row['grade_basis'] = basis
            row['grade_records'] = n
            row['grade_provisional'] = prov
        active = [r for r in (report.get('sources') or {}).values() if r.get('state') == 'found']
        report['grades'] = {
            GRADE_TOKEN: len([r for r in active if r.get('data_grade') == GRADE_TOKEN]),
            GRADE_ACTIVITY: len([r for r in active if r.get('data_grade') == GRADE_ACTIVITY]),
            GRADE_UNKNOWN: len([r for r in active if r.get('data_grade') == GRADE_UNKNOWN]),
        }
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return report


def write_discovery(report):
    """原子写 discovery.json。"""
    try:
        tmp = DISCOVERY_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DISCOVERY_PATH)
    except Exception as e:
        print('  ! discovery.json 写入失败：%s' % e, file=sys.stderr)


def discover_sources(verbose=True):
    """只按各客户端自带的候选路径清单探测，不做全盘模糊匹配。

    找到就把 SOURCES[client]['path'] 切到命中的候选，并写 resolved-sources.json，
    这样后续单独跑 scan 也能用上。
    """
    report = {'generated_at': now_iso(), 'schema_version': 1, 'sources': {}, 'opaque': {}}
    resolved = {}
    found = 0
    for client, spec in SOURCES.items():
        checked, hit, n_files = [], None, 0
        configured_here = client in _explicit_paths
        if configured_here:
            # 用户在 sources.json 里显式指定过路径：只探测它，不让默认候选把它顶掉
            cands = [spec['path']]
        else:
            cands = list(spec.get('candidates') or [])
            if spec.get('path') and os.path.normpath(spec['path']) not in \
                    [os.path.normpath(c) for c in cands]:
                cands.append(spec['path'])
        for cand in cands:
            cand = os.path.normpath(cand)
            if cand in checked:
                continue
            checked.append(cand)
            if spec.get('probe') == 'file':
                if os.path.isfile(cand):
                    hit, n_files = cand, 1
                    break
            elif os.path.isdir(cand):
                fs = list_files(client, cand)
                if fs:
                    hit, n_files = cand, len(fs)
                    break
        state = 'found' if hit else 'not_found'
        if hit:
            found += 1
            SOURCES[client]['path'] = hit
            resolved[client] = hit
        report['sources'][client] = {
            'state': state,
            'mode': spec.get('mode'),
            'category': 'collectable',            # 能采集 -> 进账本
            'visibility': spec.get('visibility', 'unknown'),
            'label': spec.get('label'),
            'configured': configured_here,
            'candidates_checked': [redact_path(c) for c in checked],
            'files': n_files,
            'path_hint': redact_path(hit or spec.get('path', '')),
        }
        if verbose and not hit:
            print('  [%-9s] not_found（已试 %d 个候选）' % (client, len(checked)))
            if spec.get('hint'):
                print('             提示：%s' % spec['hint'])

    # 不透明存储：认得出、读不出来。登记为 UNKNOWN，绝不静默略过。
    for key, spec in OPAQUE_STORES.items():
        checked, hit = [], None
        for cand in spec.get('candidates') or [spec['path']]:
            cand = os.path.normpath(cand)
            if cand in checked:
                continue
            checked.append(cand)
            if os.path.exists(cand):
                hit = cand
                break
        try:
            size = os.path.getsize(hit) if hit else 0
        except OSError:
            size = 0
        report['opaque'][key] = {
            'state': 'found' if hit else 'not_found',
            'mode': 'opaque',
            'category': 'unreadable',             # 认得出但读不了 -> 不进账本
            'data_grade': GRADE_UNKNOWN,
            'grade_basis': 'unreadable_store',
            'label': spec['label'],
            'reason': spec['reason'],
            'bytes': size,
            'candidates_checked': [redact_path(c) for c in checked],
            'path_hint': redact_path(hit or spec['path']),
        }

    report['found'] = found
    report['total'] = len(SOURCES)
    regrade_sources(report)
    try:
        with open(RESOLVED_PATH, 'w', encoding='utf-8') as f:
            json.dump(resolved, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('  ! resolved-sources.json 写入失败：%s' % e, file=sys.stderr)
    return report


def cmd_discover(args):
    rep = discover_sources()
    write_discovery(rep)
    print()
    print('  自动发现：%d / %d 个可采集数据源' % (rep['found'], rep['total']))
    for client, row in rep['sources'].items():
        mark = 'OK ' if row['state'] == 'found' else '-- '
        print('    %s%-9s %-22s %-9s %-9s 文件 %-4s %s'
              % (mark, client, row['label'], row['state'],
                 row.get('data_grade', '?'), row['files'], row.get('grade_basis', '')))
    if rep.get('opaque'):
        print()
        print('  不透明存储（认得出、读不出用量，登记为 UNKNOWN）：')
        for key, row in rep['opaque'].items():
            mark = 'OK ' if row['state'] == 'found' else '-- '
            print('    %s%-16s %-9s %s' % (mark, key, row['state'], row['reason']))
            if row['state'] == 'found':
                print('      %s（%.1f MB）' % (row['path_hint'], row['bytes'] / 1048576))
    g = rep.get('grades') or {}
    print()
    print('  数据等级：TOKEN %d ｜ ACTIVITY %d ｜ UNKNOWN %d'
          % (g.get(GRADE_TOKEN, 0), g.get(GRADE_ACTIVITY, 0), g.get(GRADE_UNKNOWN, 0)))
    print('  发现结果：%s' % DISCOVERY_PATH)
    if rep['found'] == 0:
        print('  ⚠ 未发现任何受支持数据源。')
        return EXIT_NO_SOURCES
    return EXIT_OK


# ---------------------------------------------------------------- usage board

BOARD_WINDOW_DAYS = 45


def build_board(conn=None):
    """生成 Usage Board Schema v1。只读账本，不读源文件，不含路径与 event id。"""
    own_connection = conn is None
    conn = conn or connect()
    pricing, pricing_sources = load_pricing_with_sources()
    layers = load_pricing_layers()
    _pid_cache = {}

    def _model_provider_identity(model):
        """模型在账本中唯一可靠的 provider 身份；混合 / 不可知 → None。"""
        if model in _pid_cache:
            return _pid_cache[model]
        try:
            rows = conn.execute(
                'SELECT DISTINCT provider FROM usage_event WHERE model=?',
                (model,)).fetchall()
        except sqlite3.Error:
            rows = []
        ids = {_provider_identity(r[0]) for r in rows}
        ids.discard(None)
        pid = ids.pop() if len(ids) == 1 else None
        _pid_cache[model] = pid
        return pid

    def resolve_cost(model, i, o, cr, cw):
        rec = resolve_price_record(layers, model,
                                   provider=_model_provider_identity(model))
        return rec, record_cost(rec, i, o, cr, cw)

    # 数据等级：声明作上界 + 账本记录作实证。路径已消失但账本仍有记录时等级依然成立。
    grade_map = {}
    for _c, _spec in SOURCES.items():
        try:
            _tbl = 'usage_event' if _spec.get('mode') == 'usage' else 'activity_event'
            _has_rec = (conn.execute('SELECT COUNT(*) c FROM %s WHERE client=?' % _tbl,
                                     (_c,)).fetchone()['c'] or 0) > 0
        except Exception:
            _has_rec = False
        _found = bool(os.path.exists(_spec.get('path') or '')) or _has_rec
        grade_map[_c] = grade_of(_c, _spec, _found, conn=conn)

    day_expr = "date(ts_ms/1000,'unixepoch','localtime')"

    # ---- 口径（Round 5 起）：board 顶层 = EFFECTIVE（默认产品口径）。
    # 已验证血统重放（is_replay=1）不再进入 summary/daily/clients/models，
    # 但 raw 行一条不删 —— 完整账本经 dedup.raw_summary 与 --view raw 审计。
    # 旧 v0 账本（无标记列）自动退化为 raw == effective。
    ucols = {r[1] for r in conn.execute('PRAGMA table_info(usage_event)')}
    rf = 'AND is_replay = 0' if 'is_replay' in ucols else ''      # replay filter

    daily = conn.execute("""
        SELECT {d} AS day, client,
               COUNT(*) events, SUM(input_tokens) input, SUM(output_tokens) output,
               SUM(reasoning_tokens) reasoning, SUM(cache_read_tokens) cache_read,
               SUM(cache_write_tokens) cache_write
        FROM usage_event
        WHERE ts_ms >= ? {rf}
        GROUP BY day, client ORDER BY day
    """.format(d=day_expr, rf=rf),
        (int((dt.datetime.now() - dt.timedelta(days=BOARD_WINDOW_DAYS))
             .timestamp() * 1000),)).fetchall()

    clients = conn.execute("""
        SELECT client, COUNT(*) events, SUM(input_tokens) input, SUM(output_tokens) output,
               SUM(reasoning_tokens) reasoning, SUM(cache_read_tokens) cache_read,
               SUM(cache_write_tokens) cache_write,
               MIN(ts_ms) a, MAX(ts_ms) b
        FROM usage_event WHERE 1=1 {rf} GROUP BY client
        ORDER BY (SUM(input_tokens)+SUM(cache_read_tokens)) DESC
    """.format(rf=rf)).fetchall()

    models = conn.execute("""
        SELECT model, provider, COUNT(*) events, SUM(input_tokens) input,
               SUM(output_tokens) output, SUM(reasoning_tokens) reasoning,
               SUM(cache_read_tokens) cache_read, SUM(cache_write_tokens) cache_write
        FROM usage_event WHERE model <> '' {rf} GROUP BY model
        ORDER BY (SUM(input_tokens)+SUM(cache_read_tokens)) DESC
    """.format(rf=rf)).fetchall()

    # ---- Phase 0：by_project（EFFECTIVE；哈希键 + 安全显示名，绝不含完整路径/event id）----
    # 每个 project 再按 model 聚合计价：total = input+output；cache_read 独立；
    # 未定价模型的 io 单列进 pricing_coverage，绝不折算成 0 元。
    project_rows = conn.execute("""
        SELECT u.project_key AS pk,
               COALESCE(NULLIF(r.project_kind,''), 'unknown') AS kind,
               COALESCE(NULLIF(r.display_name,''), u.project_key) AS display,
               COUNT(*) events,
               COUNT(DISTINCT u.client || char(31) || u.session_id) sessions,
               SUM(u.input_tokens) i, SUM(u.output_tokens) o,
               SUM(u.cache_read_tokens) cr, SUM(u.cache_write_tokens) cw,
               MAX(u.last_seen) last_seen
        FROM usage_event u
        LEFT JOIN project_registry r ON u.project_key = r.project_key
        WHERE u.project_key IS NOT NULL {rf}
        GROUP BY u.project_key
        ORDER BY (SUM(u.input_tokens)+SUM(u.output_tokens)) DESC
    """.format(rf=rf)).fetchall()
    by_project = []
    for pr in project_rows:
        by_model_proj = conn.execute("""
            SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
                   SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
            FROM usage_event WHERE is_replay=0 {rf} AND project_key=?
            GROUP BY model
        """.format(rf=rf), (pr['pk'],)).fetchall()
        cost_by_cur = {}
        priced_io = 0
        total_io = 0
        for m in by_model_proj:
            i = m['i'] or 0
            o = m['o'] or 0
            cr = m['cr'] or 0
            cw = m['cw'] or 0
            total_io += i + o
            _rec, res = resolve_cost(m['model'], i, o, cr, cw)
            if res and res['available']:
                priced_io += res['covered_io_tokens']
                cost_by_cur[res['currency']] = round(
                    cost_by_cur.get(res['currency'], 0.0) + res['amount'], 4)
        try:
            _meta = json.loads(conn.execute(
                'SELECT metadata_json FROM project_registry WHERE project_key=?',
                (pr['pk'],)).fetchone()[0] or '{}') or {}
        except Exception:
            _meta = {}
        by_project.append({
            'project_key': pr['pk'],
            'display_name': pr['display'],
            'project_kind': pr['kind'],
            'aliases': _meta.get('aliases') or [],
            'original_display': _meta.get('original_display'),
            'events': pr['events'],
            'sessions': pr['sessions'],
            'input_tokens': pr['i'] or 0,
            'output_tokens': pr['o'] or 0,
            'cache_read_tokens': pr['cr'] or 0,
            'total_tokens': (pr['i'] or 0) + (pr['o'] or 0),
            'estimated_cost_by_currency': cost_by_cur or None,
            'pricing_coverage': {
                'priced_io_tokens': priced_io,
                'total_io_tokens': total_io,
                'token_coverage_pct': (round(priced_io / total_io * 100, 1)
                                       if total_io else None),
            },
            'last_seen': pr['last_seen'],
        })

    act = conn.execute("""
        SELECT client, COUNT(*) records,
               COUNT(DISTINCT session_key) sessions,
               COUNT(DISTINCT {d}) days, MIN(ts_ms) a, MAX(ts_ms) b
        FROM activity_event GROUP BY client ORDER BY records DESC
    """.format(d=day_expr)).fetchall()

    rng = conn.execute('SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM usage_event '
                       'WHERE 1=1 {rf}'.format(rf=rf)).fetchone()
    tot = conn.execute("""
        SELECT COUNT(*) events, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(reasoning_tokens) r, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
        FROM usage_event WHERE 1=1 {rf}
    """.format(rf=rf)).fetchone()

    # raw 全量聚合（审计口径，进 dedup.raw_summary；v0 账本下与 effective 相同）
    tot_raw = conn.execute("""
        SELECT COUNT(*) events, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(reasoning_tokens) r, SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
        FROM usage_event
    """).fetchone()
    models_raw = conn.execute("""
        SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
        FROM usage_event WHERE model <> '' GROUP BY model
    """).fetchall() if rf else models

    # ---- 成本（Round 5 基于 EFFECTIVE 口径；Round 8-C 币种感知）：
    # 未匹配单价的模型一律 cost=null，绝不折算成 0。
    # 不同币种分别聚合（USD 是 USD，CNY 是 CNY），绝不相加：
    #   - estimated_cost_by_currency：按币种的完整估算（含第三方参考价）
    #   - reliable_estimated_cost_by_currency：仅 user_channel + official
    #   - reference_estimated_cost_by_currency：仅第三方参考价
    #   - cost_usd / estimated_cost_usd_top50_models：仅 USD 桶（legacy 语义收窄）
    priced_models = 0
    unpriced_models = 0
    cost_by_currency = {}
    reliable_cost_by_currency = {}
    reference_cost_by_currency = {}
    priced_by_source = {'manual_verified': 0, 'auto_reference': 0}
    priced_by_type = {PT_USER_CHANNEL: 0, PT_OFFICIAL: 0,
                      PT_THIRD_PARTY_REFERENCE: 0, PT_UNAVAILABLE: 0}
    reliable_priced_tokens = 0
    reference_priced_tokens = 0
    model_rows = []
    for r in models:
        rec = resolve_price_record(
            layers, r['model'],
            provider=_model_provider_identity(r['model']))
        res = record_cost(rec, r['input'] or 0, r['output'] or 0,
                          r['cache_read'] or 0, r['cache_write'] or 0)
        ptype = (rec or {}).get('pricing_type') or PT_UNAVAILABLE
        if res and res['available']:
            priced_models += 1
            cost_by_currency[res['currency']] =                 cost_by_currency.get(res['currency'], 0.0) + res['amount']
            bucket = (reliable_cost_by_currency if is_reliable_record(rec)
                      else reference_cost_by_currency)
            bucket[res['currency']] =                 bucket.get(res['currency'], 0.0) + res['amount']
            priced_by_type[ptype] = priced_by_type.get(ptype, 0) + 1
            legacy_src = 'manual_verified' if ptype == PT_USER_CHANNEL else \
                ('auto_reference' if ptype == PT_THIRD_PARTY_REFERENCE else ptype)
            priced_by_source[legacy_src] =                 priced_by_source.get(legacy_src, 0) + 1
            if is_reliable_record(rec):
                reliable_priced_tokens += res['covered_io_tokens']
            else:
                reference_priced_tokens += res['covered_io_tokens']
        else:
            unpriced_models += 1
            ptype = PT_UNAVAILABLE
        model_rows.append({
            'model': r['model'], 'provider': r['provider'] or '',
            'events': r['events'],
            'input': r['input'] or 0, 'output': r['output'] or 0,
            'reasoning': r['reasoning'] or 0,
            'cache_read': r['cache_read'] or 0, 'cache_write': r['cache_write'] or 0,
            # R8-D 兼容恢复：cost_usd 为 Board v1 legacy 字段——
            #   USD priced -> cost；CNY priced / unpriced -> null（绝不折算）
            'cost_usd': (round(res['amount'], 6)
                         if res and res['available'] and res['currency'] == 'USD'
                         else None),
            'cost': round(res['amount'], 6) if res and res['available'] else None,
            'currency': res['currency'] if res and res['available'] else None,
            'pricing_source': legacy_src if res and res['available']
            else 'unpriced',
            # V1.1 溯源：pricing_type / applicability / retrieved_at
            'pricing_type': ptype,
            'pricing_applicability': (rec or {}).get('applicability')
            if res and res['available'] else None,
            'pricing_retrieved_at': (rec or {}).get('retrieved_at'),
            'priced': bool(res and res['available']),
            'cost_partial': bool(res and res['partial']),
            'covered_io_tokens': (res or {}).get('covered_io_tokens', 0),
        })
    total_cost = cost_by_currency.get('USD', 0.0)   # legacy usd 字段 = USD 桶

    client_rows = []
    for r in clients:
        spec = SOURCES.get(r['client'], {})
        client_rows.append({
            'client': r['client'], 'label': spec.get('label', r['client']),
            'mode': spec.get('mode', 'usage'),
            'data_grade': grade_map.get(r['client'], (GRADE_UNKNOWN,))[0],
            'events': r['events'],
            'input': r['input'] or 0, 'output': r['output'] or 0,
            'reasoning': r['reasoning'] or 0,
            'cache_read': r['cache_read'] or 0, 'cache_write': r['cache_write'] or 0,
            'first_day': day_of(r['a']) if r['a'] else None,
            'last_day': day_of(r['b']) if r['b'] else None,
        })

    daily_rows = [{
        'day': r['day'], 'client': r['client'], 'events': r['events'],
        'input': r['input'] or 0, 'output': r['output'] or 0,
        'reasoning': r['reasoning'] or 0,
        'cache_read': r['cache_read'] or 0, 'cache_write': r['cache_write'] or 0,
    } for r in daily]

    source_rows = []
    for client, spec in SOURCES.items():
        rows = conn.execute(
            'SELECT path, missing_since FROM source_file WHERE client = ?', (client,)).fetchall()
        alive = len([x for x in rows if not x['missing_since']])
        gone = len([x for x in rows if x['missing_since']])
        is_act = spec.get('mode') == 'activity'
        rec = conn.execute('SELECT COUNT(*) c FROM %s WHERE client=?' %
                           ('activity_event' if is_act else 'usage_event'),
                           (client,)).fetchone()['c']
        exists = os.path.exists(spec['path'])
        gmap = grade_map.get(client, (GRADE_UNKNOWN, 'not_found', 0, True))
        source_rows.append({
            'client': client, 'label': spec.get('label', client),
            'mode': spec.get('mode', 'usage'),
            'visibility': spec.get('visibility', 'unknown'),
            'data_grade': gmap[0],
            'grade_basis': gmap[1],
            'grade_provisional': bool(gmap[3]),
            'state': 'found' if exists else 'not_found',
            'files_alive': alive, 'files_missing': gone, 'records': rec,
            'path_hint': redact_path(spec['path']),
        })

    # 不透明存储（认得出、读不出）也进 board，别让 Dashboard 以为「没有这个客户端」
    opaque_rows = []
    for key, spec in OPAQUE_STORES.items():
        hit = next((c for c in (spec.get('candidates') or [spec['path']])
                    if os.path.exists(os.path.normpath(c))), None)
        opaque_rows.append({
            'client': key, 'label': spec['label'], 'mode': 'opaque',
            'data_grade': GRADE_UNKNOWN, 'grade_basis': 'unreadable_store',
            'state': 'found' if hit else 'not_found',
            'reason': spec['reason'],
            'bytes': os.path.getsize(hit) if hit else 0,
            'path_hint': redact_path(hit or spec['path']),
        })

    grade_counts = {g: 0 for g in GRADES}
    for client in SOURCES:
        grade_counts[grade_map.get(client, (GRADE_UNKNOWN,))[0]] += 1
    grade_counts[GRADE_UNKNOWN] += len(opaque_rows)

    # 未匹配单价的模型 + 候选键（只给候选，绝不自动选）。给 Dashboard 做"待补价"清单。
    try:
        pricing_gaps = model_pricing_gaps(conn=conn, pricing=pricing)
    except Exception:
        pricing_gaps = []

    # ---- v1 重放血统审计块（Round 5）：顶层已是 EFFECTIVE，raw 供审计对照 ----
    dedup = None
    if rf:
        n_replay = conn.execute(
            'SELECT COUNT(*) c FROM usage_event WHERE is_replay = 1').fetchone()['c']
        raw_by_currency = {}
        raw_priced = 0
        for r in models_raw:
            res = cost_of_with_currency(pricing, r['model'], r['i'] or 0,
                                        r['o'] or 0, r['cr'] or 0,
                                        r['cw'] or 0,
                                        source=pricing_sources.get(
                                            r['model'].strip().lower()))
            if res['available']:
                raw_priced += 1
                raw_by_currency[res['currency']] =                     raw_by_currency.get(res['currency'], 0.0) + res['amount']
        raw_cost = raw_by_currency.get('USD', 0.0)   # legacy usd 字段 = USD 桶
        by_client_rows = conn.execute("""
            SELECT client, COUNT(*) raw, SUM(is_replay) replay
            FROM usage_event GROUP BY client ORDER BY raw DESC
        """).fetchall()
        state = replay_state_summary()

        def _sum_block(row, cost, priced, n_models, by_currency):
            return {
                'usage_events': row['events'] or 0,
                'input_tokens': row['i'] or 0,
                'output_tokens': row['o'] or 0,
                'reasoning_tokens': row['r'] or 0,
                'cache_read_tokens': row['cr'] or 0,
                'cache_write_tokens': row['cw'] or 0,
                'total_tokens': (row['i'] or 0) + (row['o'] or 0),
                'estimated_cost_usd_top50_models':
                    round(cost, 4) if cost else None,
                'estimated_cost_by_currency':
                    {k: round(v, 4) for k, v in sorted(by_currency.items())}
                    if by_currency else None,
                'priced_models': priced,
                'unpriced_models': n_models - priced,
            }

        dedup = {
            'status': 'validated' if state else 'available',
            'method': 'lineage_seed_v1',
            'default_view': 'effective',
            'raw_events': tot_raw['events'] or 0,
            'replay_events': n_replay,
            'effective_events': tot['events'] or 0,
            'unresolved_events': (state or {}).get('unresolved_events'),
            'confidence': 'confirmed_lineage',
            'marked_at': (state or {}).get('marked_at'),
            'raw_summary': _sum_block(tot_raw, raw_cost, raw_priced,
                                      len(models_raw), raw_by_currency),
            'effective_summary': _sum_block(tot, total_cost, priced_models,
                                            len(models), cost_by_currency),
            'by_client': [{
                'client': r['client'],
                'label': SOURCES.get(r['client'], {}).get('label', r['client']),
                'raw': r['raw'],
                'replay': r['replay'] or 0,
                'effective': r['raw'] - (r['replay'] or 0),
            } for r in by_client_rows],
        }

    # ---- Round 8：定价覆盖率（additive）。
    # 口径冻结：一切成本均为「API 等价估算成本」（当前参考单价 × 有效用量），
    # 不是真实支付账单（Actual Spend）。覆盖率主指标 = Token 覆盖率，
    # 模型覆盖率仅作参考（低用量模型拉低模型覆盖率但不代表成本不可信）。
    priced_tok = sum(m['covered_io_tokens'] for m in model_rows if m['priced'])
    eff_tok = (tot['i'] or 0) + (tot['o'] or 0)
    pricing_coverage = {
        'effective_models': len(model_rows),
        'priced_models': priced_models,
        'unpriced_models': unpriced_models,
        'effective_tokens': eff_tok,
        'priced_tokens': priced_tok,
        'unpriced_tokens': eff_tok - priced_tok,
        'token_coverage_pct': round(priced_tok * 100.0 / eff_tok, 2) if eff_tok else None,
        'model_coverage_pct': round(priced_models * 100.0 / len(model_rows), 2)
        if model_rows else None,
        # V1.1：可靠 / 第三方参考分开统计，绝不混成一个 coverage。
        'reliable_priced_tokens': reliable_priced_tokens,
        'reference_priced_tokens': reference_priced_tokens,
        'reliable_coverage_pct': round(reliable_priced_tokens * 100.0 / eff_tok, 2)
        if eff_tok else None,
        'reference_coverage_pct': round(reference_priced_tokens * 100.0 / eff_tok, 2)
        if eff_tok else None,
        'priced_by_source': dict(sorted(priced_by_source.items())),
        'priced_by_type': dict(sorted(priced_by_type.items())),
        'multi_currency': len(cost_by_currency) > 1,
        'pricing_basis': 'current_reference',
        'cost_semantics': 'api_equivalent_estimate',
        'actual_spend': 'unknown',
        'as_of': now_iso(),
    }

    try:
        sessions_total = conn.execute(
            'SELECT COUNT(DISTINCT client || char(31) || session_id) c '
            'FROM usage_event WHERE is_replay=0').fetchone()['c']
    except sqlite3.OperationalError:
        sessions_total = conn.execute(
            'SELECT COUNT(DISTINCT client || char(31) || session_id) c '
            'FROM usage_event').fetchone()['c']

    board = {
        'schema_version': 1,
        'generated_at': now_iso(),
        'privacy': {'paths_included': False, 'event_ids_included': False},
        'summary': {
            'usage_events': tot['events'] or 0,
            'input_tokens': tot['i'] or 0,
            'output_tokens': tot['o'] or 0,
            'reasoning_tokens': tot['r'] or 0,
            'cache_read_tokens': tot['cr'] or 0,
            'cache_write_tokens': tot['cw'] or 0,
            'total_tokens': (tot['i'] or 0) + (tot['o'] or 0),
            'projects_total': len(by_project),
            'sessions_total': sessions_total,
            'usage_first_day': day_of(rng['a']) if rng['a'] else None,
            'usage_last_day': day_of(rng['b']) if rng['b'] else None,
            'priced_models': priced_models,
            'unpriced_models': unpriced_models,
            'estimated_cost_usd_top50_models':
                round(total_cost, 4) if total_cost else None,
            # Round 8-C：按币种的完整估算（USD 桶 + 其它币种分列，绝不相加）
            'estimated_cost_by_currency':
                {k: round(v, 4) for k, v in sorted(cost_by_currency.items())}
                if cost_by_currency else None,
            # V1.1 双口径：可靠估算（user_channel + official）与
            # 含第三方参考（+ third_party_reference）分开；二者都仍是
            # API 等价值估算，绝不等于实际支出。
            'reliable_estimated_cost_by_currency':
                {k: round(v, 4) for k, v in sorted(reliable_cost_by_currency.items())}
                if reliable_cost_by_currency else None,
            'reference_estimated_cost_by_currency':
                {k: round(v, 4) for k, v in sorted(reference_cost_by_currency.items())}
                if reference_cost_by_currency else None,
            'actual_spend': {'status': 'unknown',
                             'reason': '暂无账单 / 订阅 / 充值 / 人工录入数据；'
                                       'API 等价值估算不代表实际支出'},
            'pricing': pricing_meta(),
        },
        'grades': {
            'counts': grade_counts,
            'by_client': {c: grade_map.get(c, (GRADE_UNKNOWN,))[0] for c in SOURCES},
            'opaque': [r['client'] for r in opaque_rows if r['state'] == 'found'],
        },
        'window_days': BOARD_WINDOW_DAYS,
        'daily': daily_rows,
        'clients': client_rows,
        'by_project': by_project,
        'activity': [{
            'client': r['client'], 'label': SOURCES.get(r['client'], {}).get('label', r['client']),
            'mode': SOURCES.get(r['client'], {}).get('mode', 'activity'),
            'records': r['records'], 'sessions': r['sessions'], 'days': r['days'],
            'first_day': day_of(r['a']) if r['a'] else None,
            'last_day': day_of(r['b']) if r['b'] else None,
        } for r in act],
        'models': model_rows,
        'pricing_gaps': pricing_gaps,
        'sources': source_rows,
        'opaque_stores': opaque_rows,
        'dedup': dedup,
        'pricing_coverage': pricing_coverage,
    }
    if own_connection:
        conn.close()
    return board


def replay_state_summary():
    """读最近一次 replay-mark 写下的状态（未决计数等）。文件不存在 = 未跑过标记。"""
    try:
        with open(REPLAY_STATE_PATH, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def cmd_replay_mark(args):
    """从只读 dsh 源 + 账本现有 (session_id, event_id) 重推导血统重放标记。

    Round 4 正式迁移流程：不依赖任何临时分析产物，可随时重跑复核；
    幂等（已正确的行不会被再次更新）；绝不 DELETE，绝不改 raw 字段。
    断链/模糊一律保守保留（is_replay=0），并计入 unresolved。
    """
    conn = connect(create=True)
    files = list_files('dsh')
    if not files:
        print('  未找到 dsh 会话文件，无法重推导标记。')
        conn.close()
        return EXIT_NO_SOURCES
    lin = DshLineage()
    lin.load(files)

    confirmed = unresolved = checked = changed = 0
    parse_failed = 0
    for path in files:
        sid = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
        try:
            events = list(parse_dsh_file(path))
        except Exception as e:
            parse_failed += 1
            print('  ! 解析失败 %s：%s' % (sid[:24], e), file=sys.stderr)
            continue
        vmap = lin.classify_file(sid, events)
        h = lin.header(sid)
        sl = h.get('seedLength')
        if sl is not None and h.get('parentSession'):
            # 种子区内未能验证为重放的 = 断链/模糊（保守保留）→ unresolved
            unresolved += sum(1 for v in vmap.values() if v[3] in ('broken', 'ambiguous'))
            confirmed += sum(1 for v in vmap.values() if v[3] == 'replay')
        for seq, (ir, reason, src, _st) in vmap.items():
            cur = conn.execute(
                'UPDATE usage_event SET is_replay=?, replay_reason=?, '
                'replay_source_session=? '
                "WHERE client='dsh' AND session_id=? AND event_id=? "
                'AND (is_replay IS NOT ? OR replay_reason IS NOT ? '
                'OR replay_source_session IS NOT ?)',
                (ir, reason, src, sid, 'seq%d' % seq, ir, reason, src))
            changed += cur.rowcount
        checked += len(events)
    conn.commit()

    n_db_replay = conn.execute(
        'SELECT COUNT(*) c FROM usage_event WHERE is_replay = 1').fetchone()['c']
    n_db_dsh = conn.execute(
        "SELECT COUNT(*) c FROM usage_event WHERE client = 'dsh'").fetchone()['c']
    state = {
        'schema_version': 1,
        'method': 'lineage_seed_v1',
        'marked_at': now_iso(),
        'events_checked': checked,
        'parse_failed_files': parse_failed,
        'confirmed_replay': confirmed,
        'unresolved_events': unresolved,
        'rows_changed_this_run': changed,
        'db_replay_rows': n_db_replay,
        'db_dsh_rows': n_db_dsh,
    }
    tmp = REPLAY_STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, REPLAY_STATE_PATH)
    conn.close()

    print()
    print('  血统重放标记（lineage_seed_v1）')
    print('    源会话文件 %d ｜ 解析事件 %d ｜ 解析失败文件 %d'
          % (len(files), checked, parse_failed))
    print('    本源判定：confirmed %d ｜ unresolved（断链/模糊，保留）%d'
          % (confirmed, unresolved))
    print('    账本更新行数 %d ｜ 账本内 replay 行 %d / dsh 行 %d'
          % (changed, n_db_replay, n_db_dsh))
    print('    状态：%s' % REPLAY_STATE_PATH)
    print('    raw 账本未做任何删除；聚合默认口径不变（RAW）。')
    return EXIT_OK


def _dsh_session_head(path):
    """只读 dsh 会话文件头部：type:session（cwd / id / agentPreset）+ session/title。

    流式解压、找到即止（session 事件在文件头部）；设 8MB / 2000 行上限，
    防止异常文件把回填拖成全量解压。
    """
    if not os.path.isfile(path):
        return None
    cwd = sid = agent_preset = s_title = None
    it = _plain_lines(path) if not path.endswith('.zstd') else _zstd_lines(path)
    n = 0
    nbytes = 0
    try:
        for raw in it:
            n += 1
            nbytes += len(raw)
            if n > 2000 or nbytes > (8 << 20):
                break
            s = raw.decode('utf-8', 'replace')
            if '"cwd"' not in s and '"session/title"' not in s and '"agentPreset"' not in s:
                continue
            try:
                o = json.loads(s)
            except Exception:
                continue
            t = o.get('type')
            if t == 'session':
                cwd = o.get('cwd') or cwd
                agent_preset = (o.get('agentPreset') or '').strip() or agent_preset
                sid = o.get('id') or sid
            elif t == 'session/title':
                dd = o.get('data') or {}
                tt = dd.get('title') if isinstance(dd, dict) else None
                if tt:
                    s_title = str(tt).strip()
            if cwd and s_title:
                break
    except Exception:
        return None
    return {'cwd': cwd, 'session_native_id': sid, 'title': s_title, 'agent': agent_preset}


def _workbuddy_session_head(path):
    """只读 workbuddy 文件：ai-title（会话显示名）与 cwd，找到即止。"""
    if not os.path.isfile(path):
        return None
    cwd = None
    title = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"ai-title"' in line:
                    try:
                        o = json.loads(line)
                        if o.get('type') == 'ai-title':
                            if not title:
                                title = (o.get('aiTitle') or '').strip() or None
                            cwd = o.get('cwd') or cwd
                    except Exception:
                        pass
                elif cwd is None and '"cwd"' in line:
                    try:
                        o = json.loads(line)
                        if isinstance(o, dict) and o.get('cwd'):
                            cwd = o['cwd']
                    except Exception:
                        continue
                if cwd and title:
                    break
    except Exception:
        return None
    return {'cwd': cwd, 'title': title}


def _catpaw_session_head(path):
    """只读 catpaw 转录：行内 cwd（项目键来源）。"""
    if not os.path.isfile(path):
        return None
    cwd = None
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                if '"cwd"' not in line:
                    continue
                try:
                    o = json.loads(line)
                    if isinstance(o, dict) and o.get('cwd'):
                        cwd = o['cwd']
                        break
                except Exception:
                    continue
    except Exception:
        return None
    return {'cwd': cwd}


def _register_project_session(conn, source, session_id, pk, kind, pdisp, sdisp, agent, ts):
    """归因注册表写入（project_key 为空时仍写 session 行，project 留空 = 未归属）。"""
    _upsert_project_and_session(conn, dict(client=source, session_id=session_id,
        project_key=pk, project_kind=kind, project_display=pdisp,
        session_title=sdisp, agent=agent), ts)


def cmd_attribution_backfill(args):
    """Phase 0 历史回填：为已入库的 usage_event / activity_event 补项目与会话归因。

    - 幂等：重复执行结果一致；可中断后重跑（UPDATE 覆盖写同一值）。
    - 只写 project_key 与两张注册表：不改 token / cache / replay / pricing / 事件主键。
    - 原始源已不可读的会话 → 维持未归属（不猜测），并明确计数上报。
    """
    if not os.path.isfile(DB_PATH):
        print('  账本不存在：%s —— 没有可回填的数据' % DB_PATH)
        return EXIT_NO_SOURCES
    conn = connect()
    migrate_schema(conn)
    ts = now_iso()
    report = {}

    # ---- zcode：join 原始 sqlite 的 session 表 ----
    zpath = SOURCES['zcode']['path']
    mapped = {}
    if os.path.isfile(zpath):
        zc = sqlite3.connect('file:' + zpath.replace('\\', '/') + '?mode=ro', uri=True)
        zc.row_factory = sqlite3.Row
        for r in zc.execute(
                "SELECT id, project_id, directory, title FROM session "
                "WHERE project_id IS NOT NULL AND project_id<>''"):
            pk, _np = project_key_from_path(r['directory'])
            mapped[r['id']] = (pk, PROJECT_KIND_PROJECT,
                               path_display_name(r['directory']),
                               (r['title'] or '').strip() or None)
        zc.close()
    n_ev = n_sess = orphans = 0
    for (sid,) in conn.execute(
            "SELECT DISTINCT session_id FROM usage_event WHERE client='zcode'").fetchall():
        m = mapped.get(sid)
        if not m:
            orphans += 1
            continue
        pk, kind, disp, title = m
        pk = resolve_merged_key(conn, pk)
        cur = conn.execute(
            "UPDATE usage_event SET project_key=? WHERE client='zcode' AND session_id=? AND project_key_manual=0",
            (pk, sid))
        n_ev += cur.rowcount
        _register_project_session(conn, 'zcode', sid, pk, kind, disp, title, None, ts)
        n_sess += 1
    conn.commit()
    report['zcode'] = {'sessions': n_sess, 'events_updated': n_ev, 'orphans': orphans}

    # ---- dsh：逐 source_file 读 session 事件头 ----
    n_ev = resolved = missing = 0
    for sid, sf in conn.execute(
            "SELECT DISTINCT session_id, source_file FROM usage_event WHERE client='dsh'").fetchall():
        head = _dsh_session_head(sf)
        if not head or not head.get('cwd'):
            missing += 1
            continue
        pk, _np = project_key_from_path(head['cwd'])
        pk = resolve_merged_key(conn, pk)
        cur = conn.execute(
            "UPDATE usage_event SET project_key=? WHERE client='dsh' AND session_id=? AND project_key_manual=0",
            (pk, sid))
        n_ev += cur.rowcount
        _register_project_session(conn, 'dsh', sid, pk, PROJECT_KIND_PROJECT,
                                  path_display_name(head['cwd']),
                                  head.get('title'), head.get('agent'), ts)
        resolved += 1
    conn.commit()
    report['dsh'] = {'sessions_resolved': resolved, 'events_updated': n_ev,
                     'sources_missing': missing}

    # ---- workbuddy：逐 source_file 读 ai-title + cwd ----
    n_ev = resolved = missing = 0
    for sid, sf in conn.execute(
            "SELECT DISTINCT session_id, source_file FROM usage_event WHERE client='workbuddy'").fetchall():
        head = _workbuddy_session_head(sf)
        if not head or not head.get('cwd'):
            missing += 1
            continue
        pk, _np = project_key_from_path(head['cwd'])
        pk = resolve_merged_key(conn, pk)
        cur = conn.execute(
            "UPDATE usage_event SET project_key=? WHERE client='workbuddy' AND session_id=? AND project_key_manual=0",
            (pk, sid))
        n_ev += cur.rowcount
        _register_project_session(conn, 'workbuddy', sid, pk, PROJECT_KIND_WORK_ITEM,
                                  path_display_name(head['cwd']),
                                  head.get('title'), None, ts)
        resolved += 1
    conn.commit()
    report['workbuddy'] = {'sessions_resolved': resolved, 'events_updated': n_ev,
                           'sources_missing': missing}

    # ---- catpaw / traecn：活动归因（只写注册表，不产生 token/cost）----
    n_sess = missing = 0
    for sk, sf in conn.execute(
            "SELECT DISTINCT session_key, source_file FROM activity_event WHERE client='catpaw'").fetchall():
        head = _catpaw_session_head(sf)
        if not head or not head.get('cwd'):
            missing += 1
            continue
        pk, _np = project_key_from_path(head['cwd'])
        _register_project_session(conn, 'catpaw', sk, pk, PROJECT_KIND_WORKSPACE,
                                  path_display_name(head['cwd']), None, None, ts)
        n_sess += 1
    conn.commit()
    report['catpaw'] = {'sessions': n_sess, 'sources_missing': missing}

    n_sess = 0
    for sk, sf in conn.execute(
            "SELECT DISTINCT session_key, source_file FROM activity_event WHERE client='traecn'").fetchall():
        mproj = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(sf))))
        pk = project_key_from_native('traecn', mproj) if mproj else None
        disp = None
        if mproj:
            try:
                import urllib.parse as _up
                dec = _up.unquote(mproj.replace('~', '%'))
                disp = re.sub(r'-{2,}', ' ', dec).strip(' -') or None
            except Exception:
                disp = None
        _register_project_session(conn, 'traecn', sk, pk, PROJECT_KIND_PROJECT,
                                  disp, None, None, ts)
        n_sess += 1
    conn.commit()
    report['traecn'] = {'sessions': n_sess}

    print('  Phase 0 归因回填完成（幂等，可重复执行）：')
    for k, v in report.items():
        print('    [%-9s] %s' % (k, v))

    print('  回填后 EFFECTIVE project coverage：')
    for r in conn.execute("""
            SELECT client, COUNT(*) n,
                   SUM(CASE WHEN project_key IS NOT NULL THEN 1 ELSE 0 END) p
            FROM usage_event WHERE is_replay=0 GROUP BY client"""):
        pct = (r['p'] / r['n'] * 100) if r['n'] else 0.0
        print('    [%-9s] %d / %d（%.1f%%）' % (r['client'], r['p'], r['n'], pct))
    conn.close()
    return 0


def cmd_board(args):
    board = build_board()
    out = args.out or BOARD_PATH
    tmp = out + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(board, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)
    s = board['summary']
    d = board.get('dedup') or {}
    print('  已写入 %s' % out)
    print('    schema_version=%d  窗口=%d 天  口径=%s'
          % (board['schema_version'], board['window_days'],
             d.get('default_view', 'raw')))
    print('    计费请求 %s ｜ 新输入 %s ｜ 输出 %s ｜ 缓存读取 %s'
          % (f"{s['usage_events']:,}", f"{s['input_tokens']:,}",
             f"{s['output_tokens']:,}", f"{s['cache_read_tokens']:,}"))
    by_cur = s.get('estimated_cost_by_currency') or {}
    if s['estimated_cost_usd_top50_models'] is None and not by_cur:
        print('    已定价成本：未配置（%d 个模型没有单价，不折算成 $0）' % s['unpriced_models'])
    else:
        sym = {'USD': '$', 'CNY': '¥'}
        for cur in sorted(by_cur):
            print('    已定价成本 %s：%s%.4f' % (cur, sym.get(cur, cur + ' '),
                                              by_cur[cur]))
        print('    已定价模型 %d 个 / 未配置 %d 个'
              % (s['priced_models'], s['unpriced_models']))
        if len(by_cur) > 1:
            print('    （多币种：未做汇率换算，不可直接相加）')
    if d.get('replay_events'):
        print('    审计：raw %s ＝ effective %s ＋ 已验证重放 %s'
              % (f"{d['raw_events']:,}", f"{d['effective_events']:,}",
                 f"{d['replay_events']:,}"))
    print('    privacy: paths_included=false, event_ids_included=false')
    return EXIT_OK


# ---------------------------------------------------------------- 价格同步（唯一的联网层）

PRICING_SOURCES = {
    'litellm': {
        'url': 'https://raw.githubusercontent.com/BerriAI/litellm/main/'
               'model_prices_and_context_window.json',
        'source_id': 'litellm/model_prices_and_context_window.json',
        'scale': 1_000_000,          # LiteLLM 是「每 token」，换算成「每百万 token」
    },
    'openrouter': {
        'url': 'https://openrouter.ai/api/v1/models',
        'source_id': 'openrouter/api/v1/models',
        'scale': 1_000_000,
    },
}

# 允许用环境变量覆盖价格源地址：企业内网镜像、离线自测都会用到。
#   USAGE_LEDGER_PRICING_URL_LITELLM=file:///D:/mirror/litellm.json
for _name in list(PRICING_SOURCES):
    _ov = os.environ.get('USAGE_LEDGER_PRICING_URL_%s' % _name.upper())
    if _ov:
        PRICING_SOURCES[_name]['url'] = _ov
        PRICING_SOURCES[_name]['source_id'] = _ov


def _fetch_json(url, timeout=25):
    req = urllib.request.Request(url, headers={'User-Agent': 'usage-ledger/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8', 'replace'))


def _norm_price(v, scale):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return round(f * scale, 8)


def _parse_litellm(raw):
    out = {}
    for model, row in (raw or {}).items():
        if not isinstance(row, dict):
            continue
        inp = _norm_price(row.get('input_cost_per_token'), 1_000_000)
        outp = _norm_price(row.get('output_cost_per_token'), 1_000_000)
        if inp is None and outp is None:
            continue
        out[model] = {
            'input': inp, 'output': outp,
            'cache_read': _norm_price(row.get('cache_read_input_token_cost'), 1_000_000),
            'cache_write': _norm_price(row.get('cache_creation_input_token_cost'), 1_000_000),
        }
    return out


def _parse_openrouter(raw):
    out = {}
    for row in (raw or {}).get('data', []) or []:
        mid = row.get('id')
        pr = row.get('pricing') or {}
        if not mid:
            continue
        inp = _norm_price(pr.get('prompt'), 1_000_000)
        outp = _norm_price(pr.get('completion'), 1_000_000)
        if inp is None and outp is None:
            continue
        out[mid] = {
            'input': inp, 'output': outp,
            'cache_read': _norm_price(pr.get('input_cache_read'), 1_000_000),
            'cache_write': _norm_price(pr.get('input_cache_write'), 1_000_000),
        }
    return out


# ==================================================================
# V3：项目写入能力（Alias / 归项目 / Merge）—— 手工归因的唯一写入口。
# 原则：只动 project_key 与注册表元数据，绝不改 token/事件原始行；
# 每次写入后原子重建 usage-board.json；调用方（serve.py）负责扫描锁互斥。
# ==================================================================

def _project_meta(conn, key):
    row = conn.execute(
        'SELECT metadata_json FROM project_registry WHERE project_key=?',
        (key,)).fetchone()
    if row is None or not row[0]:
        return {}
    try:
        m = json.loads(row[0])
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def write_board():
    """重建并原子写盘 usage-board.json（写入能力完成后调用）。"""
    board = build_board()
    tmp = BOARD_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(board, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOARD_PATH)
    return board


def _commit_project_write(conn):
    """Keep the ledger and derived board unchanged when board rebuilding fails."""
    previous = None
    if os.path.isfile(BOARD_PATH):
        with open(BOARD_PATH, 'rb') as f:
            previous = f.read()
    tmp = BOARD_PATH + '.tmp'
    replaced = False
    try:
        board = build_board(conn=conn)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(board, f, ensure_ascii=False, indent=2)
        os.replace(tmp, BOARD_PATH)
        replaced = True
        conn.commit()
    except Exception:
        conn.rollback()
        if replaced:
            if previous is None:
                os.remove(BOARD_PATH)
            else:
                with open(tmp, 'wb') as f:
                    f.write(previous)
                os.replace(tmp, BOARD_PATH)
        elif os.path.exists(tmp):
            os.remove(tmp)
        raise


def _require_project(conn, key):
    if not isinstance(key, str) or not key.strip():
        raise ValueError('project_key 必须是非空字符串')
    if not conn.execute('SELECT 1 FROM project_registry WHERE project_key=?', (key,)).fetchone():
        raise ValueError('项目不存在：%s' % key)
    return resolve_merged_key(conn, key)


def _ensure_manual_project(conn, project_key, display_name):
    now = now_iso()
    row = conn.execute(
        'SELECT project_key FROM project_registry WHERE project_key=?',
        (project_key,)).fetchone()
    if row is None:
        conn.execute(
            'INSERT INTO project_registry (project_key, project_kind,'
            ' display_name, first_seen_at, last_seen_at) VALUES (?,?,?,?,?)',
            (project_key, 'manual', display_name or project_key, now, now))


def manual_project_key(name):
    """用户新建项目名的稳定键（manual 命名空间，绝不与路径/原生键冲突）。"""
    name = (name or '').strip()
    if not name:
        raise ValueError('项目名不能为空')
    return 'm' + hashlib.sha256(
        ('manual|' + name.lower()).encode('utf-8', 'replace')).hexdigest()[:16]


def project_alias_add(alias, project_key=None, display_name=None):
    """给项目添加别名；project_key 缺省时按别名新建 manual 项目。幂等。"""
    alias = (alias or '').strip()
    if not alias:
        raise ValueError('别名不能为空')
    creating = project_key is None
    if creating:
        project_key = manual_project_key(alias)
    conn = connect(create=True)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if creating:
            _ensure_manual_project(conn, project_key, display_name or alias)
        else:
            project_key = _require_project(conn, project_key)
        meta = _project_meta(conn, project_key)
        aliases = meta.get('aliases') or []
        if alias not in aliases:
            aliases.append(alias)
        meta['aliases'] = aliases
        if display_name:
            row = conn.execute(
                'SELECT display_name FROM project_registry WHERE project_key=?',
                (project_key,)).fetchone()
            # 首次改名时保留自动识别名作为 original_display（provenance）
            if row and row[0] and 'original_display' not in meta:
                meta['original_display'] = row[0]
            conn.execute(
                'UPDATE project_registry SET display_name=?,'
                ' display_name_manual=1 WHERE project_key=?',
                (display_name, project_key))
        conn.execute(
            'UPDATE project_registry SET metadata_json=? WHERE project_key=?',
            (json.dumps(meta, ensure_ascii=False), project_key))
        _commit_project_write(conn)
        events = conn.execute(
            'SELECT COUNT(*) FROM usage_event WHERE project_key=?',
            (project_key,)).fetchone()[0]
    finally:
        conn.close()
    return {'ok': True, 'project_key': project_key, 'alias': alias,
            'events': events}


def project_assign_sessions(project_key, sessions, display_name=None):
    """把会话（含其全部事件）归入指定项目。sessions: [{source, session_id}]。"""
    if not isinstance(sessions, list) or not sessions:
        raise ValueError('sessions 不能为空')
    conn = connect(create=True)
    try:
        conn.execute('BEGIN IMMEDIATE')
        project_key = _require_project(conn, project_key)
        moved_sessions = moved_events = 0
        now = now_iso()
        for s in sessions:
            if not isinstance(s, dict):
                raise ValueError('sessions 元素必须是对象')
            if not isinstance(s.get('source'), str) or not isinstance(s.get('session_id'), str):
                raise ValueError('source 与 session_id 必须是非空字符串')
            src = s['source'].strip()
            sid = s['session_id'].strip()
            if not src or not sid:
                raise ValueError('sessions 元素必须含 source 与 session_id')
            exists = conn.execute(
                'SELECT 1 FROM usage_event WHERE client=? AND session_id=?'
                ' UNION ALL SELECT 1 FROM activity_event WHERE client=? AND session_key=? LIMIT 1',
                (src, sid, src, sid)).fetchone()
            if not exists:
                raise ValueError('会话不存在：%s / %s' % (src, sid))
            n = conn.execute(
                'UPDATE session_registry SET project_key=?, attribution_manual=1'
                ' WHERE source=? AND session_id=?',
                (project_key, src, sid)).rowcount
            if not n:
                # 无注册行的会话（如未归属事件）：补建注册行，让它出现在项目下钻里
                conn.execute(
                    'INSERT OR IGNORE INTO session_registry'
                    ' (source, session_id, project_key, attribution_manual,'
                    '  first_seen_at, last_seen_at)'
                    ' VALUES (?,?,?,?,?,?)',
                    (src, sid, project_key, 1, now, now))
                n = 1
            moved_sessions += n
            moved_events += conn.execute(
                'UPDATE usage_event SET project_key_auto=COALESCE(project_key_auto,project_key),'
                ' project_key=?, project_key_manual=1'
                ' WHERE client=? AND session_id=?',
                (project_key, src, sid)).rowcount
        _commit_project_write(conn)
    finally:
        conn.close()
    return {'ok': True, 'project_key': project_key,
            'sessions_updated': moved_sessions, 'events_updated': moved_events}


def project_merge(from_key, to_key):
    """把 from 项目整体并入 to：事件/会话迁移 + 别名合并 + 注册表留痕。"""
    from_key = (from_key or '').strip()
    to_key = (to_key or '').strip()
    if not from_key or not to_key:
        raise ValueError('from/to project_key 不能为空')
    if from_key == to_key:
        raise ValueError('不能把项目合并到它自己')
    conn = connect(create=True)
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM project_registry WHERE project_key=?',
                        (from_key,)).fetchone() is None:
            raise ValueError('源项目不存在：%s' % from_key)
        if conn.execute('SELECT 1 FROM project_registry WHERE project_key=?',
                        (to_key,)).fetchone() is None:
            raise ValueError('目标项目不存在：%s' % to_key)
        from_key = resolve_merged_key(conn, from_key)
        to_key = resolve_merged_key(conn, to_key)
        if from_key == to_key:
            raise ValueError('不能合并到同一项目或形成合并循环')
        # Merge 是用户决策：迁移行标记 manual=1，重扫不再回到旧 key；
        # 新事件若再派生旧 key，由 resolve_merged_key 在入口改写到 to_key。
        events = conn.execute(
            'UPDATE usage_event SET project_key_auto=COALESCE(project_key_auto,project_key),'
            ' project_key=?, project_key_manual=1'
            ' WHERE project_key=?', (to_key, from_key)).rowcount
        sessions = conn.execute(
            'UPDATE session_registry SET project_key=?, attribution_manual=1'
            ' WHERE project_key=?', (to_key, from_key)).rowcount
        mf = _project_meta(conn, from_key)
        mt = _project_meta(conn, to_key)
        fd = conn.execute(
            'SELECT display_name FROM project_registry WHERE project_key=?',
            (from_key,)).fetchone()
        aliases = set(mt.get('aliases') or []) | set(mf.get('aliases') or [])
        if fd and fd[0]:
            aliases.add(fd[0])
        mt['aliases'] = sorted(aliases)
        mt['merged_from'] = (mt.get('merged_from') or []) + [from_key]
        conn.execute(
            'UPDATE project_registry SET metadata_json=? WHERE project_key=?',
            (json.dumps(mt, ensure_ascii=False), to_key))
        # 源注册表留痕（不删行）：events 已全部迁走，board 不再展示
        mf['merged_into'] = to_key
        conn.execute(
            'UPDATE project_registry SET metadata_json=? WHERE project_key=?',
            (json.dumps(mf, ensure_ascii=False), from_key))
        _commit_project_write(conn)
    finally:
        conn.close()
    return {'ok': True, 'from': from_key, 'to': to_key,
            'events_moved': events, 'sessions_moved': sessions}


def cmd_pricing_sync(args):
    """显式联网价格同步。失败必须返回非零码，并且**保留旧缓存**。

    缓存保护分两层：
      1. 本轮所有来源都失败 → 完全不写文件（旧缓存原样保留），返回非零码。
      2. 部分来源失败 → 把新结果**按字段合并到旧缓存之上**，而不是整份替换。
         否则「litellm 成功、openrouter 失败」会把上一次从 openrouter 拿到的
         价格条目全部抹掉 —— 那也是破坏缓存。
    """
    names = ['litellm', 'openrouter'] if args.source == 'all' else [args.source]
    merged, used, errors = {}, [], []
    for name in names:
        spec = PRICING_SOURCES[name]
        try:
            raw = _fetch_json(spec['url'])
        except urllib.error.HTTPError as e:
            errors.append('%s: HTTP %s' % (name, e.code))
            print('  ! %s 拉取失败：HTTP %s' % (name, e.code), file=sys.stderr)
            continue
        except urllib.error.URLError as e:
            errors.append('%s: %s' % (name, e.reason))
            print('  ! %s 拉取失败：%s' % (name, e.reason), file=sys.stderr)
            continue
        except Exception as e:
            errors.append('%s: %s' % (name, e))
            print('  ! %s 拉取失败：%s' % (name, e), file=sys.stderr)
            continue
        parsed = _parse_litellm(raw) if name == 'litellm' else _parse_openrouter(raw)
        if not parsed:
            errors.append('%s: 解析出 0 条价格' % name)
            print('  ! %s 解析出 0 条价格' % name, file=sys.stderr)
            continue
        used.append(name)
        # 后到的源不覆盖先到的已有条目：先 litellm 后 openrouter，缺失字段才补
        for k, v in parsed.items():
            cur = merged.setdefault(k, {})
            for f in PRICE_FIELDS:
                if cur.get(f) is None and v.get(f) is not None:
                    cur[f] = v[f]
        print('  %s：取得 %d 个模型价格' % (name, len(parsed)))

    if not used:
        print()
        print('  价格同步失败：没有从任何来源取得价格。')
        print('  已保留原有缓存 %s（未改动）。' % PRICING_AUTO_PATH)
        for e in errors:
            print('    · %s' % e)
        return EXIT_PARTIAL

    # 以旧缓存为底，新数据按字段覆盖上去 —— 本轮没覆盖到的模型不会丢
    base = {}
    kept = 0
    if os.path.isfile(PRICING_AUTO_PATH):
        try:
            with open(PRICING_AUTO_PATH, encoding='utf-8') as f:
                raw_old = json.load(f)
            for k, v in raw_old.items():
                if k.startswith('_') or not isinstance(v, dict):
                    continue
                base[k] = {f: v.get(f) for f in PRICE_FIELDS}
        except Exception as e:
            errors.append('旧缓存不可读（将整份重建）：%s' % e)
    for k, v in merged.items():
        cur = base.setdefault(k, {f: None for f in PRICE_FIELDS})
        for f in PRICE_FIELDS:
            if v.get(f) is not None:
                cur[f] = v[f]
    kept = len([k for k in base if k not in merged])

    payload = {
        '_meta': {
            'source': ','.join(used),
            'source_id': ','.join(PRICING_SOURCES[n]['source_id'] for n in used),
            'retrieved_at': now_iso(),
            'models_total': len(base),
            'models_this_run': len(merged),
            'models_kept_from_cache': kept,
            'note': '聚合目录价，仅作估算输入；部分供应商存在峰谷/Batch/Priority/阶梯计费。'
                    '手工 pricing.json 的条目优先级高于本文件。本文件按字段合并，'
                    '本轮未覆盖到的旧条目会保留。',
            'errors': errors,
        }
    }
    payload.update(base)
    tmp = PRICING_AUTO_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PRICING_AUTO_PATH)
    print()
    print('  已写入 %s（共 %d 个模型：本轮取得 %d，沿用旧缓存 %d；来源 %s）'
          % (PRICING_AUTO_PATH, len(base), len(merged), kept, ','.join(used)))
    if errors:
        print('  部分来源失败（已记录在 _meta.errors，旧条目已保留）：%s' % '; '.join(errors))
        return EXIT_PARTIAL
    return EXIT_OK


# ---------------------------------------------------------------- 价格候选

PRICE_FIELDS = ('input', 'output', 'cache_read', 'cache_write')


def _norm_model(s):
    """把模型名归一化，用于**找候选**（不用于自动定价）。

    `dashscope/glm-5.1` → `glm51`；`deepseek-ai/DeepSeek-V3.2` → `deepseekv32`
    """
    s = (s or '').strip().lower()
    s = s.split('/')[-1]                      # 去掉供应商前缀
    s = re.sub(r'[^a-z0-9\u4e00-\u9fff]+', '', s)
    return s


def _raw_key(s):
    """取不带供应商前缀的原始键，用于展示。"""
    return (s or '').strip().split('/')[-1]


def price_candidates(model, pricing, limit=4):
    """给一个没匹配到单价的模型找**候选键**。

    只做检索与排序，**绝不自动选定**：供应商前缀、版本后缀、日期快照这些差异
    必须由人判断（价格错配比缺价更危险）。
    返回 [{'key','score','reason','input','output','cache_read','cache_write'}]
    """
    nm = _norm_model(model)
    if not nm:
        return []
    raw = _raw_key(model).lower()
    scored = []
    for key, val in pricing.items():
        nk = _norm_model(key)
        if not nk:
            continue
        # 供应商前缀取**原始大小写**：索引键被小写化过，
        # 用索引键会让 `vendorA` 显示成 `vendora`，候选就不好认了。
        prefix = (val.get('_key') or key).split('/')[0] \
            if '/' in (val.get('_key') or key) else ''
        score, reason = 0, ''
        if nk == nm:
            score, reason = 100, '归一化后完全相同'
        elif nk.startswith(nm) or nk.endswith(nm):
            score, reason = 82, '归一化后是前缀/后缀关系'
            if abs(len(nk) - len(nm)) <= 3:
                score, reason = 90, '归一化后仅差少量后缀'
        elif nm in nk or nk in nm:
            score, reason = 66, '归一化后互相包含'
        elif raw and raw in key.lower():
            score, reason = 58, '原始名出现在候选键中'
        else:
            a, b = set(nm), set(nk)
            ov = len(a & b) / max(len(a | b), 1)
            if ov >= 0.72:
                score, reason = int(40 + ov * 20), '字符重合度 %.0f%%' % (ov * 100)
        if score <= 0:
            continue
        if prefix:
            reason += ' · 供应商 %s' % prefix
        vals = {f: val.get(f) for f in PRICE_FIELDS}
        if all(vals[f] is None for f in PRICE_FIELDS):
            reason += ' · 该键没有可用价格数据'
        scored.append({'key': val.get('_key') or key, 'score': score,
                       'reason': reason, **vals})
    scored.sort(key=lambda x: (-x['score'], x['key']))
    return scored[:limit]


def model_pricing_gaps(conn=None, pricing=None, limit_models=20, limit_cand=4):
    """列出「出现过的模型但没单价」的清单，并给出候选（不含自动选择）。"""
    own = None
    try:
        own = conn or connect()
        pricing = pricing if pricing is not None else load_pricing()
        rows = own.execute("""
            SELECT model, COUNT(*) events,
                   SUM(input_tokens) i, SUM(output_tokens) o,
                   SUM(cache_read_tokens) cr
            FROM usage_event WHERE model <> '' GROUP BY model
        """).fetchall()
    except Exception:
        return []
    finally:
        if conn is None and own is not None:
            try:
                own.close()
            except Exception:
                pass

    gaps = []
    for r in rows:
        c, ok = cost_of(pricing, r['model'], r['i'] or 0, r['o'] or 0,
                        r['cr'] or 0, 0)
        if ok:
            continue
        gaps.append({
            'model': r['model'],
            'events': r['events'],
            'tokens': (r['i'] or 0) + (r['cr'] or 0),
            'candidates': price_candidates(r['model'], pricing, limit_cand),
        })
    gaps.sort(key=lambda x: -x['tokens'])
    return gaps[:limit_models]


def cmd_pricing_candidates(args):
    """输出未匹配单价的模型及其候选键 —— 只给候选，不自动选。"""
    pricing = load_pricing()
    gaps = model_pricing_gaps(pricing=pricing, limit_models=args.limit)
    if not gaps:
        print()
        print('  账本里出现过的模型都有单价了，没有候选要列。')
        print()
        return EXIT_OK
    print()
    print('  未匹配单价的模型（成本不计入；下面只是**候选**，需要你自己判断后手填）')
    print('  优先级：pricing.json（手工）> pricing.auto.json（自动），按字段合并')
    print()
    for g in gaps:
        print('  ● %s' % g['model'])
        print('      %s 请求 · %s token（新输入+缓存读取）'
              % (f"{g['events']:,}", f"{g['tokens']:,}"))
        if not g['candidates']:
            print('      （没找到任何候选；这个模型可能不在任何价目表里）')
            continue
        for c in g['candidates']:
            print('      - %-46s %s' % (c['key'], c['reason']))
            print('          in=%s out=%s cache_read=%s cache_write=%s'
                  % (c['input'], c['output'], c['cache_read'], c['cache_write']))
    print()
    print('  确认后用 3 个字段写进 pricing.json，例如：')
    print('    {')
    print('      "你的模型名": {"input": 0.5, "output": 1.5,'
          ' "cache_read": 0.05, "cache_write": null}')
    print('    }')
    print('  单位：美元 / 每 100 万 token。留 null 的字段不会覆盖自动同步到的值。')
    print('  本命令**不会**替你选，也**不会**写入任何文件。')
    print()
    return EXIT_OK


# ---------------------------------------------------------------- html 报告

CLIENT_COLOR = {
    'zcode': '#1D9E75',
    'dsh': '#7F77DD',
    'workbuddy': '#378ADD',
}


def _compact(n):
    n = float(n or 0)
    if n >= 1e9:
        return '%.2fB' % (n / 1e9)
    if n >= 1e6:
        return '%.1fM' % (n / 1e6)
    if n >= 1e3:
        return '%.1fK' % (n / 1e3)
    return '%d' % n


def cmd_html(args):
    conn = connect()
    if not os.path.isfile(DB_PATH):
        print('  账本不存在，先跑 scan')
        return

    span = conn.execute('SELECT MIN(ts_ms) a, MAX(ts_ms) b, COUNT(*) c FROM usage_event').fetchone()
    if not span['c']:
        print('  账本为空，先跑 scan')
        return

    # 按天 × 客户端
    daily = conn.execute("""
        SELECT date(ts_ms/1000,'unixepoch','localtime') AS day, client,
               COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr
        FROM usage_event GROUP BY day, client ORDER BY day
    """).fetchall()
    days = sorted({r['day'] for r in daily})

    # 只画最近 45 天，避免图过长
    if len(days) > 45:
        days = days[-45:]
        keep = set(days)
        daily = [r for r in daily if r['day'] in keep]

    per_day = {}
    for r in daily:
        per_day.setdefault(r['day'], {})[r['client']] = r

    day_total = {}
    for d in days:
        day_total[d] = sum((c['i'] or 0) + (c['o'] or 0) for c in per_day.get(d, {}).values())
    peak = max(day_total.values()) if day_total else 1

    clients = conn.execute("""
        SELECT client, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(reasoning_tokens) r, SUM(cache_read_tokens) cr,
               MIN(ts_ms) a, MAX(ts_ms) b
        FROM usage_event GROUP BY client ORDER BY (SUM(input_tokens)+SUM(cache_read_tokens)) DESC
    """).fetchall()
    models = conn.execute("""
        SELECT model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr
        FROM usage_event WHERE model <> '' GROUP BY model
        ORDER BY (SUM(input_tokens)+SUM(cache_read_tokens)) DESC LIMIT 20
    """).fetchall()

    pricing = load_pricing()
    mrows = conn.execute("""
        SELECT model, SUM(input_tokens) i, SUM(output_tokens) o,
               SUM(cache_read_tokens) cr, SUM(cache_write_tokens) cw
        FROM usage_event GROUP BY model
    """).fetchall()
    total_cost = 0.0
    priced = set()
    for r in mrows:
        c, ok = cost_of(pricing, r['model'], r['i'] or 0, r['o'] or 0, r['cr'] or 0, r['cw'] or 0)
        if ok:
            total_cost += c
            priced.add((r['model'] or '').strip().lower())

    health = []
    for client, spec in SOURCES.items():
        rows = conn.execute(
            'SELECT path, missing_since FROM source_file WHERE client = ?', (client,)).fetchall()
        alive = len([r for r in rows if not r['missing_since']])
        gone = len([r for r in rows if r['missing_since']])
        if spec.get('mode') == 'activity':
            ev = conn.execute('SELECT COUNT(*) c FROM activity_event WHERE client=?',
                              (client,)).fetchone()['c']
            mode = '活动量'
        else:
            ev = conn.execute('SELECT COUNT(*) c FROM usage_event WHERE client=?',
                              (client,)).fetchone()['c']
            mode = '用量'
        health.append((spec['label'], client, spec['path'], os.path.exists(spec['path']),
                       alive, gone, ev, mode))

    # 活动量（不记 token 的客户端）
    act_rows = conn.execute("""
        SELECT client,
               COUNT(DISTINCT date(ts_ms/1000,'unixepoch','localtime')) days,
               COUNT(DISTINCT session_key) sessions,
               COUNT(*) n, MIN(ts_ms) a, MAX(ts_ms) b
        FROM activity_event GROUP BY client ORDER BY n DESC
    """).fetchall()

    dup = conn.execute("""
        SELECT COUNT(*) c, COALESCE(SUM(n-1),0) extra FROM (
            SELECT COUNT(*) n FROM usage_event WHERE client='dsh'
            GROUP BY ts_ms, input_tokens, output_tokens, cache_read_tokens, model
            HAVING COUNT(DISTINCT session_id) > 1)
    """).fetchone()

    ti = sum(r['i'] or 0 for r in clients)
    to = sum(r['o'] or 0 for r in clients)
    tr = sum(r['r'] or 0 for r in clients)
    tcr = sum(r['cr'] or 0 for r in clients)
    tn = sum(r['n'] for r in clients)

    def esc(s):
        return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

    rows_html = []
    for d in days:
        cells = per_day.get(d, {})
        segs = []
        for cname in ('zcode', 'workbuddy', 'dsh'):
            if cname in cells:
                v = (cells[cname]['i'] or 0) + (cells[cname]['o'] or 0)
                pct = v / peak * 100
                segs.append(
                    '<span class="seg" style="width:%.3f%%;background:%s" title="%s %s"></span>'
                    % (pct, CLIENT_COLOR[cname], cname, _compact(v)))
        rows_html.append(
            '<div class="row"><span class="lbl">%s</span>'
            '<span class="bar">%s</span>'
            '<span class="val">%s</span></div>'
            % (d[5:], ''.join(segs), _compact(day_total.get(d, 0))))

    client_rows = []
    for r in clients:
        days_span = ''
        if r['a']:
            days_span = '%s ~ %s' % (day_of(r['a']), day_of(r['b']))
        client_rows.append(
            '<tr><td><span class="dot" style="background:%s"></span>%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="dim">%s</td></tr>'
            % (CLIENT_COLOR.get(r['client'], '#888780'), esc(r['client']),
               f"{r['n']:,}", f"{r['i'] or 0:,}", f"{r['o'] or 0:,}",
               f"{r['r'] or 0:,}", f"{r['cr'] or 0:,}", days_span))

    model_rows = []
    for r in models:
        has_p = (r['model'] or '').strip().lower() in priced
        model_rows.append(
            '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
            % (esc(r['model']), f"{r['n']:,}", f"{r['i'] or 0:,}", f"{r['o'] or 0:,}",
               f"{r['cr'] or 0:,}", '✓' if has_p else '—'))

    health_rows = []
    for label, client, path, exists, alive, gone, ev, mode in health:
        status = ('<span class="pill ok">源目录在</span>' if exists
                  else '<span class="pill bad">源目录不在</span>')
        health_rows.append(
            '<tr><td>%s</td><td class="dim">%s</td><td class="mono dim">%s</td>'
            '<td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>'
            % (esc(label), esc(mode), esc(path), f'{ev:,}', f'{alive} / {gone}', status))

    activity_rows = []
    for r in act_rows:
        rng_txt = '%s ~ %s' % (day_of(r['a']), day_of(r['b'])) if r['a'] else ''
        activity_rows.append(
            '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="dim">%s</td></tr>'
            % (esc(CLIENT_LABEL.get(r['client'], r['client'])),
               f"{r['days']:,}", f"{r['sessions']:,}", f"{r['n']:,}", rng_txt))

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>智账 · PathOrbit AI Ledger</title>
<style>
:root{--bg:#FAFAF8;--card:#FFFFFF;--line:#E4E2DB;--tx:#2C2C2A;--dim:#888780}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--tx);
 font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1020px;margin:0 auto}
h1{font-size:22px;font-weight:500;margin:0 0 4px}
h2{font-size:15px;font-weight:500;margin:32px 0 12px}
.sub{color:var(--dim);font-size:13px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card .k{color:var(--dim);font-size:12px}
.card .v{font-size:22px;font-weight:500;margin-top:6px;font-variant-numeric:tabular-nums}
.card .h{color:var(--dim);font-size:11px;margin-top:2px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px}
.row{display:flex;align-items:center;gap:10px;margin-bottom:5px}
.lbl{width:52px;color:var(--dim);font-size:11px;font-variant-numeric:tabular-nums;flex:none}
.bar{flex:1;height:14px;background:#F1EFE8;border-radius:3px;display:flex;overflow:hidden}
.seg{display:block;height:100%%}
.val{width:62px;text-align:right;font-size:11px;color:var(--dim);
 font-variant-numeric:tabular-nums;flex:none}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--dim);font-weight:400;font-size:12px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:7px}
.dim{color:var(--dim)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11px}
.pill.ok{background:#E1F5EE;color:#085041}
.pill.bad{background:#FCEBEB;color:#791F1F}
.note{color:var(--dim);font-size:12px;margin-top:10px}
.legend{display:flex;gap:16px;margin-bottom:14px;font-size:12px;color:var(--dim)}
.warn{background:#FAEEDA;border:1px solid #EF9F27;border-radius:10px;padding:12px 16px;
 font-size:13px;margin-top:12px}
</style></head><body><div class="wrap">
<h1>智账</h1>
<div class="sub">用量跨度 %s ~ %s · 用量记录 %s 条 · 活动量 %s 条 · 生成于 %s</div>

<div class="cards">
  <div class="card"><div class="k">计费请求</div><div class="v">%s</div>
    <div class="h">三个客户端合计</div></div>
  <div class="card"><div class="k">新输入 token</div><div class="v">%s</div>
    <div class="h">不含缓存读取</div></div>
  <div class="card"><div class="k">输出 token</div><div class="v">%s</div>
    <div class="h">含推理 %s</div></div>
  <div class="card"><div class="k">缓存读取 token</div><div class="v">%s</div>
    <div class="h">通常才是成本大头</div></div>
</div>

<h2>每日消耗（新输入 + 输出）</h2>
<div class="panel">
<div class="legend"><span><span class="dot" style="background:#1D9E75"></span>ZCode</span>
<span><span class="dot" style="background:#378ADD"></span>WorkBuddy</span>
<span><span class="dot" style="background:#7F77DD"></span>dsh</span></div>
%s
<div class="note">只显示最近 45 天；条长按当日在账本中的最大日消耗归一化。</div>
</div>

<h2>按客户端</h2>
<div class="panel"><table>
<tr><th>客户端</th><th class="num">请求</th><th class="num">新输入</th><th class="num">输出</th>
<th class="num">推理</th><th class="num">缓存读取</th><th>账本内跨度</th></tr>
%s
</table></div>

<h2>按模型（前 20）</h2>
<div class="panel"><table>
<tr><th>模型</th><th class="num">请求</th><th class="num">新输入</th><th class="num">输出</th>
<th class="num">缓存读取</th><th class="num">有单价</th></tr>
%s
</table>
<div class="note">成本估算合计：%s</div>
%s
</div>

<h2>活动量（本地不记 token 的客户端）</h2>
<div class="panel"><table>
<tr><th>客户端</th><th class="num">活跃天</th><th class="num">会话数</th>
<th class="num">记录数</th><th>跨度</th></tr>
%s
</table>
<div class="note">CatPaw 的会话转录、Trae CN 的记忆摘要本地都不写 token/cost
（Trae CN 的 ai-agent 数据库是加密格式），所以这两个客户端只统计活动量，
<strong>不并入也不影响</strong>上面的 token 与成本统计。</div>
</div>

<h2>数据源留存</h2>
<div class="panel"><table>
<tr><th>客户端</th><th>类型</th><th>源路径</th><th class="num">账本记录</th>
<th class="num">现存 / 已清退</th><th>状态</th></tr>
%s
</table>
<div class="note">「已清退」= 这个文件以前扫到过、现在磁盘上没了。
它的用量记录仍在账本里 —— 这就是持久账本的意义。</div>
</div>

%s
</div></body></html>""" % (
        day_of(span['a']), day_of(span['b']), f"{span['c']:,}",
        f"{sum(r['n'] for r in act_rows):,}",
        dt.datetime.now().strftime('%Y-%m-%d %H:%M'),
        f'{tn:,}', f'{ti:,}', f'{to:,}', f'{tr:,}', f'{tcr:,}',
        '\n'.join(rows_html),
        '\n'.join(client_rows),
        '\n'.join(model_rows),
        ('$%.4f' % total_cost) if total_cost else '未配置单价（跑 pricing-template）',
        ('<div class="warn">dsh 有 %d 组用量在多个会话里重复出现，'
         '估计多算约 %s 条（forked/subagent 会话重放父历史）。'
         '看 dsh 数字时保留一点折扣。</div>'
         % (dup['c'], f"{dup['extra']:,}")) if dup and dup['c'] else '',
        '\n'.join(activity_rows),
        '\n'.join(health_rows),
        ('<h2>口径说明</h2><div class="panel"><div class="note">'
         '账本内部统一为「新输入不含缓存读取，缓存读取单列」。三个客户端原始记账口径并不一致：'
         'ZCode 与 dsh 的 input 不含 cache，WorkBuddy 的 prompt_tokens 含 cache（已换算）。'
         '所以不要跨客户端直接比 input 数字。</div></div>'),
    )

    out = args.out or os.path.join(BASE, 'report.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)
    print('  已生成 HTML 报告：%s' % out)
    conn.close()


def main():
    ap = argparse.ArgumentParser(description='智账 · 你的本地 AI 使用账本')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('scan', help='扫描数据源并写入账本（幂等）')
    p.add_argument('--full', action='store_true', help='忽略增量缓存，强制全量重扫')
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser('report', help='汇总报表')
    p.add_argument('--by', choices=list(BY), default='day')
    p.add_argument('--days', type=int, default=0, help='只看最近 N 天')
    p.add_argument('--view', choices=['raw', 'effective'], default='effective',
                   help='effective=产品默认口径（剔除已验证血统重放）；'
                        'raw=完整账本审计口径')
    p.set_defaults(func=cmd_report)

    p = sub.add_parser('status', help='数据源健康与留存情况')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('activity', help='活动量报表（CatPaw / Trae CN 这类不记 token 的客户端）')
    p.add_argument('--by', choices=['client', 'day'], default='client')
    p.add_argument('--days', type=int, default=30, help='--by day 时看最近 N 天')
    p.set_defaults(func=cmd_activity)

    p = sub.add_parser('pricing-template', help='生成 pricing.json 模板')
    p.set_defaults(func=cmd_pricing_template)

    p = sub.add_parser('pricing-candidates',
                       help='列出未匹配单价的模型及其候选键（只给候选，不自动选）')
    p.add_argument('--limit', type=int, default=20, help='最多列多少个模型')
    p.set_defaults(func=cmd_pricing_candidates)

    p = sub.add_parser('export', help='导出 CSV')
    p.add_argument('--out')
    p.set_defaults(func=cmd_export)

    p = sub.add_parser('discover', help='自动发现受支持的数据源（写 discovery.json）')
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser('attribution-backfill',
                       help='Phase 0：为已入库事件补项目/会话归因（幂等，可重跑）')
    p.set_defaults(func=cmd_attribution_backfill)

    p = sub.add_parser('board', help='生成 usage-board.json（Usage Board Schema v1）')
    p.add_argument('--out')
    p.set_defaults(func=cmd_board)

    p = sub.add_parser('replay-mark',
                       help='从 dsh 源重推导血统重放标记（幂等，绝不删除）')
    p.set_defaults(func=cmd_replay_mark)

    p = sub.add_parser('pricing-sync', help='显式联网同步价格（写 pricing.auto.json）')
    p.add_argument('--source', choices=['litellm', 'openrouter', 'all'], default='all')
    p.set_defaults(func=cmd_pricing_sync)

    p = sub.add_parser('html', help='生成自包含的 HTML 报告')
    p.add_argument('--out')
    p.set_defaults(func=cmd_html)

    args = ap.parse_args()
    sys.exit(args.func(args) or EXIT_OK)


if __name__ == '__main__':
    main()
