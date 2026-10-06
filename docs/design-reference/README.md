# docs/design-reference — DESIGN REFERENCE ONLY

本目录是**设计参考**，不是 Runtime。

- `*.png` 母版图：V3 视觉母版（NIGHT_CLOUD 系）与过程定稿。
- `final-v2-source/`：当前 Overview V2 运行时美术（`web/assets/art/final-v2/`）的
  生产母版与尺寸标注源。
- `legacy-brand/`：rebrand（AI 用量账本）之前的旧品牌图标基线。

约束（自 V3-00 起）：

1. 本目录任何文件**绝不能**被 serve.py 路由、Tauri 壳或任何 Runtime HTML/CSS/JS 引用。
2. Runtime 使用的美术必须位于 `web/assets/`（或 sidecar 内嵌副本）。
3. 新母版进入本目录时，同时更新本 README。

当前 V3 最新母版：`FINAL_VISUAL_MASTER_NIGHT_CLOUD.png` /
`FINAL_OVERVIEW_MASTER_NIGHT_CLOUD.png`（工作树），对应 R13 首页收敛验收见
`../verification/R13-home-consolidation/`。
