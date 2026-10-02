# ADR 0047：处理器上下文带行预算——单次尝试上限与主动放弃（保留逐模型原因）

- 状态：accepted
- 日期：2026-10-02
- issue：#227（本 ADR 是 #227 的交付物）
- 相关 / 修订：ADR 0017（hookspec 冻结：本次是**数据字段扩展**，签名与语义不变）、ADR 0018（P3 处理器链与失败聚合）、ADR 0030（链内数据流）、ADR 0031（翻译产物形态）、ADR 0009（派生状态不落库）

**新增场景从 **AC-221** 起**（AC-221..222，见 `SCENARIOS.md`）。

## 背景

部署实测（近 24h journal）：`processor deadline expired` **17 次**；kogumagoya 全量入库的
568 条里一度 **31 条**卡在翻译链上，条目永久 `pending`（NNW 与 Reader API 都读不到）。
读代码得到三条算术，都不是推断：

- 单次 HTTP 上限写死 `read=300s`（`src/plugins/processors/translate/processor.py`），而行预算是
  `DEFAULT_CALL_TIMEOUT=360s`（`src/app/enrichment_worker.py`）：一个"连上但不回"的模型在
  **第一块**就能吃掉 83% 的行预算，链里的保底模型没有机会。
- 链是**每块**从 index 0 重新走的，且处理器**每行新 spawn** 一个子进程：坏模型的学费
  每块、每行各付一次（跨行记忆是 #228 的范围）。
- 预算耗尽的唯一表达是父进程**击杀**子进程（`src/app/processor_runtime.py`），而击杀把
  子进程里已经攒好的逐模型原因一起丢掉，行的 detail 只剩 `network / processor deadline
  expired`——它既不说是哪个模型，也不说它做了什么。

## 决策

1. **运行时把行 deadline 放进处理器读到的上下文**：`_child_main` 以
   `{**item_ctx, "deadline": request["deadline"]}` 调用 `process`，值是绝对
   `time.monotonic()` 时刻（与子进程看门狗同一时钟）。这是**数据字段扩展**：
   `process(item_ctx, *, target_lang, store)` 的签名与语义不变，ADR 0017 的冻结不被触碰。
   没有该键的调用（进程内直调、测试）含义是"没有预算要遵守"，沿用历史超时。
2. **单次尝试的预算是「剩余的一半、封顶 60s」**：`attempt_timeout(remaining) =
   min(60s, remaining/2)`。封顶让一个慢模型不能独占 6 分钟的行；折半让链里**下一个**模型
   仍有份额，且逐次折半之和 < 剩余预算，处理器永远留得下报告原因的时间（几何级数自限，
   不需要魔法 reserve 常数）。
3. **不足 0.5s 不发起尝试**：这么短的调用不可能返回译文，发起它只是在与父进程的击杀赛跑。
   此时**主动**抛 `network`（transient，交回 worker 退避重试），detail 保留已收集的逐模型
   原因；一个模型都没问过时明确说明"预算在问任何模型之前就用完了"——"没问过"与"都说不"
   对读行的人是两件事。
4. **没有 deadline 时不改变既有超时**（`connect=10 / read=300 / write=15 / pool=360`）：
   进程内调用与历史行为一致。
5. **每个 HTTP 阶段都被单次预算收窄**（connect/write/pool 取 `min(默认, 预算)`）：一个越过
   尝试预算的 connect 是同一个缺陷下沉一层。

## 后果

- 行的失败第一次**可诊断**：detail 说明哪个模型、什么原因；`network` 与 `blocked` 的聚合
  语义（ADR 0018、issue #145）不变。
- 一行最多把剩余预算的一半花在单个模型上，链的后续模型有份额。
- 长文档仍可能因块数多而耗尽预算（每块成功时预算几乎不动，失败时按块折半）：块级进度与
  "只补缺失块"是 #232 的范围。
- 60s 的封顶在 #228 做成了部署旋钮（`PEARSS_TRANSLATE_ATTEMPT_SECONDS`，默认 60s；慢 provider
  必须调大——实测有 relayed gemini-flash 单次回复 126s）；0.5s 的下限仍是模块常量。
- **保证的边界（实测，写本 ADR 时量的）**：httpx 的超时本身是精确的（标称 0.5–5s，实测偏差 ≈0.02s，
  负载下同样），但**子进程的墙钟**在调度压力下会漂 1–2s：同一台 8 核机器上并行跑别的构建时，
  一次标称 2.8s 的尝试实测走了 4.9s。所以"处理器总能先报告"对**真实行预算**成立（`DEFAULT_CALL_
  TIMEOUT=360s`，受 60s 上限约束，余量巨大）；当行预算只剩几秒时，击杀仍可能赢得赛跑——那时行
  仍以 transient 失败（可重试、可重排），但 detail 只剩 `processor deadline expired`。让"被击杀也
  保留诊断"是 #230（子进程心跳 + detail 合并）的范围，不是本 ADR 能兑现的。因此本 ADR 不把
  "永不丢原因"写成不变量。

## 被否决

| 备选 | 否决理由 |
|---|---|
| 把 deadline 作为 `process()` 的新关键字参数 | 改签名即破坏 ADR 0017 的 hookspec 冻结；数据字段足够 |
| 按块均分预算（剩余 / 剩余块数） | 块数只有 `process` 内部才知道，且一块失败后无法把预算还给别的块；折半更简单且自限。**带触发条件推迟**：若实测出现"前几块成功、后几块饿死"，再按块重分配 |
| 用墙钟（`time.time()`）传 deadline | 跨进程、跨时钟调整不稳；运行时已用 monotonic 看门狗，两处必须同源 |
| 预算耗尽时返回部分产物 | 产物是**一个** artifact（ADR 0031），半篇译文入库等于把损坏内容当成功；部分进度应做成可续跑的缓存（#232），不是产物 |
| 击杀时由父进程向子进程索取原因 | 击杀是最后手段（子进程可能挂死），不能在它上面加一次往返；原因必须由子进程**在预算内**送出 |

## 参考

- `src/app/processor_runtime.py`（`_child_main` 注入 deadline）、
  `src/plugins/processors/translate/processor.py`（`attempt_timeout`、`_attempt_http_timeout`、
  `_out_of_budget`）
- `src/core/plugins/hooks.py`（`process` 的上下文契约）
- `tests/integration/test_processor_deadline.py`（AC-221/222：真实 worker + SQLite + 真不回的
  provider）、`src/plugins/processors/translate/test_processor.py`
