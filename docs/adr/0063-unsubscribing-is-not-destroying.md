# 0063 · 退订不是销毁：停用策略与销毁内容分开

- 状态：accepted
- 日期：2026-10-07
- issue：#373（本 ADR 是 #373 的决策记录；场景号段 AC-216..217 见 `SCENARIOS.md` 的 #373 行）
- 修订：**部分 superseded [ADR 0021](0021-deletion-is-deletion.md)** —— 「`DELETE /v1/sources/{id}` 即物理删除」的部分；
  该 ADR 关于「条目与订阅的物理删除」的其余结论（删除就是删除、没有墓碑、正文按引用计数回收）不变
- 相关：[ADR 0046](0046-work-as-a-first-class-citizen.md) D-D（作业化拆分）、
  [ADR 0061](0061-chain-ownership-and-per-subscription-visibility.md)（按 `(item, subscription)` 判定可见性）、
  [ADR 0024](0024-reader-api.md)（Reader API 的 `ac=unsubscribe`）、
  [ADR 0010](0010-mcp-tools-via-core-api.md)（MCP 写工具经 `/v1`）
- 实现：`src/adapters/repository/deletion.py`、`src/app/subscriptions_routes.py`、
  `src/app/origins_routes.py`、`src/app/sources_routes.py`、`src/app/reader/operations.py`、
  `src/app/middleware.py`、`src/app/mcp/tools/writes.py`

**本 ADR 不占 AC**（决策记录，先例 #172 与 ADR 0046）：场景由 #373 承担。

## 背景

同一个 `DELETE` 之前只有一种含义：**真删**（ADR 0021）。它同时服务两种完全不同的意图：

- 「我不想再读这个源了」（**退订**）——一个随时可能反悔的动作；
- 「这个源是错的/被污染的，连同它抓来的东西一起清掉」（**销毁**）——不可逆。

实测的代价（2026-10-06）：取消一个订阅时一次 `DELETE` 连带删掉 **12 条条目与 24 个正文文件**，
不可逆，且**没有任何地方记录为什么**。Reader 的 `ac=unsubscribe` 走的是同一个入口，
所以客户端侧栏里点一下「退订」，得到的是销毁。

ADR 0061 之后这两件事在模型里第一次可以分开：**策略在 `subscription`**（链、folder、schedule、status），
**事实在 `origin`**（条目、正文、抓取记录），**可见性是 `(item, subscription)` 的产物**
（`item_subscription_ready`，外键 CASCADE 挂在 subscription 上）。所以「停止采集」与「销毁内容」
落在两张不同的表上，不需要再共用一个动词。

## 决定

1. **`DELETE /v1/subscriptions/{id}:unsubscribe`** —— 退订。无 `reason`、幂等、可重放；
   只把订阅置为 `disabled`，**不删任何行、不动任何正文文件**。
2. **`DELETE /v1/origins/{id}:purge`** —— 销毁。`reason` **必填**（缺失或全空白 → `422 reason_required`，
   且拒绝发生在任何写入之前）；审计行**带上理由**；这是**唯一**删除内容的入口。
3. **旧 `DELETE /v1/sources/{id}` 保留 URL、改变语义**：它现在就是退订（回执字段随之从
   `sources`/`items` 变为 `subscriptions`），并标 `deprecated`。让旧 URL 继续真删会更危险——
   调用方以为自己在退订，实际在销毁。
4. **Reader `ac=unsubscribe` 同义**（同一个 repository 入口），因此客户端的「退订」不再销毁内容。
5. **退订后频道从所有读视图消失**（Reader 侧栏、folder 列表、RSS 输出、频道解析），
   **重新订阅同一来源会复活同一行**（`(origin, folder)` 派生 id 不变），于是**同一批 item id 再次可见**
   （AC-216）。条目本身在退订期间仍然可按 id 取用：内容没有被销毁，只是没有频道在读它。

## 为什么是「停用」而不是「删掉订阅行」

删订阅行看起来更干净，但 `item_subscription_ready` 是**通过 CASCADE 挂在 subscription 上的**：
删行会连带删掉「这条条目在这个频道已就绪」的记录，重新订阅时就无法「再次可见」，
除非在读时重新派生就绪——那等于把 ADR 0061 已经否决过的「读时派生」重新引入（第二个事实源）。
停用保留了行、就绪与 paging cursor，代价只是每个读视图都要排除 `disabled`（已在
`resolve_channel` / `channel_for_subscription` / Reader 的订阅与 folder 列表实现）。

## 被否决的备选

- **退订即删订阅行**：见上。丢掉就绪 → AC-216 的「再次可见」失败；恢复它就要读时派生。
- **`DELETE` 带可选 `reason`**：不填时退化为静默销毁，正是本 ADR 要消除的形态。
  理由必须是**通过条件**而不是**可选注解**。
- **内容软删除标记**（给 item 加 `deleted_at`）：引入第三种「条目是否存在」，
  与 ADR 0021 的「删除就是删除」和 ADR 0061 的闸门语义都要重新定义，且让每个读点多一个过滤条件。
- **只加新端点、旧 `DELETE` 保持真删**：语义漂移更危险——同一意图在两个 URL 上后果不同，
  而旧 URL 恰恰是老客户端在用的那个。

## 影响

- **破坏性变更**：旧 `DELETE /v1/sources/{id}` 的语义与回执字段变了（`sources`/`items`/`files_removed`
  → `subscriptions`）。文档、OpenAPI 快照、MCP 工具描述同步更新。
- **MCP 工具面**：`remove_subscription` 不再标 `destructive`（它什么都不删），
  新增 `purge_origin`（`destructive`，`reason` 必填）。工具面仍在 18000 字符预算内。
- **审计**：请求可以携带「为什么」，中间件把它写进审计行的 `args`（`audit_args`）。
  这是审计行第一次承载理由；理由不是凭据，也不是不可信正文，因此不进敏感字段黑名单。
- **`publish_status` 不受影响**：它继续只表达内容状态（pending/ready/published），
  可见性由 ADR 0061 的物化就绪决定；退订不碰它。
