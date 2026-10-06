# 智账 · Runtime 与启动

> Round 7 冻结：本项目所有**正式运行**（Server / 计划任务 / 启动器）
> 只使用仓库自带的 `.venv` 解释器。禁止使用系统 PATH 中的裸 `python` /
> `pythonw`，失败时明确报错，绝不静默退回。

## 1. 创建 Runtime（一次性）

```bash
cd <repo>
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-core.txt
```

- `requirements-core.txt` 目前只有 `zstandard==0.25.0`
  （Core 解析 dsh 会话的唯一第三方依赖；其余功能纯标准库）
- 不使用 pipenv / poetry / conda

## 2. 为什么禁止系统 Python

1. dsh 解析依赖 `.venv` 里的 zstandard —— 系统 Python 没有它，
   任何变化的 dsh 文件都会解析失败，自动任务会进入已知的 partial 状态
2. 依赖漂移不可控：全局环境装了什么、卸了什么，产品无法承诺
3. 计划任务在无用户会话的后台运行，必须有确定性的解释器路径

## 3. Runtime 检查

```bash
.venv\Scripts\python scripts\check_runtime.py
```

逐项验证：解释器在 `.venv` 内、Python ≥ 3.10、zstandard 可导入、
仓库关键文件（ledger.py / autopilot.py / serve.py / schema / web UI）在位。
任何一项失败 → 非零退出并明确说明，启动器会就此停止。

## 4. 快速启动

| 动作 | 命令 / 脚本 |
|---|---|
| 启动 Web + API（后台，无控制台） | 双击 `start-server.bat` |
| 停止 Web + API | 双击 `stop-server.bat` |
| 手动增量扫描（discover→scan→board→health） | 双击 `run-scan.bat` |
| 查看采集状态 | `.venv\Scripts\python autopilot.py status` |
| 查看健康 | `.venv\Scripts\python autopilot.py health` |

- Server 默认地址 `http://127.0.0.1:8787`（只绑定 127.0.0.1）
- 单实例：已运行时再次启动会明确提示 `already running`，不会起第二个；
  端口被**其它程序**占用时报告 `port_in_use` 并拒绝强杀未知进程
- `server-state.json`（gitignore）：运行中服务的 PID / 端口 / 启动时间；
  进程异常退出后留下旧文件，下次启动自动检测并清理（PID 已死 → 自愈）
- `server.log`（gitignore，512KB 轮转）：服务层日志；
  `auto.log`（gitignore）：采集层日志。两者分离，不混写

## 5. 与 API 的关系

`serve.py` 与调度采集共享同一 Core、同一 usage.db、同一 board。
Web UI 的「刷新账本」按钮通过 API 触发一次增量扫描（trigger=api），
与计划任务扫描共用同一把**跨进程扫描锁**（usage-ledger-scan.lock），
任何时刻最多一个 writer。

## 6. 常见问题

- **双击 start-server.bat 提示找不到 .venv** → 先做第 1 节
- **提示 already running** → 直接浏览器打开 http://127.0.0.1:8787/
- **提示 port_in_use** → 8787 被其它程序占用；用
  `.venv\Scripts\python serve.py --port 8788` 换端口（工具永不强杀未知进程）
- **stop-server 提示 PID 非本产品** → server-state.json 记录的 PID 经命令行
  核验不是 serve.py；此时工具拒绝终止，请人工处理
