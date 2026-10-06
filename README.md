# 智账 · PathOrbit AI Ledger

**你的本地 AI 使用账本。** 把散落在不同 AI 工具里的使用记录（token、
请求、成本估算），按项目、会话、事件整理成长期属于你自己的账本。

> 本仓库为智账的**官方源码仓**（GPL-3.0-only）。
> 正式安装包与更新通道见
> [MetalRiver/zhizhang Releases](https://github.com/MetalRiver/zhizhang/releases)。

## 产品简介

智账是一款 **local-first 的 Windows 桌面应用**（Tauri + Python 后端），
自动发现本机 AI 工具的使用记录并汇入单一 SQLite 账本：

- 自动发现本机 AI 工具（严格锚定，宁缺毋错）
- 原始记录永久审计保留（RAW），确认的重复回放被标记（REPLAY），
  默认展示口径为 EFFECTIVE
- 按项目 / 会话 / 事件三级归集，支持人工纠错（改名 / 归组 / 合并）
- 成本为「公开 API 等价估算」（CNY / USD 分列），无法确认价格时如实
  标注 null，不会算成「免费」

## 主要能力

- **账本总览**：项目 / 会话 / 用量 / 估算成本一屏掌握
- **探索**：逐事件审计（RAW / EFFECTIVE / REPLAY 三口径）
- **更新账本**：增量扫描 + 交互期间互斥保护
- **数据完整性**：每个工具的数据等级如实标注（完整用量 / 只能确认使用过）
- **托盘与后台**：可选关闭后驻留系统托盘、可选开机自启
- **账本位置自选**：任意本机磁盘、中文路径，支持安全迁移与失联恢复
- **应用内安全更新**：minisign 签名验证，验签失败拒绝安装

## Windows 支持

当前仅支持 Windows 10/11 x64（WebView2 Runtime 安装器会自动引导安装）。

## 本地优先与隐私原则

- 账本只保存在你这台电脑（可自选位置），无账号、无云同步
- 不上传 Prompt、代码、对话正文、文件路径
- 无遥测；「检查更新」仅在你点击时联网
- 卸载不删除你的账本数据

## 从源码构建

```bash
# 后端测试（Python 3.12）
python -m venv .venv
.venv\Scripts\pip install -r requirements-core.txt
.venv\Scripts\python -m unittest discover -s tests

# 桌面壳测试（Rust）
cd src-tauri && cargo test && cd ..

# 桌面版构建（需 Node.js + Rust + PyInstaller）
npm ci
.venv\Scripts\pip install -r requirements-build.txt
npm run desktop:build
```

产物：`src-tauri/target/release/bundle/nsis/` 安装器 +
PyInstaller onedir sidecar。普通构建不需要任何私钥；
正式发布签名使用 minisign 私钥（维护者持有）。

## 正式安装包

见 [Releases](https://github.com/MetalRiver/zhizhang/releases)。
应用内更新走 `latest.json` manifest + minisign 签名验证。

## 许可证

本项目源代码以 **GPL-3.0-only** 发布（见 `LICENSE`）。
品牌名称与视觉资产不随 GPL 授权，见 `BRAND.md`。
第三方组件清单见 `THIRD-PARTY-NOTICES.md`。

## 第三方产品名称声明

Codex、WorkBuddy、Trae、CatPaw、zcode、dsh 等名称归各自权利人所有。
智账与上述项目无隶属、合作或背书关系，名称仅用于兼容性与数据源说明。

## 贡献

欢迎 issue 与 PR，见 `CONTRIBUTING.md`；安全问题见 `SECURITY.md`。

## 发布历史说明

Public source history begins with **0.11.0-rc.6**.
Earlier release candidates (rc.1–rc.5) were distributed as binary
releases before the public source repository was established; their
binary releases remain available in the Releases section.
