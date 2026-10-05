# ADR 0058：写工具不 elicit——退役确认机制，用 token、描述与 annotations、幂等、审计替代

- 状态：accepted
- 日期：2026-10-05
- issue：#270（本 ADR 是 #270 的交付物）
- 相关 / 修订：**整体 supersede** [ADR 0053](0053-write-confirmation-shape.md)（写确认的形状：一次确认 + 可选单字段编辑）——`Confirmation`、四段 `Resolve` resolver、每个写工具的 `_<tool>_fields()` 单源、可编辑集与 `declined`/`aborted` 两种拒绝拼写一并删除。**部分修订** [ADR 0010](0010-*.md)：写工具仍经 `/v1`，但「确认在 server 侧」一句不再成立（server 侧没有确认）。ADR 0036（工具面是 `/v1` 的语义投影）不受影响。取代 `docs/api/mcp-tool-guide.md` 第三节旧版「每个写工具 elicited 一次」的呈现规则；该文第五节「elicitation 不是安全边界」保留并升格为本 ADR 的依据。

**场景**：AC-262..266（见 `SCENARIOS.md`，归属 #270）。退役 AC-75、AC-238..240、AC-241..242、AC-243..245、AC-246..248。

## 背景

写操作必须经 elicitation 由用户确认，这条规则写在 AGENTS.md 与工具指南里，实现是 `app/mcp/confirmation.py`（1992 行）。

2026-10-05 的实际使用把它推翻了：用户在**支持 MCP 的 agent harness**（codemode 通道）里要求「抓页面 → 以 agent 入库 → 启动翻译管线」，结果是

- 该通道下 **13 个读工具全部可用**；
- **16 个写工具全部不可用**：每个都回 JSON-RPC `-32021 MissingRequiredClientCapability`，因为客户端未声明 elicitation 能力；
- 唯一出路是绕开工具面直接打 `/v1`（同一个 Bearer token + `Idempotency-Key`）——本次 3 条入库、1 次改链、1 次恢复链全部是手工 `curl` 完成的。

而本仓库自己早就写下了这条规则的真实性质（工具指南第五节）：

> **elicitation 不是安全边界**。它要求*配合的*客户端向用户展示确认；想写入的客户端自己回答「确认」即可。它约束的是误操作，不是对抗 —— 真正的边界是这个 token

即：**收益（防误操作）只在「客户端愿意配合」时存在，代价（完全不可用）却落在所有不配合/无能力的客户端上。** 而 MCP elicitation 是可选、较新的能力，今天的多数客户端与框架都没有实现。

后果还包括**发现与可用不一致**：agent 客户端能从 `list_tools` 里看到 16 个写工具（`read_only_hint=false`），却一个都调不动；失败发生在**协议层**（`-32021`），而不是「要怎样才能写」的指引层。对一个把「agent 通过 MCP 操作控制平面」写进 `architecture-spec.md` 的项目，这是主路径不可用。

## 决策

**16 个写工具不再 elicit，直接执行**（issue #270 的选项 B，用户 2026-10-05 选定）。

1. **工具面**：不再注入 `confirmed` 参数，不再发 `elicitation/create`；每次调用直接构造请求体、经 `/v1` 发出。返回体**保留** `audit` 与 `correlation_id`（`audit_id` 仍随响应返回）。
2. **机制退役**：`Confirmation` 模型、四段 `Resolve` resolver、`_matches` / `_render` / `_comma_join`、每个写工具的 `_<tool>_fields()` 单源、`_editable` 可编辑集、以及 `declined` / `aborted` 两种拒绝拼写全部删除；`app/mcp/confirmation.py` 移除，两个仍在用的纯函数（`_read_state_change`、`ingest_targets`）移入 `tools/writes.py`。统一的写路径保留为 `_write()`（执行 + 错误映射 + 归属 + 截断）。
3. **判据退役或改写**：AC-75（确认覆盖执行参数）、AC-238..240（单轮确认形状）、AC-241..242（可编辑集）、AC-243..245（修改路径）、AC-246..248（结构化可编辑字段）退役；`SCENARIOS.md` 相应行改为「已明确不做」；AC-57（描述 ⇔ annotations 机械对账）**保留并强化**——它现在是工具面唯一的结构性副作用声明。
4. **替代面**（失去确认后剩下的四层，写进 AGENTS.md 与工具指南第三节）：
   - **token 权限**：`PEARSS_MCP_TOKEN`，真正的边界；
   - **描述与 annotations**：破坏性工具（`remove_source` / `delete_items` / `delete_source_items`）在描述里明写不可逆后果，且 `destructiveHint=true`；出网/消耗预算的工具 `openWorldHint=true`；
   - **幂等**：写仍带 `Idempotency-Key`（每次新生成），重放不重复生效；
   - **可审计**：每次写都有 Core API 审计行与 correlation id，事后可追溯。

## 理由

- **收益与代价不对称**。确认买到的只是「配合的客户端上少一次误操作」，付出的却是「无该能力的客户端一个写工具都调不了」。后者已经付过一次代价，而且是主路径。
- **它不承担安全职责**（指南第五节，本仓库自己的判断）。既然真正的边界是 token，删除它不会削弱边界；如果它承担了安全职责，那正确的做法是修边界而不是保留一个可选能力的硬依赖。
- **误操作仍被接住三层**：身份/目标不可改（想改就重新发起请求——这条在 #251 里已经确立，与确认无关）；破坏性工具在描述与 `destructiveHint` 里明说；每次写都可追溯。
- **被否决的备选**（记录当时的权衡，详见 issue #270）：
  - **选项 A：保留现状**。理由：确认是产品语义；代价：主路径对 agent 不可用，与项目定位冲突。
  - **选项 C：能力自适应**（有 elicitation 走现确认，没有则在入参上要求 `ack: <摘要指纹>`）。安全属性与现状同级，仍是折中；把确认的两种承载拼进同一份工具面，形状复杂度上升，而收益本来就只在配合的客户端上成立。
  - **选项 D：只修可达性**（工具描述标注能力要求 + `-32021` 指引指向 `/v1`）。等于承认工具面是摆设。

## 后果

- **16 个写工具对所有客户端可用**，包括没有 elicitation 能力的通道；「发现得到就调得动」重新成立。
- 失去的那一层：对话框曾是「执行前最后一次手工核对」。接受它的是：默认路径的执行值本来就只来自服务端派生（用户没有落脚点），所以失去的是一次**确认动作**，不是一次**可控性**。
- 工具面契约变化：16 个工具的 `inputSchema` 去掉 `confirmed`，描述去掉「需要确认」措辞，`tests/contract/mcp_tools.json` 随之更新；快照 diff 即契约 diff。
- 一批测试随之退役：`tests/unit/test_write_guard.py`、`tests/unit/test_submit_items_fingerprint.py`、`tests/integration/test_submit_items_confirmation.py` 删除；`test_mcp_server.py` 的拒绝/篡改/编辑路径用例删除；`tests/e2e/test_failure_paths.py` 的两条「未确认的写不落库」改为其它失败路径仍有覆盖。
- **写仍然有 actor 与审计归属**：路径未变（工具仍经 `/v1`），actor 仍记 `api:full`，correlation id 仍随响应返回。

## 可逆性

可逆但不便宜：恢复需要重建确认模型、resolver 与逐工具字段派生（正是本 ADR 删掉的那些）。重做触发条件见 #270——例如未来某个高风险写工具（跨源批量销毁之类）的误操作代价被证明高到 token + 描述 + 幂等 + 审计都兜不住，那时**只为那一类**重新引入确认，而不是全量恢复。
