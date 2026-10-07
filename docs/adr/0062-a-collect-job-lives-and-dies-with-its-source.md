# 0062 · collect job 与它的源同生共死

- 状态：accepted
- 日期：2026-10-07
- issue：#374（本 ADR 是 #374 的交付物）
- 相关：[ADR 0021](0021-deletion-semantics.md)（删除语义）、[ADR 0009](0009-next-run-time-is-derived.md)（下次抓取时间从历史推导）、
  [ADR 0046](0046-work-as-a-first-class-citizen.md)（作业与账本）

**本 ADR 不占 AC**（决策记录，先例 #172 与 ADR 0046）：场景由 #374 承担，验收判据见 issue。

## 背景

`job` 表与 `origin` 表之间**没有外键**：`job.config` 是一个字符串（历史上存 `source.id`），
`job.type == 'collect'` 的行就是「这个源在被采集」这条声明。它没有外键是历史原因，但后果是
结构性的——**删除源时没有任何东西会级联到它**。

`delete_source()` 删 `item` → `raw_fetch` → `origin`，不碰 `job`。而 `ensure_collect_job()`
是按 `config == source_id` **查已有行**的（幂等，因为它在源创建路径上被调用）。两件事合起来是：

```text
删源  → job 行留下（孤儿）
重订阅同一来源（同 canonical_url → 同 origin id）
      → ensure_collect_job 查到孤儿 → 复用它的 id
      → 新订阅继承旧生命周期的 job_run 历史
```

继承历史不是外观问题。自适应策略的 `last_collect_at` 与 `consecutive_failures` **是从
`job_run` 推导的**（`adapters/repository/scheduling.py`，ADR 0009 的同一条思路：推导而不落库），
所以一个刚订阅的源会带着上一次生命的失败计数与退避开始。观测到的一次实例：94 条 job / 93 个源，
两条孤儿 `job-cdf79e5a…`、`job-f8015736…`；同一 URL 重新订阅时新源直接落在旧 job 上。

此外 `list_jobs` 的条数因此永远对不上源数，且当时没有任何入口能清理这些行。

## 决定

**A. collect job 随源一起删除。**

1. `delete_source()` 删除该源的 collect job（`config == source_id`），并在回执里报告
   `jobs` 计数。`job_run.job_id` 是 `ON DELETE CASCADE`，所以该 job 的 run 历史一并消失。
2. 重订阅同一来源**不继承**旧 run 历史：`ensure_collect_job` 查不到行，于是新建 `job-<source_id>`，
   `last_collect_at` 为 `None`、`consecutive_failures` 为 0。
3. 在既有维护节拍（`prune_every`）上增加一趟清扫：删除 `config` 已不对应任何 origin 的 collect job，
   用于本决定之前留下的行（以及任何绕过 faÃ§ade 的写入）。清扫**删除**而不是标记，理由见下。

## 被否决的备选

- **B. 保留诊断历史：`status='retired'`，`list_jobs` 默认不列、`ensure_collect_job` 不复活。**
  语义上诱人（run 历史是诊断），但 id 是**派生值** `job-<source_id>`：retired 行占着这个 id，
  重订阅要拿到「全新 job」就必须改 id 方案（或允许同 id 两行共存），那是 schema 级改动。
  而 job 是**机械结构**不是内容：它不承载条目、正文或已读状态，保留一份「某个已不存在的源的
  执行历史」对诊断的价值低于它作为第二事实源的代价（与 ADR 0009 拒绝落库 `next_run_time` 同向）。
- **C. 只提供清理入口**（`DELETE /v1/jobs/{id}` 或一次性 CLI）：孤儿能被手工收拾，但
  **bug 本身还在**——重订阅仍会复用孤儿 job 并继承旧历史。修症状不修因果。

## 影响

- 退订/删源之后，该源的采集声明与其执行历史一起消失；`list_jobs` 的条数与源数重新对得上。
- 「新订阅带旧退避」这一类故障不再可能：退避的唯一来源是**本生命周期**的 run 历史。
- 与 #373「退订 ≠ 销毁」一致：job 是**策略侧**的声明（「这个源在被采集」），
  所以它随订阅一起消失；**内容**（item / 正文 / 已读）的销毁仍是显式且要理由的另一件事。
- 清扫是删除操作，所以它的判据是「`config` 不指向任何 origin」，而不是「看起来旧」；
  它只在维护节拍上跑，并在删除时记一条 warning（计数可审计）。
