# 待填单价清单（pricing.json）

> 生成时间：2026-09-19 00:18 ｜ 生成器：`ledger.py pricing-candidates`

单价单位：**美元 / 每 100 万 token**。共 **30** 个模型缺单价，按 token 量从大到小排列。

## 为什么这里不替你自动填

同一个模型在不同渠道的单价可以差 2 倍以上（例：`glm-5.2` 在 cloudflare 是 1.4、在 deepinfra 是 0.75）。**价格错配比缺价更危险** —— 缺价只是成本显示 `null`，错价会让你对花费产生错误判断。所以这里只列候选，选哪个取决于你**实际通过哪家渠道调用**。

判断渠道的方法：看 `sources.json` / `resolved-sources.json` 里各客户端的实际路径，以及你在客户端里配置的 API 提供方。


## 一、速览

| # | 模型 | 请求数 | Token 量 | 候选数 | 候选单价区间（input, 美元/百万） |
|---|---|---:|---:|---:|---|
| 1 | `GLM-5.3-Flash` | 8,492 | 5,525,086,115 | 6 | 0.090 ~ 0.150 |
| 2 | `deepseek-v4.1-flash` | 1,577 | 500,157,037 | 6 | 0.150 ~ 0.300 |
| 3 | `hy4-preview` | 1,067 | 114,246,224 | 6 | 0.180 ~ 0.845 |
| 4 | `kimi-k2.7` | 1,050 | 98,778,208 | 6 | 0.680 ~ 1.900 |
| 5 | `glm-5.2` | 650 | 51,648,469 | 6 | 0.750 ~ 1.400 |
| 6 | `minimax-m3` | 555 | 47,332,143 | 6 | 0.280 ~ 0.300 |
| 7 | `glm-5.2-a` | 633 | 47,295,866 | 6 | 0.600 ~ 1.400 |
| 8 | `deepseek-ai/DeepSeek-V4-Pro` | 171 | 19,573,353 | 6 | 1.300 ~ 2.400 |
| 9 | `glm-5.3` | 288 | 19,311,528 | 6 | 1.127 ~ 1.400 |
| 10 | `doubao-seed-evolving-latest-version` | 170 | 17,996,132 | 6 | 0.072 ~ 2.000 |
| 11 | `hy3` | 164 | 12,833,289 | 6 | 0.132 ~ 0.156 |
| 12 | `kimi-k2.6` | 95 | 7,231,255 | 6 | 0.750 ~ 0.950 |
| 13 | `qwen3.8-max` | 22 | 5,713,442 | 6 | 1.650 ~ 2.000 |
| 14 | `glm-5.3-flash` | 40 | 5,508,387 | 6 | 0.090 ~ 0.150 |
| 15 | `deepseek-v4-flash-version` | 48 | 5,327,238 | 6 | 0.090 ~ 0.300 |
| 16 | `deepseek-v4-pro-0813` | 32 | 3,793,058 | 6 | 0.578 ~ 1.320 |
| 17 | `glm-4.7` | 49 | 2,489,889 | 6 | 0.400 ~ 0.600 |
| 18 | `glm-5.2-x` | 14 | 1,018,124 | 6 | 0.600 ~ 1.400 |
| 19 | `qwen-plus` | 5 | 545,164 | 6 | 0.260 ~ 0.400 |
| 20 | `deepseek-r1-0528` | 8 | 499,504 | 6 | 0.250 ~ 3.000 |
| 21 | `deepseek-v4-flash-0731` | 2 | 415,859 | 6 | 0.060 ~ 0.440 |
| 22 | `GLM-5.3` | 9 | 256,216 | 6 | 1.127 ~ 1.400 |
| 23 | `deepseek-modlens/deepseek-v4-flash-vision-exp` | 1 | 108,488 | 6 | 0.220 ~ 0.440 |
| 24 | `deepseek-ai/DeepSeek-V3.2` | 5 | 86,861 | 6 | 0.580 ~ 0.740 |
| 25 | `glm-z1-flash` | 4 | 76,608 | 1 | —（候选均无价格） |
| 26 | `deepseek-v3` | 1 | 67,234 | 6 | 0.200 ~ 1.140 |
| 27 | `qwq-plus` | 1 | 21,229 | 6 | 0.260 ~ 0.800 |
| 28 | `glm-5-turbo` | 1 | 19,645 | 6 | 0.600 ~ 1.200 |
| 29 | `glm-4.7-flash` | 1 | 17,181 | 6 | 0.060 ~ 0.070 |
| 30 | `GLM-5-Turbo` | 2 | 0 | 6 | 0.600 ~ 1.200 |

## 二、逐模型候选明细


### 1. `GLM-5.3-Flash`

- 出现量：**8,492 请求 · 5,525,086,115 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/glm-5.3-flash` | 归一化后完全相同 · 供应商 aihubmix | 0.11268 | 0.39438 | 0.02817 | — |
| `friendliai/zai-org/GLM-5.3-Flash` | 归一化后完全相同 · 供应商 friendliai | 0.15 | 0.5 | 0.03 | — |
| `glm-5.3-flash` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `nebius/zai-org/GLM-5.3-Flash` | 归一化后完全相同 · 供应商 nebius | 0.15 | 0.5 | — | — |
| `openrouter/z-ai/glm-5.3-flash` | 归一化后完全相同 · 供应商 openrouter | 0.09 | 0.3 | 0.018 | — |
| `perplexity/perplexity/glm-5.3-flash` | 归一化后完全相同 · 供应商 perplexity | 0.15 | 0.5 | 0.03 | — |


### 2. `deepseek-v4.1-flash`

- 出现量：**1,577 请求 · 500,157,037 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `deepseek-v4.1-flash` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `deepseek/deepseek-v4.1-flash` | 归一化后完全相同 · 供应商 deepseek | 0.15 | 0.6 | 0.003 | — |
| `openrouter/deepseek/deepseek-v4.1-flash` | 归一化后完全相同 · 供应商 openrouter | 0.15 | 0.6 | 0.003 | — |
| `together_ai/deepseek-ai/DeepSeek-V4.1-Flash` | 归一化后完全相同 · 供应商 together_ai | 0.3 | 1.2 | 0.006 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-v4p1-flash` | 字符重合度 100% · 供应商 fireworks_ai | 0.22 | 0.66 | 0.007 | — |
| `fireworks_ai/deepseek-v4p1-flash` | 字符重合度 100% · 供应商 fireworks_ai | 0.22 | 0.66 | 0.007 | — |


### 3. `hy4-preview`

- 出现量：**1,067 请求 · 114,246,224 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/hy4-preview` | 归一化后完全相同 · 供应商 aihubmix | 0.845 | 2.535 | 0.04225 | — |
| `hy4-preview` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `openrouter/tencent/hy4-preview` | 归一化后完全相同 · 供应商 openrouter | 0.834 | 2.501 | 0.042 | — |
| `tencent/hy4-preview` | 归一化后完全相同 · 供应商 tencent | 0.834 | 2.501 | 0.042 | — |
| `openrouter/tencent/hy3-preview` | 字符重合度 80% · 供应商 openrouter | 0.18 | 0.6 | 0.06 | — |
| `tencent/hy3-preview` | 字符重合度 80% · 供应商 tencent | 0.18 | 0.6 | 0.06 | — |


### 4. `kimi-k2.7`

- 出现量：**1,050 请求 · 98,778,208 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `kimi-k2.7` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `aihubmix/kimi-k2.7-code-highspeed` | 归一化后是前缀/后缀关系 · 供应商 aihubmix | 1.9 | 7.999 | 0.32167 | — |
| `azure_ai/kimi-k2.7-code` | 归一化后是前缀/后缀关系 · 供应商 azure_ai | 0.95 | 4 | 0.19 | — |
| `cloudflare/@cf/moonshotai/kimi-k2.7-code` | 归一化后是前缀/后缀关系 · 供应商 cloudflare | 0.95 | 4 | 0.19 | — |
| `dashscope/kimi-k2.7-code` | 归一化后是前缀/后缀关系 · 供应商 dashscope | 0.95 | 4 | 0.19 | — |
| `deepinfra/moonshotai/Kimi-K2.7-Code` | 归一化后是前缀/后缀关系 · 供应商 deepinfra | 0.68 | 3.4 | 0.136 | — |


### 5. `glm-5.2`

- 出现量：**650 请求 · 51,648,469 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `cloudflare/@cf/zai-org/glm-5.2` | 归一化后完全相同 · 供应商 cloudflare | 1.4 | 4.4 | 0.26 | — |
| `dashscope/glm-5.2` | 归一化后完全相同 · 供应商 dashscope | 1.4 | 4.4 | 0.28 | — |
| `deepinfra/zai-org/GLM-5.2` | 归一化后完全相同 · 供应商 deepinfra | 0.75 | 2.4 | 0.14 | — |
| `friendliai/zai-org/GLM-5.2` | 归一化后完全相同 · 供应商 friendliai | 1.4 | 4.4 | 0.26 | — |
| `glm-5.2` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `mistral/glm-5-2` | 归一化后完全相同 · 供应商 mistral | 1.4 | 4.4 | 0.14 | — |


### 6. `minimax-m3`

- 出现量：**555 请求 · 47,332,143 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/minimax-m3` | 归一化后完全相同 · 供应商 aihubmix | 0.288 | 1.152 | — | — |
| `deepinfra/MiniMaxAI/MiniMax-M3` | 归一化后完全相同 · 供应商 deepinfra | 0.28 | 1.1 | 0.056 | — |
| `fireworks_ai/accounts/fireworks/models/minimax-m3` | 归一化后完全相同 · 供应商 fireworks_ai | 0.3 | 1.2 | 0.06 | — |
| `fireworks_ai/minimax-m3` | 归一化后完全相同 · 供应商 fireworks_ai | 0.3 | 1.2 | 0.06 | — |
| `minimax-m3` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `minimax/minimax-m3` | 归一化后完全相同 · 供应商 minimax | 0.3 | 1.2 | 0.06 | — |


### 7. `glm-5.2-a`

- 出现量：**633 请求 · 47,295,866 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `glm-5.2-a` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `baseten/zai-org/GLM-5` | 归一化后互相包含 · 供应商 baseten | 0.95 | 3.15 | — | — |
| `cloudflare/@cf/zai-org/glm-5.2` | 归一化后互相包含 · 供应商 cloudflare | 1.4 | 4.4 | 0.26 | — |
| `dashscope/glm-5.2` | 归一化后互相包含 · 供应商 dashscope | 1.4 | 4.4 | 0.28 | — |
| `deepinfra/zai-org/GLM-5` | 归一化后互相包含 · 供应商 deepinfra | 0.6 | 2.08 | 0.12 | — |
| `deepinfra/zai-org/GLM-5.2` | 归一化后互相包含 · 供应商 deepinfra | 0.75 | 2.4 | 0.14 | — |


### 8. `deepseek-ai/DeepSeek-V4-Pro`

- 出现量：**171 请求 · 19,573,353 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/deepseek-v4-pro` | 归一化后完全相同 · 供应商 aihubmix | 1.69 | 3.38 | 0.14027 | — |
| `azure_ai/deepseek-v4-pro` | 归一化后完全相同 · 供应商 azure_ai | 1.74 | 3.48 | 0.145 | — |
| `dashscope/deepseek-v4-pro` | 归一化后完全相同 · 供应商 dashscope | 2.4 | 4.8 | 0.2 | — |
| `deepinfra/deepseek-ai/DeepSeek-V4-Pro` | 归一化后完全相同 · 供应商 deepinfra | 1.3 | 2.6 | 0.1 | — |
| `deepseek-ai/deepseek-v4-pro` | 归一化后完全相同 · 供应商 deepseek-ai · 该键没有可用价格数据 | — | — | — | — |
| `deepseek-v4-pro` | 归一化后完全相同 | 1.32 | 3.96 | 0.044 | — |


### 9. `glm-5.3`

- 出现量：**288 请求 · 19,311,528 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/glm-5.3` | 归一化后完全相同 · 供应商 aihubmix | 1.1268 | 3.9438 | 0.2817 | — |
| `baseten/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 baseten | 1.4 | 4.4 | 0.14 | — |
| `friendliai/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 friendliai | 1.26 | 3.96 | 0.234 | — |
| `glm-5.3` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `nebius/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 nebius | 1.4 | 4.4 | — | — |
| `novita/zai-org/glm-5.3` | 归一化后完全相同 · 供应商 novita | 1.4 | 4.4 | 0.26 | — |


### 10. `doubao-seed-evolving-latest-version`

- 出现量：**170 请求 · 17,996,132 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `doubao-seed-evolving-latest-version` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `xai/grok-build-latest` | 字符重合度 80% · 供应商 xai | 2 | 6 | 0.3 | — |
| `us-gov.nvidia.nemotron-nano-12b-v2` | 字符重合度 76% | 0.24 | 0.72 | — | — |
| `us-gov.nvidia.nemotron-nano-3-30b` | 字符重合度 76% | 0.072 | 0.288 | — | — |
| `us-gov.nvidia.nemotron-nano-9b-v2` | 字符重合度 76% | 0.072 | 0.276 | — | — |
| `azure_ai/Cohere-embed-v3-multilingual` | 字符重合度 72% · 供应商 azure_ai | 0.1 | — | — | — |


### 11. `hy3`

- 出现量：**164 请求 · 12,833,289 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/hy3` | 归一化后完全相同 · 供应商 aihubmix | 0.1562 | 0.6248 | 0.03905 | — |
| `deepinfra/tencent/Hy3` | 归一化后完全相同 · 供应商 deepinfra | 0.14 | 0.58 | 0.035 | — |
| `hy3` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `novita/tencent/hy3` | 归一化后完全相同 · 供应商 novita | 0.14 | 0.58 | 0.035 | — |
| `openrouter/tencent/hy3` | 归一化后完全相同 · 供应商 openrouter | 0.132 | 0.528 | 0.033 | — |
| `tencent/hy3` | 归一化后完全相同 · 供应商 tencent | 0.132 | 0.528 | 0.033 | — |


### 12. `kimi-k2.6`

- 出现量：**95 请求 · 7,231,255 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/kimi-k2.6` | 归一化后完全相同 · 供应商 aihubmix | 0.95 | 3.9995 | 0.16084 | — |
| `azure_ai/kimi-k2.6` | 归一化后完全相同 · 供应商 azure_ai | 0.95 | 4 | 0.16 | — |
| `cloudflare/@cf/moonshotai/kimi-k2.6` | 归一化后完全相同 · 供应商 cloudflare | 0.95 | 4 | 0.16 | — |
| `deepinfra/moonshotai/Kimi-K2.6` | 归一化后完全相同 · 供应商 deepinfra | 0.75 | 3.5 | 0.15 | — |
| `kimi-k2.6` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `moonshot/kimi-k2.6` | 归一化后完全相同 · 供应商 moonshot | 0.95 | 4 | 0.16 | — |


### 13. `qwen3.8-max`

- 出现量：**22 请求 · 5,713,442 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/qwen3.8-max` | 归一化后完全相同 · 供应商 aihubmix | 1.69 | 5.07 | 0.169 | 2.1125 |
| `dashscope/qwen3.8-max` | 归一化后完全相同 · 供应商 dashscope | 2 | 6 | 0.25 | — |
| `deepinfra/Qwen/Qwen3.8-Max` | 归一化后完全相同 · 供应商 deepinfra | 1.65 | 4.951 | 0.206 | — |
| `novita/qwen/qwen3.8-max` | 归一化后完全相同 · 供应商 novita | 2 | 6 | 0.25 | — |
| `openrouter/qwen/qwen3.8-max` | 归一化后完全相同 · 供应商 openrouter | 2 | 6 | 0.25 | 2.5 |
| `qwen3.8-max` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |


### 14. `glm-5.3-flash`

- 出现量：**40 请求 · 5,508,387 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/glm-5.3-flash` | 归一化后完全相同 · 供应商 aihubmix | 0.11268 | 0.39438 | 0.02817 | — |
| `friendliai/zai-org/GLM-5.3-Flash` | 归一化后完全相同 · 供应商 friendliai | 0.15 | 0.5 | 0.03 | — |
| `glm-5.3-flash` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `nebius/zai-org/GLM-5.3-Flash` | 归一化后完全相同 · 供应商 nebius | 0.15 | 0.5 | — | — |
| `openrouter/z-ai/glm-5.3-flash` | 归一化后完全相同 · 供应商 openrouter | 0.09 | 0.3 | 0.018 | — |
| `perplexity/perplexity/glm-5.3-flash` | 归一化后完全相同 · 供应商 perplexity | 0.15 | 0.5 | 0.03 | — |


### 15. `deepseek-v4-flash-version`

- 出现量：**48 请求 · 5,327,238 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `deepseek-v4-flash-version` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `aihubmix/deepseek-v4-flash` | 归一化后互相包含 · 供应商 aihubmix | 0.142 | 0.284 | 0.0284 | — |
| `azure_ai/deepseek-v4-flash` | 归一化后互相包含 · 供应商 azure_ai | 0.19 | 0.51 | 0.028 | — |
| `dashscope/deepseek-v4-flash` | 归一化后互相包含 · 供应商 dashscope | 0.2 | 0.4 | 0.04 | — |
| `deepinfra/deepseek-ai/DeepSeek-V4-Flash` | 归一化后互相包含 · 供应商 deepinfra | 0.09 | 0.18 | 0.018 | — |
| `deepseek-v4-flash` | 归一化后互相包含 | 0.3 | 1.2 | 0.006 | — |


### 16. `deepseek-v4-pro-0813`

- 出现量：**32 请求 · 3,793,058 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `deepinfra/deepseek-ai/DeepSeek-V4-Pro-0813` | 归一化后完全相同 · 供应商 deepinfra | 1.3 | 2.6 | 0.1 | — |
| `deepseek-v4-pro-0813` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `deepseek/deepseek-v4-pro-0813` | 归一化后完全相同 · 供应商 deepseek | 0.57816 | 1.7345 | 0.018396 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-v4-pro-0813` | 归一化后完全相同 · 供应商 fireworks_ai | 1.32 | 3.96 | 0.044 | — |
| `nebius/deepseek-ai/DeepSeek-V4-Pro-0813` | 归一化后完全相同 · 供应商 nebius | 1.32 | 3.96 | — | — |
| `novita/deepseek/deepseek-v4-pro-0813` | 归一化后完全相同 · 供应商 novita | 1.32 | 3.96 | 0.132 | — |


### 17. `glm-4.7`

- 出现量：**49 请求 · 2,489,889 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `baseten/zai-org/GLM-4.7` | 归一化后完全相同 · 供应商 baseten | 0.6 | 2.2 | — | — |
| `deepinfra/zai-org/GLM-4.7` | 归一化后完全相同 · 供应商 deepinfra | 0.4 | 1.75 | 0.08 | — |
| `glm-4.7` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `novita/zai-org/glm-4.7` | 归一化后完全相同 · 供应商 novita | 0.6 | 2.2 | 0.11 | — |
| `openrouter/z-ai/glm-4.7` | 归一化后完全相同 · 供应商 openrouter | 0.4 | 1.75 | 0.08 | — |
| `together_ai/zai-org/GLM-4.7` | 归一化后完全相同 · 供应商 together_ai | 0.45 | 2 | — | — |


### 18. `glm-5.2-x`

- 出现量：**14 请求 · 1,018,124 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `glm-5.2-x` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `baseten/zai-org/GLM-5` | 归一化后互相包含 · 供应商 baseten | 0.95 | 3.15 | — | — |
| `cloudflare/@cf/zai-org/glm-5.2` | 归一化后互相包含 · 供应商 cloudflare | 1.4 | 4.4 | 0.26 | — |
| `dashscope/glm-5.2` | 归一化后互相包含 · 供应商 dashscope | 1.4 | 4.4 | 0.28 | — |
| `deepinfra/zai-org/GLM-5` | 归一化后互相包含 · 供应商 deepinfra | 0.6 | 2.08 | 0.12 | — |
| `deepinfra/zai-org/GLM-5.2` | 归一化后互相包含 · 供应商 deepinfra | 0.75 | 2.4 | 0.14 | — |


### 19. `qwen-plus`

- 出现量：**5 请求 · 545,164 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `dashscope/qwen-plus` | 归一化后完全相同 · 供应商 dashscope | 0.4 | 1.2 | — | — |
| `openrouter/qwen/qwen-plus` | 归一化后完全相同 · 供应商 openrouter | 0.26 | 0.78 | 0.052 | 0.325 |
| `qwen-plus` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `qwen/qwen-plus` | 归一化后完全相同 · 供应商 qwen | 0.26 | 0.78 | 0.052 | 0.325 |
| `qwen_ai_platform/qwen-plus` | 归一化后完全相同 · 供应商 qwen_ai_platform | 0.4 | 1.2 | — | — |
| `qwencloud/qwen-plus` | 归一化后完全相同 · 供应商 qwencloud | 0.4 | 1.2 | — | — |


### 20. `deepseek-r1-0528`

- 出现量：**8 请求 · 499,504 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `crusoe/deepseek-ai/DeepSeek-R1-0528` | 归一化后完全相同 · 供应商 crusoe | 3 | 7 | — | — |
| `deepinfra/deepseek-ai/DeepSeek-R1-0528` | 归一化后完全相同 · 供应商 deepinfra | 0.5 | 2.15 | 0.4 | — |
| `deepseek-r1-0528` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `deepseek/deepseek-r1-0528` | 归一化后完全相同 · 供应商 deepseek | 0.5 | 2.15 | 0.35 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-r1-0528` | 归一化后完全相同 · 供应商 fireworks_ai | 3 | 8 | — | — |
| `hyperbolic/deepseek-ai/DeepSeek-R1-0528` | 归一化后完全相同 · 供应商 hyperbolic | 0.25 | 0.25 | — | — |


### 21. `deepseek-v4-flash-0731`

- 出现量：**2 请求 · 415,859 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `azure_ai/DeepSeek-V4-Flash-0731` | 归一化后完全相同 · 供应商 azure_ai | 0.44 | 1.32 | 0.014 | — |
| `dashscope/deepseek-v4-flash-0731` | 归一化后完全相同 · 供应商 dashscope | 0.2 | 0.4 | 0.04 | — |
| `deepinfra/deepseek-ai/DeepSeek-V4-Flash-0731` | 归一化后完全相同 · 供应商 deepinfra | 0.08 | 0.18 | 0.016 | — |
| `deepseek-v4-flash-0731` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `deepseek/deepseek-v4-flash-0731` | 归一化后完全相同 · 供应商 deepseek | 0.06 | 0.12 | 0.012 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-0731` | 归一化后完全相同 · 供应商 fireworks_ai | 0.22 | 0.66 | 0.007 | — |


### 22. `GLM-5.3`

- 出现量：**9 请求 · 256,216 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `aihubmix/glm-5.3` | 归一化后完全相同 · 供应商 aihubmix | 1.1268 | 3.9438 | 0.2817 | — |
| `baseten/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 baseten | 1.4 | 4.4 | 0.14 | — |
| `friendliai/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 friendliai | 1.26 | 3.96 | 0.234 | — |
| `glm-5.3` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `nebius/zai-org/GLM-5.3` | 归一化后完全相同 · 供应商 nebius | 1.4 | 4.4 | — | — |
| `novita/zai-org/glm-5.3` | 归一化后完全相同 · 供应商 novita | 1.4 | 4.4 | 0.26 | — |


### 23. `deepseek-modlens/deepseek-v4-flash-vision-exp`

- 出现量：**1 请求 · 108,488 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `deepseek-modlens/deepseek-v4-flash-vision-exp` | 归一化后完全相同 · 供应商 deepseek-modlens · 该键没有可用价格数据 | — | — | — | — |
| `deepseek-v4-flash-vision-exp` | 归一化后完全相同 | 0.3 | 1.2 | 0.006 | — |
| `deepseek/deepseek-v4-flash-vision-exp` | 归一化后完全相同 · 供应商 deepseek | 0.3 | 1.2 | 0.006 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-v4-flash-vision-exp` | 归一化后完全相同 · 供应商 fireworks_ai | 0.22 | 0.66 | 0.007 | — |
| `fireworks_ai/deepseek-v4-flash-vision-exp` | 归一化后完全相同 · 供应商 fireworks_ai | 0.22 | 0.66 | 0.007 | — |
| `novita/deepseek/deepseek-v4-flash-vision-exp` | 归一化后完全相同 · 供应商 novita | 0.44 | 1.32 | 0.028 | — |


### 24. `deepseek-ai/DeepSeek-V3.2`

- 出现量：**5 请求 · 86,861 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `azure_ai/deepseek-v3.2` | 归一化后完全相同 · 供应商 azure_ai | 0.58 | 1.68 | — | — |
| `bedrock/ap-northeast-1/deepseek.v3.2` | 归一化后完全相同 · 供应商 bedrock | 0.74 | 2.22 | — | — |
| `bedrock/ap-south-1/deepseek.v3.2` | 归一化后完全相同 · 供应商 bedrock | 0.74 | 2.22 | — | — |
| `bedrock/ap-southeast-3/deepseek.v3.2` | 归一化后完全相同 · 供应商 bedrock | 0.74 | 2.22 | — | — |
| `bedrock/eu-north-1/deepseek.v3.2` | 归一化后完全相同 · 供应商 bedrock | 0.74 | 2.22 | — | — |
| `bedrock/sa-east-1/deepseek.v3.2` | 归一化后完全相同 · 供应商 bedrock | 0.74 | 2.22 | — | — |


### 25. `glm-z1-flash`

- 出现量：**4 请求 · 76,608 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `glm-z1-flash` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |


### 26. `deepseek-v3`

- 出现量：**1 请求 · 67,234 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `azure_ai/deepseek-v3` | 归一化后完全相同 · 供应商 azure_ai | 1.14 | 4.56 | — | — |
| `deepinfra/deepseek-ai/DeepSeek-V3` | 归一化后完全相同 · 供应商 deepinfra | 0.32 | 0.89 | — | — |
| `deepseek-v3` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `deepseek/deepseek-v3` | 归一化后完全相同 · 供应商 deepseek | 0.27 | 1.1 | 0.07 | — |
| `fireworks_ai/accounts/fireworks/models/deepseek-v3` | 归一化后完全相同 · 供应商 fireworks_ai | 0.9 | 0.9 | — | — |
| `hyperbolic/deepseek-ai/DeepSeek-V3` | 归一化后完全相同 · 供应商 hyperbolic | 0.2 | 0.2 | — | — |


### 27. `qwq-plus`

- 出现量：**1 请求 · 21,229 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `dashscope/qwq-plus` | 归一化后完全相同 · 供应商 dashscope | 0.8 | 2.4 | — | — |
| `qwen_ai_platform/qwq-plus` | 归一化后完全相同 · 供应商 qwen_ai_platform | 0.8 | 2.4 | — | — |
| `qwencloud/qwq-plus` | 归一化后完全相同 · 供应商 qwencloud | 0.8 | 2.4 | — | — |
| `qwq-plus` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `dashscope/qwen-plus` | 字符重合度 75% · 供应商 dashscope | 0.4 | 1.2 | — | — |
| `openrouter/qwen/qwen-plus` | 字符重合度 75% · 供应商 openrouter | 0.26 | 0.78 | 0.052 | 0.325 |


### 28. `glm-5-turbo`

- 出现量：**1 请求 · 19,645 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `glm-5-turbo` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `novita/zai-org/glm-5-turbo` | 归一化后完全相同 · 供应商 novita | 1.2 | 4 | 0.24 | — |
| `openrouter/z-ai/glm-5-turbo` | 归一化后完全相同 · 供应商 openrouter | 1.2 | 4 | 0.24 | — |
| `z-ai/glm-5-turbo` | 归一化后完全相同 · 供应商 z-ai | 1.2 | 4 | 0.24 | — |
| `baseten/zai-org/GLM-5` | 归一化后互相包含 · 供应商 baseten | 0.95 | 3.15 | — | — |
| `deepinfra/zai-org/GLM-5` | 归一化后互相包含 · 供应商 deepinfra | 0.6 | 2.08 | 0.12 | — |


### 29. `glm-4.7-flash`

- 出现量：**1 请求 · 17,181 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `cloudflare/@cf/zai-org/glm-4.7-flash` | 归一化后完全相同 · 供应商 cloudflare | 0.0605 | 0.4 | — | — |
| `deepinfra/zai-org/GLM-4.7-Flash` | 归一化后完全相同 · 供应商 deepinfra | 0.06 | 0.4 | 0.01 | — |
| `glm-4.7-flash` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `novita/zai-org/glm-4.7-flash` | 归一化后完全相同 · 供应商 novita | 0.07 | 0.4 | 0.01 | — |
| `openrouter/z-ai/glm-4.7-flash` | 归一化后完全相同 · 供应商 openrouter | 0.0605 | 0.4 | 0.01 | — |
| `z-ai/glm-4.7-flash` | 归一化后完全相同 · 供应商 z-ai | 0.0605 | 0.4 | — | — |


### 30. `GLM-5-Turbo`

- 出现量：**2 请求 · 0 token**

| 候选键 | 匹配依据 | input | output | cache_read | cache_write |
|---|---|---:|---:|---:|---:|
| `glm-5-turbo` | 归一化后完全相同 · 该键没有可用价格数据 | — | — | — | — |
| `novita/zai-org/glm-5-turbo` | 归一化后完全相同 · 供应商 novita | 1.2 | 4 | 0.24 | — |
| `openrouter/z-ai/glm-5-turbo` | 归一化后完全相同 · 供应商 openrouter | 1.2 | 4 | 0.24 | — |
| `z-ai/glm-5-turbo` | 归一化后完全相同 · 供应商 z-ai | 1.2 | 4 | 0.24 | — |
| `baseten/zai-org/GLM-5` | 归一化后互相包含 · 供应商 baseten | 0.95 | 3.15 | — | — |
| `deepinfra/zai-org/GLM-5` | 归一化后互相包含 · 供应商 deepinfra | 0.6 | 2.08 | 0.12 | — |


## 三、填写模板

把下面这段复制到 `pricing.json`（替换掉同名的 null 条目即可）。**留 `null` 的字段不会覆盖自动同步到的值** —— 只想填 input/output 也完全可以。

```json
{
  "GLM-5.3-Flash": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-v4.1-flash": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "hy4-preview": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "kimi-k2.7": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5.2": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "minimax-m3": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5.2-a": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-ai/DeepSeek-V4-Pro": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5.3": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "doubao-seed-evolving-latest-version": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "hy3": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "kimi-k2.6": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "qwen3.8-max": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5.3-flash": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-v4-flash-version": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-v4-pro-0813": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-4.7": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5.2-x": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "qwen-plus": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-r1-0528": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-v4-flash-0731": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "GLM-5.3": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-modlens/deepseek-v4-flash-vision-exp": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-ai/DeepSeek-V3.2": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-z1-flash": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "deepseek-v3": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "qwq-plus": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-5-turbo": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "glm-4.7-flash": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "GLM-5-Turbo": {"input": null, "output": null, "cache_read": null, "cache_write": null}
}
```


填完后重新扫描即可看到成本：

```bat
python autopilot.py run
```
