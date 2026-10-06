# 0061 · 一个 origin 多个订阅：链的归属与按订阅判定的可见性

- 状态：accepted
- 日期：2026-10-07
- issue：#372（本 ADR 是 #372 的交付物；实现见 #371，场景号段在 `SCENARIOS.md` 的 #371 行）
- 取代 / 修订：**修订 [ADR 0046](0046-work-as-a-first-class-citizen.md) §6 D-A/D-B 的阶段性安排**（拆表已完成：0026 建表、0027 切读写、0028 删镜像）；与 [ADR 0035](0035-retrieval-capability.md)（链与闸门）、[ADR 0009](0009-next-run-time-is-derived.md)（推导而不落库）同向；[ADR 0024](0024-reader-api.md)（已读在条目上、归类在订阅上）不受影响

**本 ADR 不占 AC**（决策记录，先例 #172 与 ADR 0046）：场景由 #371 承担，号段见 `SCENARIOS.md`。

## 背景

拆表之后，策略在 `subscription`（`pipeline_chain`、`target_lang`、`folder`、`schedule_policy`），
事实在 `origin`。这让「一个来源两套策略」（一个翻译、一个不译）第一次**可表达**，也让两件原本
不成问题的事变成问题：

1. **链挂在订阅上，条目挂在 origin 上**。`enqueue_chain` 现在的入参是一个 item，链取自
   `chain_for_source(subscription)`；一个 origin 有两个订阅时，「这篇新条目该走哪条链」在模型里
   没有答案。
2. **闸门是 item 级的**。`publish_status`（`pending`/`ready`/`published`）是**一行一值**，被五个读点
   （feed 两处、items_query 两处、reader 两处）与一个写点（enrichment worker）共享，SQL 形式是
   `visible_item_rows()`（`_shared.py`）。两个订阅的链完成时间不同时，「这篇条目可见了吗」也
   没有答案。

现状中的两个有利事实（读代码，非推断）：

- **enrichment 的身份已经是 `(item, plugin, kind, lang, input_hash)`**（`item_enrichment` 的唯一约束），
  所以两条链对同一 item 产出不同产物是**已经支持**的：不同 `lang` / 不同 `input_hash` 天然分行，
  同 spec 同输入则共享一行（幂等）。
- **闸门只有一个实现**（`_shared.visible_item_rows` + `core.item_state.is_visible`），
  因此把它从 item 级扩到订阅级是**一处改动**，不是六处。

## 决定

**1. 每个订阅各入其链。** 新条目入库时，按该 origin 的**每个**订阅的 `pipeline_chain` 各自入队。
两套链因此是独立的作业流：一方的失败与重试不影响另一方，一方的产物不满足另一方的上游要求
（[ADR 0030](0030-chain-dataflow.md) 的上游规则按订阅各自的链解析）。

**2. 可见性按 `(item, subscription)` 判定，且物化。** 新表
`item_subscription_ready(item_id, subscription_id, ready_at)`（PK 复合、两个外键 `ON DELETE CASCADE`），
语义是「该订阅的链对这条条目已完成」：

- **写入点**：该订阅的链完成处——与今天写 `publish_status=ready` 的**同一个执行点**，因此不存在
  「两条路径各自判断完成」的可能。
- **读取**：feed / Reader 的可见性谓词改为 `join item_subscription_ready on subscription_id`，
  热路径仍然是等值 join，不做读时聚合。
- **一致性守卫**（物化方案的固有风险是「漏写即静默不可见」）：一条测试断言**物化的 ready 集合
  等于从 enrichment 派生的集合**，并注入式验证——移除写入路径必须让守卫变红。
- `publish_status` **保留原义**（内容冻结 / 首次输出），**不再是可见性判据**。

**3. enrichment 身份足以承载两套链。** 见「背景」：身份里的 `lang` 与 `input_hash` 已经把两条链的
产物分开，因此不需要给 `item_enrichment` 加 `subscription_id`——那会把「产物属于哪条链」变成
存储问题，而它本来就是**推导**问题（产物由 spec 与输入决定，不由订阅决定）。

## 被否决的备选

- **读时派生可见性**（不建表：读时 EXISTS over enrichments）。单一事实源更纯，符合 ADR 0009/0046
  的折叠倾向，但代价落在**最热的路径**上：每次 feed / Reader 读都要把「该订阅链的每个 spec 都已
  完成」算出来，且必须新增查询计划测试来守住 issue #77 那条线。
  **否决理由**：物化把成本放在写一次、读多次的正确一侧；而它的风险（漏写）可以用一条注入式
  守卫关掉，读时派生的风险（热路径退化）只能用更多索引和更多测试换。
- **只按「主订阅」入链**（第二订阅共享第一订阅的处理结果）。最省事，但「主」没有领域依据——
  两个订阅是平等的策略，把其中一个叫主线是把实现顺序写进模型；而且它丢掉了「一个翻译一个不译」
  的一半价值，也就是拆表要支持的那件事本身。
- **闸门仍全局**（任一链先完成即全局可见）。改动最小，但语义是错的：它会把 A 频道尚未处理的
  条目先发给 B 频道，而 B 的读者看到的内容与 B 的策略无关——「看不到就先别发」这条既有不变量
  （ADR 0035）会被削弱成「总有一条链先跑完」。
- **给 `item_enrichment` 加 `subscription_id`**。见决定 3：它把可推导的事实落成第二份存储。

## 影响

- **迁移**（#371）：建 `item_subscription_ready`；删 `UNIQUE(tenant_id, origin_id)`
  （「一个 origin 一个订阅」是 1:1 阶段的规则，不是领域规则）。
- **读路径**：#371 把 feed / Reader 的可见性谓词切到新表的 join；`visible_item_rows`（现为
  `publish_status != 'pending'`）在**订阅作用域内**仍然有效，因此它可以继续服务「内容冻结」，
  但读路径不再以它作为可见性判据。
- **`docs/domain/data-model.md`** 的 ItemEnrichment 与「发布闸门」两节记录本决定。
- **不变**：已读/星标仍按条目同步（ADR 0024）；`schedule_policy` 与「下次何时抓」仍按订阅推导
  （ADR 0009）；enrichment 的幂等身份不变。
