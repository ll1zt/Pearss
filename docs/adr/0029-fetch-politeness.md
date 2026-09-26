# 0029: 抓取礼貌——per-host 限速、单轮上限与冷却

Status: accepted
Date: 2026-09-24
Context: issue #35。2026-09-24 实测事故：为一个 122 篇文章的博客源启用 fulltext 链后，
122 条 enrichment 行同时入队，worker 以并发 3、每轮 60s 的节奏**连续抓取该站 122 个页面**。
用户指出「不应该直接放量抓取 blog，这点不够礼貌」，随后立即摘链并清队列（处置记录在 issue）。

## 问题

处理器链的模型里没有「这个请求打向谁」的概念：链上每个 spec 按条目建行，
抓取的节奏完全由 worker 的并发与 sweep 周期决定，与目标站点无关。对
`translate` 这类调用 operator 自选 provider 的处理器这没问题；对 `fulltext`
这类抓**条目自身 URL**（第三方站点）的处理器，它就变成了对别人博客的突发流量。

## 决策

### 1. 限速在宿主侧实施，不在处理器内

处理器运行在**每条一行、用完即弃**的沙箱子进程里（`app/processor_runtime`），
进程内计数器与时钟跨行不存在，天然无法限速。只有 worker 进程看得见整个队列，
所以状态与判定都在那里（`app/fetch_pacing.py` 的 `HostPacer`）。

状态在内存中：重启丢失限速簿记，远小于一次未限速突发的代价，且不需要 schema。

### 2. 三道边界，都以**目标 host** 为键

| 边界 | 作用 | 默认 |
|---|---|---|
| 最小间隔 | 同一 host 两次抓取之间的最小间隔（等待而非丢弃） | 2s |
| 单轮上限 | 一次 sweep 对同一 host 最多派发 N 条，超额留队列 | 2 条 |
| 冷却期 | host 返回 blocked（403/429）后，跳过该 host 一段时间 | 900s |

间隔的落点是**抓取前**（`HostPacer.pace()`，在即将发请求的线程里），
不是认领时：认领是单线程的（适合放上限与冷却判定），但真正的请求发生在
线程池里，只在认领时等待会让同一 host 的多个请求仍然同时出去——这一点是
测试先发现的（未修正版本实测相邻请求间隔 0.02s）。

三道边界之外还有一条**并发度**约束，本表未涵盖，由 ADR 0033 定义（2026-09-26）：
同一 host 同时只允许一次抓取在途。原因是冷却期只能在**抓取返回后**才置位，而单轮
上限会把同一 host 的多行一起放行——被拒的对端响应慢时，第二条会抢在冷却期之前发出
（issue #57：延迟 3s 时实测 2 次请求而非 1 次）。ADR 0033 只补这一条不变量，不改本
篇的间隔／上限／冷却三项取值与语义。

### 3. 只约束「抓第三方站点」的处理器

`PACED_PROCESSORS = {"fulltext"}`，是白名单而非「所有处理器」。`translate`
调用的是 operator 自己选、自己付费的 provider，按条目 host 限速它等于用
错误的理由节流（同一条目可以来自任何 host）。将来出现抓取型处理器时加入该集合。

### 4. `:backfill-chain` 默认保守，且可继续

`limit` 默认 10（`PEARSS_BACKFILL_DEFAULT_LIMIT`）。`limit` 约束的是**本次入队行数**，
不是「扫描哪些条目」——后者会让重复调用变成空操作（最新的 N 条已入队，计数恒为 0，
积压尾部永远够不着），这一点由测试钉住（`test_user_backfill_queues_a_watchable_batch...`）。

### 5. 不读 robots.txt（显式否决）

实测四个目标站的 robots.txt 全部是 `Disallow:`（全允许），本次伤害来自**请求速率**
而非路径许可。为每个 host 额外抓取并缓存 robots.txt，会引入一个必须同样受限的
第三方请求，把「礼貌」变成「更多请求」；而它不能替代速率限制。需要路径级许可的
operator 可在源层面选择不订阅。此决定记录在此以便日后回溯。

## 影响

- `app/fetch_pacing.py`（新）：`HostPacer`、`host_of`、`backfill_default_limit`
- `app/enrichment_worker.py`：认领时 `reserve()`（上限/冷却），抓取前 `pace()`（间隔）；
  blocked 分类 → `record_blocked(host)`；被推迟的行**不消耗尝试次数**（回队列而非计一次失败）
- `app/sources_routes.py`：`:backfill-chain` 新增可选 `limit`，默认取配置
- `adapters/repository/enrichment.py`：`limit` 语义修正为「入队行数」
- `.env.example` / `tests/unit/test_env_allowlist_coverage.py`：四个宿主侧变量登记
- 观测：推迟时记 INFO（host、原因），阻塞时记 WARNING（host、冷却时长）

## 验收

`tests/integration/test_fetch_politeness.py`（真实 SQLite + 真实子进程 + 记录到达时刻的
本地 origin，零真实站点请求）：

- AC-1：5 篇积压 → 单轮最多 2 条、相邻抓取间隔 ≥ 配置值、全部条目最终完成
- AC-2：host 返回 429 → 命中的行退避，该 host 进入冷却，下一轮不再触碰
- AC-3（`test_sources_api.py`）：默认批次 3 条、重复调用推进下一批、显式 limit 覆盖默认
