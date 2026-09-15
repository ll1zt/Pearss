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
- `architecture-spec.md:17,221` 已把这件事登记为 **A3「主动数据接入」**，
  形态「外部 agent 持 token 幂等推送」，优先级 P3，标注未实施。
- **但 `POST /internal/items` 已经是一个内容写入端点**（`main.py:395`），
  且形态与本文的决策几乎相同：接受 `ItemDraft`、调用 `normalise_and_store()`。
  它是 `@app.post` 直接注册的，不在任何 `@router` 上，也不在任何文档里 ——
  第一轮勘察以 `@router` 为口径，因此漏掉了它。它的来历与处置见下节。

### 已存在的临时端点：调查结论

`git log -S` 定位到引入提交 `8911f8a`（2026-09-03，项目第一个 app 提交），
其原始 docstring 直接说明了意图：

> Internal and unauthenticated for now: authentication and the audit
> middleware are part of the P1 control-plane work, and **this endpoint
> exists so the pipeline can be exercised end to end before then.**

即：它是 **P1 的端到端验证脚手架**，不是 A3 的实现。但它从未被拆除，
而是在 `0b12e4f`（保护 Core API）里被**顺带加固**：加上
`Depends(write_required)`，并把上面那句临时性说明改写成「Write-scoped:
this mutates the corpus...」。**临时性的自我标注就此消失**，脚手架看起来
成了正式端点。`grep` 全部文档（`docs/`、`architecture-spec.md`、`AGENTS.md`、
`README.md`）在本次调查前对 `internal/items` 零命中，A3 也始终标注未实施 ——
它从未作为需求交付物存在过。

它现在的实际角色是**测试夹具**：`tests/integration/test_app.py` 有 15 处调用，
用于验证审计、body 上限、幂等、scope 这些与它本身无关的行为。

三个已确认的缺陷（均与「脚手架未被回收」这一根因一致）：

1. **不在 `EarlyAuthMiddleware` 的保护范围内**（`hardening.py:133` 的
   `_PROTECTED_PREFIXES` 只有 `("/v1",)`）。鉴权仍然有效（路由上有
   `write_required`），但发生在 FastAPI 读完并校验 body 之后 —— 正是该中间件
   存在的理由（其 docstring：否则未认证调用者可免费探测每个写端点的校验行为）。
2. **`source_id` 由请求体提供**（`ItemDraft.source_id`，FastAPI 直接绑定这个
   dataclass）。**持有写 token 的调用者可以往任意已存在的源注入条目**，
   包括 rss/twitter 源。这与本文「source_id 强制取自路径」的越权防线直接冲突。
3. **出现在公开 OpenAPI 契约里**（`tests/contract/openapi.json:1150`，
   `operationId: ingest_draft_internal_items_post`）。按 ADR 0010，spec 是
   MCP 工具面的生成来源，所以这个「内部」端点事实上是发布契约的一部分。

## 决策

**新增 `POST /v1/sources/{source_id}:ingest`；它只接受内容字段，
构造 `ItemDraft` 后调用 `app.pipeline.normalise_and_store()`。
随后删除 `POST /internal/items`。**

agent 被建模为**一个没有 `fetch` 钩子的采集器**：它不抓取，它推送，但产出物
是同一个 `ItemDraft`，汇入同一条管线。这满足 `architecture-spec.md:189` 的收敛
规则（一切 ingest 路径在 `ItemDraft → normalize → Item` 汇合）。

**关于已存在的 `/internal/items`：不并存，而是升格后回收。** 两个都接受
`ItemDraft` 的写端点是比现状更坏的结局（两套鉴权语义、两处越权面、契约里两个
近义操作）。因此把脚手架的能力搬进 `/v1` 的正确形态并加固，然后删除它。
`tests/integration/test_app.py` 里那 15 处调用改为走新端点 —— 它们断言的是审计、
body 上限、幂等、scope，与端点路径无关，迁移不影响断言的语义。

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

**请求体不是 `ItemDraft` 本身**，而是一个只含内容字段的独立 schema
（`IngestItem`）：`external_id`、`title`、`canonical_url`、`published_at`、
`content`、`metadata`、`derivation_note`。`ItemDraft` 在服务端构造，
`source_id` 由路径注入。

**这一点必须如此，不能图省事直接绑定 `ItemDraft`**：`ItemDraft` 含
`source_id`，而 `/internal/items` 正是直接绑定它，于是 `source_id` 成了
请求方可控字段（见上文缺陷 2）。「agent 只能提供内容是什么，不能提供它属于谁」
是本决策的核心越权防线，用一个独立 schema 是让这条防线**在类型层面无法被绕过**，
而不是靠运行时检查 —— 后者会在某次重构里被忘记。

`publish_status`、`content_ref`、`guid` 从来不在 `ItemDraft` 上，故不受影响；
本节列它们是为了记录「这些字段永不可由请求方提供」这条不变量。

### 响应语义（调用方须知）

响应返回 `submitted` 与 `inserted` 两个计数，以及逐条的 `results[]`，每条含
`id`、`external_id`、`action`、`reason`。**调用方应按 `action` 分支，不要按
计数判断**：HTTP 201 只表示请求被受理，不等于每一条都入库。

两个会让 `inserted` 小于 `submitted` 的原因，都是**正常的去重结果**而非错误：

- **`skip_same_source`** —— 同源已有相同内容的条目。身份是
  `sha256(tenant, source_id, external_id)`，所以**换个 `external_id` 但内容相同
  （例如两条都只有标题、正文为空）仍会被判为同一条**：`decide_dedupe` 在身份
  不匹配后按 `content_hash` 匹配，而空内容共享同一个 hash。agent 若把「空条目」
  当作彼此的区分依据，会看到 201 而实际只入库一条。
- **`suppress`** —— 相同内容已从**其他源**发布过。这是跨源去重，防止同一篇文章
  以两个 GUID 重复出现在阅读器里。

`Idempotency-Key` 必须对应**一个逻辑操作**，重试同一操作时复用、换操作时更换。
复用同一 key 发送不同 body 会**返回首次的响应并丢弃新内容**（该仓库所有写端点
的既有语义，响应带 `Idempotent-Replay: true` 头以示区别）。automated agent 尤其
要注意：这个失败模式不会报错。

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
