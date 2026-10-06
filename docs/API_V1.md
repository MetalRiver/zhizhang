# 智账 · Local API v1

> Round 6 · `serve.py` —— 统一本地服务：智账 Web UI + Local API + board 兜底 + 手动增量扫描。
> 仅 Python 标准库实现；无 FastAPI / Flask / Node。

## 启动

```bash
python serve.py                 # http://127.0.0.1:8787（默认端口）
python serve.py --port 8788
python serve.py --open          # 启动后打开系统浏览器（默认不打开）
```

启动横幅：

```
  智账 · PathOrbit AI Ledger Local Server
  http://127.0.0.1:8787
  Web Dashboard  →  http://127.0.0.1:8787/
  Local API v1   →  http://127.0.0.1:8787/api/v1/overview
  只绑定 127.0.0.1 ｜ board 是业务事实源 ｜ Ctrl+C 退出
```

## 安全边界

- **只绑定 `127.0.0.1`**：代码层硬编码，无 `--host` 参数；拒绝 LAN / 公网暴露
- **静态白名单**：仅 `web/` 内资源；禁止目录浏览；拒绝 `../` 路径穿越
- **无 CORS 头**：Web 与 API 同源，浏览器无需跨域
- **POST scan 校验**：
  - `Content-Type` 必须为 `application/json`（否则 400）
  - `Origin` 允许缺失（CLI/curl）或 `http://127.0.0.1:<port>` / `http://localhost:<port>`；其它 Origin → 403
  - 请求体只接受 `{}`；`{"full": true}` → 400；任何未知字段 → 400
- **不泄露**：无 traceback、无绝对路径、无用户会话内容、无 SQLite 内部信息
- v0 无账号体系（localhost 单用户产品，避免过度设计）

## 业务事实源

**`usage-board.json`（Usage Board Schema v1）是唯一业务事实源。**

- API 的所有 projection 端点都是 **board 的直接投影**，与 `overview` 的对应字段
  逐字节一致（`sources == overview.sources`、`clients == overview.clients` 等）
- API **不重新计算**成本 / replay / effective / 口径——一切业务事实来自 Core/Board
- GET 优先读取产物文件（而非每次现场聚合），因此扫描期间 GET 继续返回最近
  成功的产物，绝不阻塞、绝不读到半写状态（产物全部原子写）

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 智账 UI（web/v1/shell.html；旧 index.html 已退役） |
| GET | `/assets/...` | 静态资源（web/assets 白名单内） |
| GET | `/usage-board.json` | 真实 board 兜底通道（仓库根产物） |
| GET | `/api/v1/overview` | board 完整正式 schema（逐字节一致，不发明第二套） |
| GET | `/api/v1/health` | health.json 透传；缺失时按只读逻辑诚实推导（unknown），绝不伪造 healthy |
| GET | `/api/v1/sources` | overview.sources 投影 |
| GET | `/api/v1/clients` | overview.clients 投影 |
| GET | `/api/v1/models` | overview.models 投影 |
| GET | `/api/v1/usage` | overview.daily 的窗口投影，见下方窗口语义 |
| GET | `/api/v1/activity` | overview.activity 投影 |
| POST | `/api/v1/scan` | 手动增量扫描（唯一写入口），见下 |

### GET /api/v1/usage

```
GET /api/v1/usage?days=7
200 →
{
  "daily": [...],              // board.daily 的最近 N 行（不补 0、不造数）
  "requested_days": 7,
  "available_days": 45,        // board.daily 中实际存在的天数
  "window_days": 45,           // board 导出窗口
  "truncated": false
}
```

`?days=90`（超过窗口/实际数据）→ 200 + `truncated: true` + 真实存在的行——
**不把缺失天数补成 0 使用**。`days` 非整数或不在 1..366 → 400。

### POST /api/v1/scan

```
POST /api/v1/scan
Content-Type: application/json
{}
200 →
{
  "ok": true, "status": "success", "events_inserted": 0, "events_updated": 0,
  "board_written": true, "health": "partial", "errors": []
}
```

编排 = 复用 `autopilot.run_once(do_pricing=False, trigger='api')`：
发现 → 增量采集 → board → health → run-state → auto.log 一轮新状态。
**不支持且不支持未来支持于本 API**：`--full`、pricing-sync、migration、
delete/reset、scheduler 安装。请求出现 `full` → 400。

并发：进程内单扫描锁；已有扫描时第二个 POST → `409 {"ok":false,
"error":"scan_in_progress"}`。扫描期间 GET 继续返回最近成功产物。

## 响应规范

- JSON：`application/json; charset=utf-8`
- 错误统一：`{"ok": false, "error": "...", "message": "..."}`
- 状态码：200 成功 / 400 请求错误 / 403 跨站拒绝 / 404 未知路径 /
  409 扫描进行中 / 500 内部错误 / 503 产物暂不可用
- overview / health / 投影端点返回**产物原始对象**（不加包装），保证与
  board/health 文件逐字节一致可比对

## UI 数据通道优先级（web/index.html）

1. `GET /api/v1/overview` → 传输态 **LOCAL API · CONNECTED**（扫描按钮可见）
2. `GET usage-board.json` → **本地看板 · LOCAL BOARD**（扫描按钮隐藏）
3. 内嵌 SNAPSHOT → **离线快照 · SNAPSHOT**（诚实标注，绝不显示 CONNECTED/LIVE）

传输状态（Transport）与健康状态（Health：healthy/partial/failed/unknown）
是两个独立概念，互不混用。

## Project / Session 归因（Phase 0，R12）

默认口径 EFFECTIVE（is_replay=1 排除）。`view=raw` 可显式切换审计口径（仅 sessions/events）。
分页：`limit`（1..500，默认 50）与 `offset`（默认 0）。project_key 为哈希键，绝不包含完整本地路径。

| 端点 | 说明 |
|---|---|
| `GET /api/v1/projects?limit&offset` | 项目列表（board.by_project）：project_key、display_name、project_kind、events、sessions、input/output/cache_read、total_tokens（=input+output）、estimated_cost_by_currency、pricing_coverage、last_seen |
| `GET /api/v1/projects/{project_key}` | 单项目详情（同上字段） |
| `GET /api/v1/projects/{project_key}/sessions?limit&offset` | 项目内会话列表（注册表 + 事件数 / token 求和，无定价重算） |
| `GET /api/v1/sessions/{source}/{session_id}` | 会话详情（注册表 + EFFECTIVE token 聚合 + project 关联） |
| `GET /api/v1/sessions/{source}/{session_id}/events?limit&offset&view` | 会话内事件分页（ts/model/token 各列/total_tokens；`view=raw` 含 replay 行） |

错误语义：`404 project_not_found` / `404 session_not_found` / `400 bad_request`（view 非法）/ `503 ledger_unavailable`（DB 缺失）。
成本语义：全部复用 board 的 cost_of_with_currency —— 未定价绝不折算 0 元，CNY/USD 分桶独立，pricing_coverage 单列。
