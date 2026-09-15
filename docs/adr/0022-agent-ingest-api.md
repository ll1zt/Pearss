# 0022 · A3 主动数据接入：agent 提交内容走采集器同一条管线

- 状态：accepted
- 日期：2026-09-14
- 实现：`POST /v1/sources/{source_id}:ingest`、`SourceType.AGENT`（已存在）、
  `app.pipeline.normalise_and_store`（复用）、`app/scheduler.py`（跳过 agent 源）
- 修订：`architecture-spec.md`（A3 状态 P3 → 实施中）、`docs/api/style-guide.md`、
  `docs/guides/collector-guide.md`、`docs/domain/data-model.md`
- 关联：ADR 0007（存储边界净化）、ADR 0010（MCP 必须经 `/v1`）、
  ADR 0012（登录型采集器的信任边界）、ADR 0016（公网暴露与发布闸门）

## 背景

需求：**另一个通用 agent 抓了网页/推文之后，想把内容存进 Pearss。**

现状（读代码得到，非推断）：

- `SourceType.AGENT` 已是一等枚举成员（`core/enums.py:137`），在 DB CHECK
  约束里（`db_models.py:151`），`source_canonical_key()` 有专属 `agent://<name>`
  分支（`normalize.py:159-177`），`SourceCreate._valid_url` 的注释明确写了
  agent 源合法携带合成 URL（`sources_routes.py:305-316`）。**接缝是设计过的。**
- 但**没有任何内容写入端点**。`grep` 全部 `/v1` 写端点：只有删除
  （ADR 0021）与配置/消费状态。`items_routes.py` 连 `body` 都排除在可读字段外
  （`DEFAULT_ITEMS_FIELDS` 注释：`body` is deliberately absent）。
- 唯一会原地改写已存在行的是 `_update_pending()`，仅对 `pending` 行生效；
  注释写明「the gate is what makes an in-place content change permissible at all」。
- `architecture-spec.md:17,221` 已把这件事登记为 **A3「主动数据接入」**，
  形态「外部 agent 持 token 幂等推送」，优先级 P3。

## 决策

**新增 `POST /v1/sources/{source_id}:ingest`；它只接受 `ItemDraft` 字段，
构造草稿后调用 `app.pipeline.normalise_and_store()`。**

agent 被建模为**一个没有 `fetch` 钩子的采集器**：它不抓取，它推送，但产出物
是同一个 `ItemDraft`，汇入同一条管线。这满足 `architecture-spec.md:189` 的收敛
规则（一切 ingest 路径在 `ItemDraft → normalize → Item` 汇合）。

### 为什么不新开一条写入路径

四条既有不变量决定了这个选择，任何一条被破坏都是实质降级：

1. **净化只发生在存储边界一次**（`pipeline.py:136-140` 是全文唯一的
   `sanitize_html()` 调用点）。注释给的理由是：净化在这里做，所有下游读者
   （RSS / Reader API / 未来任何输出面）因此不必记得净化——而「每个渲染点
   各自净化」正是会漏的失败模式。若新端点自己写正文，就是把这个模式复制第二份。
2. **内容只能从采集链路进来**，所以「已净化」是可信前提。
3. **身份是确定性的**：`guid`/`item_id` = `sha256("id1", tenant, source_id,
   external_id)[:32]`。这让幂等免费，且不依赖 agent 正确使用幂等键。
4. **发布闸门由 `is_visible()` 把守**，pending 条目在 `/v1` 与 Reader API 都
   返回 404（刻意的：不泄漏闸门存在）。

走 `normalise_and_store()` 一次调用即白送这四样，且与采集器**完全同源**。

### 端点契约

```json
POST /v1/sources/{source_id}:ingest
{
  "items": [
    {
      "external_id": "simonwillison-2026-09-14-scaling",
      "title": "…",
      "canonical_url": "https://simonwillison.net/2026/Sep/14/…",
      "published_at": "2026-09-14T10:00:00Z",
      "content": "<p>…正文 HTML…</p>",
      "metadata": { "author": "Simon Willison", "submitted_by": "claude-desktop" }
    }
  ]
}
```

**请求体不得包含**：`source_id`、`publish_status`、`content_ref`、`guid`。
这些全部由管线推导。agent 只能提供「内容是什么」，不能提供「它处于什么状态」
或「它属于谁」——`source_id` 强制取自路径，否则 agent 能往任意源注入内容，
破坏来源归属。这是本决策的核心越权防线。

## 三个已拍板的默认（用户 2026-09-14 决定）

**1. agent 提交的内容默认可见（不滞留）。** 初稿建议默认 `PENDING` 作为
「人在环上」的额外闸门，用户否决：「Agents 提交的内容默认可见就行了」。
因此 ingest 不带 `pipeline_chain` 时条目立即 `promote_to_ready()`，
与无链的 rss 源行为一致。**代价须记明**：agent 的转述/改写不经用户确认即
进入可见流，其错误（幻觉、错摘要）会像真实内容一样被客户端抓到。用户接受了
这个代价，换取的是「agent 写进去就能在 NNW 里看到」的即时性。

**2. `canonical_url` 不必是 http(s)。** 这需要松动 `canonicalize_url()`
（`normalize.py:107-110` 目前对非 http/https 抛 `ValueError`）。
**这是本决策里唯一需要改既有代码语义的地方，也是唯一的新风险面**，故单列一节。

**3. 不按源限速和限额。** 用户：「暂时不用按源限速和限额」。
既有闸门仍然生效且**必须显式确认接入**：`BodySizeLimitMiddleware`
（`PEARSS_MAX_BODY_BYTES`，默认 1 MiB）、`sanitize_html` 自身的
`MAX_INPUT_CHARS = 2_000_000` 上限。**仍需新增单次提交条数上限**
（建议 100，与 `/v1/items:delete` 的 `max_length=500` 同量级）——这不是
「按源限速」，是请求体解析的基本卫生：没有它，1 MiB 的 body 可装数千条
只有标题的条目，打爆去重表与富化队列。**限速的替代**：不做硬限流，但把
ingest 计数落审计，超阈值时写告警（从既有 AuditLog 推导，不新造机制）。

## 非 http(s) URL 的处理（决策 2 的落地）

`normalize()` 无条件调用 `canonicalize_url(draft.canonical_url)`
（`normalize.py:338`），非 http(s) 会抛异常并**使整次提交失败**（不是单条跳过）。

选择：**`canonicalize_url` 保持现状不动**（它服务于身份与去重，`canonical_url`
是条目身份的一部分，「可点击的出处」这个语义不能稀释）；**ingest 路由在构造
`ItemDraft` 之前，对非 http(s) 的 URL 做归一化替换**——沿用 agent 源已有的
合成键思路：无出处 URL 的提交记为该源的 `agent://<name>` 键下的合成 URL，
原始 URL 若存在则存入 `metadata`。

拒绝的方案：放宽 `canonicalize_url`。理由：它会同时放宽 rss/web 采集器
（单个函数服务三种源类型），为了 agent 的便利把 RSS 的身份语义一起松掉，
是把局部需求扩散成全系统影响。**约束留在 ingest 路由内，改动不外溢。**

## 撤销/回滚

ingest 是纯增量端点，回滚 = 移除路由 + 调度器恢复排程 agent 源。已入库
条目**不回滚**（它们是合法 Item 行，与采集来的条目无结构差异）——这正是
「同一条管线」带来的好处：没有需要单独清理的 agent 专属状态。

## 后果

- **正向**：agent 内容自动获得净化、CAS 存储、六种去重裁决、发布闸门、
  审计、幂等，零重复实现；`architecture-spec` 的 A3 从「规划」变「实施中」。
- **代价**：新增一个写入面 = 新增一个认证面与滥用面。缓解：独立 token
  （见下）、`source_id` 不可指定、条数上限、审计告警。
- **明知的缺口**：agent 提交内容与采集内容在库里**无法区分**，除
  `source.type = 'agent'` 这条线索外。若将来需要「只删 agent 塞进来的东西」
  或按来源审计，需在此 ADR 上追加 revision，不在实现里 ad hoc 加字段。

## 鉴权（实现要求）

独立凭据 `PEARSS_INGEST_TOKEN`，**与 `PEARSS_API_TOKEN`、Reader API 凭据
完全隔离**（`architecture-spec.md` A3 明写「独立 token」）。理由：该通道
凭据泄漏的损失面应为「能往 agent 源推内容」，而非「能删源/改配置/读已读状态」。
比较用 `hmac.compare_digest`（全库既有纪律）。审计 actor 记 token 对应名字。
**该端点必须纳入既有的 `AuthFailureRateLimitMiddleware`**——否则它是一个
不限速的写入口。
