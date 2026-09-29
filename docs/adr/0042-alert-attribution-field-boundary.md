# 0042: 告警与诊断的归因字段边界——把「never quoting」细化为两类

Status: accepted
Date: 2026-09-29
Context: issue #152。一次实机告警只带了源 id 和一行**全库全时**的统计：

```
⛔ Pearss collection degraded
1 source(s) with a chain that cannot finish: 76b6b942cd69370315b1ab3c7b42ae44
success rate 98% over 50 runs
dominant error class: network (781 runs)
```

「哪条源」要回 API 查、「为什么」要读文件和日志（真因是 24 条 `parse`，
而 `network 781` 是全库累计，把人带偏），`diagnose_source` 的工具描述承诺
「answered in one call」，返回体却没有 enrichment 的 `error_class` 与 `detail`
（投影漏项，违反 ADR 0036）。

本 ADR 只为**出境字段边界**作决定，不重复 ADR 0034 的触发/节流设计。

## 决策

### 1. 归因字段允许出境（有界）

允许出现在告警体与 `:diagnose` 里：

- 源 **id** 与源 **标题**（用户自己配置的标识符，用于回答「哪条源」）
- 失败**分类**与**计数**
- **provider 错误文本**，截断（通知侧 120 字符；库里 500）

理由：这些字段回答的是「哪条源、什么错」，不是用户内容；provider 错误文本是远端服务
对**请求**的拒绝说明，不是条目正文。源标题是用户自己写的，不是抓来的内容。

### 2. 内容字段禁止出境

仍然禁止：条目正文与正文片段、凭据/cookie/token/Authorization、author 个人信息、
完整私有 URL。

- `alert_body` 仍由**固定字段**构造，不插异常 repr（ADR 0034 决策 5 的构造纪律不变）
- `:diagnose` 的 `failed_enrichments` 只返回元数据与错误文本，**不返回条目正文**
  （AGENTS.md：列表类工具不得批量返回正文）

### 3. 降级开关

若日后认为源标题敏感，可退回「只发 id + 分类」：一个常量/环境变量即可，**结构不变**。
本条不预设开关现在存在，只承诺退回不需要改数据结构。

## 与 ADR 0034 的关系

- 0034 决策 5 的「不发源 URL、源用 id 标识」**保留**。
- 0034 决策 5 的「错误详情按 issue 要求也**不发原文**（可能含凭据）」与「已否决 ·
  把错误详情原文放进告警体」被本条**收窄**：provider 错误文本（截断）现在允许，
  因为它回答的正是归因问题；其余红线（正文、凭据、PII、URL）**不变**。
- 0034 的触发条件、边沿触发、MIN_INTERVAL 与本条无关，不被修改。

## 后果

- `core/alerting.py` 新增 `ExhaustedSource`（归因值），`alert_body` 增加归因段，
  `dominant error class` 明确标注 `(all-time)`。
- `exhausted_enrichment_sources` 返回归因；`failed_enrichments_for_source` 供
  `:diagnose` 使用。
- `:diagnose` 增加 `failed_enrichments` 与 `last_run.error_class`；MCP 描述与
  snapshot 同步。
- 无 migration；告警仍不出现在任何 HTTP 响应里。

## 验收

见 `SCENARIOS.md` 的 AC-150..156 与 `tests/unit/test_alerting.py`、
`tests/integration/test_alerting.py`、`tests/integration/test_sources_api.py`。
隐私负向用例断言告警体与 `:diagnose` 都**不含条目正文**。
