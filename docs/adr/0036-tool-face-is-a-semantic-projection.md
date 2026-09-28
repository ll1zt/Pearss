# 0036 · 工具面是 /v1 的语义投影：一 endpoint 可对多工具，聚合放端点里

- 状态：accepted
- 日期：2026-09-28
- 补充：[ADR 0010](0010-mcp-tools-via-core-api.md)（修订其「工具与 /v1 endpoint 一一对应」一句）

## 背景：那句话只在一个方向上为真

ADR 0010 写：

> §8「MCP 是 OpenAPI 的投影」在**语义层**保持：工具与 `/v1` endpoint **一一对应**、
> 错误码同源、命名对齐 `operationId`。

实测（读代码，非推断）：`/v1` 侧是**一个**端点

```python
# src/app/sources_routes.py:447
@router.patch("/sources/{source_id}", operation_id="updateSource")
```

而 MCP 侧有**三个**工具都落在它上面：

| 工具 | 请求体 | `open_world_hint` | 确认文案 |
|---|---|---|---|
| `update_schedule_policy` | `{schedule_policy}` | **true** | "Set source X schedule to …?" |
| `set_source_status` | `{status}` | false | "Set source X status to paused?" |
| `set_source_folder` | `{folder}` | false | "Move source X to folder Y?" |

它们被拆开**不是漂移，是有收益的**：确认文案要按**意图**分化（"暂停采集"与"移进文件夹"
读起来完全不同），annotations 要按**副作用**分化（改频率会给源站压流量，移文件夹不会）。

**反向**那句则一直成立且被强制：每个工具都必须落在某个 `/v1` endpoint 上——工具不直调
repository，每次调用都经 `/v1` 并留审计（ADR 0010 决策 2，`tests/e2e/test_attribution.py`
钉住这件事）。

所以问题不是"哪个方向错了"，而是**声明把两个方向写成了一个**。

## 决策 1：一个 endpoint 可以对应多个工具

当且仅当满足其一：

- **(a) 确认文案需按意图分化**——确认的价值在于让用户读懂将要发生什么，一个通用动词做不到；
- **(b) annotations 需按副作用分化**——`openWorldHint` / `destructiveHint` 是给 host 读的
  （它据此决定是否追加自己的确认），取并集等于对所有操作都按最危险的那个对待。

反向约束不变：**每个工具必须落在某个 `/v1` endpoint 上**，由审计覆盖强制。

## 决策 2：聚合放 `/v1`，不放 MCP server

当某个真实问题需要"一次调用回答"、而现在要串 3–6 个工具时（例：「这个源为什么没更新？」
= `list_sources` → `get_source` → `list_jobs` → `list_runs` → `list_enrichments` →
`get_system_snapshot`），**新增一个 `/v1` 聚合端点**，再把它 1:1 投影成一个工具。

- 先例：`get_system_snapshot`（ADR 0019）已经就是"一次调用回答一个更宽问题"的聚合端点。
- 理由：**分层**。`app -> plugins -> adapters -> core` 里，编排属于 app；MCP 呈现层只做
  截断、字段选择、elicitation 与错误映射（ADR 0010 原则 1「服务器要极容易写，复杂编排留在
  host」）。把编排放进呈现层，会让它与 `/v1` 的校验、审计与幂等分叉——那三样都在 `/v1`
  这一层才有定义。

## 决策 3：补上 `pipeline_chain` 的投影缺口

`PATCH /v1/sources/{source_id}` **已经**接受 `pipeline_chain` 与 `target_lang`
（`SourcePatch`，三态语义：缺省=不动 / 显式 `null`=清空 / 值=设置；改动前用
`parse_chain(..., known_plugins=registered_processor_ids())` 成对校验）。

而 MCP 侧没有对应工具，于是「先订阅、后来才想给历史条目补翻译」这条路走不通：
`add_source` 用不上（源已存在），`backfill_source` 要求源**已有**链，唯一出路是
`remove_source`（不可逆）再重建——丢掉全部历史与已读状态。

因此新增同形的第四个窄工具 `set_source_pipeline_chain`，落在那个**既有**端点上。

## 理由：被否掉的备选

| 备选 | 否掉的理由 |
|---|---|
| 恢复真 1:1：把三个窄工具合并成一个 `update_source` | 确认文案变通用；`open_world_hint` 必须取并集（host 对"移文件夹"也追加确认）；且本身是破坏性工具面变更，也要 ADR 与迁移路径 |
| 允许 MCP server 内组合多个端点 | 呈现层变成编排器，违反原则 1 与分层；组合逻辑会与 `/v1` 的校验/审计/幂等分叉 |
| 直接改写 ADR 0010 的措辞 | 违反 0000 的「不改写已接受的 ADR」——历史决策就是历史。本 ADR 走 0003/0005 的先例：新 ADR + 旧文件状态行标注 |

## 后果

- 工具数与端点数的差不再是漂移，而是被本 ADR 认可的形状；新增工具时判据明确：
  **它是否落在既有端点上**。
- "一次调用回答更宽问题"的需求有了明确落点（新 `/v1` 端点），不会以"在 server 里串一下"
  的形式出现。
- 代价：一 endpoint 对多工具意味着**端点的语义与工具名不再互推**，读代码时要多看一眼装饰器。
  这条 ADR 就是那个"多看一眼"的索引。

## 可逆性

可逆。每个工具都是薄投影，合并或拆分是机械改动（要同步 `tests/contract/mcp_tools.json`
与 `docs/api/mcp-tool-guide.md`）。聚合端点独立可删，不影响其余端点。
