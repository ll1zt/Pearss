# 跨模块验收场景编号登记表

场景编号（`AC-N`）是**可追溯单元**：一个编号对应一条验收场景，一个编号只属于一个号段。
本文件是**跨模块编号的唯一事实源**——`docs/adr/`、issue 正文与测试文件头都指向这里。

模块自己的场景登记表跟模块放在一起（先例：`src/plugins/processors/fulltext/SCENARIOS.md`），
那是**独立的命名空间**，编号只在该模块内有意义。见文末「模块内登记表」。

## 规则

1. **谁先合并谁占号。** 两个并行分支声明同一号段时，先合并的一方保留编号，后到者
   **整体让号**，并在提交说明与 issue 评论里写清让位原因。
2. **一个号只属于一个号段。** 下表各行两两不相交。声明新号段前先在这里占一行。
3. **号段必须闭合**（`AC-A..AC-B`，或单号 `AC-A`）。**不接受开区间**（如 `AC-72..`）：
   开区间会吞掉其后所有编号，使「这个号有没有主」失去答案——本表的守卫就曾因此对
   注入的未登记编号完全沉默。未定长的预留要写明确的右界，用满再改本行往右扩。
4. **声明占了号 ≠ 实际占了号。** 占号后必须在测试文件头写 `Scenario register`
   指向规范来源。`tests/unit/test_structure_guards.py` 机械对账：仓库里出现的任何
   `AC-N` 必须落在**恰好一个**号段内，且本表声明的起始编号必须与对应 ADR 的措辞一致。
5. **往后排，不回填空洞。** 空洞可能是历史号段的一部分（如 AC-4..6、AC-9..13 属于
   P1/P2 历史号段，未在任何现存的测试里出现，但不得被重新占用）。
6. **一个 issue 的号段保持连续**；追加号段另起一行并在「归属」里注明（先例：AC-47..52
   是 #64 的追加号段）。

## 号段登记表

| 号段 | 归属 | 声明来源 | 状态 | 证据 |
|---|---|---|---|---|
| AC-1..35 | P1/P2 既有计划（历史号段，整体保留） | ADR 0029 / 0030 / 0031 / 0032、`docs/guides/testing.md` | 已合并 | `tests/integration/test_http_header_timeout.py` 等 |
| AC-36..39 | #63 MCP 工具结果脱敏 | `docs/api/mcp-tool-guide.md` §5 | 已合并 | `tests/integration/test_mcp_result_redaction.py` |
| AC-40 | #64 audit_id 串号 | `docs/api/mcp-tool-guide.md` §3 | 已合并 | `tests/integration/test_audit_id_crosstalk.py` |
| AC-41..43 | #65 列表截断（41/42 有测试，43 由 README 重测承担） | `docs/api/mcp-tool-guide.md` §2 | 已合并 | `tests/unit/test_mcp_list_truncation.py`、`tests/integration/test_mcp_list_truncation.py` |
| AC-44..46 | #67 submit_items 的确认指纹**已退役** | issue #67 / #270 / ADR 0058 | 已明确不做 | 写确认机制整体退役，指纹不复存在；submit_items 仍是作业句柄 + 逐条账本（AC-211） |
| AC-47..52 | #64 工具体覆盖（追加号段） | `docs/api/mcp-tool-guide.md` §3 | 已合并 | `tests/integration/test_mcp_tool_coverage.py` |
| AC-53..54 | #68 list_plugins 的 limit 与分页 | `docs/api/mcp-tool-guide.md` §2 | 已合并 | `tests/integration/test_list_plugins_limit.py` |
| AC-55..57 | #69 guide 一致性与缺席的守卫（原声明 49..51） | `docs/api/mcp-tool-guide.md` §2 / §6 | 已合并 | `tests/unit/test_mcp_tool_guide.py` |
| AC-58..61 | #70 search_items 的 q 语义与边界（原声明 52..55） | issue #70 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（描述必须写明 q 只匹配标题、full_text 搜正文） |
| AC-62..63 | #71 窄工具与 /v1 的形状差异（原声明 56..57） | issue #71 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（每个工具必须落在 OpenAPI 声明的端点上） |
| AC-64..65 | #72 list_enrichments 的维度（原声明 58..59） | issue #72 | 已合并 | `tests/integration/test_items_api.py`（`/v1/items/{id}/enrichments`） |
| AC-66..68 | #73 工具面常驻上下文预算（原声明 60..62） | issue #73 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（预算守卫 + 任务级工具）、`tests/integration/test_sources_api.py`（`:diagnose`） |
| AC-69..71 | #74 outputSchema 空壳（原声明 63..65） | issue #74 | 已合并 | `tests/integration/test_mcp_conformance.py`（instructions 携带协议字段）、`tests/unit/test_mcp_tool_guide.py`（写明保留宽松 schema 的理由） |
| AC-72..74 | #75 工具名命名空间（原声明 66..68） | issue #75 | 已明确不做 | `docs/api/mcp-tool-guide.md` §七（决定 + 重做触发条件）；`tests/unit/test_mcp_tool_guide.py::test_the_guide_records_the_naming_decision` |
| AC-75..77 | #76 写工具守卫统一（原声明 69..71）**已退役** | issue #76 / #270 / ADR 0058 | 已明确不做 | 写确认机制整套退役，与它同生共死的守卫一并删除；取代它的结构性判据是 AC-57（描述 ⇔ annotations）与 AC-263（schema 无确认参数） |
| AC-78..80 | #77 列表查询全表扫与索引（原声明 72..74） | ADR 0035 §9 | 已合并 | `tests/integration/test_item_query_plan.py` |
| AC-81..87 | #78 中日英分词（原声明 75..81） | ADR 0035 §9 | 已合并 | `tests/unit/test_text_search.py`（27 条 golden 集，真 FTS5 表验召回） |
| AC-88..92 | #79 item_fts 表（原声明 82..86） | ADR 0035 §9 | 已合并 | `tests/integration/test_search_index.py` |
| AC-93..98 | #80 检索面 full_text 与 bm25（原声明 87..92） | ADR 0035 §9 | 已合并 | `tests/integration/test_search_surface.py` |
| AC-99 | #82 向量检索按语义相似召回（原声明 93；真模型，无关键词也能召回） | ADR 0035 §9 | 已合并 | `tests/integration/test_vector_search.py` |
| AC-100 | #82 模型确实是多语言的（原声明 94；跨语言余弦，需真模型） | ADR 0035 §9 | 已合并 | `tests/integration/test_embedding_semantics.py` |
| AC-117 | #82 真模型：单位向量与声明维度、改写近于无关、query/passage 前缀生效 | ADR 0035 §9 | 已合并 | `tests/integration/test_embedding_semantics.py` |
| AC-118 | #82 模型下载：哈希钉住、镜像只换来源不换产物、被篡改不留残骸 | ADR 0040 | 已合并 | `tests/integration/test_fetch_model.py` |
| AC-119 | #82 闸门拿到的是**条目的内容哈希**而非行里的派生标识（双重摘要 = 闸门永不开，条目永远 pending） | ADR 0035 §9 | 已合并 | `tests/integration/test_gate_with_model_identities.py` |
| AC-120 | #82 向量检索：按接近度排序、pending 不是候选、自身可排除、坏载荷与错宽度跳过而不致命 | ADR 0035 §9 | 已合并 | `tests/integration/test_vector_search.py` |
| AC-101 | #82 换模型不复用旧向量（原声明 95，前置缺陷） | ADR 0035 §9 | 已合并 | `tests/integration/test_content_revision_gate.py` |
| AC-102 | #82 内容变更后向量重算（原声明 96） | ADR 0035 §9 | 已合并 | `tests/integration/test_gate_with_model_identities.py` |
| AC-103..104 | #82 短/空文本不崩溃、失败归类到既有 taxonomy（原声明 97..98） | ADR 0035 §9 | 已合并 | `src/plugins/processors/embed/test_encoder.py` |
| AC-115..116 | #82 编码器：声明维度的单位向量、批内 padding 被 mask（短文本单独与批内同向量） | ADR 0035 §9 | 已合并 | `src/plugins/processors/embed/test_encoder.py` |
| AC-114 | #120/#82 选定的运行时在本环境真跑一次推理（384 维、两次同向量） | ADR 0040 | 已合并 | `tests/integration/test_embedding_runtime.py` |
| AC-105..107 | #83 融合检索：语义召回不被字面封顶、精确命中居首、不注入词法噪声（原声明 99..101） | ADR 0035 §9 | 已合并 | `tests/integration/test_hybrid_search.py`、`tests/integration/test_search_surface.py` |
| AC-108..109 | #83 相关文章（有下限的邻居）与近重复提示（只提示、不合并）（原声明 102..103） | ADR 0035 §9 | 已合并 | `tests/integration/test_vector_search.py`、`tests/integration/test_items_api.py` |
| AC-110 | #89 单租户假设（第二个租户会被报出） | ADR 0038 | 已合并 | `tests/integration/test_tenancy.py` |
| AC-111..113 | #61 告警的两个未实现条件（卡死链、循环不存活）与循环互看 | ADR 0034 / ADR 0039 | 已合并 | `tests/integration/test_alerting.py` |
| AC-125..127 | #130 X Following frontier 截断丢推文（会话模块破坏页内降序假设） | issue #130 | 已合并 | `src/plugins/collectors/twitter/test_paging.py`、`tests/integration/test_twitter_paging.py` |
| AC-128..129 | #131 分页半途失败先推进 frontier 再丢弃已抓到的 drafts | issue #131 | 已合并 | `tests/integration/test_twitter_paging.py` |
| AC-130 | #132 分页中途失败一律归类为 blocked（真实 ErrorClass 丢失） | issue #132 | 已合并 | `src/plugins/collectors/twitter/test_walk.py`、`tests/integration/test_twitter_paging.py` |
| AC-131..132 | #133 假断档：page_sort_span 把模块内容 id 当页位置 | issue #133 | 已合并 | `src/plugins/collectors/twitter/test_walk.py`、`src/plugins/collectors/twitter/test_collector.py` |
| AC-133..138 | #145 翻译模型链：任一 model 失败不中断，全部失败才终止（分类与 detail 聚合） | issue #145 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-139 | #259 空补全的旧判据（`parse`）**已退役**：空补全改判瞬时（`network`），使 worker 可重试 | issue #145 / #259 | 已明确不做 | 决定与重做触发条件见 #259 与 ADR 0055：持续空补全仍会在 `MAX_ATTEMPTS` 后停在 `failed`；若「持续空正文」成为常态，再评估区分「持续空」并恢复永久类。测试改由 AC-22（`network`）与新用例钉住 |
| AC-140 | #145 此前零覆盖的 429/5xx 与「连不上」回退（钉住现状） | issue #145 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-141..147 | #150 翻译块内 void 元素（br/wbr/img/hr）：成对标记之外的自闭合还原 | issue #150 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-148..149 | #151 翻译：超长块被 _segment 切段后段间分隔不丢（短块字节不变） | issue #151 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-150..156 | #152 告警与诊断的归因：源标题/分类/计数/detail 摘要；`:diagnose` failed_enrichments + last_run.error_class | issue #152 | 已合并 | `tests/unit/test_alerting.py`、`tests/integration/test_alerting.py`、`tests/integration/test_sources_api.py` |
| AC-157..160 | #91 X 视频渲染为宿主受限的 `<video>`（非白名单宿主/自动播放/事件属性被剥离，降级存活） | issue #91 | 已合并 | `tests/unit/test_sanitize.py`、`tests/unit/test_properties_sanitize.py`、`src/plugins/collectors/twitter/test_collector.py` |
| AC-161 | #159 add_sources 批量建源：一次**调用**覆盖一批（ADR 0044） | issue #159 | 已合并 | `tests/integration/test_sources_batch.py`、`tests/integration/test_mcp_add_sources.py` |
| AC-162 | #159 「用户没看过的列表不能执行」的**确认指纹**判据**已退役** | issue #159 / #270 / ADR 0058 | 已明确不做 | 写工具不再 elicit，指纹随确认机制一并删除；批量仍是一次调用一个作业句柄 |
| AC-163..166 | #159 批量选项校验、幂等重放、上限拒绝与常驻上下文预算 | issue #159 | 已合并 | `tests/integration/test_sources_batch.py`、`tests/unit/test_mcp_tool_guide.py` |
| AC-167 | #167 文章抽取器提为共享件 `adapters/article.py`：抽取输出逐字节不变，且仓库里只有一份实现 | issue #167 / ADR 0045 | 已合并 | `tests/unit/test_article.py`、`tests/unit/test_structure_guards.py` |
| AC-168 | #168 采集器新增可选能力 `collect_item`：未声明的源在调用采集器之前被拒绝 | issue #168 | 已合并 | `tests/unit/test_collect_item_capability.py`、`tests/integration/test_collect_item_rpc.py` |
| AC-169..171 | #169 rss 实现 `collect_item`（WordPress 地址取全文且身份对齐 / 非 WP 回落共享抽取器 / 产物达阈值故链不二次抓取） | issue #169 | 已合并 | `tests/unit/test_rss_collect_item.py` |
| AC-172..175 | #170 按地址抓取入库（同源校验先于抓取 / 非本源拒绝且不落库 / 一条失败不影响其余 / 节拍夹取与幂等重放） | issue #170 | 已合并 | `tests/integration/test_fetch_items.py`、`tests/e2e/test_failure_paths.py` |
| AC-176 | #171 MCP `fetch_source_items` 的**确认指纹**覆盖 URL 列表**已退役** | issue #171 / #270 / ADR 0058 | 已明确不做 | 写工具不再 elicit，指纹随确认机制一并删除；该工具仍是作业句柄 + 账本（AC-193..194） |
| AC-193 | #191 `fetch_source_items` 句柄化：响应为 `work_id` + counts 且**大小不随地址数变化**（10 个与 40 个地址字节等长）；新增读侧 `get_work`、`work_units`（逐条账本 + outcome 过滤 + 错误码沿用 /v1） | issue #191 | 已合并 | `tests/integration/test_mcp_work_tools.py` |
| AC-194 | #191 「确认覆盖 targets 与 params」的判据**已退役** | issue #191 / #270 / ADR 0058 | 已明确不做 | 写工具不再 elicit；params 的校验仍是 `/v1` 的职责 |
| AC-195..196 | #192 旧 `:fetch-items` 降级为受限兼容壳：小批（≤ 20）保持既有同步逐条语义**且**把同一次运行补记进账本（终态 work + 每地址一条 unit，永不占用在飞命令索引）；超限 422 且错误体指向 `/v1/works`（不再劝调用方切批——切批正是丢结果的那种投递） | issue #192 | 已合并 | `tests/integration/test_fetch_items.py` |
| AC-197..198 | #193 两个读原语：`GET /v1/items?url=` 按**规范化后的** canonical url 精确查条目（追踪参数/片段不影响命中；未知 query 参数保留；未命中 200 空集；非 http(s) 422；pending 同样查不到）／`GET /v1/sources/{id}:queue` 队列四计数 + 由完成时间戳推导的 `rate_per_min`（不落库），计数与 `/v1/sources/{id}/enrichments` 逐行对账一致且响应为常数大小；两者并入既有 MCP 读工具（`search_items(url=)`、`diagnose_source` 的 `chain`） | issue #193 | 已合并 | `tests/integration/test_items_api.py`、`tests/integration/test_sources_api.py` |
| AC-199..200 | #194 单地址原子 `POST /v1/sources/{id}:fetch-item`：一条收据（outcome + item_id/error_class + 一个 target 的账本引用）且同时写出一条可查账本（终态 work + 该地址的 unit）／非同源是收据而非异常（`rejected` + 零请求）／源级拒绝与复数端点一致（404/422/409 且在拓取之前）；**与 work 执行器等价**（同一地址经单数端点与经一个 target 的 work，item 身份与 outcome 相同且只多出一行） | issue #194 | 已合并 | `tests/integration/test_fetch_items.py` |
| AC-201..202 | #195 行级重试 `POST /v1/enrichments/{id}:retry`：只动该行（其余行的 status/attempt_count/error_class/detail 按值不变）／重排重启额度（attempt_count 归零、失败信息清空、`next_attempt_at` 清空）但**不会无限重置**（已 queued/running 的行是幂等空操作 `requeued: 0`，succeeded 行 409，不存在的行 404）；整源端点降级为「选择 + 逐行调用同一实现」；MCP `retry_enrichments` 用 `enrichment_id` 选行（不新增工具名） | issue #195 | 已合并 | `tests/integration/test_sources_api.py`、`tests/integration/test_mcp_server.py` |
| AC-203..204 | #196 读状态一等原子 `GET|PATCH /v1/items/{id}/read-state`：写后可回读且无行=未读未标星（`updated_at: null`）／省略字段保留原值（先标读后标星仍两者为真）／同 key 重放回同一响应／空 patch 400、未知条目 404；**写读状态不动条目事实**（guid/content_hash/publish_status/content_ref/updated_at 按字面值不变）；合并走 `core.reader_state.merge_state`（与 Reader 层同一含义） | issue #196 | 已合并 | `tests/integration/test_items_api.py` |
| AC-205..206 | #197 执行器机制与四个 kind（fetch/delete/enrich/retry）：kind→executor 分派（prepare 一次 / 逐 target 执行 / `pacing` 仅出网 kind）、通用循环逐目标入账本与断点续跑、删除的文件回收挂在该行提交后；**delete** 缺失目标 = `skipped` 而非 `failed`、**enrich** 与 `backfill_chain` 逐行对账一致且重复入队 = `skipped`、**retry** 只重排 failed（succeeded = `skipped`、不存在 = `failed`）；无执行器的 kind 停在 `queued`（不认领也不失败） | issue #197 | 已合并 | `tests/integration/test_work_kinds.py` |
| AC-207..208 | #220 subscribe 与 ingest 两个 kind 的执行器（从 #197 拆出）：共享选项（policy/type/chain）**整个作业校验一次**（不可用即逐目标归因、不落任何源），逐 URL 类型推断与建源（已存在 = `skipped` 且**不改写**既有 policy、坏 URL 只失败自己）／ingest 逐条以 canonical JSON 为目标、校验与身份规则走共享的 `app.ingest`（无可用 URL 时铸 `agent.invalid` 身份），写入走 `normalise_and_store`（去重命中 = `skipped` 且指向同一 item） | issue #220 | 已合并 | `tests/integration/test_work_kinds.py` |
| AC-209..210 | #198 CLI `just work`（文件 → 命令值，数据不过模型上下文）：`submit` 从文件读目标（跳过空行与 `#` 注释）且 **stdout 只有 work_id**（列表不回显、说明走 stderr），幂等键由命令值派生（同一文件在同一幂等窗口内是同一个作业，不只是「在飞指纹去重」）／`status` / `units [--outcome failed]`（制表符分列的机器可读行）／`retry-failed` 只重发失败目标（成功项不重跑）且无失败时不提交空作业；凭据来自环境、永不打印 | issue #198 | 已合并 | `tests/integration/test_work_cli.py` |
| AC-211..212 | #199（A 组）目标已是列表的复数工具改交作业：`delete_items` / `add_sources` / `submit_items` 返回 `work_id` + 折叠 counts（**不含逐条 results**），逐条结果从 `work_units` 查；确认字段收敛为 kind + 目标列表指纹 + params；**未动任何端点**（无兼容风险）；工具面快照守卫通过 | issue #199 | 已合并 | `tests/integration/test_mcp_add_sources.py`、`tests/integration/test_mcp_server.py` |
| AC-213 | #199（B 组之一）`:retry-enrichments` 改为「服务端选择 → 提交 `kind=retry` 作业 → 202 + 句柄」：逐行结果从 `work_units` 查（回放同 key 返回同一句柄）；**选择留在服务端**（调用方不翻页持有列表）；无失败行 = 200 + `work_id: null` + 全零 counts（不提交空作业）；MCP `retry_enrichments` 的整源模式改为接受 200/202 并返回句柄 | issue #199 | 已合并 | `tests/integration/test_sources_api.py` |
| AC-214 | #199（B 组之二）`:backfill-chain` 改为「服务端选择 → 提交 `kind=enrich` 作业 → 202 + 句柄」：选择 = **最新的、仍缺该链产物的条目**（按 `enqueue_chain` 的身份 plugin/kind/lang/当前 content_hash 判定），所以「带 limit 重复调用」会前进而不是永远重选同一批；无链 422 `no_pipeline_chain`／链不可解析 422 `invalid_chain`（都在选择之前）；无待补条目 = 200 + `work_id: null`；MCP `backfill_source` 返回句柄 | issue #199 | 已合并 | `tests/integration/test_sources_api.py`、`tests/integration/test_mcp_server.py` |
| AC-215 | #199（B 组之三，收官）`POST /v1/sources/{id}/items:delete` 改为「服务端选择 → 提交 `kind=delete` 作业 → 202 + 句柄」：选择用 `items_for_source`（**与删除本身同一个谓词**，所以预览/计数/作业不可能各说一套）；无匹配 = 200 + `work_id: null`；文件回收由 worker 在该行提交后执行；源本身与游标不变（订阅继续）；MCP `delete_source_items` 返回句柄。**至此所有复数工具都返回句柄**（AC-211 的原始目标） | issue #199 | 已合并 | `tests/integration/test_sources_api.py`、`tests/integration/test_mcp_server.py` |
| AC-218..219 | #202 Reader 写路径重映射到一等读状态原子：edit-tag 与 `PATCH /v1/items/{id}/read-state` **走同一个领域函数** `apply_read_state`（结构守卫断言 `merge_state` 只有唯一调用方；等价测试断言同一 item 经两个入口写 read/star 后是**同一行、两个 flag 都在**，且 Reader 自己的 categories 仍报两个标签）；Reader 的对外形状与认证不对称不变（既有 Reader 集成测试断言**未改**） | issue #202 | 已合并 | `tests/unit/test_structure_guards.py`、`tests/integration/test_reader_api.py` |
| AC-220 | #218 读状态上工具面：读侧**并入既有工具**（`get_item` 默认响应带 `is_read`/`is_starred`，显式 `fields` 时仍只回请求的字段）；写侧新工具 `set_item_read_state`（确认后才写、省略字段保留原值）；写只动 `read_state` 行——条目自身列（guid/content_hash/publish_status/content_ref）字面值不变；点名不了任何变化的调用与不存在的 item 各自被拒且**不落库** | issue #218 | 已合并 | `tests/integration/test_mcp_tool_coverage.py` |
| AC-223 | #216 工具错误只有一种形状：`list_enrichments` 缺目标时的拒绝从手写嵌套信封改为与 `_error_from` 一致的**扁平**形状（`error` 是码字符串 + `suggestion` + `next_actions`）；结构守卫扫描 `src/app/mcp/**` 禁止再出现手写嵌套 `"error": {`（JSON-RPC 的错误体在 `app/mcp_guard.py`，不在此列） | issue #216 | 已合并 | `tests/unit/test_structure_guards.py`、`tests/integration/test_mcp_tool_coverage.py` |
| AC-224 | #203 作业账本的保留窗口：调度器按窗口清理**已完成且 `finished_at` 过期**的 work（unit 随 `ON DELETE CASCADE` 一起走）；**没有 `finished_at` 的作业永不清理**——那是仍在排队/运行的作业，它的行正在被执行者写入；`item` / `item_enrichment` 行数与条目自身列在清理前后不变；窗口默认 30 天、`PEARSS_WORK_RETENTION_DAYS=0` 永久保留、坏值回落默认（不会变成「全删」）；账本在文档里被定为**运维记录**而非事实源，counts 仍由账本折叠、不落库 | issue #203 / ADR 0046 D-F | 已合并 | `tests/integration/test_scheduler_maintenance.py`、`tests/unit/test_retention.py` |
| AC-177..178 | #174 长度度量唯一（`adapters/article.plain_length`）：升级判定按**可见文本**（实体编码不改变判定 / 不拿更短的正文换掉更长摘要） | issue #174 | 已合并 | `src/plugins/collectors/rss/test_wordpress.py`、`tests/unit/test_article.py`、`tests/unit/test_structure_guards.py` |
| AC-179 | #179 模型不可用时用例 skip 而不是失败：「不存在」与「不可读」得同一答案（reason 指向 `just fetch-model`），真实可读时照常执行 | issue #179 | 已合并 | `tests/integration/test_model_weights.py`、`tests/integration/test_embedding_semantics.py` |
| AC-180..182 | #187 work/work_unit 持久层：迁移双向往返、`(work_id,target)` 唯一约束由 DB 强制、counts 由账本折叠（无计数列） | issue #187 | 已合并 | `tests/integration/test_work_ledger.py`、`tests/unit/test_work_fold.py` |
| AC-183..185 | #188 `POST /v1/works`：响应 O(1)（句柄 + 折叠 counts，无逐条结果）／同一命令值在飞时复用同一 work（不产生第二批）／大提交不阻塞 | issue #188 | 已合并 | `tests/integration/test_works_api.py` |
| AC-186..188 | #189 读一个作业：句柄回环（账本覆盖每个 target 恰好一次）／逐目标归因（rejected 与 failed 分开计数）／账本分页与过滤稳定 | issue #189 | 已合并 | `tests/integration/test_works_read.py` |
| AC-189..192 | #190 抓取执行器：同源地址抓取入库且身份落在已有条目上（再抓一次不新增行）／跨源地址拒绝且 origin 零请求／一条失败不拖垮整批（partial）／origin 拒绝后中止本轮、其余只记 rejected 不再发请求；断点续跑按账本跳过已有行 | issue #190 | 已合并 | `tests/integration/test_work_fetch.py`、`tests/unit/test_work_fold.py` |
| AC-221..222 | #227 翻译管线的预算传递：运行时把行 deadline 放进处理器上下文（`item_ctx["deadline"]`，绝对 monotonic）、单次尝试 ≤ `min(60s, 剩余/2)` 且不足 0.5s 不发起、预算耗尽主动以 transient 失败并保留逐模型原因（不再只剩 `processor deadline expired`） | issue #227 / ADR 0047 | 已合并 | `tests/integration/test_processor_deadline.py`、`src/plugins/processors/translate/test_processor.py` |
| AC-225..230 | #228 模型健康登记表：状态在 worker（内存态、可丢、不落库）、策略随行下行 + 逐模型报告上行（不改 hookspec 签名）；首选配置顺序（冷却到期自动复位）、行内每模型最多失败 2 次（**按行不按块**）、失败冷却 5min 起翻倍封顶 1h、冷却中跳过、一次成功即复位、永久类只问一次、整链冷却立刻 transient 且零 provider 请求 | issue #228 / ADR 0048 | 已合并 | `tests/unit/test_model_health.py`、`tests/integration/test_model_failover.py`、`src/plugins/processors/translate/test_processor.py` |
| AC-232 | #230 被击杀的行保留诊断：宿主另存子进程最后 3 行日志（从最新往回拼、≤320 字符，落在 `detail[:500]` 内）并附到「没来得及上报」的失败（deadline 击杀/取消/无结果退出）；处理器以 `pearss.progress` logger 按块记 `chunk i/N`（约定即 API，无需新 hook）；子进程静掉 httpx/httpcore 的 INFO（否则每请求一行会挤掉尾部）；正常失败仍以聚合为准 | issue #230 / ADR 0050 | 已合并 | `tests/integration/test_processor_deadline.py`、`src/plugins/processors/translate/test_processor.py` |
| AC-233 | #231 `:diagnose` 增一格 `model_health`：`preferred`（下一行会先问谁）/`skipped`/`attempts_per_model`，以及每个**当前冷却中**模型的 `cooling_seconds`、`cooldown_seconds`（本次这一档）、`failures`、`last_failure_class`、`since_last_failure_seconds`；是 worker 内存登记表的**只读投影**（不落库、不缓存、时间报相对秒） | issue #231 / ADR 0051 | 已合并 | `tests/integration/test_sources_api.py`、`tests/unit/test_model_health.py` |
| AC-234..235 | #232 块级进度落盘：运行时给每行 `item_ctx["cache_dir"]`（plugin/lang/item 三级）、处理器每块成功后原子写入（临时名 + replace）并在下一块前查缓存（进度行标 `(cached)`）；重试只问缺失块且产物与一次性完成**逐字节一致**；缓存**不是事实源**（不建表、不加列、不写 item/enrichment 字段），成功后运行时删除、失败时保留（永久失败的行留一个可丢目录，写明不做清理器）；块级并发推迟 | issue #232 / ADR 0052 | 已合并 | `src/plugins/processors/translate/test_processor.py`、`tests/integration/test_processor_cache.py` |
| AC-236 | #211 ADR 0046 §7 补写兼容壳（`POST /v1/sources/{id}:fetch-items`）的**删除触发条件**：任一成立即删壳——①一个完整保留窗口（30 天）内 `audit_log` 里该 endpoint 的调用为零（可观测：审计记的是**路由模板**，`_operation`），②壳开始长功能，③下一次破坏式 API 变更窗口；时间型判据（「发版后」「过半年」）明确排除；触发条件①的可观测性由用例钉住（调用一次壳，审计行必须能被 `like 'POST /v1/sources/%:fetch-items'` 选中） | issue #211 / ADR 0046 §7 修订 | 已合并 | `tests/integration/test_fetch_items.py` |
| AC-237 | #212 `running` 作业的租约：只认「账本安静超过窗口」的 running（每写一条 unit 是一次心跳，窗口由 `PEARSS_WORK_LEASE_SECONDS` 给、默认 900 秒、`0` 关闭）——因此活的慢作业不会被第二个执行者接走（不重跑正在抓的目标、不重复出网），而没有 unit 的 running 按 `created_at` 计时仍可被接管；坏值回落默认（打错字不会静默关掉保护） | issue #212 / ADR 0046 §7 修订 | 已合并 | `tests/integration/test_work_fetch.py`、`tests/unit/test_retention.py` |
| AC-238..240 | #250 写确认默认路径单轮化（schema 只剩 `confirmed`）**已退役** | issue #250 / #270 / ADR 0058 | 已明确不做 | 写工具不再 elicit，对话框形状不复存在 |
| AC-241..242 | #251 每个写工具显式声明可编辑字段集**已退役** | issue #251 / #270 / ADR 0058 | 已明确不做 | 编辑路径整体删除；「身份/目标不可改」的语义仍成立（想改就重新发起请求），只是不再有可编辑集这个结构 |
| AC-243..245 | #252 写确认的修改路径（两段式 + 最终确认）**已退役** | issue #252 / #270 / ADR 0058 | 已明确不做 | 编辑路径整体删除 |
| AC-231 | #229 处理器子进程的日志进 journal：子进程 stderr 成为日志通道（`<LEVEL> <logger> <message>`）、宿主在子进程运行**期间**排空并按原级别重记（带 `[plugin_id item_id]` 前缀）、无级别前缀的行（traceback）按 warning 转发、转发有界（200 行 / 每行 1000 字符）；翻译链的回落行首次可在 journal 里看到 | issue #229 / ADR 0049 | 已合并 | `tests/integration/test_processor_logs.py` |
| AC-246..248 | #257 结构化可编辑字段的解析与拒绝形状**已退役** | issue #257 / #270 / ADR 0058 | 已明确不做 | 编辑路径整体删除 |
| AC-249..250 | #258 部分模型冷却时**链未走完**的失败不得是永久类：聚合在「有配置的模型未被本次询问」（策略跳过=冷却中，或行内预算已用尽）时取 `network`（瞬时、可重试），detail 以 `not tried: <模型列表>` 开头点名未尝试者；链**走完**（无跳过）时维持原判（全永久→`parse`，AC-137/139 不变） | issue #258 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-251..254 | #264 「无正文」的块/分段不进 provider 请求：判据是先剥掉 `[[n]]`/`[[/n]]` 标记再 `strip()` 非空（`_flatten_inline` 把 void 元素变成标记后，`<p><img/></p>` 的文本就是 `[[0]]`——truthy，被 `if unit_text:` 放行）；发送侧与重建侧用**同一谓词**，否则回复游标错位；被跳过的块保留原文、被跳过的分段原样回填（否则它代表的 void 元素会被删掉）。只有内联标记的块、只有空白的块、混排文档的对齐、超长块的纯标记分段 | issue #264 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-255 | #265 provider 丢掉的 `img` 标记保留图片（追加到该块译文末尾，已保留的标记不重复追加）；`br`/`wbr`/`hr` 维持 ADR 0031 §47 的「丢弃」（AC-146 不变） | issue #265 / ADR 0054 | 已合并 | `src/plugins/processors/translate/test_processor.py` |
| AC-256..258 | #268 模型降权按一行的失败**比例**：「一行里答对过」不再复位升级档。报告变为 `(model, asked, failed, last_error_class)`；行失败比例 ≥ `PEARSS_MODEL_FAILURE_RATIO`（默认 0.5）为降权行（连续降权冷却翻倍），低于阈值算抖动（不冷却不升级），该行无失败则复位 | issue #268 / ADR 0056 | 已合并 | `tests/unit/test_model_health.py`、`tests/integration/test_model_failover.py` |
| AC-259..261 | #269 `:diagnose` 的 `model_health` 增一格**窗口计数**：按模型折叠最近 10 行（attempts / failures / fallbacks / failure_rate / 最近失败类与距今秒），与「当前冷却」的 `models` 并存——让「长期半坏、但低于降权门槛所以从不冷却」的模型在诊断面可见；从不失败的模型不出现（无噪音）；冷却模型两格都在 | issue #269 / ADR 0057 | 已合并 | `tests/unit/test_model_health.py`、`tests/integration/test_sources_api.py` |
| AC-262..266 | #270 写工具**不 elicit**：16 个写工具不再注入 `confirmed`、不再发 `elicitation/create`，任何客户端（含无 elicitation 能力者）都能调用；入参 schema 无确认参数；不再出现 `-32021`；写仍有 actor 与审计归属；破坏性工具的不可逆在描述里明说且 `destructiveHint=true` | issue #270 / ADR 0058 | 已合并 | `tests/integration/test_mcp_server.py`、`tests/unit/test_mcp_tool_guide.py`、`tests/e2e/test_failure_paths.py` |
| AC-267..269 | #274 Reader 流的分页 tiebreak 与续页谓词同向（`id DESC` 配 `id <`）：时间戳相同且多于一页的组按小于一页的页长走查仍恰好覆盖全集且不重复（AC-267）；无日期的尾部组（`nullslast` 之后的一整个并列组）同样不重不漏（AC-269）；并列组里的已读条目仍能出现在 Read You 形态（`s=reading-list&it=read`）的已读走查里（AC-268） | issue #274 / ADR 0024 | 已合并 | `tests/integration/test_reader_continuation.py` |
| AC-270 | #275 Reader 流游标到达深度上限（`_MAX_SERVED`）时**走查结束**：新式 `served|published|external` 游标返回空 `itemRefs` 且不给 continuation（此前解码成 「第 1 页」会重启走查）；旧式裸整数游标的夹紧行为与「不可解析 → 从头开始」不变 | issue #275 / ADR 0024 | 已合并 | `tests/integration/test_readyou_compat.py` |
| AC-271 | #283 走查 helper 有界：`_walk_all` 在 `_MAX_PAGES`（50）页后以明确断言失败（`pagination did not terminate after 50 pages`）——一个不再前进的 continuation 表现为**失败**（点名原因）而不是挂到全局超时 | issue #283 | 已合并 | `tests/integration/test_reader_continuation.py` |
| AC-272 | #284 深度上限只对**良构**游标生效：形状（十进制 served、两个 `|`、十进制 external id）先于上限判定——`100000|garbage`（缺分隔符）与 `100000|...|xyz`（尾部非整数）一律走「不可解析 → 从头开始」（此前分别被判为「耗尽」返回空页、以及抛 ValueError 变成 500）；**良构**游标达上限仍结束走查（AC-270 不变） | issue #284 | 已合并 | `tests/integration/test_readyou_compat.py` |
| AC-273..274 | #285 `ot` 窗口只约束**有日期**的历史（`published_at IS NULL OR published_at > ot`）：已读的**无日期**条目出现在 Read You 全局 sync 形态（`s=reading-list&it=read&ot=…`）的已读集合里（AC-273）；有日期但早于窗口的条目仍在集合外（AC-274，维持现状） | issue #285 / ADR 0024（2026-10-05 修订） | 已合并 | `tests/integration/test_reader_ot_window.py` |
| AC-275..277 | #290 日志引导（原声明 AC-271..273，撞 #283/#284/#285 已合并的 AC-271..274 而让号）：幂等 `configure_logging` + correlation-id Filter 把当前请求 id 注入每条记录并渲染进格式串（AC-275）；并发请求的 id 不串号（AC-276）；启动 banner 报出生效 level 与脱敏后的 db 位置且不泄漏凭据（AC-277） | issue #290 | 已合并 | `tests/unit/test_log_setup.py` |
| AC-278..279 | #291 后台 loop 死亡时保留 traceback（原声明 AC-274..275，让号）：scheduler / work / enrichment 三处 `stop()` 的最外层异常处理从「只记异常类型名」改为 `LOGGER.exception`，journal 里有完整 message 与 call site（AC-278）；正常取消（`CancelledError`）不产生 ERROR/traceback 行（AC-279） | issue #291 | 已合并 | `tests/integration/test_loop_tracebacks.py` |
| AC-288..289 | #296 仓储/队列关键路径日志（原声明 AC-284..285，让号）：work 认领时一行 INFO（含 work id、kind、target 数，与既有 finish 汇总配合）（AC-288）；空 sweep 在 INFO 静默、DEBUG 才有 "no work"（AC-289）；幂等键复用、索引写入/删除、source 创建/命中各记 DEBUG | issue #296 | 已合并 | `tests/integration/test_repository_logs.py` |
| AC-290..292 | #297 日志量护栏（原声明 AC-286..288，让号）：INFO/WARNING 事件驱动且 O(1)（空转 tick / 空 sweep 在 INFO 静默，AC-290）；`rate_limited_logger` 同一 key 每窗口一行 + `suppressed N` 汇总、级别与原行一致（AC-291）；存储 N 条 draft 的 INFO 行数与 N 无关（含全量 dedupe 的第二遍）（AC-292） | issue #297 | 已合并 | `tests/unit/test_log_limits.py`、`tests/integration/test_log_volume.py` |
| AC-280..282 | #292 关键决策点 debug（原声明 AC-276..278，让号）：dedupe 决策在 DEBUG 报出动作、matched_id 与命中来源（identity vs content_hash）（AC-280）；`normalise_and_store` 逐条报 action + item id（AC-281）；默认 INFO 不产生这些行（AC-282）；fetch 预检拒绝原因与 work 逐单位落账同样只记 DEBUG | issue #292 | 已合并 | `tests/integration/test_decision_debug_logs.py` |
| AC-286..287 | #295 外部抓取 per-request 日志（原声明 AC-282..283，让号）：每次 `_request` 在 DEBUG 留一行脱敏摘要（method、脱敏 URL、status、耗时、字节数 / not modified；失败为 class + 耗时），绝不打印 header/凭据/body | issue #295 | 已合并 | `tests/unit/test_http.py` |
| AC-293..294 | #298 处理器子进程日志转发带 correlation id（原声明 AC-289..290，让号）：`_ChildLog` 在 spawn 时捕获一次 correlation id，并以 `extra={"correlation_id": …}` 盖在每条重记的行上，Filter 对已带 id 的记录不再覆盖；子进程写一条 warning，宿主重记的行含该 id 与 `[plugin_id item_id]` 前缀（AC-293），无请求上下文（sweep）时一次子进程运行的所有行共享一个非空 id（AC-294） | issue #298 | 已合并 | `tests/integration/test_processor_logs.py` |
| AC-283 | #293 调度器空转可观测（原声明 AC-279，让号）：`report.ran == 0` 的 tick 在 INFO 级静默、DEBUG 级打一行心跳（含 considered 数与最近的下次 due 时刻）；`ran > 0` 维持原 INFO 汇总 | issue #293 | 已合并 | `tests/integration/test_scheduler.py` |
| AC-284..285 | #294 请求级访问日志（原声明 AC-280..281，让号）：每请求一行应用访问日志，含 correlation id、method、route 模板、status、actor、duration_ms（AC-284）；读请求 2xx 默认 DEBUG，写请求与 ≥400 的响应 INFO（AC-285：读 200 在 INFO 静默、5xx 在 INFO 一行）；同时静掉 `uvicorn.access` 以免双写 | issue #294 | 已合并 | `tests/integration/test_request_logs.py` |
| AC-295 | #308 启动 banner 的 db 位置对「无路径的网络型 URL」不泄漏凭据：`describe_database_url` 改用 `urlsplit`，只输出 scheme + path，authority（`user:password@host`）与 query 一律丢弃；无 scheme 的字符串仍报 `<unparseable>`。修前 `postgresql://operator:s3cr3t@db.internal` 被当成 path 打印成 `postgresql:///operator:s3cr3t@db.internal`，密码进 journal | issue #308 | 已合并 | `tests/unit/test_log_setup.py` |
| AC-296 | #309 访问日志对未匹配路由的路径做控制字符转义：`_operation` 走 `adapters/log_safety.escape_control_characters`，请求路径里的换行（解码后的 `%0A`）渲染成 `\x0a` 而不是真的断行，于是一个请求只产生一行日志、无法伪造日志条目；审计行同一函数受益。printable 字符（空格、CJK）原样保留 | issue #309 | 已合并 | `tests/unit/test_log_safety.py`、`tests/integration/test_request_logs.py` |

「保留」= 号段已分配但测试尚未合并；实现时按该号段编号，**不要另起号段**。

「已明确不做」= 该号段的 issue 已裁决 declined：证据列写明决定与**重做触发条件**，
不要求测试引用。把 declined 的号段留在「保留」是错的——「保留」读作「pending」（issue #125）。

> **#87（core 助手的执行点）**：它的判据是机械属性而非场景，由
> `tests/unit/test_structure_guards.py::test_every_core_helper_has_a_production_caller` 守住，
> 因此不占 AC 号段。

## 一次真实的整体让号（2026-09-28，issue #95）

两个工作流并行声明了同一片号位，**谁都没错**——原布局假设 #63..#68 用 36..48（13 个
号位：4+1+3+3+2），于是 #69..#76 从 49 起、检索工作流从 72 起。

合并后的事实是 #63..#68 用了 **36..54**（19 个号位）：#64 的工具体覆盖追加了
`AC-47..52`（6 个），把已合并块撑大了 6。于是 49 及以后的每一个声明都撞上了已合并的
号段——#69 的 49..51 撞 #64 的 47..52，#70 的 52..55 同时撞 #64 与 #68。

处置：**整条前沿右移 6 位**。`AC-49..71` → `AC-55..77`，`AC-72..103` → `AC-78..109`。
每条 issue 的场景数与相对顺序不变（只平移），只有号段整体移位——这是代价最小的让号：
另一种做法是把 #69..#76 的 23 条场景压进 17 个号位，那意味着要合并或删掉场景，是真实
的覆盖损失；而检索工作流的 32 个号位**全部尚未实现**（零测试引用），平移只改本表与
声明。上表「归属」列括号里的「原声明 N」保留了让号痕迹。

## 模块内登记表（独立命名空间）

这些表与上面的跨模块序列**互不干扰**：它们的 `AC-N` 只在所属模块内有意义，读跨模块
号段时不要把它们算进去。（它们目前的取值都落在历史号段 AC-1..35 内，所以守卫不需要
额外的命名空间标记；新模块表若要用 AC-36 以上的号，先在本文件里占一行。）

| 模块 | 登记表 | 号段 | 说明 |
|---|---|---|---|
| fulltext 处理器 | `src/plugins/processors/fulltext/SCENARIOS.md` | AC-1..AC-13 | 与链级场景分两册（AC-7/AC-8 的测试落在 `tests/integration/test_fulltext_chain.py`） |
| translate 处理器 | `src/plugins/processors/translate/test_processor.py` 文件头 | AC-19..AC-24 | 与 ADR 0031 同册（该文件是规范来源） |

## 怎么用

占号（在自己的 issue 里新开一条场景清单时）：

1. 看本表最后一行，取下一个空闲起点，**留足本 issue 的场景数**；
2. 在本表加一行：号段、归属（`#NN 简述`）、声明来源、状态 `保留`、证据留空；
3. 测试文件头写 `Scenario register (normative source: <上表「声明来源」的路径>)`；
4. 合并后把状态改为 `已合并` 并补上证据路径。

如果合并时发现号段被别的分支占了（先合并者优先），把本表那一行与所有测试文件头
一起重编号，并在提交说明与 issue 评论里写明让位原因。
