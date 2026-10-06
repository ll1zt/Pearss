# 领域数据模型

本文定义核心领域实体、关系、数据归属和备份语义。字段的权威机器可读定义在 OpenAPI、SQLAlchemy 模型与 Alembic migration；本文解释其领域意义和不变量。

## 通用约定

- 主键使用项目唯一批准的稳定 ID 格式（建议 UUIDv7）
- 所有时间均为 UTC RFC 3339 / ISO 8601
- `tenant_id` 在单用户阶段保留，默认单一 tenant；不得在业务逻辑中硬编码省略它
- `created_at`、`updated_at` 适用于所有可变事实源记录
- 大型 payload（HTML、XML、音频、译文文件）存对象/文件存储，数据库仅持久化 `payload_ref` 与内容摘要

## 核心实体

### Origin

一个内容来源的**事实**一半：RSS/Atom feed、网页采集器目标，或 agent 主动接入端点
（A3，ADR 0022）。「怎么消费它」是策略，在 `Subscription` 上 —— 这一分为二是 ADR 0046
的拆表（阶段 A 建表复制 / B 切读写 / C 删镜像）。

| 字段 | 说明 |
|---|---|
| `id` | 稳定来源 ID。**它就是旧 `source.id`**：拆表没有重新编号，因此既有 item id、guid 与 `/v1/sources/{id}` 都保持原义 |
| `type` | `rss` / `web` / `agent` |
| `url` | RSS 或网页 URL。agent 类型必须携带合成名 `agent://<name>`：它不是抓取目标，而是推送端点，去重键需要这个名字（`origin_canonical_key` 对空 url 报错） |
| `canonical_url` | 规范化后的去重 URL。**NOT NULL**：它正是唯一约束比较的列，而 SQL 中 NULL 彼此不等 —— 可空会让这些行完全落在约束之外。agent 源用合成键 `agent://<name>`（见 ADR 0008） |
| `parser_plugin_id` | web 来源使用的采集器标识；P1 为硬编码准插件 ID |
| `title` | 来源自报的名字，采集时发现（feed 的 `<title>` / 页面标题）。**可空且不回填**：写入器只在缺失时设置，所以往列里塞一个 URL 派生猜测会**永久挡住真实标题**；未采集源的显示标签在展示层派生（`core.origin_display_label`） |
| `seen_frontier` | 分页型源已摄入的**最新推文 id** —— 抓取区间的**上沿**。翻页到此停止，不再重读历史。单调前进：重走旧历史时报到的更低 id 不得覆盖它 |
| `floor_cursor` | 抓取区间的**下沿**：该源走过的**最深**位置。首轮按 `PEARSS_X_FIRST_PAGES` 采样，预算耗尽时停在哪，那里就成为 floor；此后每轮只在 frontier 与 floor 之间作业。**它是停止符，不是续采起点** —— 见 §「抓取区间」。**不可从 `item` 重算**：游标是来源给的不透明串 |
| `tenant_id` | 租户预留字段 |

不变量：同一 `tenant_id` 内，`canonical_url + type` 唯一，且 `canonical_url` 不可为空 ——
否则该唯一约束对 NULL 行不生效，重复 origin 可以无限并存。

`item.origin_id` 与 `raw_fetch.origin_id` 都是 NOT NULL + `ON DELETE CASCADE` 外键 → `origin.id`：
删一个 origin 会带走它的条目、抓取记录与订阅。源内分页靠复合索引
`ix_item_origin_sort_key_id (origin_id, sort_key, id)`，没有它每次排序都落临时 B 树（issue #77）。

**备份分级提醒**：`seen_frontier` / `floor_cursor` 是**运维状态而非派生物**。
它们不能像 `RawFetch`/`JobRun` 那样靠重放管道重建，因此随 `Origin` 一起属于事实源备份范围；
丢了不会报错，但会让下一次采集把已经抓过的区间重走一遍（去重兜住重复入库，
代价是白费请求）。

### Subscription

**怎么消费一个 origin**：调度策略、处理链与阅读器归类。分类在策略这一侧，因为
「谁在消费、按什么策略消费」正是它与事实分开的原因（一个 origin 两套策略：一个翻译、
一个不译，见 issue #371）。

| 字段 | 说明 |
|---|---|
| `id` | 稳定订阅 ID。1:1 阶段等于 `origin.id`（因此 `/v1/sources/{id}` 与 Reader 流 id 都还指得准）；多订阅之后才分叉 |
| `origin_id` | 指向 `origin.id`，`ON DELETE CASCADE` |
| `schedule_policy` | `{type: fixed/adaptive, ...}`；决定下次抓取策略。**下次何时抓从 policy + 历史推导，不落库**（ADR 0009） |
| `pipeline_chain` / `target_lang` | 处理链与语言产品的目标语言（见 §「处理链」） |
| `folder` | 订阅在阅读器侧边栏中的归类，**一订阅一分类**（FreshRSS 模型：其源码明确 "a feed can only be in one category"，客户端也按此实现）。NOT NULL，默认 `default`；名称校验见 `core/folders`。它同时决定 `user/-/label/<name>` 流的内容 |
| `status` | `active` / `paused` / `disabled` |
| `trust_level` | 来源信任等级；不改变「正文不可信」的默认规则 |
| `tenant_id` | 租户预留字段 |

不变量（当前阶段）：同一 `tenant_id` 内 `origin_id` 唯一 —— **一个 origin 一个订阅**。
多订阅迁移会删掉这条约束（issue #371），那时一个 origin 才能挂两套策略。

**停用（`paused`/`disabled`）不删除任何 Item**。`DELETE /v1/sources/{id}` 则是真删除：
origin 行、它的 Item、正文文件、已读状态、处理器产物与抓取记录一并消失（ADR 0021）。
要清掉内容但保留订阅（含抓取游标），用 `POST /v1/sources/{id}/items:delete`。
旧 `/v1/sources` 读端点保留为**投影**并已标 deprecated（issue #366），`source` 表本身
在迁移 0028 已删除。

### 抓取区间（分页型源）

`seen_frontier``seen_frontier` 与 `floor_cursor` 共同界定「要抓什么」：

- **首轮**是唯一有页数策略的一轮（`PEARSS_X_FIRST_PAGES`，默认 3）。它定义了该源的
  历史深度 —— 这是操作者的选择，不是程序猜的。
- **后续每轮**从时间线顶部开始，抓到 frontier 即停。请求数因此与该源的**真实产出**
  成正比，而不是某个策略数字；`first_pages` 不约束后续轮，
  因为「预算在触及 frontier 前用尽」正是跨轮丢内容的方式。
- floor 只由「预算耗尽且未触及 frontier」这一种结局写入（walk 以
  `floor_candidate` 上报）。 frontier 命中、到达 origin 底部、触及 floor、失败，
  都不动它 —— 这些结局都没有说明区间该有多深。
- **断档（gap）只记录、不追讨**。它位于区间内部，追它等于开出第二个爬取方向；
  frontier 持续推进才是下一步抓什么的依据。

### RawFetch

一次抓取尝试的原始记录，不等同于成功入库的内容。

| 字段 | 说明 |
|---|---|
| `id` | 抓取 ID |
| `origin_id` | 所属 origin（`ON DELETE CASCADE`） |
| `payload_ref` | 原始 HTTP body 的存储引用；失败可为空 |
| `content_hash` | 原始 payload hash，用于诊断与去重 |
| `http_status` | HTTP 响应状态，适用时记录 |
| `etag` / `last_modified` | 下次条件请求使用 |
| `status` | `succeeded` / `not_modified` / `failed` |
| `error_class` | 仅 failed 时使用 ErrorClass enum |
| `fetched_at` | 抓取时间 |
| `correlation_id` | 请求与 JobRun 的关联 |

RawFetch 是可重建诊断数据，保留期应配置化；不属于长期备份的事实源。

### ItemDraft

内部短生命周期对象，不要求直接建表。所有入口（RSS、网页采集、P3 ingest）必须输出它，再进入统一 normalize 流程。

最低字段：`origin_id`、`external_id/guid`、`title`、`canonical_url`、`published_at`、`content`、`raw_fetch_id`、`metadata`。

### Item

规范化后的内容条目，是系统对外提供内容的核心事实源。

| 字段 | 说明 |
|---|---|
| `id` | 稳定 item ID |
| `origin_id` | 所属 origin（`ON DELETE CASCADE`）；多源关系可通过额外关联表表达 |
| `guid` | 对外 RSS/Reader API 的稳定 ID；首次发布后不可改变 |
| `content_hash` | 规范化内容哈希，用于跨源重复检测 |
| `title` / `url` / `published_at` | 面向消费端的规范化字段 |
| `author` | 来源给出的署名（RSS `<author>`、知乎显示名、X 的 `名字 (@handle)`）。**空串表示来源本就没署名**，不是匿名。它不参与 `guid`/`content_hash` —— 修正署名不得产生新条目 |
| `content_ref` | 正文/清洗正文存储引用 |
| `publish_status` | `pending` / `ready` / `published`。**内容冻结与首次输出的状态**；多订阅之后它**不再是可见性判据**（可见性按 `(item, subscription)` 判定，ADR 0061） |
| `published_to_feed_at` | 首次对外输出时间 |
| `tenant_id` | 租户预留 |

不变量：

- `guid`、主体内容、`content_hash` 在 `published` 后不可修改
- `pending` 不得在 RSS 或 Reader API 可见
- 内容修订让已就绪但未发布的条目回到 `pending`：`store_item` 的替换分支在
  一条带状态守卫的 UPDATE 里同时换正文并复位 `publish_status=pending`，
  处理链对新 `content_hash` 重新排队；仅当该行仍是 `pending`/`ready` 且未
  发布时替换才生效，先到一步的发布冻结旧正文，迟到的采集会话得到
  `skip_published_content_changed`
- `content_hash` 相同的 Item 必须依据明确规则合并、关联或抑制，不允许无声重复发布
- 处理结果不写回原 Item，见 ItemEnrichment

**删除（ADR 0021）**：`DELETE /v1/items/{id}` 与 `POST /v1/items:delete` 物理删除
Item 行，连带其 `read_state`、`item_enrichment` 与正文文件。两条规则：

- **已发布 GUID 不得被复用**，但**可以删除**——删除之后重新抓取会经 `normalize`
  去重，同一 external_id 若再次出现会得到同一 GUID；禁止的是把该 GUID 挂到
  不同内容上，不是禁止行消失。
- **正文文件按引用计数删**。`cas://<sha256>` 是内容寻址的，正文相同的两个 Item
  共享一个文件；只有当没有任何存活 Item 行引用该摘要时才 unlink。按 Item
  删文件会静默清空另一篇活文章的正文（`read_body` 对缺失文件只返回 `""` 并记
  warning，不报错）。
- **计数在事务内，unlink 在提交后**。行可回滚，文件删除不可回滚；在事务内
  删文件会让一次回滚留下「活条目指向缺失正文」。详见 ADR 0021 的 `ReclaimPlan`。
- **时间窗只作用于有发布时间的 Item**。没有 `published_at` 的条目不属于任何
  窗口，`since` 与 `until` 都会排除它——按日期清内容不会顺带删掉无日期条目。

### ItemEnrichment（P3）

翻译、摘要、TTS 等处理器的独立产物。

| 字段 | 说明 |
|---|---|
| `item_id` | 对应 Item |
| `plugin_id` / `plugin_version` | 产生结果的处理器与版本 |
| `kind` | `translation` / `summary` / `tts_audio` 等 |
| `lang` | 适用语言；非语言产物可为空 |
| `payload_ref` | 产物存储引用 |
| `input_hash` | 处理所依据的 Item 内容版本 |
| `upstream_plugin_id` / `upstream_digest` | 本产物消费的前序产物（插件 + 产物摘要）；两者均为 NULL = 读的是 Item 原始正文（ADR 0030） |
| `status` | `queued` / `running` / `succeeded` / `failed` |
| `error_class` | 失败分类，适用时填写 |

不变量：`item_id + plugin_id + plugin_version + kind + lang + input_hash` 唯一；同一输入可以存在多个版本产物；派生物可通过重放管道重建。

链内数据流（ADR 0030）：后序 spec 的输入是最近一个前序**可消费**产物（`fulltext`、
`summary`；`translation` 明确不可消费以免自我喂养）。`upstream_*` 记录的是**已消费**的
依赖，而上游解析按 `input_hash` 钉版本，因此条目内容修订后的新行消费新产物，旧行作为
历史保留。`upstream_*` **不**参与唯一约束：该表的契约是同一内容版本重处理 = 替换该行。

**多订阅下的链与可见性（ADR 0061）**：`pipeline_chain` 是**订阅**策略，因此一个 origin 有多个
订阅时，新条目**按每个订阅各入其链**；可见性按 `(item, subscription)` 判定，物化在
`item_subscription_ready(item_id, subscription_id, ready_at)` 上（链完成处写入，读路径按
`subscription_id` join），并由一条「物化集合 == 从 enrichment 派生的集合」的守卫防止漏写。
enrichment 的幂等身份里的 `lang` / `input_hash` 已经把两条链的产物分开，所以该表**不含**
`subscription_id` —— 产物属于哪条链是推导出来的，不是存出来的。

### Job 与 JobRun

`Job` 是长期声明；`JobRun` 是某次实际执行。

> P1 现状：采集任务与源一一对应（`type='collect'`，`config` 存 origin id）。
> **`schedule` 列故意留空** —— 下次何时抓是从 `subscription.schedule_policy` 与历史
> **推导**的，不落库，否则会与推导结果成为两个事实源（ADR 0009）。

| 实体 | 关键字段 | 说明 |
|---|---|---|
| Job | `id`、`type`、`schedule`、`priority`、`status`、`config` | 可被调度器实现替换而不变的声明模型 |
| JobRun | `id`、`job_id`、`status`、`error_class`、`metrics`、`correlation_id` | 诊断、重试与观测记录 |

`JobRun.error_class` 只能使用领域 ErrorClass enum；详细状态见 `docs/domain/state-machines.md`。

### FeedOut

一条对外 RSS 信息流配置。

| 字段 | 说明 |
|---|---|
| `id` / `name` | 稳定标识和显示名称 |
| `filter_query` | 选择哪些 ready/published Item |
| `pipeline_chain` | 输出依赖的处理器链；P1 可为空 |
| `format` | RSS 版本/输出格式配置 |
| `token_hash` | 保护私有 feed 的认证 token 哈希，禁止明文保存 |
| `tenant_id` | 租户预留 |

### ReadState（P1，原 P3）

由 Reader API 写回的消费状态。原定 P3，随 C4（Reader API）一起提前到 P1，
见 ADR 0004。

| 字段 | 说明 |
|---|---|
| `item_id` | 对应 Item |
| `is_read` / `is_starred` | 已读、星标状态 |
| `updated_at` | 状态最后写入时间 |
| `tenant_id` | 租户预留 |

ReadState 是事实源；清理 Item 时它随 Item 一起删除（`ON DELETE CASCADE`，ADR 0021 定义了删除语义）。已读状态没有独立于其 Item 的意义：条目不再可见时，「谁读过它」既不构成内容也不构成可查询的事实。

### Plugin（P2）

插件注册表记录，不是插件代码本身。随部署来的采集器在启动时 upsert；运行时
装载的插件由 `:import` 写入、`:uninstall` 删除（ADR 0023）。

| 字段 | 说明 |
|---|---|
| `id` | collector_id，也是运行时插件的目录名 |
| `manifest_json` | 声明的 identity 与 features（`core/plugins/hooks.py` 的 `Manifest.as_dict()`）；**不含**执行体与入口路径 |
| `checksum` | canonical JSON 的 sha256。**不是签名**：没有签名机制，字段名容易误读。它记录「status 决策是针对哪个 manifest 内容做出的」 |
| `status` | `enabled` / `disabled`，运维的启停决定 |
| `trust` | `builtin` / `third-party`。**纯描述性，不产生行为差异**——与 `subscription.trust_level` 同构：区分「随 Pearss 来」与「你自己装的」，供运维辨识。真正的隔离由子进程沙箱提供（ADR 0017），不由此字段决定（ADR 0023） |
| `tenant_id` / `created_at` / `updated_at` | 预留与时间戳 |

### IdempotencyRecord（P1）

支撑「所有写操作幂等」的存储。身份是 `actor + endpoint + key`，值存首次响应以便重放。

| 字段 | 说明 |
|---|---|
| `actor` / `endpoint` / `key` | 复合主键；三者共同定义一次逻辑写 |
| `status_code` / `response_body` | 首次结果，重放时原样返回 |
| `created_at` / `expires_at` | 窗口期；过期后视为不存在 |

不变量与取舍：

- **带过期，非长期事实源**。幂等保证只需覆盖客户端的重试窗口；永久保留会让表无界增长
- 键**按 actor 隔离**：不同调用方复用同一 key 不得互相重放对方的结果
- 重放在响应头标 `Idempotent-Replay: true`，否则重试与首次执行无法区分
- 键由调用方**每次操作新生成**，不从参数派生：参数派生的稳定键会把第二次有意为之的操作永久重放成第一次的结果

### ReaderCredential（P1）

Reader API 的登录。单用户，故一行。

| 字段 | 说明 |
|---|---|
| `username` | 主键 |
| `password_hash` | scrypt 哈希，自描述格式 `scrypt$<n>$<r>$<p>$<salt>$<hash>` |
| `created_at` / `updated_at` | — |

不变量：**明文密码永不入库**，只存在于环境变量。auth token 由 `HMAC-SHA256(username + 哈希)` 派生，因此**改密码会使所有已登录客户端掉线**（见 ADR 0005、runbook 的轮换流程）。

### AuditLog

每一个有业务意义的调用留下的不可变审计事实。

| 字段 | 说明 |
|---|---|
| `id` / `occurred_at` | 审计事件身份与发生时间 |
| `actor` / `session_id` | 谁、在哪个会话发起 |
| `operation` / `target` | 做了什么、对什么做 |
| `args_redacted` | 已脱敏参数摘要；绝不保留 token/Cookie/正文 |
| `outcome` | 成功、拒绝、失败 |
| `correlation_id` | 与日志、JobRun、响应关联 |

AuditLog 是事实源；修正错误需新写一条事件，不得原地篡改旧事件。

### Work 与 WorkUnit（作业账本，ADR 0046）

一条「命令」的记录：`work` 是命令本身（kind / targets / params / fingerprint / status /
created_at / finished_at），`work_unit` 是逐目标的结果（outcome / error_class / detail / ref）。
逐条结果从这里读（`work_units` / `just work units`），响应因此不随目标数增长。

账本是**运维记录**，不是事实源：它回答「这个目标当时发生了什么」，用于诊断与续跑，
**不进事实源备份语义**，到期即清——默认 30 天（`PEARSS_WORK_RETENTION_DAYS`，设 `0` 永久保留）。
`counts` 不落库（ADR 0046：`counts == fold(units)`），所以清理之后不保留聚合副本；
长期统计从事实源（Item / ItemEnrichment）重导，而不是从账本。

清理的两条边界（`adapters/repository/work.py::prune_work_ledger`）：

- **没有 `finished_at` 的 work 永不清理**：那代表作业仍是活的（queued / running），
  它的行正是运行中的执行者写入 unit 的地方；按年龄删它会让「正在跑」看起来像「凭空消失」。
- **只动账本两张表**：unit 随 work 走（`ON DELETE CASCADE`），Item / ItemEnrichment 一行不碰。

## 关系概览

```text
Origin 1 --- 1 Subscription (当前约束；多订阅迁移后为 1 --- N)
Origin 1 --- N RawFetch
Origin 1 --- N Item
RawFetch 1 --- N ItemDraft (逻辑关系)
Item   1 --- N ItemEnrichment
Job    1 --- N JobRun
Item   1 --- 1 ReadState (每 tenant)
Job    1 --- 1 Origin   (type='collect')
FeedOut N --- N Item (由 filter_query 与发布记录隐式关联)
Plugin 1 --- N ItemEnrichment
```

## 事实源、派生物与备份

| 类型 | 实体 | 处理 |
|---|---|---|
| 事实源 | Origin、Subscription、Item、FeedOut、Job、Plugin、ReadState、AuditLog | 定时备份、恢复演练、迁移严格管理（ADR 0046 拆表已完成：`Source` 已不存在，`Origin` 的 id 就是旧 `source.id`） |
| 可重建运行记录 | RawFetch、JobRun | 配置保留期；近期保留用于诊断，不进入常规长期备份 |
| 可重建派生物 | ItemEnrichment、处理器产物 | 从 Item + pipeline 配置重放；可按成本选择保留，不视为唯一事实源 |
| 运维记录 | Work、WorkUnit（作业账本） | 默认 30 天保留窗口后清理（`PEARSS_WORK_RETENTION_DAYS`，ADR 0046 D-F）；**不进备份**；未结束的作业永不清理 |
| 可丢弃临时物 | 处理器块级缓存（`<storage>/processor-cache/`） | **不进备份**、不参与恢复一致性：丢了的代价只是重译（ADR 0052）。成功行由运行时清理；永久失败的行会留下一个小目录（写明不做清理器） |

注意：如果某个外部内容以后无法再次获取，而原始 payload 又是唯一副本，应通过来源配置将该 RawFetch/payload 提升为备份对象，不能机械执行“RawFetch 不备份”。
