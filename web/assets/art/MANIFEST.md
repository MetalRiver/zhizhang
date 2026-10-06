# Brand Assets · 智账 / PathOrbit AI Ledger

**Brand**：智账
**English**：PathOrbit AI Ledger
**Parent**：PathOrbit / 途有引力
**Tagline**：你的本地 AI 使用账本

## Icon master（唯一正式母图）

`zhizhang-master.png`（1254×1254，蓝金视觉体系：打开账本 + 蓝青数据柱 +
金色轨迹环 + 金色节点 + 圆角底板）。

## 生成资产（由 scripts/build_brand_assets.py 从母图生成）

- `zhizhang-{1024,512,256,128,64,48,32,24,16}.png`：正方形居中裁切 +
  LANCZOS 缩放；≤64px 使用增强小尺寸变体（满幅裁切 + 轻度对比/锐化，
  仅工程化适配，不改设计）。
- `zhizhang.ico`：multi-size（256/128/64/48/32/24/16）。
- `src-tauri/icons/{32x32,128x128,128x128@2x,icon}.png + icon.ico`：
  Tauri bundle 要求的正式文件名，同源生成（Windows EXE/任务栏/安装器）。

关系：master →（scripts/build_brand_assets.py）→ runtime PNG/ICO →
Tauri assets。禁止引入母图之外的生成过程文件。

## V1.1 Legacy Retirement（2026-10）

Functional V1 成为唯一正式 Runtime UI 后，只为旧页面存在的资产已退役
（motif-*.svg、cloud-divider.svg、seal-archive.svg、shell/ 长卷、scene/
五层场景、final-v2/ 立绘）。它们仅存在于 Git 历史，不再随包分发。

## 旧品牌图标处置（2026-10 品牌迁移）

旧 UsageLedger 图标（icon.svg、icon-*.png、icon.ico、seal-red.svg）为
RUNTIME_OLD/UNUSED，已从当前运行时移除（Git 历史保留）。当前 runtime
favicon = `zhizhang-32.png`。

## 当前接入点

| 槽位 | 接入 | 资产 |
|---|---|---|
| 浏览器/WebView 图标 | `<link rel="icon">`（web/v1/shell.html） | `zhizhang-32.png` |
| Windows EXE/任务栏/安装器 | `tauri.conf.json > bundle > icon` | `src-tauri/icons/*`（同源） |
| 窗口/启动页/About | 智账 / PathOrbit AI Ledger | 文案（web/v1、src-tauri/public） |
