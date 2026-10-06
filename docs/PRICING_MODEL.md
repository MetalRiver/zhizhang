# 智账 · 成本语义与定价模型（Round 8 冻结）

> 本文件是产品成本语义的**唯一权威定义**。任何 UI / API / 文档措辞与本文件
> 冲突时，以本文件为准。

## 1. 两种成本，永不同称

| 概念 | 定义 | 状态 |
|---|---|---|
| **Estimated API-equivalent cost**（API 等价估算成本） | 按经人工验证的**当前参考 Token 单价**，对有效用量（effective usage）的估算金额 | ✅ 本产品实现 |
| **Actual Spend**（真实支出） | 信用卡 / API 账单 / 订阅扣费 / 企业合同 / 渠道结算产生的真实支付金额 | ❌ 未实现，留待未来 Billing Connector |

**禁止**：把 estimated cost 描述成「实际支出 / 实际账单 / 真实支付 / 真实总成本」。
即使 100% 模型都有价格也不允许——估算与账单在语义上永远不同。

## 2. 价格时间语义：Current Reference Pricing

价格是**当前参考单价**（as-of 验证时点），不是事件发生当天的历史价格。
产品承诺：「按当前已验证参考单价估算」。

- 不实现历史价格引擎（effective_from/to 时序库留待未来单独设计）
- 价格会变；估算随人工更新价格而变，历史数字不具备"当时账单"含义

## 3. 证据等级（Evidence Tiers）

| Tier | 来源 | 可否作为人工确认依据 |
|---|---|---|
| **A** | 模型/Provider 官方价格页或官方 API 文档 | ✅ 可以 |
| **B** | 权威来源转述的官方定价（官方公告的可靠转载），且映射明确 | ✅ 可以（须注明转述来源） |
| **C** | OpenRouter / LiteLLM 等第三方目录 | ⚠️ 仅候选 / 交叉验证，默认不升级为人工事实 |
| **D** | 名称相似 / family 推测 / fuzzy candidate | ❌ 绝对禁止 |

禁止模糊模型自动定价：`glm-x` / `glm-x-preview` / `glm-x-flash` / `glm-x:providerA`
不因名字相似继承 family 价格。必须确认 模型身份 + 实际 provider + 对应 SKU；
无法确认 → 保持 null。**health partial 比填错价格更正确。**

## 4. null / 0 / 订阅 三种语义

| 情形 | 正确语义 |
|---|---|
| 不知道价格 | `null`（未配置），**不是 $0** |
| 官方明确 free / zero-priced tier / 合同价 0 | `0`（须 `verified_zero: true` + 证据记录） |
| 订阅 / 包月产品（如 GLM Coding Plan） | Token 单价**不能自动写 0**——订阅支出与 API 等价定价是两种口径；API 等价估算按公开 API 参考价计算 |

## 5. 与 Core 的关系（不建第二套计算器）

- 成本计算唯一入口：`ledger.cost_of()`（effective 口径 × 当前参考单价）
- confirmed replay 不参与默认估算；RAW 可审计查看；activity 永不参与成本
- `pricing.json`（人工事实源，字段级优先）> `pricing.auto.json`（候选缓存），
  方向永不逆转：sync 抓到更高分候选不会覆盖人工确认值

## 6. Actual Spend 的未来位置（本轮不实现）

```
现在：AI Clients → Ledger Core → reference pricing → Estimated API-equivalent cost
未来：Billing Connectors（发票/订阅/合同） → Actual Spend（独立并列，互不覆盖）
```

## 7. 覆盖率语义

- **主指标 = Token 覆盖率**（priced tokens / effective tokens）——成本可信度
  由用量权重决定，不由模型个数决定
- 模型覆盖率仅作参考（长尾低用量模型会拉低它）
- 覆盖率未到 100% 不是失败；诚实标注 15 verified / 16 unknown 优于编造
- board additive 块：`pricing_coverage`（含 token_coverage_pct / as_of /
  cost_semantics / pricing_basis）
