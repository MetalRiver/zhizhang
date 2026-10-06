# Release Engineering

## 构建流程

```bash
# 1. Runtime 检查
.venv\Scripts\python scripts\check_runtime.py

# 2. 全量测试
.venv\Scripts\python -m unittest discover -s tests

# 3. PyInstaller 打包
.venv\Scripts\python -m PyInstaller UsageLedger.spec --noconfirm

# 4. 复制 web/ 到 dist（PyInstaller datas 有时不含目录）
cp -r web/ dist/UsageLedger/web/

# 5. 创建 Portable ZIP
cd dist
powershell -Command "Compress-Archive -Path 'UsageLedger' -DestinationPath 'UsageLedger-<ver>-win-x64-portable.zip' -Force"

# 6. Installer（如果安装了 Inno Setup）
iscc UsageLedger.iss

# 7. SHA-256
sha256sum UsageLedger-*.zip UsageLedger-*.exe > SHA256SUMS.txt
```

## APP_ROOT / DATA_ROOT

| | Frozen (安装/Portable) | 开发 (源码仓) |
|---|---|---|
| APP_ROOT | exe 所在目录 | repo root |
| DATA_ROOT | %LOCALAPPDATA%\UsageLedger | repo root |
| 环境变量覆盖 | USAGE_LEDGER_HOME | 不需要 |

## 升级

新版本安装覆盖 APP_ROOT（程序文件）。
DATA_ROOT（%LOCALAPPDATA%\UsageLedger\）不受影响。
schema migration 由 Core 自动处理（user_version 探测式迁移）。

## 卸载

卸载只删除 APP_ROOT。DATA_ROOT（含 usage.db）默认保留。
用户可手动删除 %LOCALAPPDATA%\UsageLedger\ 清空所有数据。

## 安全

- 仅绑定 127.0.0.1
- 无遥测、无云端上传、无自动更新
- POST scan 有 Origin 校验 + 互斥锁
- 路径穿越保护
