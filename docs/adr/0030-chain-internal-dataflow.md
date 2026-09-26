# 0030: 处理器链内的数据流——后序处理器消费前序产物

Status: accepted
Date: 2026-09-26
Context: issue #34。kxxt 等源配了 `[fulltext, translate]`，但读者看到三层互不匹配的
内容：**译文是原始摘要的译文**（50~64 字符），下面才是 4000+ 字符的全文（原文语
言），再下面是摘要。ADR 0018 定义链的执行模型时，每个 spec 都读条目原始正文
（`item.content_ref`）——链只有「顺序」语义，没有「流通」语义。**注意：0018 并未
explicit 写下一句「每个处理器只能看到 Item 原始内容」，它只是没有定义 spec 之间的输入
通道，而代码把每个 spec 的输入都取成了 `item.content_ref`。** 因此本 ADR 是**补充**
0018 的空白（链内数据流），不是推翻它的某句话。

## 决策

### 1. 后序 spec 的输入是最近一个前序产物，按链位置决定

`upstream_for(chain, spec)` 返回该 spec 之前**最近一个可消费产物**的 spec；没有则
返回 None（读条目原始正文）。纯函数，`core.enrichment` 内，无需 I/O。

选择「位置」而非「kind 配对表」：`[fulltext, summary, translate]` 中 translate 消费
的是 summary，而不是 fulltext。加一个中间 spec 时行为显而易见地改变，不需要维护一
张 kind→kind 映射表——映射表的失败模式是静默继续送错输入。

### 2. 可消费性按 kind 声明，且 translation 不可消费

`CONSUMABLE_KINDS = {fulltext, summary}`：

- `translation` **明确排除**。译文里的正文已经是目标语言，再消费它既是一次白花钱的
  no-op（处理器会判 `is_already_in` 直接产出空产物），又让「翻译链」具备自我喂养能
  力。排除它等于同时堵住这个循环。
- `tts_audio` 是产物不是文本输入，排除。
- 新增 kind 时必须显式加入此集合——默认不可消费，避免「新 kind 自动成为别人的输入」
  这种意外。

### 3. 依赖是**记录**的，不只是遵守的

`item_enrichment` 新增两列（migration 0021，纯 ADD COLUMN）：

- `upstream_plugin_id`：产出该输入的插件
- `upstream_digest`：该产物的身份 = `cas://<sha256>` 引用的摘要段

两者同时为 NULL 是**一等状态**，含义是「读的是条目原始正文」——所有单 spec 链以及
本次改动之前的全部历史行都是这个状态。空串不能代替 NULL：那会变成「依赖了空」。

记录而不只是遵守，是因为「上游产物变了」这个判断只有记录能回答。两种不记录的做法
都会坏：每次 sweep 全量重算（每次真金白银调 provider），或永不重算（译文永远停在
旧文本上）。

### 4. 上游未产出时**等待**，绝不降级用原始正文

claim 时与执行时都检查上游产物是否已存在（同一 `input_hash`）：

- 不存在 → 行**原样退回 queued**（不消耗 attempts、不记失败），下一轮 sweep 再试；
- 存在 → 读它，作为输入。

**不允许**「上游没有就先用原始摘要兜着」——那正是本 issue 要消灭的三层不匹配。上游
失败时后序行保持 queued、条目保持 pending，失败可见。

推论：**一轮 sweep 只推进链的一个位置**。这不是缺陷而是规则本身——产物必须先存在，
消费者才可能跑。worker 循环（默认 60s 一轮）是天然驱动。claim 阶段曾有一份「按上游
就绪过滤」的重复实现，注入式验证证明它的移除**没有任何测试会变红**（执行时守卫产生
完全相同的可观测结果），因此按「不测试冗余」删除。

### 5. 上游解析按版本钉住，不取「最新」

`upstream_product_ref(..., content_hash=row.input_hash)`：产物必须属于**当前正在处理
的那个条目版本**。取「最新成功产物」会把上一版的全文喂给这一版的 translate——跨版本
串味，也正是依赖列要防的失效模式。条目正文修订后，管线按新 content_hash 重新入队整条
链，新行消费新产物；旧行作为历史保留（产物 pin 在版本上，见 ADR 0018）。

### 6. 不拓宽唯一约束

`uq_enrichment_product` 保持 `(item, plugin, version, kind, lang, input_hash)`，不把
`upstream_digest` 并进去。`input_hash` 标识的是「这条链作用于哪个条目版本」，而该表的
契约是「同一内容版本重处理 = **替换**该行，不堆积副本」（见 `db_models.ItemEnrichment`）。
上游版本变化是对既有行的依赖记录更新，不是新的产物身份。这个选择让 migration 0021
只是两个 ADD COLUMN（可 upgrade/downgrade，无表重建、无数据删除），代价是同一
`input_hash` 的既有行必须能容纳输入变更——这由「重处理即替换该行」的既有契约覆盖。

## 对照 ADR 0018

0018 定义的机制全部保留：链挂在 Source、闸门按 required spec 判、产物不回写 Item、
`input_hash` 幂等、失败不自动重试、Reader 合并视图。0030 只补上 0018 未定义的
**同链内 spec 之间的输入通道**，所以 0018 的 Status 保持 accepted，不被标记
superseded；两份 ADR 合起来读即完整语义。

Reader 输出层次（0018 §3）不变：译文 → 全文 → 摘要，只是现在这三层真正互相对应。

## 已否决

- **按 kind 配对表指定输入**：需要维护，且加中间 spec 时静默送错。位置语义更小、更明显。
- **把上游产物内联进 `item_ctx` 复用同一个 `content` 字段**（即不区分来源）：行无法
  记录自己消费了什么，失效判断只能靠猜。显式列 + 显式 item_ctx 字段把来源摆到明处。
- **上游失败时降级用原始正文**：见决策 4。
- **拓宽唯一约束以容纳上游变更**：见决策 6；代价（表重建 + downgrade 删数据）与收益
  （同一 input_hash 的双上游产物，现实中不存在）不匹配。
- **让 translation 可消费**：自我喂养循环，且必为 no-op。

## 影响

- `core/enrichment.py`：`CONSUMABLE_KINDS`、`upstream_for`。
- `adapters/db_models.py` + migration 0021：两个可空列 + 一个复合索引
  `(item_id, upstream_plugin_id)`。
- `adapters/repository/enrichment.py`：`upstream_for_spec`（spec + 产物 ref，按版本钉住）。
- `app/enrichment_worker.py`：`_resolve_upstream`、`_digest_of`；成功写入与 payload
  同一条 UPDATE 记录依赖；上游未就绪时退回 queued。
- 处理器契约不变：`process(item_ctx, target_lang, store)`，`item_ctx` 新增
  `upstream_digest` 字段（无上游时为空串）。处理器无需感知上游是谁——这正是它保持
  可替换的原因。
- 存量行：`upstream_*` 为 NULL，语义仍正确（它们读的确实是条目正文）。要让存量译文
  改吃全文，走 `backfill`/`retry_enrichments` 重排队，与本 ADR 无关。

## 验收

`tests/integration/test_chain_dataflow.py`（AC-14..AC-18，本文件头即登记表）、
`tests/unit/test_enrichment_core.py`（规则真值表）、
`tests/e2e/test_client_sync_journey.py::test_j11`（真插件 + 真 provider + 真 Reader）。
注入式验证：把下游改回读 `item.content_ref` → 5 个 integration 测试与 j11 变红
（j11 的红证据直接指出 provider 被喂的是摘要原文 `"No publication date on this one."`）。
