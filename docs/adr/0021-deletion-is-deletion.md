# 0021 · 删除就是删除：条目与订阅的物理删除

- 状态：**部分 superseded by [0063](0063-unsubscribing-is-not-destroying.md)**（`DELETE /v1/sources/{id}` 的语义已改为退订；物理删除移到 `DELETE /v1/origins/{id}:purge`，且需要理由）
- 日期：2026-09-14
- 实现：`src/adapters/repository/deletion.py`、`0017_retire_source_deleted.py`、
  `items_routes.py` / `sources_routes.py`、`mcp_server.py`
- 修订：`docs/domain/data-model.md`（Item/Source 的「不得删除」不变量）、
  `docs/domain/state-machines.md`（Source 状态机去掉 `deleted` 的软删除语义）、
  `docs/api/style-guide.md`（`DELETE /v1/sources/{id}` 语义）、
  `docs/api/mcp-tool-guide.md`、`docs/operations/runbook.md`
- 关联：ADR 0008（canonical_url NOT NULL）、ADR 0010（MCP 必须经 `/v1`）、
  ADR 0013（显式正文获取）
- 取代：本 ADR 取代其初稿「撤回（retraction）」方案（未提交即为草案）

## 背景

用户要求 MCP 能**删除订阅、删除源条目内容**，理由三条：

1. 客户端的缓存有时效，过期之后服务端删掉内容不会造成客户端错误；
2. 开发阶段调试需要清掉测试数据；
3. 错误抓取（mis-fetch）的内容需要能清理。

现状（读代码得到）：

- **没有任何条目删除能力**。`items_routes.py` 零写端点；Reader 只有
  ClientLogin、stream/contents、edit-tag、subscription/edit、mark-all-as-read。
- **`remove_source` / `DELETE /v1/sources/{id}` 是软删除**：源转 `status='deleted'`，
  条目全部保留，且**保留 tombstone 行**——重新订阅同一 feed 会命中该行并复活它
  （`test_readding_a_deleted_source_reactivates_it` 固定了这个行为）。
- **`data-model.md`**：「停用/删除 Source 不得删除已有 Item」。
- **`netnewswire.md`**：「不得删除已发布 GUID 后复用」。
- **外键 CASCADE**：`read_state.item_id`、`item_enrichment.item_id`、
  `item.source_id`、`raw_fetch.source_id` 都是 `ondelete="CASCADE"`。
  删一行会自动带走其已读状态、译文产物、抓取记录。
- **正文是内容寻址的共享文件**：`filestore.store_body` 按 sha256 落盘，
  正文相同的两篇条目**共享同一个文件**。
- **可见性由 SQL 里的 `!= 'pending'` 把守**，共 7 处（见下表）。

## 被否决的方案：撤回（retraction）

初稿把这件需求理解成「不能真删，只能隐藏」，于是设计了第四种
`publish_status='retracted'`、可撤销、不回收磁盘。**这偏离了用户的原意**：
用户明确指出客户端缓存会过期，因此删除是安全的——这是用户给出的论据，
不是需要被绕开的障碍。用户要的是清理，不是遮蔽。

具体否决理由：

- 可逆性不是需求。agent 清理垃圾不该留下不断增长的隐藏行、再配一个工具去
  查看、第三个工具去撤销。
- 不回收磁盘等于没清理。「清理掉无用的条目内容」的语义是内容消失，
  不是从视野里挪到一个看不见的地方。
- 「不得复用已发布 GUID」**不禁止删除**。它禁止的是 GUID 被复用到不同内容上；
  删除之后不复用即可，两者并不冲突。初稿把「不可复用」误读成「不可删除」。

## 决定

### 1. 条目删除：硬删除，含正文与产物

新增 `DELETE /v1/items/{item_id}` 与批量 `POST /v1/items:delete`，
以及按源清空 `POST /v1/sources/{source_id}/items:delete`。

删除一个条目意味着：

| 对象 | 处理 | 依据 |
|---|---|---|
| `item` 行 | 删 | 这是删除本身 |
| `read_state` 行 | 删（CASCADE） | 条目没了，已读状态无宿主 |
| `item_enrichment` 行 + 其 `payload_ref` 文件 | 删（CASCADE + 显式删文件） | 派生物随主体消失 |
| `content_ref` 指向的正文文件 | **仅当无其他 item 行引用该摘要时**删 | 见下 |

### 2. 正文文件的引用计数是唯一真正需要小心的地方

CAS 按 sha256 寻址，**两篇正文相同的条目共享一个文件**。若按条目删文件：

- 会**静默清空另一篇活着的文章**的正文——`read_body` 对缺失文件只返回 `""`
  加一行 warning，不报错。这是最坏的失败形态：用户看到一篇空白文章，
  日志里只有一行 warning。
- 因此删除前必须查引用：`select count(*) from item where content_ref = :ref`
  （在删掉本行**之前**执行，或删完后重查）；仅当计数为 0 才 unlink。

删文件**失败不阻断行删除**：文件删不掉是磁盘问题，行已删就是已删，
留个孤儿文件由后续 GC 收拾。反过来（先删文件、后删行中途失败）会留下指向
缺失正文的活条目，那才是坏结果。

#### 引用计数必须在事务内算，unlink 必须在提交后做

这条是初版实现踩过的坑，写下来是因为它不直观：**行可以回滚，unlink 不行。**
删除的两半原子性不同——事务回滚会把行还回来，而已经删掉的文件不会回来。
若在事务内 unlink，回滚后得到的是「活条目指向不存在的正文」，
`read_body` 返回空串且不报错，正是上面说的静默清空，只是触发路径换成了
「提交前抛异常」。

因此切成两半（`ReclaimPlan`）：

1. **事务内**：删行、flush，然后按引用计数算出哪些摘要已无存活引用——
   这一刻的计数才是真的（共享正文必须活下来，只有计数能回答）；
2. **提交后**：调用方执行 `reclaim()` 逐个 unlink。

代价是这中间窗口里文件还在（多占一点磁盘，可接受），收益是回滚路径
退化成「多一个孤儿文件」而不是「多一篇空白文章」。`reclaim()` 幂等，
重复调用安全。

`_raw_fetch_row` 不写 `payload_ref`（原始 feed 正文可从 origin 重建，
存两份是浪费），所以删除路径**没有**抓取 payload 的 unlink 分支——
对一列恒为 NULL 的字段做循环是死代码，何况抓取 payload 不是 item 正文，
不会有 item 行替它计数，用 `_still_referenced` 会问错表。

### 3. 删除订阅 = 删源行 + 删它的条目（用户已确认）

`DELETE /v1/sources/{source_id}` 的语义**改变**：不再是打标记，而是

1. 删除该源的所有 item 行（连带 read_state、item_enrichment 行与产物文件，
   正文文件按引用计数）；
2. 删除该源的 raw_fetch 行与抓取批次记录；
3. 删除 source 行本身。

**这是一个破坏性语义变更**，影响面必须显式承担（见「后果」）。
改变的理由：用户认为「删除订阅」的直觉就是内容一并消失，且分离成两个动作
（下线 + 清空）只是把同一个意图拆成两次确认，不增加安全性——两次都点了确认
的人，第三次也会点。

`SourceStatus.DELETED` 随之**没有存在意义**：不再有「已删除但行还在」的状态。
源状态机回到 `active` / `paused` / `disabled`，`deleted` 从 enum 与状态机里移除。

### 4. 重新订阅同一 feed 会创建新源

删掉源行意味着不再有 tombstone。`canonical_url` 唯一约束仍在，
`add_source` 的「命中已存在行则复活」逻辑对**软删除的行**不再适用——
因为那种行不再存在。

新行为：删除后重新订阅同一 canonical URL，创建**全新的 source 行**，
`seen_frontier` / `floor_cursor` 从空开始，因此会重新采样历史
（首轮 `PEARSS_X_FIRST_PAGES`）。这是删除的应有之义：删掉的东西不会因为
重新订阅而回来。**已删除条目的 GUID 不再被复用**——新源的新条目从 origin
重新取，external_id 由新源派生，因此 GUID 与旧的不同。

### 5. 不保留撤销余地

- 没有 tombstone，没有 `retracted` 状态，没有 `unretract`。
- 删除动作本身**记入 AuditLog**（谁、何时、删了哪些 id、多少条），
  这是追溯而非恢复：内容不再保留。
- 不做删除前自动备份。用户已明确选择「硬删除，不可撤销」。

## 工具面（MCP）

全部经 `/v1`（ADR 0010），全部 elicitation 确认，全部写 AuditLog。

| 工具 | 说明 | 确认内容 |
|---|---|---|
| `delete_items(item_ids)` | 删指定条目（含正文、译文、已读状态） | 逐条标题 + 源 + 明写不可逆 |
| `delete_source_items(source_id, since?, until?, include_published?)` | 清空某源的条目但保留订阅 | 源标签 + 条数 + 时间范围 |
| `delete_source(source_id)` | 删订阅**及其全部条目** | 源标签 + 将删除条数 + 明写不可逆 |

`delete_source` 复用现有工具名（语义变更见 §3）。`delete_source_items` 是
新工具：它的存在让「清掉这批抓错的内容，但订阅留着继续抓」成为可能——
这正是用户的场景 3，且没有它就必须删掉再重建订阅，会丢掉
`seen_frontier` 的历史深度。

### REST 面（`/v1`）

| 端点 | 说明 |
|---|---|
| `DELETE /v1/items/{item_id}` | 删单条 |
| `POST /v1/items:delete` | 批量删，body `{item_ids: [...]}` |
| `POST /v1/sources/{id}/items:delete` | 按源清空（可选 since/until/include_published） |
| `DELETE /v1/sources/{id}` | 语义变更：删源 + 条目 |

`:verb` 后缀与既有 `:trigger`、`:enable`/`:disable` 一致。

### 幂等

`DELETE` 一个已删除的 id 返回 200 + `{"deleted": 0}` 而非 404：
重试与首次执行不可区分（AGENTS.md 要求所有写操作幂等），且客户端拿不到
「它曾经存在」与「它从不存在」的差别——两者对调用方是同一件事。

## 可见性清单（逐条列出，避免遗漏）

删除是硬删行，理论上不需要改可见性过滤——行不在了就不可能出现。但仍需检查
**级联与孤儿**：

| 位置 | 影响 |
|---|---|
| `items_query.py` / `reader.py` / `feed.py` / `rss_out.py` 的 `!= 'pending'` | 无需改：行已不存在 |
| `app/reader_routes.py:653`（edit-tag） | 已删条目不在结果集内，静默丢弃；**已有**：「a client may still hold ids for items that were withdrawn」 |
| `resolve_reader_ids` | 前缀反查落空即返回空，无影响 |
| `item_enrichment` / `read_state` | CASCADE 自动清理，无需代码 |
| `inspection.py` 的 `counts(every=PublishStatus)` | 不涉及（那是 Item 状态）；Source 状态的 `counts(every=SourceStatus)` 在移除 `deleted` 后自然少一个恒为 0 的 key |
| `mark_published` | 对不存在的行是 no-op |

### 移除 `SourceStatus.DELETED` 的四个调用点

读代码得到的完整清单。**每一处删除后都变简单**——这些守卫的存在理由只有
「软删除的行还留在库里」，行不再存在时它们就是死代码：

| 位置 | 现状 | 改法 |
|---|---|---|
| `core/enums.py:118` | enum 成员 | 删除 `DELETED` |
| `app/sources_routes.py:850` | `row.status = DELETED` | 改真删除（§3 的三步） |
| `adapters/repository/sources.py:61` | 命中软删除行则**复活**并覆盖配置 | 整段删除：行不存在时自然走 insert 分支。**这是行为变更**：重新订阅同一 canonical_url 不再复活旧行，而是新建（见 §4） |
| `app/reader_routes.py:294` | 订阅列表隐藏 `deleted` 源 | 删除该条件（行不存在，无需隐藏） |
| `app/reader_routes.py:750` | `subscription/edit` 拒绝改动 `deleted` 源 | 同上，删除该条件 |
| `adapters/db_models.py:155` | CHECK 含 `"deleted"` | CHECK 收窄为三值 |

`reader_routes.py:294` 与 `:750` 的删除要加测试确认语义未变：一个
`disabled` 源仍应被隐藏/拒绝改动（原测试用 `deleted` 造数据，
见「需要同步修改的既有断言」）。

## 迁移

一条 migration：

1. `source.status` 的既有 `deleted` 行**必须处理**。migration 内把
   `status='deleted'` 的行转成 `disabled`（保留订阅语义：仍在库里、
   不被采集、历史条目保留），**不得**在 migration 里级联删数据——
   数据删除是运行期经确认的动作，不是升级副作用。
2. `publish_status` 与 Source status 的 CHECK 约束相应收窄/调整。
   SQLite 需 `batch_alter_table` 重建表；AGENTS.md 明确「autogenerate 会漏掉
   enum CHECK，生成后必须 `alembic check` 并人工核对」。
3. `downgrade`：把 `disabled` 打回 `deleted`（信息有损但状态合法）。
   删掉的数据无法恢复——这是 downgrade 的固有事实，写在 migration 注释里。

**本地与 y700 现状**：y700 上有一个 `deleted` 状态的源（早前软删除留下）。
migration 会把它转成 `disabled`，属预期。

## 需要同步修改的既有断言与文档

| 位置 | 现状 | 改法 |
|---|---|---|
| `test_sources_api.py:475` | `assert items == 2, "published items must survive"` | 反过来：断言 items == 0 |
| `test_sources_api.py:473` | `assert status == "deleted"` | 断言 source 行不存在 |
| `test_readding_a_deleted_source_reactivates_it` | 依赖 tombstone 复活 | 重写：重新订阅得到**新源**，id 不同 |
| `test_reader_api.py:326`（sidebar 隐藏 deleted 源） | 用 `update source set status='deleted'` 造数据 | 改成 `disabled`（仍要隐藏） |
| `data-model.md:35`「停用/删除 Source 不得删除已有 Item」 | 与本 ADR 冲突 | 改写为删除语义 |
| `state-machines.md:79-98`（软删除段落） | 描述软删除 | 改写；`deleted` 状态移除 |
| `style-guide.md:34,54` | 列 `DELETE /v1/sources/{id}` | 更新语义描述 |
| ADR 0008:50（「删一个源会连带删掉…」） | 当时的推理是**不删** | 加注：本 ADR 采纳了级联删除，理由见 §2 |
| `mcp-tool-guide.md:67` | `remove_source`「软删除」 | 更新描述 |

## 实现期间发现的问题（记录以备后查）

### 1. SQLite 从未启用外键，所有 CASCADE 一直是空文

`PRAGMA foreign_keys` 的默认值是 **off**，而项目从未设置它（只设了
`journal_mode` / `busy_timeout` / `synchronous`）。也就是说本 schema 声明的
六处 `ON DELETE CASCADE` 在运行时**全部不生效**：

```
source ──CASCADE──> item, raw_fetch
item   ──CASCADE──> read_state, item_enrichment
```

过去没暴露，是因为**从来没有任何代码删除过父行**（软删除只改 status）。
本 ADR 的删除是第一个真正删父行的功能，于是它立刻表现为
`test_read_state_and_item_enrichment_rows_go_with_the_item` 失败：
item 行没了，`read_state` 还在。

修法：在 engine 的 `connect` 钩子里加 `PRAGMA foreign_keys=ON`
（`create_engine_for`）。**必须是每连接**，启动时设一次只对当次连接有效。

连带修了测试基建：约 15 个 fixture 用 `sa.create_engine(url)` 自建 engine，
绕过了 pragma——这也正是该缺陷一直不可见的原因：**自己造 engine 的测试，
测的是一个应用从不使用的数据库**。现在统一走 `create_engine_for`。

### 2. `batch_alter_table` 会让 CHECK 约束名双重加前缀

改 `source.status` 的 CHECK 时，`op.batch_alter_table` 会先反射现有表，
而反射用的 `MetaData` 带着项目的 naming convention（`ck_%(table_name)s_...`）。
库里存的 `ck_source_sourcestatus` 被再套一层前缀，变成
`ck_source_ck_source_sourcestatus`，于是 `drop_constraint` 按原名找不到，
按裸名找又会把整张表的 CHECK 改名。

因此 0017 用**显式 DDL 重建表**，并把约束名按库里实际存储的形式写出来
（`ck_source_trustlevel`，不是模型里声明的裸 `trustlevel`）——0003 的
downgrade 正是按这个名字 drop 的。这是项目里第一个改约束的迁移，
所以这个坑之前没被踩到。

## 后果

**建立**：

- agent 能真正清理无用/过时/抓错的内容，磁盘回收；
- 删除语义单一且可预测：说删除就是删除，没有隐藏层；
- 正文共享文件不会被误删（引用计数）。

**放弃**：

- **不可撤销**。误删一条已发布内容无法通过 API 恢复，只能靠数据库备份。
  这是用户明确选择的取舍。
- **破坏性语义变更**：`DELETE /v1/sources/{id}` 从「打标记」变成「删数据」。
  任何按旧语义调用它的现有客户端（或旧的 agent 指令、旧文档）会在下一次调用时
  销毁内容。必须同步更新 runbook 与本仓库全部文档引用。
- 重新订阅丢历史深度：新源从首轮页数策略重新采样，旧的 `seen_frontier` 没了。
- 客户端可能短暂显示已删条目（它缓存着），直到缓存过期。用户已接受。

**未做，留作后续**：

- 删除前的自动导出（用户已否决）；
- 孤儿 CAS 文件的 GC 任务（正文/产物文件删失败的兜底）；
- 删除 `RawFetch` payload 文件的独立保留策略（本 ADR 只按引用计数处理）。
