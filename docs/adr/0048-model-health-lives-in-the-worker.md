# ADR 0048：模型健康登记表在 worker——策略随行下行、逐模型报告随行上行

- 状态：accepted
- 日期：2026-10-02
- issue：#228（本 ADR 是 #228 的交付物）
- 相关 / 修订：ADR 0047（行预算：本 ADR 沿用它的数据通道，并把"上下文只带 deadline"扩成"也带模型策略与报告"）、ADR 0029（fetch pacing：同层同理由的先例）、ADR 0017（hookspec 冻结：本次是数据字段扩展）、ADR 0009（派生状态不落库）、ADR 0018（链的失败聚合）

**新增场景从 **AC-223** 起**（AC-223..228，见 `SCENARIOS.md`）。

## 背景

部署实测（#227 的 journal）：一个"连上但不回"的模型被**每一行、每一轮**排在第一位问，
而真正能答的备选模型永远排在它后面 —— 24h 内 17 行以 `processor deadline expired` 收场。
#227 把**单次尝试**的预算修好了（不再一次吃掉整行），但"哪个模型坏了"这件事仍然没有任何
记忆：

- 链是**每块**从 index 0 重新走的，且处理器**每行新 spawn 一个沙箱子进程**：一个子进程里的
  计数器活不过一行，下一行重新付学费（与 ADR 0029 给 fetch pacing 的理由同构）。
- 唯一能看见整条队列的是 **worker**（宿主进程）。

## 决策

1. **状态放 worker**（`app/model_health.ModelHealth`）：内存态、可丢、**不落库** —— 重启后
   忘记"哪个模型在闹脾气"的代价是**多付一次失败尝试**，而建表会让它成为 provider 行为之外的
   第二个事实源（ADR 0009 的推理）。与 `HostPacer` 同层、同理由、同形状（注入时钟、进程级
   单例 + `reset_*`、锁保护，因为 worker 用线程池跑行）。
2. **策略下行、报告上行，都是数据**（ADR 0017 的签名冻结不被触碰）：
   - 下行：`item_ctx["model_policy"] = {"skip": [冷却中的模型], "attempts_per_model": 2}`；
   - 上行：运行时在子进程里建 `item_ctx["model_events"] = []`，处理器为**每个问过的模型**
     追加一条 `(model, error_class | None)`（`None` = 这个模型在本行答上了东西），
     `run_processor(report=...)` 把它交给 worker。**失败路径也要送**：失败的行正是登记表
     最需要的行。
   - 两个键名定义在 `core/plugins/hooks.py`：`app` 与 `plugins` 两层都要读，而它们不能互相
     import。
3. **规则**（全部在 worker 侧决策，处理器只执行）：
   - **首选**：链的配置顺序不变；冷却到期后模型自动回到原位 —— 恢复是时间的流逝，不是某个
     人的再信任动作。
   - **行内预算 2**：一个模型在一行里最多失败 2 次（**按行计，不是按块**：12 段的文档不该为
     同一次故障付 12 次）。2 次之后这一行不再问它，预算给后面的模型。
   - **冷却**：失败 → 冷却 5 分钟；冷却到期后再失败 → 翻倍（5→10→20→40→60 分钟，封顶
     1 小时）；冷却中的模型被跳过。
   - **复位**：一次成功（该模型在本行答上了任何一块）即清空它的失败计数与冷却 —— 下次失败
     从 5 分钟起，而不是接着上次的翻倍。
   - **永久类只问一次**：`auth`/`parse` 不会被"再试一次"治好，所以行内不重试，直接降级。
   - **整链冷却**：立刻以 `network`（瞬时、可重试）失败，**不发任何 provider 请求**；判断放在
     处理器侧，因为只有它知道"配置的链里有哪些模型"。
4. **报告的形状是"每模型一条、折叠过"**：一个模型在本行既失败过又答上过时，记"答上" ——
   "答上了"与"失败了"不是同等有趣的两个事实，登记表在两者上做的事也不同（复位 vs 升级冷却）。
5. **旋钮**：`PEARSS_MODEL_COOLDOWN_SECONDS`（0 = 永不冷却）、
   `PEARSS_MODEL_COOLDOWN_MAX_SECONDS`、`PEARSS_MODEL_ATTEMPTS_PER_MODEL`；
   单次尝试上限 `PEARSS_TRANSLATE_ATTEMPT_SECONDS`（默认 60s，慢 provider 必须调大）。

## 后果

- 坏模型不再每次都被排在第一：一次失败之后，链里后面的模型顶上；一个真的恢复了的模型在冷却
  到期后自动回到原位。
- **单模型链上，一条失败的行会让这条链冷却 5 分钟**（AC-228 的直接后果）。这是"整链冷却不硬试"
  的代价，也是**推荐部署 ≥2 个模型**的原因 —— 已写进 `.env.example` 与 env-reference。
- 冷却**不区分失败原因**（`auth`/`parse` 与 `network` 都会冷却），差别只在"行内是否重试"：
  永久类只问一次。这样一条被拒的 key 既不会拖住整行，也不会被无限重问。
- 状态可丢：重启后第一次失败重新付一次学费。可接受，也是不落库的全部理由。
- 进程级状态意味着**测试必须重置**：`conftest.py` 加了 autouse 的 `reset_health()`（与
  `reset_pacer` 同理），否则一个让模型失败的用例会把下一个用例的链冷却掉。

## 被否决

| 备选 | 否决理由 |
|---|---|
| 让子进程自己记健康 | 处理器每行新 spawn，状态活不过一行 —— 这正是不解决问题的原样 |
| 落库 / 建表 | 派生状态、可丢；且会成为 provider 行为之外的第二个事实源（ADR 0009） |
| worker 自己走链（一次 spawn 一个模型） | 需要 worker 知道"链里有哪些模型" = 处理器配置的所有权错位（或改 hookspec 声明链）；且丢掉"块级降级"（文档第 7 段失败时，第 1..6 段要重译） |
| 从 `detail` 文本反推哪个模型失败 | 字符串解析，脆弱；且**成功路径没有 detail** —— 而"一次成功即复位"必须知道是哪个模型答的 |
| 新增一个 hookspec 方法报告结果 | ADR 0017 冻结签名；数据字段足够（#227 已开先例） |
| 把"整链冷却"判断放 worker | worker 不知道链的成员；放处理器侧才是"配置的链整个在冷却"这个正确语义 |
| 冷却只对"重复失败"生效（首次失败不冷却） | 与用户拍板的"失败即冷却 5 分钟、到期再失败才翻倍"不符；也让一次真正的故障继续被每轮重问 |

## 参考

- `src/app/model_health.py`（登记表）、`src/app/enrichment_worker.py`（策略下行 + 报告折叠）、
  `src/plugins/processors/translate/processor.py`（`_RowPlan` / `_ModelReport`）
- `src/core/plugins/hooks.py`（`MODEL_POLICY_KEY` / `MODEL_EVENTS_KEY`）、
  `src/app/processor_runtime.py`（`report` 出口）
- `tests/unit/test_model_health.py`（注入时钟的确定性单测）、
  `tests/integration/test_model_failover.py`（真 worker + 真 provider 的跨行行为）
- ADR 0047（行预算）、ADR 0029（fetch pacing）、ADR 0017（hookspec 冻结）、ADR 0009（不落库）
