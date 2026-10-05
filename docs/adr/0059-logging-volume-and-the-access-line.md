# ADR 0059：日志量规则与访问行——INFO 事件驱动、一行一请求、uvicorn.access 退役

- 状态：accepted
- 日期：2026-10-06
- issue：#297（规则）、#294（访问行）、#313/#315（限速器接线）、#316（崩溃行）、#321（本 ADR 的交付物）
- 相关 / 修订：**修订** [ADR 0049](0049-child-stderr-is-a-log-channel.md) 决定 3：重记的处理器行除级别与
  `[plugin_id item_id]` 前缀外，还带 `correlation_id`（宿主在 spawn 时捕获一次）。与
  [ADR 0017](0017-*.md) 不冲突：id 在宿主侧捕获，插件协议未变。与
  [ADR 0016](0016-*.md)（对外暴露硬化）相邻：访问行是它的观测面。

**场景**：AC-284..285、AC-290..292（见 `SCENARIOS.md`；本批其余场景由各自的修复分支登记，
本 ADR 记录的是它们共同的长期约束）。

## 背景

单用户 feed 系统的 journal 是排障的**唯一事实源**，但它也是共享资源：一条反复失败的源、一个
重试的客户端、一条中文时间线，都能在默认级别把 journal 填满，把真正的信号挤出去。

2026-10 的可观测性批次（#290..#298）把大量明细降到 DEBUG，并把「INFO/WARNING 事件驱动且
O(1)」写进了 `docs/operations/runbook.md`。随后的 review 发现规则**没有机械约束**：

- 生产路径上仍有多条 O(N) INFO——逐条 draft 的「已是目标语言」、逐行 enrichment、退避源
  每 tick 一行；其中存储路径那条被 AC-292 的测试「证明」不存在，因为该测试**没传 chain**，
  驱动不到那条分支；
- 为高频路径造的 `adapters.log_limits.rate_limited_logger` 没有任何生产调用点；
- uvicorn 自己的 access log 与应用访问行各写一行；
- 访问行在两类请求上缺失：未捕获异常的 500（Starlette 在中间件**外层**应答）与外侧中间件
  拒绝的请求（413/401/403/429）。

## 决策

1. **INFO/WARNING 事件驱动且 O(1)**：每个作业、每次失败 O(1)，不随 poll / tick / item 增长；
   周期性、空转、成功但无事件的路径一律 DEBUG。判据落在**输入规模**上，因此验证必须驱动真实
   规模（含 chain、含队列、含退避），只跑空路径等于没测。
2. **访问行是「每请求一行」的契约**：含 correlation id、method、route 模板、status、actor、
   duration_ms；读 2xx 为 DEBUG，写与 ≥400 为 INFO。契约意味着**不许有缺口**：未捕获异常的
   500 由中间件自己在异常路径补记，外侧中间件的拒绝同样要留下记录。
3. **静掉 `uvicorn.access`，由应用访问行替代**。
4. **不可信文本进日志前必须转义/脱敏**：请求路径、work target、外部 URL 都可能是攻击者选定
   的字符串；控制字符会断行伪造记录，query / userinfo / fragment 可能是凭据。实现单一
   （`adapters/log_safety`、`adapters/redaction`），由结构守卫钉住「只有一处」。
5. **重复告警用限速器**：同一 key 每窗口一行 + 窗口滚动后补一行 `suppressed N`（级别与原行
   一致，DEBUG 路径的汇总不升到 INFO）。限速器必须**有生产调用点**，否则就是文档在撒谎。

## 后果

- journal 体积与输入规模解耦：一次请求一行、一次作业一行，可用 correlation id 串起审计行与
  作业；日志因此可以安全地贴给第三方。
- 逐条明细只在 DEBUG。运维看 per-item 细节要临时开 DEBUG——这是刻意的取舍，用体积换细节。
- 限速器的 suppressed 汇总是**惰性**的（窗口滚动后要等该 key 再次说话）。进程在窗口内退出会
  丢计数：未在 shutdown 接 `flush()`，因为限速器既有模块级实例（scheduler）也有每 app 实例
  （hardening），没有单一收口。记录为已知限制。
- 每个新增日志点都要回答两个问题：**什么级别**、**会不会随输入增长**；测试要驱动真实规模。

## 被否决

- **给 `uvicorn.access` 挂 Filter 或自定义 format 来降噪**：级别门在 Filter 之前，Filter 无法
  按请求降噪；它也拿不到 route 模板、actor 与 correlation id。AC-285 的「读在 INFO 静默、5xx
  在 INFO 一行」在该方案下不可实现，所以是替换而不是过滤。
- **把「退避中的失败源」和「未认证探测」直接降到 DEBUG**：前者会让一个持续三天的坏源在默认
  级别完全消失；后者会让未认证洪水**静默**而不是**安静**（访问行尚未覆盖外侧中间件之前，
  那行是该流量唯一的记录）。两者都改为限速，而不是降级。
- **把访问日志移到最外层以覆盖外侧中间件**：需要把 correlation 中间件移到最外层，而它是
  审计行与 actor 的来源，改动会牵动 ADR 0016 的「限速器在最外层」排序与认证失败的审计语义。
  本批未做；缺口本身记在 issue #317。

## 参考

- `docs/operations/runbook.md`「日志量规则」
- `src/app/log_setup.py`、`src/app/middleware.py`、`src/app/scheduler.py`、`src/app/hardening.py`
- `src/adapters/log_limits.py`、`src/adapters/log_safety.py`、`src/adapters/redaction.py`
