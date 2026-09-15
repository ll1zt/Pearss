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
2. **列表必须分页 + 截断**。默认 `limit=50`，上限 100，与 `/v1` 一致
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
- 每个工具必须声明 `ToolAnnotations`，且与描述里的副作用陈述一致：读工具
  `readOnlyHint=true`、`openWorldHint=false`；删除类 `destructiveHint=true`；
  发起外部请求或消耗 provider 预算的写 `openWorldHint=true`。写工具一律
  `idempotentHint=false`——它们不接受调用方提供的稳定幂等键（每次调用新生成），
  重试即新意图。注解是机器可读的副作用声明：host 依据 `destructiveHint` 决定
  是否追加自己的确认，所以它与描述同属契约（进 snapshot）
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

`list_sources`、`search_items`、`get_system_snapshot`、`list_runs` 是只读，不需要确认。

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
2. **绝不返回** token、Cookie、Authorization header、私有 feed URL（含 query token）、
   用户个人数据、原始错误正文、内部堆栈
3. **日志与 AuditLog 同样脱敏**，工具参数进入审计前经 `adapters/audit.py` 过滤
4. **不暴露动态转发入口**。§8 明确「分类挂载 + 按需过滤，拒绝动态转发入口」以保留
   schema 校验：不做"给我一个 URL 我帮你调任意端点"这类工具
5. 工具参数必须有 schema 校验，非法值拒绝而不是宽容接受

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

## 七、订阅归类（folder）

`Source` 带一个 `folder`：订阅在客户端侧边栏中的分组。**一订阅一分类**，
跟随 FreshRSS（其源码明确 "a feed can only be in one category"，客户端也按该模型实现；
多对多会在这些客户端里表现为不可预期）。默认 `default`。

三条路径共享它，且都以 `user/-/label/<name>` 的形式暴露给 Reader API：

| 路径 | 用法 |
|---|---|
| `POST /v1/sources` | 创建时带 `folder` 即可归入 |
| `PATCH /v1/sources/{id}` | `{"folder": "阅读"}` 改分类；`{"folder": null}` 回 `default` |
| `POST /reader/api/0/subscription/edit` | 客户端拖拽：`ac=edit`、`s=<feed id>`、`a=user/-/label/名字` 加入、`r=...` 回默认。**不支持 subscribe/unsubscribe** —— 增删订阅走 `/v1`，有各自的鉴权与审计 |

名称校验在 `core/folders`：trim、长度上限、禁控制字符、**禁 `/`**（名字会变成 stream id，
斜杠会产生客户端无法往返的路径）。MCP 侧对应 `set_source_folder` 与 `add_source` 的
`folder` 参数。

## 八、传输与挂载

- 传输：**Streamable HTTP**（§9），与 Core API 同进程挂载，端点 `/mcp`

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

## 九、验收清单（改工具面时逐条过）

1. 工具是否经 `/v1` 而非直调仓储？（审计覆盖）
2. 返回值是否可能含正文、HTML、token、私有 URL？
3. 列表是否分页、字符串是否截断、默认字段是否最小？
4. 写操作是否走 elicitation，且确认参数 == 执行参数？
5. 写操作是否返回 `audit_id`，且该 id 能在 `audit_log` 中查到对应行？
6. 工具是否声明了与语义相符的 `ToolAnnotations`？
7. 错误是否用统一结构，`error_class` 是否取自 enum？
8. 是否更新了工具 snapshot？
9. 破坏性变更是否写了 ADR？
