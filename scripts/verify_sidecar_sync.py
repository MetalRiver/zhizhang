# -*- coding: utf-8 -*-
"""R10-D.6 · 最小 Build Verification（§16）

验证「真实 Release App 使用的 web 就是当前源码 web」，防止再出现「源码新 / App 旧」。

用法（仓库根目录）：
    .venv\\Scripts\\python.exe scripts\\verify_sidecar_sync.py            # 校验
    .venv\\Scripts\\python.exe scripts\\verify_sidecar_sync.py --fix      # 校验并在不一致时同步（仅开发期应急）

校验对象：
    web/                                      源码 web（唯一事实源）
    src-tauri/binaries/usage-ledger-backend/_internal/web/    PyInstaller sidecar 内嵌 web
    src-tauri/target/release/binaries/usage-ledger-backend/_internal/web/  Release 运行时侧车 web

判定：三处正式 V1 shell/JS/CSS 的 SHA256 必须一致，缺失或不一致则 Build FAIL。
（V1.1 起 legacy rollback 页面 index.html / overview-v2.html 已退役，不在校验范围。）
"""
import hashlib
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [
    ("source", os.path.join(ROOT, "web")),
    ("sidecar", os.path.join(ROOT, "src-tauri", "binaries", "usage-ledger-backend", "_internal", "web")),
    ("release", os.path.join(ROOT, "src-tauri", "target", "release", "binaries", "usage-ledger-backend", "_internal", "web")),
]
KEY_FILES = ["v1/shell.html", "v1/app.js", "v1/app.css"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    fix = "--fix" in sys.argv
    src_dir = TARGETS[0][1]
    rows = []
    for label, d in TARGETS:
        for name in KEY_FILES:
            p = os.path.join(d, name)
            rows.append((label, name, sha256(p) if os.path.isfile(p) else None))
    src_hashes = {name: h for label, name, h in rows if label == 'source'}
    ok = True
    print("web 内容一致性校验（SHA256）")
    for label, name, h in rows:
        matches = h is not None and h == src_hashes[name]
        mark = "OK " if matches else "DIFF"
        if not matches:
            ok = False
        print(f"  [{mark}] {label:8s} {name:12s} {h if h else '(缺失)'}")
    if not ok and fix:
        print("→ 执行 --fix：把源码 web/ 同步到 sidecar / release 侧车目录")
        for label, d in TARGETS[1:]:
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            for root, _dirs, files in os.walk(src_dir):
                rel = os.path.relpath(root, src_dir)
                out = os.path.join(d, rel) if rel != "." else d
                os.makedirs(out, exist_ok=True)
                for f in files:
                    if f.endswith((".log", ".pyc")):
                        continue
                    shutil.copy2(os.path.join(root, f), os.path.join(out, f))
        print("→ 同步完成，请重新运行校验确认")
        return 0
    print("Build " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
