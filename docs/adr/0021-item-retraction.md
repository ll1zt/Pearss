# 0021 · 条目撤回：可逆的软删除，不是数据抹除

- 状态：proposed
- 日期：2026-09-14
- 修订：`docs/domain/data-model.md`（Item 不变量的例外与新增字段）、
  `docs/domain/state-machines.md`（Item 状态机新增 `retracted`）、
  `docs/api/mcp-tool-guide.md`（撤回类写工具）、`docs/operations/runbook.md`
- 关联：ADR 0008（canonical_url NOT NULL）、ADR 0010（MCP 必须经 `/v1`）、
  ADR 0013（显式正文获取）、ADR 0018（处理器链）

## 背景

用户要求：agent 能通过 MCP 清理**无用或过时的条目内容**，并能在开发阶段
清掉**错误抓取**（mis-fetch）的条目。理由成立——客户端缓存的时效是有限的，
而过去清理错误内容的唯一手段是直接改数据库（本地 X 源就是这么处理的：
手删 item 行加它的 CAS 正文文件）。

现状约束（读代码得到，不是假设）：

1. **没有任何条目删除能力**。`items_routes.py` 零写端点；
   `reader_routes.py` 只有 ClientLogin、stream/contents、edit-tag、
   subscription/edit、mark-all-as-read；MCP 17 个工具里只有 `remove_source`，
   且它是**软删除**（源转 `deleted`，条目全部留published）。
2. **`data-model.md` 明文禁止**：「停用/删除 Source 不得删除已有 Item」；
   `state-machines.md`：「`deleted` Source 的历史 Item 默认保留，避免破坏
   已发布 RSS GUID 和 Reader API 历史；物理清理属于独立维护流程」。
3. **`netnewswire.md` 禁止复用已发布 GUID**：「不得删除已发布 GUID 后复用」。
4. **外键已是 CASCADE**：`read_state.item_id` 与 `item_enrichment.item_id`
   都 `ondelete="CASCADE"`，`item.source_id → source.id` 也是。硬删一行会
   自动带走已读状态与全部处理器产物。
5. **正文是内容寻址的共享文件**：`filestore.store_body` 按 sha256 落盘，
   两篇正文相同的条目**共享同一个文件**。删文件不能按 item 判断。
6. **可见性由两道闸把守**：`query_items`（`/v1/items`）与
   `visible_ids_for_stream`（Reader）都在 SQL 里排除 `publish_status='pending'`。
   任何新状态只要同样被排除，就自动对所有输出面不可见。
7. **`item.id` 就是 Reader 外部 id 的来源**：`resolve_reader_ids` 用它的
   hex 前缀反查。删行不会「释放」外部 id，但会让客户端手里的 id 变成悬空引用
   （现有代码已经预见到了，edit-tag 里写着「a client may still hold ids for
   items that were withdrawn」）。

## 问题

「删除」在本项目里是三件不同的事，混为一谈会破坏已发布内容的不可变保证：

1. **撤回**（retract）——我抓错了，或者内容已无价值，不要让它再出现在任何
   输出面。这是用户实际要的。
2. **物理抹除**（purge）——连同正文与产物一起从磁盘上消失。用于合规或磁盘
   回收，不是日常操作。
3. **源下线**（already exists）——`remove_source`。

用户要的是 1，偶尔需要 2 来真正回收磁盘。

## 决定

### 1. 新增 Item 状态 `retracted`，复用现有的门

`publish_status` 增加第四个取值 `retracted`。它与 `pending` 共享同一条
输出规则：**对一切输出面不可见**——RSS 输出、`/v1/items`、Reader 的
`stream/items/ids`、`stream/items/contents`、`item://` Resource。

实现上这是两个 `!=` 改成一个 `NOT IN`（或在两处各加一个条件），而不是新增
一套过滤逻辑。这是这个设计最重要的一点：**撤回的可逆性与低风险来自它复用了
已经过测试的门**，而不是新写一条并行的可见性路径。

**`is_visible()` 必须从黑名单改成白名单**。`core/item_state.py` 现有的实现是
`status is not PublishStatus.PENDING`——这是黑名单，加入 `retracted` 后它会把
撤回条目判为**可见**，也就是新状态默认**失败开放**。改成
`status in {READY, PUBLISHED}` 后，任何未来新增的状态都默认不可见，与 Reader
代码里「未识别的 state stream 返回空而不是全部」是同一条原则：给不出正确答案时
宁可不出答案，不要给一个看起来像数据的错答案。

不变量：

- `retracted` 条目**保留 guid**。不释放、不复用、不回收——`uq_item_guid`
  照样约束它，采集时重遇同一 external_id 也照样命中它（身份优先于内容）。
- `retracted` 条目的 `title`/`url`/`content_hash` 不改。
- 撤回**可撤销**：`unretract` 把 `retracted` 打回它撤回前的状态
  （记在新列 `status_before_retract` 里）。这就是「开发阶段调试」场景：
  误撤回一条，撤回来即可，不需要重新采集。

### 2. 不删行、不删正文（默认）

`retract_items` 改状态，**不动磁盘**。理由：

- 正文文件是内容寻址且**跨条目共享**的（约束 5）。按 item 删文件会误删另一个
  活着的条目正在用的正文，而 `read_body` 对缺失文件只返回空串加一行 warning——
  也就是说这个 bug 会静默地把一篇活文章变成空正文。
- `read_state` / `item_enrichment` 是 CASCADE 的，删行即丢用户已读/星标与全部
  译文。撤回一个抓错的知乎条目不该顺带丢掉同内容另一条目的状态——虽然按行隔离
  它们本不共享，但删行让「谁读过什么」永久不可查。
- 撤回是 agent 会高频做的事（清理、调试），它必须是**廉价且可逆**的。
  物理抹除是罕见的运维动作，应该是**另一个工具**，而且要求更强的确认。

因此分成两个工具，破坏半径不同、确认强度不同（见下）。

### 3. 物理抹除是独立工具，且要算引用


`purge_items` 硬删行，但在删正文前必须**统计引用**：一个 CAS 摘要可能被
多个 item 行引用（可含 retracted 行）。只有当某摘要**不再被任何存活 item 行
引用**时才删文件。否则：

- 只删行不删文件 = 磁盘不回收，但正确；
- 删行又删文件而不查引用 = 静默毁掉别的条目的正文。

`RawFetch.payload_ref` 同理，但它属于抓取诊断数据、有独立保留期，本 ADR 不
涉及。

正文文件的删除**失败不阻断行删除**：文件删不掉是磁盘问题，不是领域问题，
行已经删了就是删了，剩一个孤儿文件由 `pearss-gc`（见后果）或下次抹除收拾。
反过来（先删文件后删行中途失败）会留下指向缺失正文的活条目，那才是坏的。

### 4. 按源批量撤回

用户的真实场景是「这个源这次抓错了，清掉这批」，逐个 item_id 调用不现实。
因此提供 `retract_source_items(source_id, since=None, until=None, published_only=...)`：
按源（可选时间窗口）批量撤回，返回撤回条数与 audit id。

**默认不动已发布条目**。`published` 意味着客户端已经拿到并缓存了它；
撤回当然可以（用户的缓存会过期，这正是用户的论据），但让「批量撤回一个源」
默认把客户端里已读的历史全部抽走，是破坏性的默认值。所以：

- 单条 `retract_items`：不限状态，但 elicitation 里显示每个将被撤回条目的
  标题与当前状态，让用户看见自己撤了什么；
- 批量 `retract_source_items`：**默认只撤回 `ready`**（尚未对外输出过的）。
  要连 `published` 一起撤，必须显式传 `include_published=true`，且确认文本
  里写明「包含 N 条已发布」。

### 5. 不改 `remove_source`

用户的原始问题里含「删除订阅」——那已经有 `remove_source` 了，且它已是软删除。
本 ADR 不把它改成级联抹除，`data-model.md` 的约束 2 依然成立：**源下线不删
条目**。要清掉一个源的内容，是两个动作的组合：
`remove_source`（下线）+ `retract_source_items` 或 `purge_items`（清内容），
各自显式。这个组合比一个「删源并删内容」的工具更安全，因为它把两个不同性质
的决定分开让人确认。

## 工具面（MCP）

四个新工具，全部经 `/v1`（ADR 0010），全部要 elicitation 确认，全部写 AuditLog。

| 工具 | 破坏半径 | 确认要求 |
|---|---|---|
| `list_retracted_items(source_id?, limit?)` | 只读 | 无 |
| `retract_items(item_ids)` | 改状态，可逆 | 列出每个条目标题 + 原状态 |
| `unretract_items(item_ids)` | 改回，可逆 | 列出标题 + 将恢复成什么 |
| `retract_source_items(source_id, since?, until?, include_published?)` | 批量，可逆 | 源标签 + 将撤回条数 + 是否含已发布 |
| `purge_items(item_ids)` | **不可逆**：删行 + 可能删正文/产物 | 逐条列出标题 + 明写不可逆 + 明写「客户端缓存副本无法收回」 |

`list_retracted_items` 是必需的配套：没有它，agent 撤回之后无法知道撤过什么、
无法撤销。审计表不该被当作查询面使用（它是取证事实源，且 `args_redacted`）。

### REST 面（`/v1`）

MCP 工具只是调用方，领域动作必须先在核心 API 存在（否则 MCP 会绕开审计——
ADR 0010 的原文理由）：

- `POST /v1/items:retract` body `{item_ids: [...], reason?: str}`
- `POST /v1/items:unretract` body `{item_ids: [...]}`
- `POST /v1/sources/{id}/items:retract` body `{since?, until?, include_published?}`
- `POST /v1/items:purge` body `{item_ids: [...]}`
- `GET /v1/items?publish_status=retracted` 查询（现有查询面已支持按状态过滤，
  只需让 `retracted` 成为合法枚举值）

`:verb` 后缀与既有的 `:trigger`、`:enable`/`:disable` 一致。

### 6. 与既有 `WITHDRAW` 转换的关系：并存，不合并

`core/item_state.py` 已经有一个 `WITHDRAW` 转换，从 `READY` 打回 `PENDING`，
并且**显式拒绝已暴露给客户端的条目**（`exposed_to_any_client` 为真即
`InvalidTransitionError`）。它要解决的是「发布前发现不对，撤回」，不是我们要
的「发布后发现不对，撤回」。

两者不合并，因为它们的合法前提正好相反：

| | `WITHDRAW` | `retract` |
|---|---|---|
| 起始状态 | 仅 `READY` | `ready` / `published` 均可 |
| 已暴露条目 | **拒绝** | **允许**（这是它的全部意义） |
| 目标状态 | `pending` | `retracted` |
| 语义 | 处理链还没完，先别发 | 已经发了，现在收回 |

把 `WITHDRAW` 放宽到接受 `published` 会破坏它存在的理由——它的
`exposed_to_any_client` 检查是「已发布内容不可变」这条不变量在状态机里的落点。
`retract` 不修改内容，它改的是可见性，因此不与该不变量冲突。

实现上 `retract` 走新的 `ItemEvent.RETRACT` / `UNRETRACT`，与 `WITHDRAW` 并列
在同一个转换表里，而不是复用后者。

## 需要一并修改的可见性/枚举点（逐条列出，避免遗漏）

读代码得到的完整清单，每一处都要有测试：

| 位置 | 现状 | 改法 |
|---|---|---|
| `core/item_state.is_visible` | `is not PENDING`（黑名单） | 改白名单 `in {READY, PUBLISHED}`，**关键**：否则失败开放 |
| `adapters/repository/items_query.py:75` | `!= PENDING` | `NOT IN (PENDING, RETRACTED)` |
| `adapters/repository/reader.py:48`（stream ids） | `!= PENDING` | 同上 |
| `adapters/repository/reader.py:224`（unread counts） | `!= PENDING` | 同上 |
| `adapters/repository/feed.py:34` | `!= PENDING` | 同上 |
| `adapters/repository/feed.py:92`（`promote_to_ready` 守卫） | `is not PENDING` | 无需改：它只对 `pending` 生效，撤回条目本就不该被 promote |
| `adapters/repository/feed.py:56`（`mark_published`） | 走 `READY + FIRST_PUBLISH` 转换 | 撤回条目不是 `ready`，转换会拒绝；确认并加测试 |
| `adapters/rss_out.py:162` | `is not PENDING` | 用 `is_visible`，或同样改白名单 |
| `app/items_routes.py:281,314`（body 端点） | `is PENDING` 判 404 | 改成 `not is_visible(...)`，让撤回条目同样 404 |
| `app/reader_routes.py:459`（contents） | `!= "pending"` 字符串 | 改 `is_visible`；**不要**再散落字符串字面量 |
| `app/reader_routes.py:653`（edit-tag） | **无任何状态过滤** | 加可见性过滤：客户端可以对已撤回 id 发已读，不该因此写回状态 |
| `adapters/repository/inspection.py:127` | `counts(..., every=PublishStatus)` | 加 enum 成员即可；`every` 机制会保证 key 存在 |
| `core/enums.py` | 三值 | 加 `RETRACTED = "retracted"` |
| `adapters/db_models.py:35` | `ENUM_LENGTHS["publish_status"] = 16` | `"retracted"` 9 字符，仍是 16，无需改 |
| `app/reader_routes.py:290`（订阅列表） | 隐藏 `deleted` 源 | 不涉及；撤回是 item 级 |

`edit-tag` 这一行是读代码时才发现的：它按 id 取行、直接写 `read_state`，
**没有任何可见性判断**。撤回后客户端仍会为旧 id 发已读回执，那会给一个不再
可见的条目写状态行——正是 CASCADE 删除会带走的那些行的反面。加过滤，
让撤回条目收到已读回执时静默丢弃（客户端视角与「条目已消失」一致）。

## 迁移

`item` 表加两列，一条 migration：

- `retracted_at TEXT NULL`——撤回时间。可空，撤回时才写。
- `status_before_retract TEXT NULL`——撤销时要打回的状态。可空；非空即表示
  这一行当前是 retracted（回填一致性由 CHECK 保证不了，由写入器保证，
  测试覆盖）。

`publish_status` 的 CHECK 约束从 `('pending','ready','published')` 扩成四值。
SQLite 上 CHECK 变更需要 batch_alter_table 重建表，且 AGENTS.md 明确
「autogenerate 会漏掉 enum CHECK，生成后必须 `alembic check` 并人工核对」。

`downgrade`：撤回行**不能简单打回 pending**（那会让客户端看到一批突然出现的
条目，且 pending 的语义是「处理链未完成」）。downgrade 把 `retracted` 行按其
`status_before_retract` 恢复，然后删列、收窄 CHECK。若 `status_before_retract`
为空（不该发生），回落到 `published`——宁可保留输出，不可凭空发布。

## 保留期与磁盘回收

本 ADR **不做**自动物理清理。`retracted` 行会一直留着，占用的正文文件也不会
被回收。这是有意的：自动抹除一个 agent 高频调用的操作的产物，风险高于收益。

但要把回收路径写清楚，放进 runbook：

- 撤回后可随时 `purge_items` 显式回收；
- 想要「撤回 N 天后自动抹除」需要一个 GC 任务 + `PEARSS_RETRACT_GC_DAYS`，
  留作后续（本 ADR 的 consequences 里记为未做）。

## 后果

**建立**：

- agent 能清理无用/过时/抓错的内容，不需要数据库直连；
- 撤回可逆，因此可以在开发调试里放心使用；
- 物理抹除存在但被隔离成高确认动作，且正确统计共享正文的引用；
- 已发布 GUID 不复用、不释放，客户端历史不会被静默改写成一个不同的条目。

**放弃**：

- 撤回**不回收磁盘**。这是默认路径的代价：安全换空间。要空间就 purge。
- `retracted` 状态会渗入若干处枚举判断（`PublishStatus`、两处可见性过滤器、
  Reader 的 edit-tag 行为、`mark_published`）。每一处都要有测试。
- 客户端仍可能短暂显示已撤回条目（它缓存着），直到缓存过期或客户端重新拉取。
  这是用户明确接受的。

**未做，留作后续**：

- 自动 GC（`PEARSS_RETRACT_GC_DAYS`）；
- `purge_items` 对 `RawFetch` 的连带清理；
- 撤回原因（`reason`）目前只进 AuditLog 的 `args_redacted`，不在 item 行上留。

## 与既有约束的显式冲突（需在文档里修订原文）

`data-model.md` 的「停用/删除 Source 不得删除已有 Item」**依然成立**——本
ADR 没有提供任何「删源并删其条目」的动作。

`state-machines.md` 的「物理清理属于独立维护流程」需要修订为：物理清理现在
有一个显式的、经确认的 API 入口（`purge_items`），不再只是「流程」。
