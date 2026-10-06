#!/usr/bin/env python3
"""correctness_evidence.py — V1.1 correctness gate 证据采集（只读）。

明确区分两本真实账本（绝不混称）：
  A. repo dev ledger      <repo>/usage.db（开发仓的账本）
  B. desktop production   %LOCALAPPDATA%\\UsageLedger\\usage.db（桌面安装形态的正式账本）

分别记录 SHA256 / usage events / activity events / projects / sessions /
user_version / mtime。测试前后各采集一次，两本账都必须零变化。
用法：python tests/correctness_evidence.py [输出.json]（缺省只打印）
"""
import hashlib
import json
import os
import sqlite3
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

TARGETS = {
    'repo_dev_ledger': os.path.join(REPO, 'usage.db'),
    'desktop_production_ledger': os.path.join(
        os.environ.get('LOCALAPPDATA') or
        os.path.join(os.path.expanduser('~'), 'AppData', 'Local'),
        'UsageLedger', 'usage.db'),
}


def evidence(path):
    out = {'path': path, 'exists': os.path.isfile(path)}
    if not out['exists']:
        return out
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    out['sha256'] = h.hexdigest()
    conn = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro',
                           uri=True)
    try:
        out['usage_events'] = conn.execute(
            'SELECT COUNT(*) FROM usage_event').fetchone()[0]
        out['activity_events'] = conn.execute(
            'SELECT COUNT(*) FROM activity_event').fetchone()[0]
        out['projects'] = conn.execute(
            'SELECT COUNT(*) FROM project_registry').fetchone()[0]
        out['sessions'] = conn.execute(
            'SELECT COUNT(*) FROM session_registry').fetchone()[0]
        out['user_version'] = conn.execute(
            'PRAGMA user_version').fetchone()[0]
        out['mtime'] = datetime.datetime.fromtimestamp(
            os.path.getmtime(path)).isoformat(timespec='seconds')
    finally:
        conn.close()
    return out


def main():
    res = {k: evidence(p) for k, p in TARGETS.items()}
    text = json.dumps(res, ensure_ascii=False, indent=2)
    print(text)
    if len(sys.argv) > 1:
        with open(sys.argv[1], 'w', encoding='utf-8') as f:
            f.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
