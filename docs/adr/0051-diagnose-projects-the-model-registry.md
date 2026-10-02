# ADR 0051：`:diagnose` 的模型健康格是登记表的只读投影

- 状态：accepted
- 日期：2026-10-02
- issue：#231（本 ADR 是 #231 的交付物）
- 相关 / 修订：ADR 0048（登记表在 worker：本条是它的出口）、ADR 0036（诊断面是聚合端点）、ADR 0042（告警/诊断的归因字段边界）、ADR 0009（派生状态不落库）

**新增场景从 **AC-233** 起**（AC-233，见 `SCENARIOS.md`）。

## 背景

#228 之后，"哪个模型在冷却、为什么"只活在 worker 的**内存**里：读 journal 能看到日志（#229
之后）✓，但"此刻这条链为什么慢"没有一个可调用面。而 `:diagnose` 本来就是为这个问题存在的
（它的 docstring 第一句就是 "why is this source not updating?"），只是那时还没有模型维度。

## 决策

1. `:diagnose` 增一格 **`model_health`**：`preferred`（下一行会先问谁）、`skipped`（当前被跳过的
   模型）、`attempts_per_model`（行内预算），以及每个**当前在冷却**的模型的
   `cooling_seconds` / `cooldown_seconds`（本次这一档冷却的总时长）/ `failures`（连续失败次数）/
   `last_failure_class` / `since_last_failure_seconds`。
2. **只报当前在冷却的模型**。答上过的模型是健康的、它的 episode 已经结束；报一个已清空的 episode
   会把运维引向一个不存在的故障。
3. **`preferred` 从"首次见到的顺序"推导**。登记表只见过**被问过**的模型（#228 的 report 里带着
   顺序），它并不知道部署配置的链；把首个*未冷却*的已知模型报为 preferred 是"下一行会先问谁"的
   诚实近似 —— 这个近似被写进返回值语义里，而不是假装知道配置。
4. **不落库、不做缓存**：与同一次响应里的 `collection.loop_alive` 同一理由 —— 这是**本进程**的属性，
   落库或缓存会变成第二个事实源，而且会在重启后撒谎（ADR 0009、ADR 0048）。
5. 时间一律报**相对秒**（剩余/距今），不暴露 `time.monotonic` 的绝对值：它对操作者没有意义。

## 后果

- "这条链为什么慢"第一次能在一次调用里回答到模型维度。三个面互补：#229 的**日志**（连续的证据）、
  #230 的**行 detail**（死掉那一次的诊断）、这一格（当下状态）。
- 重启后这一格是空的 —— 登记表可丢（ADR 0048 已记录这个取舍），所以空不是缺陷。
- 暴露面：模型名（provider 的模型 id，不是凭据）与错误类别，落在 ADR 0042 的归因字段边界内；
  item 正文、URL、凭据一律不进这一格。
- 契约快照（`tests/contract/openapi.json`）随 docstring 更新；响应 schema 本身仍是开放对象，
  所以格子的**结构**由本 ADR 与测试钉住，不由 OpenAPI 钉住。

## 被否决

| 备选 | 否决理由 |
|---|---|
| 把健康状态落库 / 物化到 source 行 | 第二个事实源，重启后会撒谎；ADR 0009/0048 已否决过同一招 |
| 用"配置的链顺序"算 `preferred` | 要求 worker/路由知道某个处理器的配置键（层错位）；而 report 的顺序已经在手 |
| 报所有见过的模型（含已恢复的） | 会把已结束的 episode 当成现状，把运维引向不存在的故障 |
| 单独开 `/v1/models:health` 端点 | 与 `:diagnose` 是同一个问题（"这条链为什么慢"），拆开就要调用方自己拼（ADR 0036 的规则） |

## 参考

- `src/app/model_health.py`（`snapshot()`）、`src/app/sources_routes.py::diagnose_source`
- `tests/unit/test_model_health.py`（注入时钟、逐值断言）、
  `tests/integration/test_sources_api.py::test_diagnose_answers_which_model_is_cooling_down`
  （真 API + 真 provider + 真 worker → `:diagnose`）
