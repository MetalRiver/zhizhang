# integrations/pathorbit — 阶段二插件（当前**未启用**）

> **状态：暂停。** 第一阶段只做独立版 Usage Ledger，禁止修改 PathOrbit。
> 本目录里的东西**不参与** Core 的任何运行路径。

## 为什么它在这里而不在 Core

Usage Ledger Core 必须能完全脱离 PathOrbit 独立运行。所以：

```
usage-ledger/
├─ ledger.py                  Core：自动发现 / 采集 / SQLite 账本 / 出 board / 价格
├─ autopilot.py               Core：编排 + 可观察状态（run-state / discovery / auto.log）
├─ usage-dashboard.html       Core：自包含离线 Dashboard
├─ usage-board.schema.json    Core：出口协议
└─ integrations/
   └─ pathorbit/              ← 插件。Core 不 import 它，不知道它存在
      ├─ pathorbit_adapter.py
      ├─ README.md
      ├─ out/                 生成产物（可随时重建）
      └─ tests/test_adapter.py
```

Core 的源码里**一个 PathOrbit 字样都没有**，并且有一条测试专门守着这件事
（`tests/test_integration.py::TestNoPathOrbitDependency`），包括
「把 `integrations/` 整个删掉，Core 依然要能跑」这条硬断言。

## 它做什么

只读 Core 的出口 `usage-board.json`，映射成一个静态模块片段，注入到**带明确插槽**的宿主 HTML：

- 校验 Schema v1 + privacy 约束（含 `paths_included` / `event_ids_included` 必须为 `false`）
- 兜底扫描有无疑似本机路径泄漏
- 目标 HTML 里必须有 `<!-- PATHORBIT_AI_USAGE -->`，否则**拒绝盲目插入**（退出码 3）
- 不碰账本 SQLite、不写知识库正文、不联网

用法：

```bash
python integrations/pathorbit/pathorbit_adapter.py usage-board.json --check
python integrations/pathorbit/pathorbit_adapter.py usage-board.json \
    --module-out integrations/pathorbit/out/ai-usage-module.json
python integrations/pathorbit/pathorbit_adapter.py usage-board.json \
    --board-html <宿主模板> --out-html <输出>
```

测试：

```bash
python -m unittest discover -s integrations/pathorbit/tests -p "test_adapter.py"
```

## 进入第二阶段的条件

以下全部完成后才允许重新接触 PathOrbit：

- [ ] 独立版功能完成（发现 / 采集 / 账本 / 留存 / board / 价格 / 自动化）
- [ ] 独立 Dashboard 完成且离线可用
- [ ] 自动发现与自动运行完成
- [ ] 价格同步完成
- [ ] 全部独立测试通过
- [ ] 独立产品验收报告通过

在此之前，PathOrbit 看板目录处于**冻结**状态，基线哈希见
`../../docs/PATHORBIT_FREEZE_BASELINE.json`。
