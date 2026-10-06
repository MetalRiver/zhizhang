# 智账 · 安装指南

## Portable 版（推荐首次使用）

1. 下载 `UsageLedger-<version>-win-x64-portable.zip`（构建产物不入库，见 `docs/RELEASE.md` 自行打包）
2. 解压到任意目录（如 `D:\Tools\UsageLedger\`）
3. 双击 `UsageLedger.exe`
4. 浏览器自动打开 `http://127.0.0.1:8787/`

**不需要安装 Python。** 全部依赖已打包。

## Installer 版

1. 下载 `UsageLedger-0.10.0-beta.1-win-x64-setup.exe`
2. 双击安装（不需要管理员权限）
3. 安装完成后从开始菜单打开「智账」

## 数据存在哪里

| 内容 | 位置 |
|---|---|
| 程序文件 | 安装目录（Portable = 解压目录） |
| 账本 usage.db | `%LOCALAPPDATA%\UsageLedger\` |
| pricing / board / health / logs | 同上 |

**卸载程序不会删除上述数据目录。**

## 停止服务

双击安装目录中的 `stop-server.bat`，或运行 `UsageLedger.exe --stop`。

## 升级

下载新版本 installer 安装到同一目录即可。用户数据（`%LOCALAPPDATA%\UsageLedger\`）不受影响。

## 已知限制（Beta）

- DeepSeek V4.1 Flash 峰谷双轨计价暂不支持静态表达（Time-aware Pricing 债）
- 自动调度在本机可能被安全策略拦截（schtasks / PowerShell 需管理员放行）
- 成本为 API 等价估算，不是真实支付账单
