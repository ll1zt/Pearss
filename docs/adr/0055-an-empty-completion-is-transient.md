# ADR 0055：空补全（HTTP 200 + 空正文）是瞬时失败，不是 `parse`

- 状态：accepted
- 日期：2026-10-05
- issue：#259（本 ADR 是 #259 的交付物）
- 相关 / 修订：**部分 supersede** [ADR 0031](0031-translation-structure.md) 第 5 条——「provider 返回空 content 仍走既有 `parse` 失败」不再成立，空补全改判 `network`（瞬时）。§5 的第一条（`_rebuild` 按块独立消费自己的回复、回复不足时保留原文）不变；AC-22「provider 段数不匹配时不静默错位」不变，只是它触发的失败换了分类。

**场景**：本 ADR 不占新号——#259 未声明场景号，它**退役 AC-139**（`SCENARIOS.md` 该号段已标「已明确不做」）。AC-22 与 AC-133..138/140 保持其号。

## 背景

ADR 0031 §5 把「HTTP 200 但 `choices[0].message.content` 为空」定为 `parse`（永久类，worker 不自动重试，见 `enrichment_worker.TRANSIENT_CLASSES`）。当时的理由是把「空回复」当作 reply 契约被破坏的证据，而不是把它当成一次抖动。

实测（issue #259）推翻了这个理由：

- 生产侧，排查时段内 `hy3` 是唯一未被冷却的模型，答对了绝大多数行，只空了 1~2 次；同一时段的 `gemini-3.5-flash-lite` 也空过 2 次——说明空 200 是**整个 relay 的瞬时行为**，不是某个模型或某段内容的问题。
- 本仓库自己的测试注释早已记录了相反的证据（`test_processor.py`）：「measured live, three such rows all succeeded on retry in 1.0s」。

代价落在**链尾或单模型链**上：链里还有后备模型时空补全只是「换下一个」（AC-133，无害）；而当没有下一个候选（唯一可用模型，或其余模型正在冷却）时，`parse` 把行**永久钉死**，没有任何机制能救它——本次事故正是这样发生的，需要人工重新入队。

张力在于 AC-139 把「可见」与「永久」绑在了一起：它要的是「不要静默」，实现却选了「不重试」。

## 决策

**`_parse_reply` 对空补全抛 `ProcessorError(ErrorClass.NETWORK, "empty completion content")`。**

分类依据是「谁的问题、是否会变」：

- `network` 表示「这一侧的瞬时问题」——provider 答了，但没答出内容；下一次请求可能就好了。
- `blocked` 是「对方拒绝/限流」（429、CAPTCHA），空 200 不是拒绝，所以不取它。
- `parse` 保留给「以现状无法处理」的确定性失败（4xx 的模型名错误、非 JSON 回复、配置缺失）。

链的行为不变：空补全仍然是「这个模型这次不行」，`_translate_one` 照常落到下一个候选；只有当**所有**候选都失败时才终止，聚合规则照旧（`_aggregate_failure`：链中有瞬时即取最后一个瞬时失败的分类，链未走完则取 `network`）。

## 理由

- **它不承担「可见性」职责**：空补全作为 `network` 仍然是失败——行进 `failed`、detail 写明 `empty completion content`、`error_class` 记在行上，只是**先把 `MAX_ATTEMPTS` 用满**。可见性由「行停在 `failed` 且带 detail」保证，不需要把它绑成永久。
- **有实测支撑**：三条这样的行在下一次尝试 1.0s 内成功。把它们判为永久，等于为一次抖动付一次人工介入。
- **失败有界**：持续返回空正文的 provider 会在 `MAX_ATTEMPTS`（3）次后退避耗尽、停在 `failed`，不会无限重试；重放消耗的是 provider 预算，与 `429` 的处理同级。
- **备选被否决**：
  - 「只有最后一个候选的空补全算瞬时，链中仍算 `parse`」：分类依赖「是不是最后一个候选」，规则更绕，而链中 `parse` 与链中 `network` 对行的影响在「还有下一个候选」时本来就没有差别。
  - 「维持现状（`parse`）」：单模型链上每次抖动钉死一行，需人工 `retry_enrichments`；这正是 #259 要修的场景。

## 后果

- AC-139 退役；AC-22 的分类期望随之从 `parse` 变为 `network`（其「不静默错位」的断言不变）。
- 单模型链的行可能连续重试 3 次（每次退避 120s / 240s）后才停在 `failed`：这是有意的——一次抖动不应变成一次人工介入。
- 链的聚合语义不变：`_TRANSIENT_CHAIN_CLASSES` 本就含 `network`，「链里任一瞬时 → 整行可重试」的规则因此把空补全也纳入其中（此前它是唯一不纳入的瞬时形状）。
- 可逆：改回 `ErrorClass.PARSE` 一行即可；但那样就回到「单模型链抖动即钉死」，重做触发条件见 #259。
