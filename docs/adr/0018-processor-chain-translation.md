# 0018: Processor Chain — Translation Reference Implementation

Status: accepted
Date: 2026-09-09
Context: P3 处理器链（spec B4/C3、E1）首个参考实现；2026-09-09 用户拍板三项决策。

## 决策

### 1. 链挂在 Source，不挂在 FeedOut

`source.pipeline_chain`（JSON 列表）+ `source.target_lang`（默认语言）：
条目采集时即知道要等待什么产物，闸门在存储时关闭。FeedOut 的
`pipeline_chain` 字段保留给"输出定制"场景（如只发译文），不承担闸门职责。

用户拍板：按源配置。

### 2. 翻译器走 OpenAI-compatible chat completions

`PEARSS_TRANSLATE_BASE_URL/API_KEY/MODEL` 三元组；任何兼容端点
（OpenAI/本地 vLLM/Ollama 的 OpenAI 模式）可换。凭据经 env allowlist
（`PEARSS_TRANSLATE_` 前缀），子进程内执行时与采集器凭据同通道。

用户拍板：OpenAI-compatible APIs。

### 3. NNW 输出 = 译文 + 原文合并

Reader API `items/contents` 在条目有成功翻译产物时返回
译文 + `<hr data-pearss="translation-separator">` + 原文。原文永不替换
（产物不回写 Item 的不变量）；顺序固定译文在前（读者订阅的就是译版）。
最新产物优先（与 conditional-state 同规则）。

用户拍板：输出给 NNW 的内容里译文+原文合并。

译文的**产物形态**由 ADR 0031 定义（2026-09-26）：译文保留原文的块级结构（标题仍是
标题、链接仍是链接、代码与图片原样），而不是最初的「一串 `<p>` 段落」。本篇只定
合并与顺序，不约定产物的内部形状；0031 不改变本节任何一句。

## 机制

- **数据**：`item_enrichment`（migration 0011），唯一键
  `(item_id, plugin_id, plugin_version, kind, lang, input_hash)`；
  `input_hash` = 处理时的内容哈希，内容变更是新产物不是覆盖。
- **链闸门**：`core.enrichment.chain_complete` 纯函数；pipeline 存储时有
  链的源入队并停留 pending，无链源保持 P1 行为立即 ready。
- **执行**：`enrichment_worker`（scheduler 旁的第二循环）扫 queued 行，
  事务内认领（queued→running），调处理器，写产物，链完成即
  `MARK_READY`。失败落 `error_class` 保持 pending，不自动重试——失败
  重试会真金白银计费，重试只发生在内容变更或运维重新触发。
- **处理器合约**：`ProcessorSpec`（processor_id/version/process），失败
  以 `ProcessorError(ErrorClass)` 为值；与 CollectorSpec 同冻结规则。
- **沙箱复用**：处理器与采集器共用 plugin_runtime 与 egress 代理；
  `PEARSS_TRANSLATE_*` 进 env allowlist。

## 后果

- 闸门的 `processing_complete` 从"永远为真的占位"变成有真实生产者；
  `HOLD/PUBLISH_ORIGINAL/force_publish` 的既有语义全部随之生效。
- 翻译产物先落地 Reader API；RSS 输出的多语言 feed 留给 FeedOut CRUD。
- 翻译器在子进程内执行是未来路径（当前 worker 进程内直调，合约与
  环境收窄已就绪，迁移是机械工作）。
- LLM 成本由 `input_hash` 幂等 + 不自动重试双重约束。
