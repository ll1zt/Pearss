# 0009 · 调度时间从策略与历史推导，不引入 APScheduler

- 状态：accepted
- 日期：2026-09-04
- 取代：`architecture-spec.md` §9 的「任务队列 = APScheduler + DB JobStore」一行、§8 相关决策

## 背景

§9 原定 P1 用 **APScheduler + DB JobStore**，演进到 P3 换 Dramatiq + Redis，理由是「换触发器不动 Job 模型」。

实现 A4 时发现一个冲突：APScheduler 的调度模型是**存储下次运行时间**（`apscheduler_jobs.next_run_time`），而本项目已有的 `core/scheduling.py` 是**纯函数推导**：

```python
next_interval(policy, observed_gaps, consecutive_failures) -> 间隔
```

两者回答同一个问题 —— "下次什么时候抓" —— 但答案存在不同的地方。同时用就是**两个事实源**。

## 决策

不引入 APScheduler。每轮 tick 从 `source.schedule_policy` + `job_run`/`item` 历史**现算**到期集合，不存任何 `next_run_time`。

`job` 表保留（每个源一条 `type='collect'` 的声明），`job_run` 记录每次执行 —— 但 `job.schedule` 列**故意留空**：该行声明"抓什么"，不声明"何时抓"。

## 理由：两个事实源什么时候会不一致

刚抓完那一刻两者一致。之后任何变更都会分叉：

| 场景 | 推导 | 存表 |
|---|---|---|
| 用户把 `fixed 30m` 改成 `fixed 5m`（14:00，上次抓 13:50） | 14:01 算出已到期 → **立刻抓** | 仍写着 14:20 → **白等 19 分钟**，除非有人记得 `reschedule_job()` |
| 抓取失败（`blocked`） | `consecutive_failures` → 间隔自动翻倍 | 时间纹丝不动，除非显式重新调度 |
| **从备份恢复数据库** | 历史恢复 → 调度自动正确 | `apscheduler_jobs` 与 `raw_fetch` 历史可能来自不同时间点，**矛盾时无从判断哪个对** |

第三条最重。§7.5 规定派生物可通过重放管道重建、且要求每季度恢复演练 —— 而**存下来的调度时间本质是"派生数据伪装成主数据"**，正是备份分级要避免的东西。

### APScheduler 的价值在推导模型下大部分消失

它最有名的能力是 **misfire handling**（进程停机错过时间，恢复后怎么办）。停机 3 小时后重启：

- **推导**：`is_due(3小时前, 30分钟)` → True → 马上抓。**这就是正确答案，免费得到**
- **APScheduler**：需要配 `misfire_grace_time`，超时就跳过 —— 是在**调参逼近**推导天然给出的结果

`coalesce`（睡醒别跑 5 次）同理：推导模型里抓完历史就更新了，天然只跑一次。

也就是说：**APScheduler 能力清单里的每一条，几乎都是在解决"我们选择存时间"带来的问题。**

## 实现要点

### 时钟必须贯穿

`collect_now` 最初用真实挂钟写 `finished_at`，而决策用注入的 `now`。两者不一致时：注入 `now=2026-06-01` 而挂钟是 2026-09-04 → elapsed 为负 → 触发"时钟偏移视为到期"规则 → **该源永远到期，退避完全失效**。

三个测试同时失败才暴露这一点。现在单一 `moment` 贯穿整个 run 的时间戳。

### claim 用条件更新

`queued -> running` 是条件更新（`WHERE status='queued'`），两个调度器同时 tick 时恰好一个 `rowcount==1`。没有它，同一个源会被并发抓两次。

### 每源独立 try

一个源的坏 policy（如 JSON 损坏）不能终止整轮 tick —— 否则表现为"调度器安静地不干活"，这是最坏的失败模式。

## 偏离 §4 的一处

§4 说「按 RawFetch 历史计算源的平均更新间隔」。但 RawFetch 记录的是**我们何时抓**，不是**源何时发布**。改用 `item.published_at` 的间隔：

- 直接度量策略真正想要的东西（源产出内容的频率）
- **不被我们自己的抓取频率扭曲** —— 用抓取间隔推抓取间隔会形成反馈回路：抓得少 → 看到的更新少 → 抓得更少

## 后果

- 改 policy、失败退避、恢复备份都自动自洽，无需任何 reschedule
- 零新依赖（不引入 apscheduler）
- 自己维护约 200 行调度逻辑（tick 循环、到期判定、并发保护）
- **P3 若引入 Dramatiq 做处理器链**，那是"任务分发"而非"何时抓"，与本决策不冲突；届时可另立 ADR

## 可逆性

可逆但成本中等：若要改用 APScheduler，需要新增存 `next_run_time` 的表、并在每次 policy 变更与失败后退避时显式 reschedule —— 也就是重新制造本 ADR 要避免的那类不一致。

触发重估的条件：出现大量需要精确到秒的定时任务，或需要跨进程分布式调度（单进程 tick 模型不再够用）。
