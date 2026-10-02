# ADR 0050：被击杀的行保留诊断——子进程日志尾部 + `pearss.progress` 约定

- 状态：accepted
- 日期：2026-10-02
- issue：#230（本 ADR 是 #230 的交付物）
- 相关 / 修订：ADR 0049（子进程 stderr 是日志通道：本条建立在它之上，把"转发进 journal"变成"回填进行的 detail"）、ADR 0047（行预算）、ADR 0018（链的失败聚合）、ADR 0029/0033（采集器侧的同类观测问题）

**新增场景从 **AC-232** 起**（AC-232，见 `SCENARIOS.md`）。

## 背景

逐模型原因住在**响应**里（#228 的 `model_events`），而击杀意味着**没有响应**：父进程在 deadline 处
杀掉子进程，行的 detail 只剩 `processor deadline expired` —— 既不说死在哪一块，也不说问过谁。
部署实测：近 24h 内 17 次。

#229 之后子进程的日志已经能到宿主，但那时只是"转发进 journal"：日志里有了，**行的 detail 里仍然没有**，
而 operator/agent 读的是后者（`:diagnose`、`list_enrichments`、告警归因）。

## 决策

1. **`_ChildLog` 另存子进程最后 3 行**（在转发之外，且**不受转发上界影响**：journal 可以看够了，最后
   那句话仍要留着）。`tail()` **从最新往回拼**、总长 ≤320 字符 —— 落在 worker 侧 `detail[:500]` 之内。
   从最新往回拼是有意的：最新那句话才是"死在哪"。
2. **没来得及上报的失败**（deadline 击杀、取消、子进程无结果退出）把这段尾巴附进 detail：

   ```
   processor deadline expired: [translate <item_id>] INFO pearss.progress chunk 3/12 |
   WARNING ...processor translate model primary failed (network): ...; falling back to fallback
   ```

   **正常失败**（子进程给出了 `error_class`）不变：聚合本来就是权威的，尾巴只会是噪音。
3. **`pearss.progress` 约定**（`core.plugins.hooks.PROGRESS_LOGGER`）：处理器想让被击杀的行知道自己走到
   哪，就用这个名字的 logger 记进度（翻译处理器每块一行 `chunk i/N`）。**不需要任何新 API 或 hook**：
   进度就是一行日志，插件面契约不变。
4. **子进程静掉 `httpx` / `httpcore` 的 INFO**：子进程不是调试代理，每个请求一行 INFO 会把处理器自己的
   行（以及尾部）挤掉 —— 这是写本 ADR 时实测到的：第一版尾部里只有 `INFO httpx HTTP Request: ...`。
5. 每行的转发仍受 #229 的上界约束（200 行 / 1000 字符）。

## 后果

- 被击杀的行第一次能回答"死在哪一块、问过谁"——这正是 #228 引入的聚合在**失败路径之外**的那一半。
- detail 变长（最多约 350 字符），仍在 500 以内；`:diagnose` 与 `list_enrichments` 直接受益。
- **边界（写明）**：子进程被 SIGKILL 时，尚未从管道读出的最后几行仍会丢 —— 尾巴是"宿主最后读到的"，
  不是"子进程最后写的"。logging 已按记录 flush、宿主每 50ms 排空，剩下的窗口只有内核管道缓冲。
- 反过来的约束：处理器的进度/日志**必须**走 `logging`（而不是裸 print 到 stdout——stdout 是协议）。

## 被否决

| 备选 | 否决理由 |
|---|---|
| 击杀时向子进程索取聚合 | 击杀是最后手段（子进程可能挂死），不能在它上面再加一次往返（与 ADR 0047 同一条理由） |
| 把进度放进 `item_ctx` 结构化回传 | 击杀时没有**回传**通道；只有**流出**的通道（stderr）能在被杀之前送达 |
| 从 detail 文本反推每个模型的原因 | 字符串解析，脆弱；而且成功路径根本没有 detail（ADR 0048 已否决过同一招） |
| 只写一条"还剩 N 秒"的心跳 | 不含块进度与模型名，回答不了操作者真正问的两个问题 |
| 不静 httpx 的 INFO | 每个请求一行噪音，既污染 journal 又挤掉尾部（实测） |
| 把尾部并进**所有**失败的 detail | 正常失败已有权威聚合，尾巴只是重复；只补"没来得及上报"的那条路径 |

## 参考

- `src/app/processor_runtime.py`（`_ChildLog.tail`、`_unreported`、子进程 logging 配置）
- `src/plugins/processors/translate/processor.py`（`PROGRESS`）、`src/core/plugins/hooks.py`（约定）
- `tests/integration/test_processor_deadline.py::test_a_killed_row_keeps_what_the_chain_had_learned`
  （滴流 provider：不触发读超时、必然跑到 deadline，击杀是确定性路径而不是时序偶然）
- `src/plugins/processors/translate/test_processor.py::test_each_block_announces_itself_on_the_progress_logger`
