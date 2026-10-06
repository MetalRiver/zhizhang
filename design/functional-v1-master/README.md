# AI Usage Ledger · Functional V1 MASTER · Final Lock

**DESIGN REFERENCE ONLY · NOT RUNTIME**

评审日期：2026-10-02，Asia/Shanghai。目标仍为 UsageLedger.exe / Tauri v2 Windows；分支 r10-release-beta。本轮 Settings Local Rebuild + Global Minor Polish 只重构设置页布局与表达，并收口字号、CTA 和抽屉间距。Product Definition、一级 IA、浅色方向和现有计量能力保持 LOCK。设计母版进入 FINAL LOCK；下一阶段为 Runtime Integration，本轮未开始接入。

直接打开本目录 `master.html`。无需服务器、安装或联网；所有数字来自确定性示例记录，不读取真实数据库、不调用 API、不操作本机来源。名称修改、移动与合并只改变内存，刷新恢复。页面持续标注示例与非 Runtime。原暗色母版已确认淘汰并移除（原 `archive/dark-v1/` 不再存在）；本目录只有这一套浅色母版和 10 张截图。

## 评审入口

| 参数 | 页面 / 行为 |
|---|---|
| `?view=first-run` | 欢迎 → 开始查找 → 建立我的账本 → 查看总览 |
| `?view=overview` | 最近 AI 都用在哪？；项目、会话优先；我的项目占主区 |
| `?view=project` | 项目抽屉；会话与工具优先；用量及原身份逐层展开 |
| `?view=session` | 会话抽屉；最近使用记录；完整记录与移到正确项目 |
| `?view=explore` | 项目 → 会话 → 使用记录；5 列默认表格；高级审计 |
| `?view=attribution` | 整理项目 / 修改项目名称 |
| `?view=attribution&tab=move` | 归到正确项目 → 预览 → 确认 |
| `?view=attribution&tab=merge` | 合并重复项目 → 预览 → 确认 |
| `?view=confidence` | 这本账完整吗？；完整用量、部分记录、成本与历史 |
| `?view=settings` | 常用 / AI 工具 / 更新账本 / 项目整理 / 数据与隐私 / 关于 / 高级 |

一级导航固定为总览、探索、设置。详情、整理与完整性解释均为叠层，未新增产品一级导航。

顶端「设计状态索引」仅供评审。`scenario` 支持 loading、scanning、success、empty、no-sources、partial-scan、scan-error、ledger-unavailable、no-pricing（unknown-pricing 同义）、unknown、unassigned、disabled、activity-only、history-gap。每个失败或受限状态都有说明和可执行的下一步；尚未启用配置明确禁用，旁边保留手动更新入口。设置页可通过 `?view=settings&setting=common / sources / scan / attribution / privacy / about / advanced` 分别评审七个内容面；参数中的 scan 仅为内部标识，界面统一为更新账本。首次使用也可通过 `step=discovered / scanning / complete / no-sources` 定位阶段。

## 视觉与信息层级

暖浅灰页面、白色内容、深中性文字。克制金色用于主要动作和估算，青色用于用量，柔和绿 / 琥珀 / 红用于状态。轻边界和少量阴影；摘要为数字栏，默认记录表只显示时间、AI 工具、模型、使用量、估算成本。

第一层回答项目、会话、使用、估算、最近活动、完整性；第二层展开模型、工具与输入 / 输出 / 缓存；第三层保留原始字段、身份、RAW / REPLAY / EFFECTIVE、Schema / Runtime / Build。`master.css` 为尺寸与视觉事实源，`master.js` 为交互和示例状态事实源。不要把示例数据、参考条或状态索引复制进正式应用。

## 截图

| 文件 | 画布 |
|---|---|
| 01-first-run.png | 1440 × 900 |
| 02-overview.png | 1920 × 1080 |
| 03-project-detail.png | 1440 × 900 |
| 04-session-detail.png | 1440 × 900 |
| 05-explore.png | 1920 × 1080 |
| 06-settings.png | 1440 × 900 |
| 07-project-attribution.png | 1440 × 900 |
| 08-data-confidence.png | 1440 × 900 |
| 09-overview-1366.png | 1366 × 768 |
| 10-overview-1180.png | 1180 × 720 |

截图是完整窗口快照。完整内容由 HTML 的真实滚动、折叠和下钻覆盖，不以截图代替交互。1180 探索采用顶部项目选择、下方会话与记录两栏；抽屉及确认框的操作始终固定。

## 验证

`node verify.cjs` 的 38 项验收覆盖四档窗口 × 8 页面、七个设置分组 × 四档窗口、建账闭环、联合筛选、下钻、整理、用量守恒、技术信息、11 类状态、更新结果与防并发、隐私 / 高级入口和全账本整理数量。结果在 `qa-report.json`；都是设计母版验收，不是正式 Runtime tests。

`node capture.cjs --only 06-settings.png` 可只刷新设置截图；可传入多个原文件名。本轮更新 06 和其他 8 张存在可见 Minor Polish 的截图，01-first-run.png 原样保留，不为交付数量重渲染无变化页面。无参数时可完整复现 10 张截图。`capture-report.json` 分别列出 refreshed / retained。检查与截图仅使用隔离的无界面 Edge 读取本地设计 HTML，不启动 Runtime。本轮 Tabbit 稳定 CLI diagnose 返回 exit 69（路由不可用），未建立连接，不声称完成用户 Tabbit 浏览器验证。默认使用本机 bundled Playwright / Edge；可用 `LEDGER_PLAYWRIGHT`、`LEDGER_BROWSER` 指定绝对路径。DPR 1，时区 Asia/Shanghai。

`runtime-boundary-report.json` 比较本轮前后 Runtime 与生产文件哈希及 Git 差异。实现人员应同时阅读 `PRODUCT_HANDOFF.md`，尤其是原有 Runtime 接入缺口。本轮没有新增产品能力缺口，也没有修复这些接入缺口。

## Settings Final Lock

设置本身为白色主内容面，已移除外层巨型卡片。二级导航默认 118px、1366 下 106px、1180 下 98px，用细色条和文字强调选中项。一次只呈现一个分组，常用不重复工具清单。普通描述 13–14px；自动更新明确尚未启用，不显示假开关。高级数据说明直达对应折叠，数据目录按钮继续只作设计提示。

First Run 的内容、流程和截图完全未动；Overview / Drawer / Explore / Attribution / Completeness 只调整允许的字号、焦点、选中态、间距和按钮视觉层级，未改变结构。原有正式接入缺口仍以 PRODUCT_HANDOFF.md 为准。
