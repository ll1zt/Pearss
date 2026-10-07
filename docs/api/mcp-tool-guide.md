# MCP Tool / Resource 语义与安全规范

本文件是新增或修改 MCP Tool、Resource 时的依据。AGENTS.md 要求遵循它，所以它必须先于代码存在。

规范来源不是新政策，而是仓库里已散落的既有约束的汇总：AGENTS.md 的领域不变量、
`architecture-spec.md` §2.3 与「非功能需求·上下文经济」、§7.1 的契约防腐条款、
以及 `docs/domain/error-taxonomy.md` 的诊断语义。若本文件与那些出处冲突，以出处为准并修正本文件。

## 一、定位：MCP 是 Core API 的投影，不是第二套后端

`architecture-spec.md` §8 把「API-first，FastMCP 从 OpenAPI 生成」列为**难逆**决策。落到实现上：

```text
agent → MCP tool → Core API (/v1) → 审计中间件 → repository
```

- **工具不直接调用 `adapters/repository`**。AGENTS.md 规定「所有 API 调用方均需经过 Core
  API 审计中间件；不得只在 MCP 层做审计」，直调仓储会绕过审计、鉴权与校验
- 工具是 Core API 的**薄封装 + 呈现层控制**：负责截断、字段挑选与错误映射
- 因此新增一个动作的正确顺序是：**先有 `/v1` endpoint，再包工具**。反过来做会让契约分裂

## 二、上下文经济（最重要的取舍）

`architecture-spec.md` 非功能需求：「工具少而高层、返回紧凑、大内容走 Resource」。

### 硬性规则

1. **不得批量返回正文**。列表类工具返回 `item_id` / `enrichment_id` 或 Resource
   URI（AGENTS.md 明文）。单条获取例外：`get_item_body` 带 `max_chars` 截断，
   且返回值必须自述 `truncated` / `total_chars`（ADR 0013）。

   这条规则的依据是**粒度**，不是"Resource 不进上下文"—— 后者是错的：
   `resources/read` 的 `text` 与工具结果同样进入对话上下文，MCP 没有
   "读取但不进上下文"的语义。Resource 仍优先，因为部分客户端把它当附件。
2. **列表必须分页 + 截断**。MCP 工具默认 `limit=20`、`/v1` 默认 `limit=50`，
   上限两边都是 100。MCP 侧给得比 `/v1` 小，是因为工具结果直接进模型上下文，而 `/v1`
   的默认值还要服务 Reader API 那类按页取全量的调用方——**两侧默认值本来就不同**，
   旧版本这里写「默认 50，与 `/v1` 一致」，两个数字都不对（issue #69）。
   **调用方要的那一页就是返回的那一页**：`_truncate` 不对主结果集（`data`）套
   `MAX_LIST_ITEMS` 兜底——它的长度由调用方的 `limit` 决定，而该 `limit` 已被
   `/v1` 端点限制在 1..100。兜底只作用于**其它**列表（任意深度的嵌套数组、写操作的
   per-item 行），被截断时一律带 `truncated_items` 标记。**截断永不静默。**
   （issue #65：兜底曾经把 `limit=100` 砍成 20 行，而调用方无从分辨「只有 20 行」
   与「100 行里显示了 20 行」。）
3. **字符串字段截断**。标题等短字段截到 200 字符并标注被截断；长文本不进返回值
4. **默认只返回摘要字段**，完整字段由调用方显式 `fields=` 索取
5. **工具数量克制**：一个工具对应一个用户意图，不做一个"什么都能干"的万能工具，
   也不把同一意图拆成多个

### 大内容走 Resource

正文通过 `item://{item_id}` Resource 提供，而不是工具返回值。理由：agent 可以按需取，
且取到的内容不进每一轮上下文。

## 三、写操作：不确认，直接执行

写工具**不走 elicitation**（ADR 0058，issue #270）。这条曾经是硬规则；退役的理由与替代面写在这里，因为「为什么没有确认」是改工具面时最先要能回答的问题。

### 为什么退役

- **它不承担安全责任**。elicitation 要求*配合的*客户端向用户展示确认；想写入的客户端自己回答「确认」即可。它约束的是误操作，不是对抗——真正的边界是 token（见第五节）。
- **它的代价落在不能配合的客户端上**。MCP elicitation 是可选、较新的能力，多数客户端与框架没有实现。实测：同一个 agent 通道下 **13 个读工具全部可用、16 个写工具全部不可用**（每个都回 JSON-RPC `-32021 MissingRequiredClientCapability`），只能绕开工具面直接打 `/v1`。对一个自称 agent-first 的项目，这是主路径不可用。

### 那么靠什么约束误操作

1. **token 权限**——真正的边界：`PEARSS_MCP_TOKEN`，独立于 Core API token；
2. **工具描述与 annotations**——破坏性工具（`purge_origin` / `delete_items` / `delete_origin_items`）在描述里明写不可逆后果，且 `destructiveHint=true`；出网或消耗 provider 预算的写工具 `openWorldHint=true`。注意注解跟着**语义**走而不是名字：`remove_subscription` 听起来像删除，但它只是退订（ADR 0063），因此 `destructiveHint=false`——把删除藏在一个叫 delete 的动词后面正是这条注解要防的事。注解是 host 决定是否追加自己的确认界面的依据，所以它和描述同属契约（进 snapshot）；
3. **幂等**——每次调用新生成 `Idempotency-Key`，重放不重复生效（因此 `idempotentHint=false`：重试是新意图）；
4. **可审计**——每次写都有 Core API 审计行与 correlation id，事后可追溯。

### 工具面必须保持的性质

- 写工具经 `/v1`（ADR 0010），不直调仓储：审计、鉴权与校验都在那条路上；
- 工具返回值带 `audit_id`，且该 id 能在 `audit_log` 查到对应行（读工具不带，因为读不被审计）；
- 每个工具声明 `ToolAnnotations`，且**与描述里的副作用陈述同源**（下表）。规则按下表读：左边是描述必须说的话，右边是随之而来的注解。**不要按工具类别记**——真出现一个出网的读工具，照类别记就会把它标错，照下表记则不会（issue #69）：

  | 描述说的 | 注解必须是 |
  |---|---|
  | 有不可逆后果（删除、force publish） | `destructiveHint=true` |
  | 发起外部请求、或消耗 provider 预算 | `openWorldHint=true` |
  | 只改本部署自己的状态 | `openWorldHint=false`、`destructiveHint=false` |
  | （写工具） | `readOnlyHint=false`、`idempotentHint=false`——不接受调用方提供的稳定幂等键（每次新生成），重试即新意图 |

  这三条与描述的一致性由 `tests/unit/test_mcp_tool_guide.py` 机械对账（措辞 ⇔ hint）。
- 入参 schema 里**不得**出现 `confirmed` / `ack` 之类的确认参数：`tests/integration/test_mcp_server.py` 对真实注册表逐个查。

### 写工具（18 个）

| 工具 | 副作用 |
|---|---|
| `add_subscription` | 引入外部内容源；`folder` 可在同一次调用里归类 |
| `add_subscription_to_origin` | 给**已有** origin 加第二个订阅（同一批事实、另一套策略：不同 folder / chain / 语言，issue #279）：按 `(origin, folder)` 幂等，**不重复入库条目**，也不产生新的外部请求（origin 已在采集） |
| `add_subscriptions` | 批量引入外部内容源（ADR 0044）：返回**作业句柄**；逐 URL 结果查 `work_units`（不可用 URL 单独 `failed`，已订阅的是 `skipped`） |
| `fetch_origin_items` | 按地址抓取入库：调用方（agent）给一批地址、**部署去抓**。返回**作业句柄**（`work_id` + 折叠 counts，响应大小不随地址数变化；作业在调用返回后继续跑），逐条结果用 `work_units` 读；地址必须在本源 host 上 |
| `update_subscription_policy` | 改变抓取频率，可能压垮对方站点 |
| `set_subscription_status` | 暂停/恢复采集，改变采集行为 |
| `set_subscription_folder` | 改变订阅在客户端侧边栏中的归类（下一次 `subscription/list` 生效） |
| `set_subscription_pipeline_chain` | 给已有源设/清处理器链：条目在闸门后等待，处理器会发出真实外部请求与花费 provider 预算 |
| `remove_subscription` | **退订**：只把订阅置为 `disabled`，条目/正文/已读状态全部保留，重新订阅同一来源即复活（ADR 0063；旧语义是删除，已改） |
| `purge_origin` | **销毁**：删除 origin 及其全部条目、正文、已读状态、产物、抓取记录与 collect job（不可逆）。`reason` 必填，写进审计行；这是唯一删除内容的工具 |
| `delete_items` | 按 id 永久删除条目（含正文、已读状态、译文）：返回**作业句柄**，逐 id 结果查 `work_units`（已消失的 id 是 `skipped`） |
| `delete_origin_items` | 清空某源的条目但保留订阅与抓取游标（可选 since/until / include_published） |
| `set_item_read_state` | 标记条目的**消费状态**（已读/未读、星标/取消）：只写 `read_state` 行，**不碰条目本体**（content_hash / guid / publish_status / content_ref 都不动，已发布内容冻结）；省略的字段保留原值；`/v1/items/{id}/read-state` 与 Reader API 报同一状态（同一领域函数） |
| `trigger_job` | 立即抓取 = 真实外部请求 |
| `submit_items` | 向 agent 源推送内容（ADR 0022：写语料库）：返回**作业句柄**，逐条结果查 `work_units` |
| `retry_enrichments` | 重排队失败的处理器链产物：`enrichment_id` 只重排**一行**（`POST /v1/enrichments/{id}:retry`），`source_id` 重排该源全部失败行 |
| `backfill_subscription_chain` | 对历史条目一次性补跑处理器链：返回**作业句柄**（服务端选择「仍缺该链产物的条目」，带 limit 重复调用会前进）；逐条结果查 `work_units` |
| `set_plugin_status` | 启停采集器插件，影响它服务的所有源 |

只读工具（13 个）：`list_sources`、`get_source`、`diagnose_source`、`search_items`、`get_item`、
`get_item_body`、`list_jobs`、`list_runs`、`list_enrichments`、`list_plugins`、
`get_work`、`work_units`、`get_system_snapshot`。

`get_work` 与 `work_units` 是作业的读侧（ADR 0046）：返回句柄的工具
（如 `fetch_origin_items`）不再内联逐条结果，因为那种响应会随地址数膨胀、
并在客户端超时时连同结果一起丢掉。
- `get_work`：状态 + 折叠 counts（`ok` / `skipped` / `rejected` / `failed` / `queued`），
  **不返回** target 列表，所以是常数大小。
- `work_units`：逐条账本（target / outcome / error_class / detail / 产物 id），
  用 `cursor` 分页，用 `outcome` 过滤——`outcome=failed` 就是「我该重发哪些」。
  `rejected`（没尝试过，例如非同源）与 `failed`（尝试了但失败）分开，因为只有后者值得重发。

issue #193 的两个读原语**并入既有工具**（不新增工具名：工具面是每轮对话的上下文预算）：

- `search_items(url=…)`：按**规范化后的 canonical url** 精确查条目——回答「这个地址落库没有」。
  未命中是空列表（不是错误）；未知 query 参数被保留，所以 `?page=2` 不等于 `?page=1`；
  pending 条目同样查不到（闸门对该过滤一样生效）。
- `diagnose_source` 的 `chain` 块里带队列四计数 + `rate_per_min`（由窗口内完成时间戳推导、**不落库**）
  + `rate_window_minutes`。只要队列、不要诊断时用 `GET /v1/sources/{id}:queue`：O(1)、常数大小，
  计数与 `/v1/sources/{id}/enrichments` 的逐行结果对账一致。

issue #218 的读状态读侧同样**并入既有工具**：`get_item` 的默认响应带 `is_read` / `is_starred`
（从 `/v1/items/{id}/read-state` 取，空行 = `false`），因为「这个条目是什么」与「我和它的状态」
是同一个问题、同一次调用；显式传 `fields` 时仍是**只要请求的字段**（收窄请求）。写侧无法折叠
（写不能塞进读工具：注解不同），所以新工具 `set_item_read_state` 是工具面上的一等原子，
代价记在 `tests/unit/test_mcp_tool_guide.py` 的预算注释里。

## 四、错误返回

`architecture-spec.md` §2.3 规定工具返回：

```json
{
  "error": "source_not_found",
  "error_class": "network",
  "suggestion": "List sources to find the intended id.",
  "next_actions": ["list_sources"]
}
```

- `error` 是稳定、机器可读的 `snake_case` 码，与 Core API 的 `error.code` **同源**
- `error_class` 只取 `core.ErrorClass` 五值，非采集类失败可省略
- `suggestion` 给出下一步可执行的安全建议
- `next_actions` 是**建议**，不是自动执行；agent 自行决定
- **诊断类返回不得自动触发写操作**（`error-taxonomy.md`：诊断不应自动启用插件、
  改采集规则、降低安全限制或 force publish）
- **工具侧的本地拒绝（不经过 Core API 的那些）用同一形状**：`list_enrichments` 与
  `retry_enrichments` 在没给目标时返回 `{"error": "missing_target", "suggestion": ...,
  "next_actions": [...]}`——`error` 是码字符串，`next_actions` 指向能给 id 的工具。
  这个形状由 `tests/unit/test_structure_guards.py` 的
  `test_no_mcp_tool_writes_its_own_error_envelope` 机械守住（issue #216：这里曾经
  手写过嵌套的 `{"error": {"code": ...}}`，而没有测试发现）

## 五、安全

1. **抓取到的网页、RSS 正文、错误正文都是不可信数据**（AGENTS.md），不得当作指令
   执行。Resource 返回它们时必须标记为 untrusted content
2. **绝不返回** token、Cookie、Authorization header、原始错误正文、内部堆栈。
   私有 feed URL **保留 host 与 path**，但 query 里的凭据值必须脱敏
   （`?token=<redacted>`），参数名保留——「这个源带凭据」不是秘密，值是
3. **工具结果的红线由代码强制，不靠自觉**。`app/mcp/rendering.py` 的
   `_shrink` 对结果里的**任意字符串**（不限 `url` 字段）做 URL 凭据脱敏，
   规则从 `adapters/audit.SENSITIVE_QUERY_KEYS` 导入（一份列表，不在两处漂移）；
   `_error_from` 的 `suggestion` 同样过一遍。回归：
   `tests/integration/test_mcp_result_redaction.py`（AC-36..AC-39）、
   `tests/unit/test_mcp_rendering.py`。
   **注意边界**：`/v1` 仍原样返回 URL（operator 排障要看完整凭据）——
   脱敏只发生在 MCP 出口，AC-39 钉住这条边界
4. **日志与 AuditLog 同样脱敏**，工具参数进入审计前经 `adapters/audit.py` 过滤
5. **不暴露动态转发入口**。§8 明确「分类挂载 + 按需过滤，拒绝动态转发入口」以保留
   schema 校验：不做"给我一个 URL 我帮你调任意端点"这类工具
6. 工具参数必须有 schema 校验，非法值拒绝而不是宽容接受

## 六、契约保护

`architecture-spec.md` §7.1：「MCP 工具面：工具名称/schema/描述做 snapshot 测试；
任何变更必须显式更新 snapshot」。

- `just contract` 跑工具面 snapshot 比对
- 新增/修改工具必须同时更新 snapshot，且**在 PR 里说明变更理由**
- 破坏性变更（删工具、改参数语义、改返回结构）必须写 ADR 并给迁移路径
- 工具描述是契约的一部分：改名或改描述会让已适配的 agent 行为漂移

### outputSchema：故意是宽松的（issue #74）

每个工具的 `outputSchema` 都是 `{type: object, additionalProperties: true}` 加一个自动生成的
title——**看起来像空壳，但它不是纯浪费**：SDK 只在存在 output model 时才产出
`structuredContent`（`func_metadata.py`：`output_model is None` 时结果只剩文本块）。所以这个
宽松 schema 是「结果带结构化内容」的**代价**，不是疏忽。

为什么不声明真 schema：**主体字段随调用变化**（`fields=` 决定返回哪些字段，`_truncate` 决定
哪些被裁剪），声明它们等于让 schema 撒谎。**协议性字段**（`truncated_items` / `total_chars` /
`audit_id` / `correlation_id` / `error`）确实可以声明，但它们**每个工具都一样**，所以写在
`server/discover` 的 `instructions` 里——一份，而不是 22 份。

两条都由测试钉住：`tests/integration/test_mcp_conformance.py` 断言 instructions 点名了这些字段，
`tests/unit/test_mcp_tool_guide.py` 断言本指南写了这个理由。

## 七、命名与描述

- 工具名 `snake_case`，动词开头，与 `/v1` 的 `operationId` 语义对齐
  （`list_sources` ↔ `listSources`）
- 每个工具必须有：一句话说明、参数说明、返回结构说明、失败时的
  `error_class` 取值范围
- 描述里写清**副作用**：是否发起外部请求、是否改变状态、是否可逆（写工具不写「需要确认」——ADR 0058）

### 工具名不带前缀（issue #75）

工具名**不**加 `pearss_` 前缀，这是决定而非遗漏：命名空间是**宿主**的职责。实测本机的 host 会
给每个 server 的工具加 server 名前缀（`forgejo_*`、`nixos_*`、`pearss_*`），而已带前缀的名字
不会被叠加（anki 的 `anki_*` 原样透传）。

重命名的收益只对「**不前缀**的客户端」存在，代价却是破坏性契约变更（工具名进 snapshot）**加上**
常驻面多 7×22 = 154 字符——与本指南第二节要压的那笔税方向相反。

**触发条件**（满足其一就重做这道题）：出现一个真实的不前缀客户端；或工具名在某个真实客户端上
发生碰撞。届时走新 ADR + 迁移路径。

## 八、订阅归类（folder）

`Source` 带一个 `folder`：订阅在客户端侧边栏中的分组。**一订阅一分类**，
跟随 FreshRSS（其源码明确 "a feed can only be in one category"，客户端也按该模型实现；
多对多会在这些客户端里表现为不可预期）。默认 `default`。

三条路径共享它，且都以 `user/-/label/<name>` 的形式暴露给 Reader API：

| 路径 | 用法 |
|---|---|
| `POST /v1/sources` | 创建时带 `folder` 即可归入 |
| `PATCH /v1/sources/{id}` | `{"folder": "阅读"}` 改分类；`{"folder": null}` 回 `default` |
| `POST /reader/api/0/subscription/edit` | 拖拽改名/移组 + App 内退订：`ac=edit`、`s=<feed id>`、`a=user/-/label/名字` 加入、`r=...` 回默认、`t=新名` 改名；`ac=unsubscribe` 删除该源（ADR 0021 语义，重放仍 OK） |
| `POST /reader/api/0/rename-tag` | 重命名分组：`s`、`dest` 都是 `user/-/label/<name>`，批量改该名字下的所有源 |
| `POST /reader/api/0/disable-tag` | 删除分组：组内源回 `default`，源本身保留 |
| `POST /reader/api/0/subscription/quickadd` | 按 URL 订阅（Read You 加订阅），返回 `streamId` |

`t=`（改名）与 `ac=unsubscribe`（删除）现已支持（2026-09-16 修订，ADR 0024）：改名由
`set_source_title_if_absent` 的条件写入保护，采集不会覆盖；删除走 `DELETE /v1/sources`
的同一仓储入口，完整 ADR 0021 语义。`ac=subscribe` 仍不提供——订阅走 `quickadd`。

名称校验在 `core/folders`：trim、长度上限、禁控制字符、**禁 `/`**（名字会变成 stream id，
斜杠会产生客户端无法往返的路径）。MCP 侧对应 `set_subscription_folder` 与 `add_subscription` 的
`folder` 参数。

## 九、传输与挂载

- 传输：**Streamable HTTP**（`architecture-spec.md` §9），与 Core API 同进程挂载，端点 `/mcp`

**入站鉴权（客户端 → `/mcp`）**

- `PEARSS_MCP_TOKEN`，**独立于** Core API token：撤销一个笔记本上的 MCP 客户端不应连带
  登出 Core API，反之亦然。它认证的是**边缘**，不是**行为者**
- **fail-closed**：未配置时端点返回 503 并拒绝一切，启动记 warning —— 「未配置」不得读作
  「未鉴权」
- **elicitation 不是安全边界**（ADR 0058 就是据此退役它的）。它要求*配合的*客户端向用户
  展示确认；想写入的客户端自己回答「确认」即可。它约束的是误操作，不是对抗 —— 真正的边界
  是这个 token。既然它不承担安全职责，却让无该能力的客户端一个写工具都调不了，退役它
  只损失「防误操作」的一层软性提醒，而那一层已由描述/annotations、幂等与审计接住

**出站鉴权（MCP server → `/v1`）**

- MCP server 作为**可信内部客户端**持有服务端 token 调 `/v1`，因此审计里
  actor 记为 `api:full`；agent 身份通过 session/correlation id 关联，不通过伪造
  actor 表达

**Host 白名单**

- MCP SDK 默认开 DNS-rebinding 防护，未知 Host 返回 **421** `Invalid Host header`。
  不设 `PEARSS_MCP_ALLOWED_HOSTS` 则**只有回环可达**
- 条目需 `host:*` 形式：SDK 只做精确匹配或 `host:*` 通配，而客户端总带端口发送，
  裸 `host` 匹配不上 `host:8000`
- 远程访问因此需要**两个显式选择**（token + Host），都不是默认值

- 未来 P3 拆分子模块 server + Gateway 时，本规范继续适用，只是挂载点变化

## 十、验收清单（改工具面时逐条过）

> **章节编号说明**：2026-09-28 之前本文件有两个「七、」段（「命名与描述」与
> 「订阅归类（folder）」），使「第七节」无法判定指向何处。去重后编号整体后移一位：
> 原「八、传输与挂载」现为**第九节**，原「九、验收清单」现为**第十节**。
> ADR 0010 里「写进 mcp-tool-guide 第八节」指的是**现在的第九节**（actor 归因那段）——
> 已接受的 ADR 不改写，所以映射记在这里。编号唯一且递增由
> `tests/unit/test_mcp_tool_guide.py` 守住。

1. 工具是否经 `/v1` 而非直调仓储？（审计覆盖）
2. 返回值是否可能含正文、HTML、token、私有 URL？
3. 列表是否分页、字符串是否截断、默认字段是否最小？
4. 写操作是否经 `/v1`、带 `Idempotency-Key`、有 `audit_id`？（写工具**不 elicit**——ADR 0058）
5. 写操作是否返回 `audit_id`，且该 id 能在 `audit_log` 中查到对应行？
6. 工具是否声明了与语义相符的 `ToolAnnotations`？
7. 错误是否用统一结构，`error_class` 是否取自 enum？
8. 是否更新了工具 snapshot？
9. 破坏性变更是否写了 ADR？
