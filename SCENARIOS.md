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
| AC-44..46 | #67 submit_items 确认指纹 | `docs/api/mcp-tool-guide.md` §3 | 已合并 | `tests/integration/test_submit_items_confirmation.py` |
| AC-47..52 | #64 工具体覆盖（追加号段） | `docs/api/mcp-tool-guide.md` §3 | 已合并 | `tests/integration/test_mcp_tool_coverage.py` |
| AC-53..54 | #68 list_plugins 的 limit 与分页 | `docs/api/mcp-tool-guide.md` §2 | 已合并 | `tests/integration/test_list_plugins_limit.py` |
| AC-55..57 | #69 guide 一致性与缺席的守卫（原声明 49..51） | `docs/api/mcp-tool-guide.md` §2 / §6 | 保留 | — |
| AC-58..61 | #70 search_items 的 q 语义与边界（原声明 52..55） | issue #70 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（描述必须写明 q 只匹配标题、full_text 搜正文） |
| AC-62..63 | #71 窄工具与 /v1 的形状差异（原声明 56..57） | issue #71 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（每个工具必须落在 OpenAPI 声明的端点上） |
| AC-64..65 | #72 list_enrichments 的维度（原声明 58..59） | issue #72 | 已合并 | `tests/integration/test_items_api.py`（`/v1/items/{id}/enrichments`） |
| AC-66..68 | #73 工具面常驻上下文预算（原声明 60..62） | issue #73 | 已合并 | `tests/unit/test_mcp_tool_guide.py`（预算守卫 + 任务级工具）、`tests/integration/test_sources_api.py`（`:diagnose`） |
| AC-69..71 | #74 outputSchema 空壳（原声明 63..65） | issue #74 | 保留 | — |
| AC-72..74 | #75 工具名命名空间（原声明 66..68） | issue #75 | 保留 | — |
| AC-75..77 | #76 写工具守卫统一（原声明 69..71） | `docs/api/mcp-tool-guide.md` §3 | 保留 | — |
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

「保留」= 号段已分配但测试尚未合并；实现时按该号段编号，**不要另起号段**。

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
