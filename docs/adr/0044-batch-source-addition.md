# ADR 0044：批量建源——一次确认覆盖一批（MCP `add_sources` + `/v1/sources:batch`）

- 状态：proposed（草案；**待裁决**）
- 日期：2026-09-29
- issue：#159
- 相关 / 修订：ADR 0041 决策 1（「批量形态本身也不缺」一句）、ADR 0036 决策 2、ADR 0010、ADR 0022（`submit_items` 的批量确认先例）、ADR 0026（审计边界）

## 背景

**用户实测**：让 agent 订阅一份清单时，N 个源要点 N 次确认弹窗。

现状（读代码，非推断）：

- MCP 面 24 个工具里只有 `add_source`（单条，`src/app/mcp/tools/writes.py:128`）；HTTP 侧只有
  `POST /v1/sources`（单数，`src/app/sources_routes.py:458`）。**没有批量端点。**
- 每个写工具经 `_guarded_write` + `Resolve(_add_source_confirmation)`：**一次调用 = 一次
  elicitation**。这是 AGENTS.md「需要用户确认的 MCP 动作必须走 elicitation」的直接后果，不是缺陷。
- 已有的批量确认先例是 `submit_items(source_id, items[])`：一次确认覆盖整批，并用
  `items_fingerprint` 覆盖「只比条数」的漏法（issue #67）。**机制已有，只是没用到建源上。**

ADR 0041 决策 1（A6 OPML 不做）的括号里写着：

> 批量形态本身也不缺——`POST /v1/sources` 与 MCP 的写工具已覆盖逐条建源，OPML 只是它的一个
> 循环外壳。

**功能上为真，交互上不成立。** 逐条覆盖的代价就是逐条确认；ADR 0041 算的是「有没有能力」，
没有算「要几次确认」。本 ADR 补上这一项，并提供一个批量**基元**（OPML 仍不做，见决策 4）。

## 决策

### 1. `/v1/sources:batch`：单请求建多条，逐条报告结果

请求体：

```
{
  "urls": ["https://a.example/feed.xml", "https://b.example/rss", ...],
  "schedule_policy": {"type": "fixed", "interval": "60m"},
  "type": "auto",                 # 可选；auto 时逐条按 collector 推断
  "folder": "",                   # 可选，共享
  "pipeline_chain": null,         # 可选，共享
  "target_lang": ""
}
```

响应 `200`：

```
{
  "created": 12, "existing": 0, "failed": 1,
  "results": [
    {"url": "...", "source_id": "...", "status": "created"},
    {"url": "...", "status": "existing", "source_id": "..."},
    {"url": "...", "status": "failed", "error_class": "parse", "detail": "..."}
  ],
  "correlation_id": "..."
}
```

约束：

- **部分失败不回滚成功项**。「一次加十三个，其中一个是坏 URL」必须留下十二个，并把坏的那条
  逐条报出来（`error_class` 只取既有 `ErrorClass` 枚举，不得新增字符串常量）。
- **与单条 `POST /v1/sources` 同语义**：已存在的 URL 仍返回现有源（幂等），只是该条标
  `existing`，不是错误。
- **一个请求一个 `Idempotency-Key`**：重放返回同一批结果（AGENTS.md 的写幂等）。逐条重试是
  一个新请求，天然幂等（已存在的 URL 返回现有源）。
- **批量上限**（建议 50）：超限 `422` 并建议分批。理由是上下文与单次事务时长，不是能力。
- **审计按现有边界**（ADR 0026）：一次请求一条 AuditLog；条目级细节在响应里，不在审计行。

### 2. MCP 侧 `add_sources`：一次 elicitation

- 确认字段（照 `submit_items` 的形状，`src/app/mcp/confirmation.py:332`）：
  - `count`（条数）
  - `urls_fingerprint`：规范化后的 `(url, type)` 列表的 canonical JSON 摘要——**它覆盖对话
    框显示不下的部分**（两条清单可以条数相同、甚至前几条相同而其余完全不同）
  - `urls_preview`：前 5 条 + `(+N more)`
  - 共享选项：`schedule_policy` / `folder` / `pipeline_chain` / `target_lang`
- `_add_sources_fields()` 是**唯一**的「确认什么 / 执行什么」清单（ADR 0010 决策 3 的守卫）；
  `_matches` 逐字段比对 → **篡改任一 URL 或任一共享选项即 `aborted`，且不创建任何源**。
- annotations 与 `add_source` 同形：`readOnlyHint=false`、`destructiveHint=false`、
  `openWorldHint=true`。

### 3. 保留 `add_source`（单条）

两者落在不同端点（`/v1/sources` 与 `/v1/sources:batch`），不违反 ADR 0010。按 ADR 0036 决策 1，
**确认文案按意图分化**正是拆分的正当理由：单条的确认读起来最清楚，批量是摘要。

### 4. OPML 仍然不做（ADR 0041 不变）

本 ADR 只提供**批量基元**。OPML 是「文件格式 + 导入语义」，其触发条件（有客户端要导入 OPML /
第二个客户端）不变。有了基元后 OPML 只是一个薄壳，但**现在仍然不做**。

## 需要修正的一句话

ADR 0041 决策 1：「批量形态本身也不缺」→ 改为「批量建源的**功能**不缺；缺的是**一次确认**覆盖
一批的形态（ADR 0044）」。

## 已否决

- **把 `add_source` 改成接受 1..N 并废弃单条**：单条时确认文案退化成摘要；且是破坏性契约变更
  （快照变更、已适配 agent 漂移）。
- **只给 Core API 加批量端点、不加 MCP 工具**：需求是「在 MCP 面批量订阅」；只加端点等于把循环
  留在 host，而 host 正是 agent 调工具的地方。
- **用 OPML 承载批量**：见决策 4；且 OPML 解决的是「跨客户端迁移」，不是「一次确认」。
- **复用 `submit_items`**：语义不同（推条目 vs 建源）；复用会让确认文案撒谎。

## 后果

- 新端点 + 新工具 + `openapi.json` / `mcp_tools.json` 双快照更新。
- **工具面预算会被顶破**：当前 24 个工具 = **13,966 字符 / 14,000 预算**（仅剩 34 字符，
  `tests/unit/test_mcp_tool_guide.py:203`）。加 `add_sources` 必须**显式提高
  `SURFACE_BUDGET_CHARS`**，或先做一轮描述瘦身（issue #73 路线 B）。本 ADR 要求**先瘦身、
  仍不足再提预算**，并把提高预算的理由写进提交说明。
- 失败路径要有测试：不只是「拒绝了且没落库」，还要**「部分失败了但成功项仍在」**。
- 条目级 `error_class` 只允许既有枚举值（AGENTS.md）。

## 验收判据（BDD）

> 场景编号：本 ADR 声明 **AC-161..166**（`SCENARIOS.md` 现最大号段为 AC-157..160）。

```gherkin
Scenario: AC-161 一次调用订阅一批，只确认一次
  Given 一份含 10 个 URL 的清单
  When agent 调用 add_sources
  Then 产生 1 次 elicitation（而非 10 次）
  And 10 个源都能被 list_sources 查到

Scenario: AC-162 确认覆盖全部执行参数
  Given 用户在确认里看到的 urls 集合与共享选项
  When 任一 URL 或任一共享选项与确认值不同
  Then 返回 aborted
  And 不创建任何源

Scenario: AC-163 部分失败不丢成功项
  Given 一批 3 个 URL，其中 1 个不可解析（如非法 scheme）
  When 提交
  Then 2 个源被创建
  And 返回里那 1 条带 status=failed 与既有 ErrorClass（不得新增字符串常量）

Scenario: AC-164 同 key 重放返回同一批结果
  Given 一次成功的批量建源使用了 idempotency key K
  When 用 K 重放同一请求
  Then 结果与首次逐字段一致
  And 没有新建重复源

Scenario: AC-165 批量上限与单条不变量不被削弱
  Given 超过上限的 URL 数
  When 提交
  Then 返回 422 并建议分批
  And 单条路径的校验（canonical_url / chain / 类型推断）与审计与单条端点完全一致

Scenario: AC-166 工具面预算仍然成立
  Given 新的工具面
  When 序列化测量
  Then 不超预算（或预算被显式提高且提交说明记录了原因）
```

**断言强度**：断言「几个源被创建 / 确认了几次 / 返回的逐条状态」，不断言调用了哪个内部函数。

## 参考

- ADR 0041 决策 1、ADR 0036 决策 1/2、ADR 0010、ADR 0022、ADR 0026
- `src/app/sources_routes.py:458`（单条 `addSource` 的幂等与返回形状——批量必须与它同语义）
- `src/app/mcp/tools/writes.py:128`（`add_source`）、`:492`（`submit_items` 的批量先例）
- `src/app/mcp/confirmation.py:198` / `:223`（`add_source` 字段与文案）、`:332`（`submit_items`
  的 fingerprint）
- `docs/api/mcp-tool-guide.md` §3（确认）、§5（工具数量克制）
- `tests/unit/test_mcp_tool_guide.py:203`（14,000 字符预算）、`:206`
- issue #73（工具面预算）、#75（命名空间）、#67（「只比条数」的先例）
