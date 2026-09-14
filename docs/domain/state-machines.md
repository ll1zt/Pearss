# 领域状态机

状态机是领域契约。状态枚举在 `core/` 中单一来源定义，数据库约束、OpenAPI、MCP schema 和本文档必须同步。任何未列出的状态转移均视为非法并返回 `409 Conflict`。

## Item 发布状态机

> **已实现**：`src/core/item_state.py`。P1 无处理器链，故条目入库即 `pending → ready`；
> 闸门本身在三条输出路径（RSS、Reader API、MCP 工具）上都强制 `pending` 不可见。
>
> **一条实现中补上的规则**：条目正文被源改写时，若该条目已 `published`，只能
> **保持不动并告警**（`skip_published_content_changed`）—— 重写会与客户端缓存分叉，
> 插入会撞冻结的 GUID。仍 `pending` 则可以就地更新，因为还没有任何人看过它。

```text
                         enrich 成功 / 无处理链
pending ------------------------------------------------> ready
   |                                                        |
   | enrich 失败 / 超时（hold）                             | 首次进入对外 feed/Reader 视图
   v                                                        v
pending <---------------------------------------------- published
   |                      仅消费状态可更新                  |
   | 用户确认 force_publish                                 | 内容、guid、content_hash 不可改
   +------------------------------------------------------> ready
```

| 当前 | 事件 | 条件 | 下一状态 | 副作用 |
|---|---|---|---|---|
| `pending` | `mark_ready` | 无处理链，或所有必需 enrichment 成功 | `ready` | 记录处理版本与时间 |
| `pending` | `enrichment_failed` | 某处理器失败 | `pending` | 记录失败 JobRun；不对外发布 |
| `pending` | `timeout` | 策略为 `hold` | `pending` | 告警/监控 pending 时长 |
| `pending` | `timeout` | 策略为 `publish_original` | `ready` | AuditLog 记录超时降级 |
| `pending` | `force_publish` | 用户经 MCP elicitation 确认，提供 reason | `ready` | AuditLog 记录 override |
| `ready` | `first_publish` | 生成稳定 GUID 且进入任一对外输出 | `published` | 写入 `published_to_feed_at`，冻结主体内容 |
| `published` | `read_state_changed` | Reader API 事件 | `published` | 更新 ReadState；不改 Item 内容 |

不变量：

- `pending` 对任何 RSS、Reader API、公开查询不可见
- `published` 不得回退；不得替换正文、译文主版本、标题、URL、GUID 或 `content_hash`
- `ready` 在首次输出前可以被撤回为 `pending`，仅限尚未暴露给任一客户端且系统明确记录撤回原因
- “发布”由可观察的首次对外输出触发，而不是 SQL update 的时间；输出适配器必须可靠写入 `first_publish`
- **“输出适配器”是复数，三条路径都算**：RSS 渲染、Reader API 的 `stream/items/contents`、
  MCP 的 `get_item_body`。此前只有 RSS 写，于是**只用 NNW 消费的条目永远停在 `ready`**
  （实测 4576 条）—— 而不可变性规则存在的理由正是“客户端会缓存”，偏偏在客户端真正缓存的
  那条路径上失效。这是本条不变量被违反的实例，不是设计选择

## JobRun 状态机

> **已实现**：`src/core/job_state.py`，转移集中在该模块，仓储层通过它改状态而非直接赋值。
> 每条合法与非法转移都有测试。`claim` 用条件更新（`WHERE status='queued'`）保证并发下
> 恰好一个赢家；进程崩溃留下的 `running` 行由调度器启动时回收为 `timed_out`。

```text
queued -> running -> succeeded
                  -> failed
                  -> timed_out
queued -> cancelled
running -> cancelled
```

| 当前 | 事件 | 下一状态 | 规则 |
|---|---|---|---|
| `queued` | worker_claimed | `running` | 原子 claim，避免重复执行 |
| `queued` | cancel | `cancelled` | 未执行任务可取消 |
| `running` | succeed | `succeeded` | 写 metrics、结束时间 |
| `running` | fail | `failed` | 必须写 `error_class`（采集相关失败）与安全错误摘要 |
| `running` | exceeds_timeout | `timed_out` | 停止 worker；记录运行时长 |
| `running` | cancel | `cancelled` | 仅在任务实现支持安全中断时允许 |

不变量：

- 终态为 `succeeded`、`failed`、`timed_out`、`cancelled`，不可更改
- `error_class` 仅允许在 `failed`（必要时 `timed_out`）写入
- 每个 JobRun 的 `correlation_id` 必填
- 重试产生新的 JobRun，并通过 `retry_of` 关联，不得将终态 run 改回 `queued`

## Source 状态机

> **部分实现**：`active ↔ paused`、`→ disabled` 由 `PATCH /v1/sources/{id}` 驱动，
> 均为软状态、不删数据。`DELETE /v1/sources/{id}` **不是状态转换**，而是物理删除
> （源行及其全部 Item、正文、已读状态、产物、抓取记录，ADR 0021），因此没有
> `deleted` 状态：一个表示「这行其实没了」的状态无法描述一行存在的东西。

```text
active <-> paused
active -> disabled
paused -> disabled
disabled -> active       # 需用户确认
```

| 当前 | 事件 | 下一状态 | 规则 |
|---|---|---|---|
| `active` | pause | `paused` | 禁止产生新的自动调度 JobRun |
| `paused` | resume | `active` | 恢复调度策略 |
| `active` / `paused` | disable | `disabled` | 停止调度；保留内容与配置 |
| `disabled` | enable | `active` | 用户确认后恢复 |

`disabled` 的源不出现在 Reader 的订阅列表里（`subscription/list` 过滤它，
`subscription/edit` 拒绝改动它），其历史 Item 仍可读。

**删除与清空是两件事**：删除订阅 = 连内容一起删（`DELETE /v1/sources/{id}`）；
清空某源的条目但保留订阅与抓取游标 = `POST /v1/sources/{id}/items:delete`。
后者存在是因为「抓错了、清掉重来」不该丢掉 `seen_frontier`——丢了会让下一轮
重新采样历史深度。

### Item 删除（非状态转换）

Item 的 `publish_status` 仍是 `pending` / `ready` / `published` 三态，
**没有** `retracted` 之类第四态：删除是物理删除，不是可见性开关。
`DELETE /v1/items/{id}` 与 `POST /v1/items:delete` 直接删行，行不存在时
后续查询自然为空，不需要在可见性过滤里再加一个状态。

## Plugin 状态机（P2，未实现）

```text
discovered -> imported -> enabled -> disabled -> removed
                  |          |
                  v          v
               rejected   disabled
```

| 当前 | 事件 | 下一状态 | 规则 |
|---|---|---|---|
| `discovered` | import_validated | `imported` | 校验 manifest、checksum、签名/信任策略 |
| `discovered` / `imported` | validation_failed | `rejected` | 保存安全失败摘要，不执行代码 |
| `imported` | enable | `enabled` | 用户确认；确定运行隔离级别 |
| `enabled` | disable / health_failure | `disabled` | 停止新任务；不杀死不可安全中断的已运行 job |
| `disabled` | enable | `enabled` | 用户确认 |
| `disabled` / `rejected` | remove | `removed` | 用户确认；保留 AuditLog，禁止删除历史产物关联 |

## ReadState 状态模型（P1，原 P3）

> 原定 P3，随 C4（Reader API）一起提前到 P1，见 ADR 0004。

ReadState 不需要复杂生命周期；它是 Item 的可变消费侧事实：

- 不存在记录等价于 `is_read=false, is_starred=false`
- Reader API 写入使用 last-write-wins，但必须带服务端接收时间 `updated_at`
- **last-write-wins 必须是单语句原子 upsert，不能是读-改-写**。读-改-写的
  INSERT 挂到 commit 才落库，并发 `edit-tag` 期间另一个请求提交同 ``(item_id,
  tenant_id)`` 会让前者在下一个查询的 autoflush 撞上 UNIQUE 约束，**整批 500**
  （2026-09-14 线上实测，NNW 表现是"标已读偶发失效"）。WAL 会把并发写串行化，
  所以正确的写法是把竞态消解在语句内部
- 删除 Item 前必须先按保留策略归档/删除 ReadState
- ReadState 改变永远不得改动 Item 发布内容或 publish_status

## 实现要求

- 所有状态转移必须集中在 `core/` 应用服务/状态机函数内，不能分散在 FastAPI route、MCP tool 或 ORM hook
- 每条合法与非法转移都有单元测试
- 写入状态变更时同时写 AuditLog（纯内部 worker 状态变更可记录 job 事件，但用户/agent 发起的变更必须可归因）
- 对“只允许一次”的 transition 使用 DB 条件更新/乐观锁，避免并发下重复发布
