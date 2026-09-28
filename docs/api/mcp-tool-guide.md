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
- 工具是 Core API 的**薄封装 + 呈现层控制**：负责截断、字段挑选、elicitation 与错误映射
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

## 三、写操作：默认需要用户确认

AGENTS.md：「写工具：默认需要用户确认」；「需要用户确认的 MCP 动作必须走 elicitation」。

### 规则

- 所有**改变状态**的工具都必须走 MCP elicitation 由用户确认，确认在 **server 侧**执行，
  不依赖客户端自觉
- 确认参数必须与**实际执行参数完全相同**。若客户端确认了 A 而 server 执行了 B，
  确认机制就成了装饰
- 工具返回值必须带 `audit_id`，让操作可归因、可追溯。Core API 在 `X-Audit-ID`
  响应头上回显本次写入的审计行 id（审计行由中间件在响应发出**之后**写入，因此
  id 无法走 body），MCP 层读到后并入工具结果。读操作不被审计，因此不携带该字段
- **audit id 必须随响应返回，不得存在共享 client 的属性上**。`CoreAPIClient`
  是**单实例**、由 `MCPServer` 服务所有并发请求，而写工具都是 `async def`；
  把 id 存在 client 属性上意味着第二个调用者在第一个读取前就覆写了它，
  于是调用者报出**别人的**审计 id —— 一个精确到足以误导排查的错误答案
  （issue #64）。因此 `request()` 返回 `(status, payload, audit_id)`，
  工具用局部变量。
  注意这个缺陷是**潜伏型**：单纯的 `asyncio.gather` 不会触发（赋值与返回之间
  没有挂起点），因此回归测试用 **barrier 强制交错**，并用
  `test_the_shared_client_keeps_no_per_call_result_state` 钉住「client 上不存在
  per-call 结果状态」这条不变量（实测：只加回属性时，并发场景仍绿，只有这条守卫变红）
- 每个工具必须声明 `ToolAnnotations`，且**与描述里的副作用陈述同源**。规则按下表
  读：左边是描述必须说的话，右边是随之而来的注解。**不要按工具类别记**——旧版本这里
  写「读工具 `openWorldHint=false`」，读起来像「读工具统一为 false」的属性，而它其实
  是「不发起外部请求、不消耗 provider 预算」的**结论**：真出现一个出网的读工具，
  照类别记就会把它标错，照下表记则不会（issue #69）

  | 描述说的 | 注解必须是 |
  |---|---|
  | 需要用户确认 | `readOnlyHint=false` |
  | 有不可逆后果（删除、force publish） | `destructiveHint=true` |
  | 发起外部请求、或消耗 provider 预算 | `openWorldHint=true` |
  | （写工具） | `idempotentHint=false`——不接受调用方提供的稳定幂等键（每次新生成），重试即新意图 |

  三条与描述的一致性由 `tests/unit/test_mcp_tool_guide.py` 机械对账（措辞 ⇔ hint），
  所以「注解是契约」不只是 snapshot 里的一行 diff。注解是机器可读的副作用声明：
  host 依据 `destructiveHint` 决定是否追加自己的确认，所以它与描述同属契约（进 snapshot）
- 破坏性操作（删除、force publish）在描述里显式标注不可逆后果

### 需要确认的清单（当前）

| 工具 | 理由 |
|---|---|
| `add_source` | 引入外部内容源（可同时指定 `folder` 归类，省去 add-then-move 的第二次确认） |
| `update_schedule_policy` | 改变抓取频率，可能压垮对方站点 |
| `set_source_status` | 暂停/恢复采集，改变采集行为 |
| `set_source_folder` | 改变订阅在客户端侧边栏中的归类（下一次 `subscription/list` 生效） |
| `remove_source` | **删除来源及其全部条目**（ADR 0021：不可逆；确认文本说明会删掉什么） |
| `delete_items` | 按 id 永久删除条目（含正文、已读状态、译文） |
| `delete_source_items` | 清空某源的条目但保留订阅与抓取游标（可选 since/until / include_published） |
| `trigger_job` | 立即抓取 = 真实外部请求 |
| `submit_items` | 向 agent 源推送内容（ADR 0022：写语料库；确认文本列出条目标题与条数） |
| `retry_enrichments` | 重排队失败的处理器链产物 |
| `backfill_source` | 对历史条目一次性补跑处理器链 |
| `set_plugin_status` | 启停采集器插件，影响它服务的所有源 |

只读工具不需要确认：`list_sources`、`get_source`、`search_items`、`get_item`、
`get_item_body`、`list_jobs`、`list_runs`、`list_enrichments`、`list_plugins`、
`get_system_snapshot`。

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

## 七、命名与描述

- 工具名 `snake_case`，动词开头，与 `/v1` 的 `operationId` 语义对齐
  （`list_sources` ↔ `listSources`）
- 每个工具必须有：一句话说明、参数说明、返回结构说明、是否需要确认、失败时的
  `error_class` 取值范围
- 描述里写清**副作用**：是否发起外部请求、是否改变状态、是否可逆

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
斜杠会产生客户端无法往返的路径）。MCP 侧对应 `set_source_folder` 与 `add_source` 的
`folder` 参数。

## 九、传输与挂载

- 传输：**Streamable HTTP**（`architecture-spec.md` §9），与 Core API 同进程挂载，端点 `/mcp`

**入站鉴权（客户端 → `/mcp`）**

- `PEARSS_MCP_TOKEN`，**独立于** Core API token：撤销一个笔记本上的 MCP 客户端不应连带
  登出 Core API，反之亦然。它认证的是**边缘**，不是**行为者**
- **fail-closed**：未配置时端点返回 503 并拒绝一切，启动记 warning —— 「未配置」不得读作
  「未鉴权」
- **elicitation 不是安全边界**。它要求*配合的*客户端向用户展示确认；想写入的客户端自己
  回答「确认」即可。它约束的是误操作，不是对抗 —— 真正的边界是这个 token

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
4. 写操作是否走 elicitation，且确认参数 == 执行参数？
5. 写操作是否返回 `audit_id`，且该 id 能在 `audit_log` 中查到对应行？
6. 工具是否声明了与语义相符的 `ToolAnnotations`？
7. 错误是否用统一结构，`error_class` 是否取自 enum？
8. 是否更新了工具 snapshot？
9. 破坏性变更是否写了 ADR？
