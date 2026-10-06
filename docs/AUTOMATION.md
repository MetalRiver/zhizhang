# 智账 · 自动采集调度（Automatic Scheduling）

> Round 7 冻结的架构：**Server 与 Scheduler 职责分离**。
>
> - `serve.py`：Web UI + Local API + 手动扫描（不内置任何定时器）
> - `autopilot.py auto --trigger scheduled`：计划采集（不提供任何 HTTP 服务）
> - 两者共享同一 Core / usage.db / board / `.venv` Runtime，
>   并由同一把**跨进程扫描锁**保证任何时刻最多一个 writer

## 1. 安装（一次显式授权）

```bash
.venv\Scripts\python autopilot.py install-auto --minutes 30
```

- 计划任务名固定：`UsageLedger-Auto`（不会创建第二个任务）
- 任务 Action（TR）强制使用 **`.venv\Scripts\pythonw.exe`**（无控制台窗口）：
  `"<repo>\.venv\Scripts\pythonw.exe" "<repo>\autopilot.py" auto --trigger scheduled`
- Runtime 缺失 → 安装直接失败并说明，绝不退回系统 Python
- 注册成功写入 `auto-schedule.json`（gitignore），记录：
  `enabled / interval_minutes / installed_at / runtime_executable / script / args / task_name / method`
- 注册后立即自检运行一次

## 2. 通道与真实状态

| 通道 | 说明 |
|---|---|
| `schtasks`（默认） | 系统自带；部分受管控机器会把它列入程序黑名单（WinError 5 / 拒绝访问） |
| `powershell`（fallback） | `--via powershell`，用 ScheduledTasks 模块注册，效果等价 |
| 两条都被策略阻止 | 正式状态 = **此机器自动调度不可用**；`auto-schedule.json` 保持 `enabled=false`；UI/health 不编绿灯 |

查看任务真实状态（只读）：`.venv\Scripts\python autopilot.py task-info`
查看运行状态：`.venv\Scripts\python autopilot.py status`

## 3. 卸载（安全预览 + 真卸载）

```bash
.venv\Scripts\python autopilot.py uninstall-auto --dry-run   # 预览：不删任何东西
.venv\Scripts\python autopilot.py uninstall-auto             # 真卸载
```

只删除：计划任务 `UsageLedger-Auto` 与 `auto-schedule.json`。
绝不触碰：usage.db / usage-board.json / server / 仓库文件 / 其它计划任务。

## 4. 跨进程扫描锁

三个写入入口（CLI scan / scheduled auto / API POST scan）共用
`usage-ledger-scan.lock`（`O_CREAT|O_EXCL` 原子创建，内容仅 PID/时间/trigger）：

- 锁被其它进程持有 → 该次扫描**明确跳过**（partial + run-state/auto.log 留痕），不排队
- 同进程编排链（serve → autopilot → ledger）按引用计数安全重入
- 崩溃残留：PID 已死 → 下次获取自动回收；PID 存活（含权限无法判定）→ 保守拒绝
- 锁文件 gitignore；被占用时 API 返回 `409 scan_in_progress`

## 5. 职责边界（冻结）

| | serve.py | autopilot（scheduled） |
|---|---|---|
| Web UI / API | ✓ | ✗ |
| 定时器 / 计划任务安装 | ✗ | ✓（install-auto 一次性注册，由 Windows 触发） |
| 采集 / board / health / run-state / auto.log | 经 API 触发时复用 | ✓ |
| pricing-sync | ✗（默认离线；显式 CLI 才联网） | ✗（除非显式 --pricing） |

## 6. 频率建议

默认 30 分钟；最低 15 分钟。采集是幂等增量（未变文件整文件跳过），
但不必高频——分钟级调度没有收益。
