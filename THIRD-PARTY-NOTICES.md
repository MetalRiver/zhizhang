# THIRD-PARTY NOTICES · 智账 (PathOrbit AI Ledger)

本文件列出智账分发包中使用的第三方组件及其许可证。
各组件版权归其各自作者所有。许可证信息以各组件官方仓库 /
随包 LICENSE 文件为准。

## 运行时组件（随分发包分发）

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| CPython | 3.12 | PSF License Agreement | 后端运行时（经 PyInstaller 打包） |
| python-zstandard | 0.25.0 | BSD-3-Clause | 解析 dsh 会话（session.jsonl.zstd） |
| Tauri | 2.12.0 | MIT OR Apache-2.0 | Windows 桌面壳 |
| tauri-plugin-single-instance | 2.5.0 | MIT OR Apache-2.0 | 单实例保护 |
| tauri-plugin-updater | 2.13.1 | MIT OR Apache-2.0 | 应用内更新（minisign 签名验证） |
| tauri-plugin-dialog | 2.8.1 | MIT OR Apache-2.0 | 系统文件夹选择对话框 |
| tauri-plugin-autostart | 2.7.0 | MIT OR Apache-2.0 | 可选开机自启（用户显式开启） |
| minisign-verify | 0.7.x | MIT | 更新包签名验证 |
| rustls / rustls-family | 0.23.x | Apache-2.0 OR MIT / ISC | 更新通道 TLS |
| reqwest / hyper / tokio | 0.12 / 1.x | MIT OR Apache-2.0 | 更新通道 HTTP |
| serde / serde_json | 1.x | MIT OR Apache-2.0 | 序列化 |
| url / idna / percent-encoding | 2.x | MIT OR Apache-2.0 | URL 解析 |
| windows-sys / windows crates | 0.59+ | MIT OR Apache-2.0 | Windows API（Job Object / 托盘 / 自启） |
| tao / tao-macros | 0.30.x | MIT OR Apache-2.0 | 窗口系统（Tauri 运行时） |
| wry | 0.47.x | MIT OR Apache-2.0 | WebView 绑定（Tauri 运行时） |
| muda / tray-icon | 0.15.x | MIT OR Apache-2.0 | 菜单 / 托盘图标（Tauri 运行时） |
| MPL-2.0 组件（cssparser、cssparser-macros、dtoa-short、option-ext、selectors 等） | — | MPL-2.0 | Tauri/Url 运行时传递依赖（文件级弱 copyleft，与本项目 GPLv3 组合合规） |
| WebView2 Runtime | 随系统 | Microsoft WebView2 EULA | UI 渲染（embedBootstrapper 随装） |

## 构建期组件（不随分发包分发）

| 组件 | 许可证 |
|---|---|
| PyInstaller 6.22.3 | GPL-2.0-or-later with PyInstaller Bootloader Exception（例外明确允许分发冻结后的应用，包括商业应用） |
| pyinstaller-hooks-contrib | Apache-2.0 OR GPL-2.0-or-later（hooks 不随包分发） |
| pillow（品牌资产管线） | MIT-CMU |
| NSIS（Tauri 打包） | zlib/libpng |
| @tauri-apps/cli（npm devDependency） | MIT OR Apache-2.0 |

## 说明

- 上述运行时组件均允许随商业/免费软件再分发；PyInstaller 的
  Bootloader Exception 明确豁免了冻结应用的 GPL 义务。
- Rust 依赖以 Cargo.lock 锁定版本为准；全部为宽松许可
  （MIT / Apache-2.0 / BSD / ISC / Zlib / Unicode-3.0 / MPL-2.0），
  与本项目 GPL-3.0-only 组合合规。
- 分发包中的产品资源（图标/文案/文档）与品牌资产版权归
  PathOrbit / 途有引力 所有，**不随 GPL 授权**（见 `BRAND.md`）。
- 第三方产品名称（Codex、WorkBuddy、Trae、CatPaw、zcode、dsh 等）
  归各自权利人所有，本项目仅作兼容性与数据源说明之用。
