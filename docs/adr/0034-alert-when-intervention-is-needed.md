# 0034: 需要介入时主动告警——复用部署侧 notify 网关，边沿触发

Status: accepted
Date: 2026-09-27
Context: issue #4。一次 **56/56 源全失败**的中断从发生到被发现持续了**至少 15 小时**。
期间 `GET /v1/system/snapshot` 的 `recent_run_success_rate` = 0.0、`pearss_read_pearss_health`
也是 0%——**数据一直在那里，只是没有任何东西会主动喊一声**。本 ADR 定义什么时候喊、喊什么、
以及为什么复用部署侧已存在的通知网关而不是自建一条出网通道。

## 决策

### 1. 复用部署侧的 notify 网关，不自建 webhook 格式

部署侧已有统一通知入口（`host-config` 的 `docs/manuals/notify-gateway.md`）：
`POST /notify`（或全主机可用的 `notify` CLI）→ Traefik（ipAllowList + 60/分/IP 限流）
→ telegram-bot（**唯一持有 Telegram token 的地方**）→ 私聊。

pearss 侧只读一个环境变量 `PEARSS_ALERT_URL`（默认取部署已注入的 `NOTIFY_URL`），
按该网关的契约 POST 纯文本。理由：

- **token 不在 pearss**：网关存在的意义就是让各服务不必持有凭据。pearss 自己发 Telegram
  就等于把 bot token 复制到第二个服务、第二个日志面、第二个备份面。
- **重试/超时/退避已经处理好**（手册陷阱 73/74）：pearss 不该重写一遍。
- **限流在网关侧**，pearss 侧只需保证不轰炸（决策 4）。
- 通知是 best-effort：`202` 只表示已入队。这一点写在这里，避免以后有人把它当送达保证。

配置缺失（`PEARSS_ALERT_URL` 空）时**静默不告警**并启一条**一次性 warning**：这是
opt-in 能力，未配置不等于故障。

### 2. 边沿触发：只在状态转换时发

`OK -> DEGRADED` 发一条，`DEGRADED -> OK` 发一条，其余 tick 不发。

56 个源同时失败时，逐源告警会发 56 条——人会把通知关掉，于是**真正的全局故障反而静默**。
这与 issue 里「只在状态转换时发」的判断一致，也与 gateway 侧 `silent` 的用途区分开：
静音是降噪，边沿触发是消除噪音源。

状态是**进程内**的（一个布尔 + 上次发送时刻），不落库：它是「是否已经喊过」的
派生态，落库会与 DB 可用性耦合，而 DB 挂了恰恰是最该喊的时候。

### 3. 什么算「需要人工/agent 介入」（DEGRADED 的定义）

只报**需要人动手**的事，不报「有失败发生」：

| 条件 | 为什么需要介入 |
|---|---|
| 最近 N（默认 50）次 run 成功率 < 阈值（默认 0.5），且 considered ≥ 最小样本（默认 10） | 共享依赖故障（代理/凭据）或多源同时坏。issue 的实例：`network` 377 条集中在同一分钟 |
| 某源的 enrichment 行**全部** `failed`（尝试次数耗尽） | 重试策略到此为止，只有 `retry-enrichments` 能救；`pending` 条目会永远卡住 |
| 后台循环不存活（scheduler / enrichment worker） | 采集或处理整个停了。`/healthz` 已能报 503，但**没人拉**就等于没有 |

**最小样本**是必要的：`recent_run_success_rate` 在只有 1 次 run 时要么 0.0 要么 1.0，
用 0.5 阈值会在启动瞬间误报。

### 4. 有界：MIN_INTERVAL 之内不重发

同一个 DEGRADED 状态在 `PEARSS_ALERT_MIN_INTERVAL_SECONDS`（默认 3600）内不重发。
这让「长时间中断」不会变成每分钟一条。恢复通知不受该间隔限制——恢复是要立即知道的事。

### 5. 告警体带已算好的聚合，且不含凭据

正文含：成功率与样本数、主错误分类与计数、首个失败时间。issue 的实例里，
「全部 network + 全部落在同一分钟」这条信息**立刻**把方向指向共享依赖，而不是逐个源排查。

红线（AGENTS.md）：正文不得含 token、Cookie、Authorization、完整私有 URL。因此
- 只发**计数与分类**，不发源 URL；源用 id 标识（agent 可据此调 API 查详情）
- 错误详情按 issue 要求也**不发原文**（可能含凭据）——只发分类与计数
- 发送前用一份固定字段构造正文，不做「把异常 repr 拼进去」这种会泄漏的事

### 6. 发送失败绝不打断采集

告警路径的异常被吞掉并记 warning。**通知是旁路，不是采集的一部分**：
因为告警发不出去而让一次 tick 失败，等于给系统加了一个新的故障模式。
这与 `notify --strict` 的默认行为一致（默认不影响调用方）。

### 7. 退避期间的周期性日志（issue 的判据之一）

源处于退避期时，每次评估都打一条 info：

```
source <id> failing: N consecutive failures, last class <x>, next probe at <t>
```

现在 `evaluate_and_collect` 在 `not decision.due` 时直接 `return "skipped"`，把 decision
丢掉，所以 journalctl 里「退避中」与「一切正常」渲染得一模一样——issue 的原话是
「让 journalctl 不再把这两者渲染得一样」。修法是让该路径在返回前记录 decision
里已有的 `consecutive_failures` / `error_class` / `next_due_at`（这些字段已存在，
不新增事实源）。

**只在 `consecutive_failures > 0` 时打**，否则每个未到期的健康源每个 tick 都打一条，
日志量随源数增长而变成噪音。

## 已否决

- **pearss 自己持 token 直发 Telegram**：见决策 1（凭据扩散）。
- **引入 ntfy/Bark/Slack 专用 SDK 或自建 webhook 格式**：网关已是统一入口，多一种格式
  多一份维护与一处凭据面。需要新通道时改网关，不改每个服务。
- **每次 tick 都发（水平触发）**：见决策 2，会导致通知被关掉。
- **落库保存告警状态**：见决策 2（与 DB 可用性耦合，而 DB 故障正是要告警的情形之一）。
- **把告警状态放进 `/healthz` 让外部轮询**：`/healthz` 只回答「进程与循环活着吗」，
  它被设计为无 DB 依赖的探针。把聚合健康塞进去会让探针与 DB 耦合，且需要外部监控
  才能变成「有人知道」，而 issue 的教训正是**不能依赖有人去看**。
- **逐源告警**：56 条淹没真正的信号。
- **把错误详情原文放进告警体**：可能含凭据/私有 URL（红线）。

## 影响

- 新增 `src/core/alerting.py`（纯函数：从聚合快照判定状态 + 构造正文，无 I/O，
  守分层规则）；`src/adapters/alerting.py`（HTTP 发送，读 `PEARSS_ALERT_URL`）；
  scheduler tick 结束后接线。
- 新环境变量：`PEARSS_ALERT_URL`、`PEARSS_ALERT_SUCCESS_RATE_THRESHOLD`、
  `PEARSS_ALERT_MIN_RUNS`、`PEARSS_ALERT_MIN_INTERVAL_SECONDS`。写入 `.env.example`
  与 `env-reference.md`；env allowlist 守卫中声明为 **host-side**（父进程发送，
  沙箱子进程不该看到它，也不需要）。
- 退避期日志：`scheduler.py` 的 `evaluate_and_collect` 在 `not decision.due` 分支
  记录 decision（不新增 DB 查询）。
- 无 migration；无 API/MCP 面变化（告警不出现在任何响应里）。
- 部署侧：需在 pearss 的 unit environment 里提供 `NOTIFY_URL`（或在 sops `pearss-env`
  里给 `PEARSS_ALERT_URL`）。本 PR 不含 host-config 改动，单独一个提交/PR 在那边。

## 验收

见 `tests/integration/test_alerting.py` 与 `tests/unit/test_alerting.py` 的场景表。
注入式验证：去掉边沿触发 → 重复告警测试变红；去掉 MIN_INTERVAL → 轰炸测试变红；
去掉最小样本 → 单次失败误报测试变红；日志仅在 `consecutive_failures > 0` → 健康源
无该日志。
