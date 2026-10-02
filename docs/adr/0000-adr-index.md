# 架构决策记录（ADR）索引

一决策一文件。本目录记录那些**难逆转、影响多个组件、或日后会被反复追问「当初为什么这么定」**的决策。

## 何时写 ADR

写：

- 数据模型、API/MCP 契约、hookspec、错误分类法的变更（替换成本最高的一层）
- 引入或替换外部依赖、运行环境、持久化方案
- 破坏式变更及其迁移路径
- 在多个可行方案间做过明确取舍的地方

不写：

- 可逆且局部的实现细节（换解析器、改函数签名）——用 commit message 说明即可
- 纯格式化、依赖小版本升级——交给 lock 文件与 CI

## 文件约定

命名 `NNNN-kebab-case-title.md`，四位数字零填充、按创建顺序递增：

```text
docs/adr/
  0000-adr-index.md          # 本文件
  0001-四位数从 0001 起的第一个决策
```

每个文件是一份自足记录，包含：

| 字段 | 内容 |
|---|---|
| `标题` | 决策的祈使句摘要，与文件名一致 |
| `状态` | `proposed` / `accepted` / `superseded by NNNN` / `deprecated` |
| `日期` | 决策被接受的日期（YYYY-MM-DD） |
| `背景` | 约束与问题；为什么现在必须做决定 |
| `决策` | 选了什么 |
| `理由` | 为什么选它；被否掉的备选方案各自差在哪 |
| `后果` | 正面与负面；后续必须遵守的约束 |
| `可逆性` | 难逆 / 可逆，以及逆转需要付出什么 |

## 纪律

- **不改写已接受的 ADR**：历史决策就是历史。要改就写新 ADR，并在旧文件头部把状态改为 `superseded by NNNN`
- 新 ADR 的「理由」必须引用它 supersede 的那份，说清环境发生了什么变化
- 本文件末尾的**索引表是本目录的完整索引**（0000–0038，逐条一行），新增 ADR 时同步补一行
- `architecture-spec.md` §8「关键设计决策记录」是按决策点组织的**精选对照表**，
  只收录跨组件、可逆性等级需要写明的那部分，**不是**完整索引——两份表用途不同，
  不要互相取代；新增 ADR 后若它改变了某个决策点的取舍，才同步 §8
- AGENTS.md 要求：破坏 API、MCP 工具或数据语义的改动必须附 ADR 和迁移路径

## 索引

| 编号 | 标题 | 状态 | 日期 |
|---|---|---|---|
| 0000 | 本索引与 ADR 约定 | accepted | 2026-09-02 |
| [0001](0001-flake-inputs-use-git-https.md) | Flake 输入使用 git+https 而非 github | accepted | 2026-09-02 |
| [0002](0002-no-devenv-use-mkshell.md) | 开发环境用 pkgs.mkShell，不引入 devenv | accepted | 2026-09-02 |
| [0003](0003-rss-cdata-passthrough-and-lxml.md) | RSS 正文以 CDATA 原样透传，并用 lxml 生成 | **部分 superseded**（净化取向被 0007 反转） | 2026-09-03 |
| [0004](0004-reader-api-promoted-to-p1.md) | Reader API（C4）从 P3 提前到 P1 | accepted | 2026-09-03 |
| [0005](0005-reader-api-credentials-and-id-mapping.md) | Reader API 的凭据与 item ID 映射 | **部分 superseded**（ID 部分被 0006 取代） | 2026-09-03 |
| [0006](0006-reader-api-ids-must-be-decimal.md) | Reader API 的 item ID 必须是十进制整数 | accepted（**修订**：contents 长式 id 零填充 16 位） | 2026-09-04 |
| [0007](0007-sanitize-feed-html-at-store-time.md) | 入库前用 nh3 净化正文 HTML | accepted | 2026-09-04 |
| [0008](0008-source-canonical-url-not-null.md) | source.canonical_url 必须 NOT NULL | accepted | 2026-09-04 |
| [0009](0009-derived-schedule-without-apscheduler.md) | 调度时间从策略与历史推导，不引入 APScheduler | accepted | 2026-09-04 |
| [0010](0010-mcp-tools-via-core-api.md) | MCP 工具面经 Core API 调用，用官方 mcp SDK | **部分 superseded**（「工具与 endpoint 一一对应」被 0036 修订） | 2026-09-04 |
| [0011](0011-telegram-collector-decisions.md) | Telegram 采集器：t.me 原生 URL、CSS 取图、可选回溯 | accepted | 2026-09-05 |
| [0012](0012-authenticated-collectors.md) | 登录型采集器：允许使用用户自供凭据 | accepted（**修订 collector-guide 第 14 行**） | 2026-09-05 |
| [0013](0013-get-item-body-tool.md) | 允许 get_item_body 工具，以截断为条件 | accepted（**修订 AGENTS.md 第 99 行**） | 2026-09-05 |
| [0014](0014-zhihu-collector-fetch-hook.md) | 知乎采集器与注册表的可选 fetch 钩子 | accepted | 2026-09-06 |
| [0015](0015-zsxq-collector-scope-query-param.md) | 知识星球采集器：scope 即 URL 查询参数 | accepted | 2026-09-06 |
| [0016](0016-public-exposure-hardening.md) | 公网暴露：移除公开 RSS 输出，隧道优先，防御内移 | accepted（**修订 §1.2 Swagger 兜底、§6 C1、§2.1 AuditLog 保留**） | 2026-09-06 |
| [0017](0017-p2-plugin-freeze.md) | P2 插件化：hookspec 冻结、pluggy 注册表、manifest 与生命周期 | accepted（**修订 §3、§6 E1–E4**；**2026-09-16 生命周期表 import/uninstall/reload 到期于 0023**） | 2026-09-06 |
| [0018](0018-processor-chain-translation.md) | 处理器链：翻译参考实现（链挂源、OpenAI-compatible、NNW 译文+原文合并） | accepted（**修订 §6 B4、E1**） | 2026-09-09 |
| [0019](0019-mcp-2026-07-28-and-health-panel.md) | MCP 2026-07-28 协议与健康面板 | accepted | 2026-09-13 |
| [0020](0020-nixos-deployment-and-tunnel.md) | NixOS 部署与隧道分离（serve 全域 / funnel 仅 Reader API） | accepted（**修订 ADR 0016 的部署形态**） | 2026-09-13 |
| [0021](0021-deletion-is-deletion.md) | 删除就是删除：条目与订阅的物理删除 | accepted | 2026-09-14 |
| [0022](0022-agent-ingest-api.md) | A3 主动数据接入：agent 提交内容走采集器同一条管线 | accepted | 2026-09-14 |
| [0023](0023-third-party-plugin-loader.md) | 第三方插件加载器：用户自负其责 | accepted | 2026-09-16 |
| [0024](0024-readyou-subscription-writes.md) | Read You 的订阅管理写端点（quickadd / rename-tag / disable-tag，`it=` 与 `xt=` 下推 SQL） | accepted（**修订 ADR 0004 的端点范围**；**修订 ADR 0010 只澄清 A5 不受影响**） | 2026-09-14 |
| [0025](0025-restore-approved-plugin-installations.md) | 从持久化审批记录恢复插件安装（启动 checksum 比对，子进程校验） | accepted（**部分 supersede 0023**） | 2026-09-17 |
| [0026](0026-audit-boundary-and-reader-post-reads.md) | 审计边界（覆盖以中间件位置定义）与 Reader POST 读取认证例外 | accepted | 2026-09-18 |
| [0027](0027-ssrf-tailnet-exemption.md) | SSRF 守卫的 tailnet 网段豁免与拒绝原因拆分 | accepted（**收窄 0016 的私有地址定义**） | 2026-09-19 |
| [0028](0028-fulltext-processor.md) | Fulltext 处理器：摘要型 feed 的网页正文抽取（按源 opt-in、新 kind、阈值回退语义） | accepted | 2026-09-23 |
| [0029](0029-fetch-politeness.md) | 抓取礼貌：per-host 限速、单轮上限与冷却（宿主侧实施，不读 robots.txt） | accepted | 2026-09-24 |
| [0030](0030-chain-internal-dataflow.md) | 处理器链内的数据流：后序 spec 消费最近前序产物（位置定序、translation 不可消费、依赖入行、上游未就绪即等待） | accepted（**补充 ADR 0018 未定义的链内输入通道**） | 2026-09-26 |
| [0031](0031-translation-structure.md) | 翻译产物的结构：块级切分 + 原标签壳回填，非文本节点（代码/图片）不经 provider，inline 用成对标记保留 | accepted（**补充 ADR 0018 的产物形态**；**修订 ADR 0030 的产物形状假设**） | 2026-09-26 |
| [0032](0032-half-open-request-header-timeout.md) | 半开请求头超时：应用层 uvicorn 协议类实施（nginx 本机未运行）、默认开启不可关闭、针对无进展而非总时长 | accepted（**补充 ADR 0016 的入口加固面**；**记录 ADR 0020 部署形态在本机的实际拓扑**） | 2026-09-26 |
| [0033](0033-same-host-concurrency.md) | 同 host 并发度：同一 host 同时只允许一次抓取在途，已认领行在得知被拒后让位（不消耗 attempt） | accepted（**补充 ADR 0029 未定义的并发度契约**） | 2026-09-26 |
| [0034](0034-alert-when-intervention-is-needed.md) | 需要介入时主动告警：复用部署侧 notify 网关（不自建出网通道/不持凭据）、边沿触发、有界重发、退避期日志 | accepted（**补充 ADR 0019 健康面的“主动”一侧**） | 2026-09-27 |
| [0035](0035-retrieval-capability.md) | 检索能力：FTS5 中日文全文（重叠 bigram+unigram 预分词、短语查询）、本地多语言向量（384 维）、RRF 混合（非级联）；**修订架构 §1.3 的“全文搜索引擎”非目标** | accepted（**修订 architecture-spec §1.3 非目标边界**） | 2026-09-27 |
| [0036](0036-tool-face-is-a-semantic-projection.md) | 工具面是 /v1 的语义投影：一 endpoint 可对多工具、聚合放端点里 | accepted（**修订 ADR 0010 的「工具与 endpoint 一一对应」一句**） | 2026-09-28 |
| [0037](0037-cancellation-semantics.md) | 取消是执行语义：所有者写意图、协作式检查点、`cancelled` 终态（**只定义，不实现**） | accepted | 2026-09-29 |
| [0040](0040-embedding-runtime-and-its-cross-layer-dependency.md) | embedding 运行时的跨层依赖：Python 依赖进 `uv.lock`（`uv add onnxruntime`）、`libstdc++`/`zlib` 由环境给 loader 路径、一条真跑推理的测试持有这个决定 | accepted（**执行 flake.nix 的「Nix 管系统库 / uv 管 Python 图」分工**） | 2026-09-29 |
| [0039](0039-loop-liveness-and-its-residual-risk.md) | 循环互看：两个后台循环各自把对方的 `liveness` 送进同一条告警通道（循环无法报告自己的死亡）；**写明并接受残余风险**——两个循环同时死时静默，被动面 `/healthz` 兜底 | accepted（**补充 ADR 0034 决策 3 的第二个条件**） | 2026-09-29 |
| [0038](0038-single-tenant-scope.md) | 单租户是范围决定：假设命名一处、启动时校验、**不做**端到端隔离（写明触发条件） | accepted | 2026-09-29 |
| [0041](0041-deferred-scope-and-its-triggers.md) | 范围裁决：A6 OPML 与 P3 剩余项（音频 feed / TTS / MCP 拆分 / Gateway / 按需数据存储）**有意不做**，每项附触发条件；**同时更正 spec 三处陈旧标记** | accepted（**修正 architecture-spec §5/§6 的未完成项口径**） | 2026-09-29 |
| [0042](0042-alert-attribution-field-boundary.md) | 告警与诊断的归因字段边界：源标题/分类/计数/provider 错误文本（截断）允许出境，正文/凭据/PII/URL 仍禁止；`dominant error class` 标注全库累计 | accepted（**收窄 ADR 0034 决策 5 的「不发原文」与对应「已否决」一条**） | 2026-09-29 |
| [0043](0043-host-constrained-video-in-feed-html.md) | 入库净化放行宿主受限的 `<video>`：只接受 `https://video.twimg.com/` 的 src，禁 autoplay/loop/muted，非白名单来源整个移除；采集器把「缩略图+外链」作为 fallback 内容 | accepted（**补充 ADR 0007 的媒体标签白名单，`iframe`/`object`/`embed` 禁令不变**） | 2026-09-29 |
| [0044](0044-batch-source-addition.md) | 批量建源：一次确认覆盖一批（MCP `add_sources` + `/v1/sources:batch`）；修订 ADR 0041「批量形态本身也不缺」一句；OPML 仍不做 | accepted（**修订 ADR 0041 决策 1 的一句话**） | 2026-09-29 |
| [0045](0045-content-level-discovery.md) | 内容级发现：地址由外部提供、采集器按地址形状抓取（`collect_item` 能力 + `:fetch-items` + MCP `fetch_source_items`）；feed 分页、只支持 WP、声明零实现能力均被否决；采集器自声明的 `discover()` 带触发条件推迟 | accepted（**部分 superseded**：D7 同步端点与 D10 不新增持久化被 [0046](0046-work-as-a-first-class-citizen.md) 取代） | 2026-09-30 |
| [0046](0046-work-as-a-first-class-citizen.md) | 作业是一等公民：命令 / 账本 / 投影（`work` + `work_unit`、counts 是折叠、六个复数形态坍缩为一个机制、事实/策略分离）；**取代 ADR 0045 的 D7/D10**；万能 `request_work` 与「调用方自己循环」被否决 | accepted（**部分取代 ADR 0045 的 D7/D10**） | 2026-10-01 |
| [0047](0047-processor-context-carries-the-row-budget.md) | 处理器上下文带行预算：运行时把行 deadline 放进 `item_ctx`（数据扩展，不改 hookspec）、单次尝试 `min(60s, 剩余/2)`、不足 0.5s 不发起、预算耗尽主动抛 transient 并保留逐模型原因；按块均分预算与「击杀时索要原因」被否决 | accepted（**补充 ADR 0017 的上下文契约**） | 2026-10-02 |
| [0048](0048-model-health-lives-in-the-worker.md) | 模型健康登记表在 worker：策略随行下行（`item_ctx["model_policy"]`）、逐模型报告上行（`model_events` + `run_processor(report=...)`）、首选/行内重试 2/冷却 5min 翻倍封顶 1h/成功复位/永久类只问一次/整链冷却零请求；子进程自记、落库、worker 自己走链与从 detail 反推均被否决 | accepted（**沿用并扩展 ADR 0047 的数据通道**） | 2026-10-02 |
| [0049](0049-child-stderr-is-a-log-channel.md) | 处理器子进程的 stderr 是日志通道：`logging` 自己的格式、宿主在子进程运行**期间**排空（否则管道写满即阻塞）、按原级别重记并加 `[plugin_id item_id]` 前缀、无级别前缀按 warning、转发有界（200 行/1000 字符）；DEVNULL、混进 stdout、退出后再读与不设上限均被否决 | accepted（**补充 ADR 0047/0048 的同一子进程边界**） | 2026-10-02 |
| [0050](0050-a-killed-row-keeps-its-diagnosis.md) | 被击杀的行保留诊断：宿主另存子进程最后 3 行（从最新往回拼、≤320 字符）并附到「没来得及上报」的失败 detail；处理器用 `pearss.progress` logger 记块进度（约定即 API）；子进程静掉 httpx/httpcore 的 INFO；击杀时向子进程索取聚合、把进度塞进 item_ctx、解析 detail 文本均被否决 | accepted（**建立在 ADR 0049 的通道上**） | 2026-10-02 |
| [0051](0051-diagnose-projects-the-model-registry.md) | `:diagnose` 的模型健康格是登记表的只读投影：报 `preferred`/`skipped`/`attempts_per_model` 与**当前冷却中**模型的冷却剩余、本次冷却时长、失败次数、最近失败类别与距今秒数；只报冷却中、`preferred` 由 report 的首次出现顺序推导、不落库不缓存、时间报相对秒；落库、用配置链序、报已恢复模型、另开端点均被否决 | accepted（**ADR 0048 的出口**） | 2026-10-02 |
| [0052](0052-block-level-progress-on-disk.md) | 块级进度落盘：运行时给每行一个 scratch 目录（`<storage>/processor-cache/<plugin>/<lang>/<item>`，经 `item_ctx["cache_dir"]`）、处理器每块成功后原子写入并在下一块前查缓存、成功后运行时删除+失败时保留；缓存的是块不是产物（净化路径照走、产物逐字节一致）、缓存不是事实源（不建表不加列）、写失败不是失败、块级并发推迟（带触发条件）；缓存做成表/由处理器自定路径/按 attempt 建目录/缓存产物/成功后保留均被否决 | accepted（**ADR 0047/0048 的续集**） | 2026-10-02 |
