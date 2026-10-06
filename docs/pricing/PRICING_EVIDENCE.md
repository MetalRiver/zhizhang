# Pricing Evidence Log（人工定价证据台账）

> 每条人工确认价格必须在此登记：来源 / 等级 / 验证日期 / 单价 / 备注。
> pricing.json 是数值事实源，本文件是可审计的证据链。两者不一致时，
> 以 pricing.json 数值为准并立即修订本文件。

---

## GLM-5.3-Flash — 状态：APPROVE CANDIDATE（待币种引擎就绪后 apply）

| 字段 | 值 |
|---|---|
| provider | bigmodel / zai（ZCode 内置通道；账本 provider 字段：builtin:bigmodel-start-plan / account:bigmodel-start-plan / builtin:zai-start-plan / builtin:bigmodel-coding-plan / zai-standard-api） |
| status | **approve_candidate**（价格证据充分；apply 被币种引擎阻塞，见 §Currency） |
| source | 晚点 LatePost 独家（2026-08-26）+ 新浪财经（2026-09-03）等多源一致转述智谱官方定价；与「GLM-5.3 旗舰价 1/10」比例自洽（0.8/2.8 = 8/28 的 1/10） |
| source_type | **Tier B**（多源权威转述官方；bigmodel.cn 定价页 JS 渲染，本轮无法静态核验原文表格） |
| verified_at / as_of | 2026-09-26 |
| currency | **CNY** |
| unit | 每百万 tokens（per 1M tokens） |
| input | 0.8 |
| output | 2.8 |
| cache_read | **0.23**（缓存命中；多源确认，与旗舰 1/10 比例自洽） |
| cache_write | 0 tokens（账本无 cache_write 用量；官方「限时免费缓存存储」属存储时长计费，与 cache_write token 计费不同维度，不做换算） |
| notes | **Reference API-equivalent rate; source usage may originate from subscription/Coding Plan channel.** 本价格不代表订阅实际支出。 |

### GLM-5.3-Flash 估算（按 0.8 / 2.8 / 0.23，CNY，effective 口径）

| 分项 | tokens | 单价 ¥/M | 估算 ¥ |
|---|---|---|---|
| Input | 3,798,000,904 | 0.8 | 3,038.40 |
| Output | 8,129,432 | 2.8 | 22.76 |
| Cache-read | 3,697,834,880 | 0.23 | 850.50 |
| Cache-write | 0 | — | 0 |
| **合计** | | | **≈ 3,911.67 CNY** |

（不做 CNY→USD 换算：项目尚未冻结汇率策略。）

---

## deepseek-v4.1-flash — 状态：verified_dynamic_price · not_applied_static

| 字段 | 值 |
|---|---|
| provider | DeepSeek 官方（workbuddy 直连） |
| status | **verified_dynamic_price / not_applied_static**（价格已核实，但静态 per-model 引擎无法忠实表达） |
| source | api-docs.deepseek.com 官方定价（Tier A）+ 用户外部复核 |
| verified_at | 2026-09-26 |
| currency | CNY |
| unit | 每百万 tokens |
| Cache hit | 空闲 ¥0.02 / 高峰 ¥0.04 |
| Cache miss (input) | 空闲 ¥1 / 高峰 ¥2 |
| Output | 空闲 ¥4 / 高峰 ¥8 |
| 高峰窗口 | 北京时间工作日 09:00–12:00、14:00–18:00；其余为空闲 |
| **legacy aliases** | **deepseek-v4-flash 与 deepseek-v4-flash-vision-exp 当前会路由至 V4.1 Flash** —— 旧模型名的既有静态价格（LiteLLM 目录，auto 层）可能已过时；修正属 Time-aware Pricing 技术债，本轮不改 pricing.json |
| notes | 官方价格明确；阻塞项是引擎表达力，不是证据。 |

---

## 其余模型 — 状态：unverified（保持 null）

- Kimi K2.7：官方页未检索到明确 K2.7 单价（K3/K2.7-Code 价格不可映射）。
- MiniMax M3 / 混元 hy4 / hy3：敞口低，本轮未检索。
- GLM-5.2 / 5.2-a / 5.3 / 5.3-flash（workbuddy）：bigmodel.cn 定价页 JS 渲染，官方表格未获取；不因家族相似继承 GLM-5.3-Flash 价格。
- 长尾（<0.1% 敞口）：逐条理由见 R8_PRICING_PROPOSAL.md。
