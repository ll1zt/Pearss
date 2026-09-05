# 领域数据模型

本文定义核心领域实体、关系、数据归属和备份语义。字段的权威机器可读定义在 OpenAPI、SQLAlchemy 模型与 Alembic migration；本文解释其领域意义和不变量。

## 通用约定

- 主键使用项目唯一批准的稳定 ID 格式（建议 UUIDv7）
- 所有时间均为 UTC RFC 3339 / ISO 8601
- `tenant_id` 在单用户阶段保留，默认单一 tenant；不得在业务逻辑中硬编码省略它
- `created_at`、`updated_at` 适用于所有可变事实源记录
- 大型 payload（HTML、XML、音频、译文文件）存对象/文件存储，数据库仅持久化 `payload_ref` 与内容摘要

## 核心实体

### Source

一个内容来源：RSS/Atom feed、网页采集器目标，或 P3 的 agent 主动接入端点。

| 字段 | 说明 |
|---|---|
| `id` | 稳定来源 ID |
| `type` | `rss` / `web` / `agent` |
| `url` | RSS 或网页 URL；agent 类型可以为空或引用配置 |
| `canonical_url` | 规范化后的去重 URL。**NOT NULL**：它正是唯一约束比较的列，而 SQL 中 NULL 彼此不等 —— 可空会让这些行完全落在约束之外。agent 源用合成键 `agent://<name>`（见 ADR 0008） |
| `parser_plugin_id` | web 来源使用的采集器标识；P1 为硬编码准插件 ID |
| `schedule_policy` | `{type: fixed/adaptive, ...}`；决定下次抓取策略 |
| `trust_level` | 来源信任等级；不改变“正文不可信”的默认规则 |
| `status` | `active` / `paused` / `disabled` / `deleted` |
| `tenant_id` | 租户预留字段 |

不变量：同一 `tenant_id` 内，`canonical_url + type` 唯一，且 `canonical_url` 不可为空 —— 否则该唯一约束对 NULL 行不生效，重复源可以无限并存。停用/删除 Source 不得删除已有 Item。

### RawFetch

一次抓取尝试的原始记录，不等同于成功入库的内容。

| 字段 | 说明 |
|---|---|
| `id` | 抓取 ID |
| `source_id` | 所属来源 |
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

最低字段：`source_id`、`external_id/guid`、`title`、`canonical_url`、`published_at`、`content`、`raw_fetch_id`、`metadata`。

### Item

规范化后的内容条目，是系统对外提供内容的核心事实源。

| 字段 | 说明 |
|---|---|
| `id` | 稳定 item ID |
| `source_id` | 主要来源；多源关系可通过额外关联表表达 |
| `guid` | 对外 RSS/Reader API 的稳定 ID；首次发布后不可改变 |
| `content_hash` | 规范化内容哈希，用于跨源重复检测 |
| `title` / `url` / `published_at` | 面向消费端的规范化字段 |
| `content_ref` | 正文/清洗正文存储引用 |
| `publish_status` | `pending` / `ready` / `published` |
| `published_to_feed_at` | 首次对外输出时间 |
| `tenant_id` | 租户预留 |

不变量：

- `guid`、主体内容、`content_hash` 在 `published` 后不可修改
- `pending` 不得在 RSS 或 Reader API 可见
- `content_hash` 相同的 Item 必须依据明确规则合并、关联或抑制，不允许无声重复发布
- 处理结果不写回原 Item，见 ItemEnrichment

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
| `status` | `queued` / `running` / `succeeded` / `failed` |
| `error_class` | 失败分类，适用时填写 |

不变量：`item_id + plugin_id + plugin_version + kind + lang + input_hash` 唯一；同一输入可以存在多个版本产物；派生物可通过重放管道重建。

### Job 与 JobRun

`Job` 是长期声明；`JobRun` 是某次实际执行。

> P1 现状：采集任务与源一一对应（`type='collect'`，`config` 存 source id）。
> **`schedule` 列故意留空** —— 下次何时抓是从 `source.schedule_policy` 与历史
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

ReadState 是事实源；清理 Item 前必须定义其级联/保留策略。

### Plugin（P2）

插件注册表记录，不是插件代码本身。

| 字段 | 说明 |
|---|---|
| `id` / `version` / `type` | 稳定身份与 collector/processor 类型 |
| `manifest` | 声明的 hooks、权限、配置 schema、入口 |
| `checksum` / `signature` | 导入校验材料 |
| `status` | 生命周期状态 |
| `trust` | `trusted` / `sandboxed` 等执行隔离决策 |

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

## 关系概览

```text
Source 1 --- N RawFetch
Source 1 --- N Item
RawFetch 1 --- N ItemDraft (逻辑关系)
Item   1 --- N ItemEnrichment
Job    1 --- N JobRun
Item   1 --- 1 ReadState (每 tenant)
Job    1 --- 1 Source   (type='collect')
Source 1 --- N Item
FeedOut N --- N Item (由 filter_query 与发布记录隐式关联)
Plugin 1 --- N ItemEnrichment
```

## 事实源、派生物与备份

| 类型 | 实体 | 处理 |
|---|---|---|
| 事实源 | Source、Item、FeedOut、Job、Plugin、ReadState、AuditLog | 定时备份、恢复演练、迁移严格管理 |
| 可重建运行记录 | RawFetch、JobRun | 配置保留期；近期保留用于诊断，不进入常规长期备份 |
| 可重建派生物 | ItemEnrichment、处理器产物 | 从 Item + pipeline 配置重放；可按成本选择保留，不视为唯一事实源 |

注意：如果某个外部内容以后无法再次获取，而原始 payload 又是唯一副本，应通过来源配置将该 RawFetch/payload 提升为备份对象，不能机械执行“RawFetch 不备份”。
