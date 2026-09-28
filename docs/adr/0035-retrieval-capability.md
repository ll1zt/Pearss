# 0035: 检索能力——FTS5 中文/日文全文检索、本地多语言向量、RRF 混合

Status: accepted
Date: 2026-09-27
Context: 用户提出增强 pearss 的查询检索能力（全文 / 语义 / 混合）。这是 P3 之后的第一个
**新能力**，而架构 §1.3 曾把「全文搜索引擎」列为非目标——本 ADR 既是技术决策，也是对该
非目标的**边界修订**。

## 背景：实测，不是推断

全部结论来自本机实测（SQLite 3.53.3 / aarch64 8 核 / 14 GB / 无 GPU；语料 7,000 items，
正文平均 391 字符、合计约 2.7M 字符）。

### 事实 1：语料是中日英混合，不是纯中文

抽样 100 条标题：**含汉字 93、含平假名 5、含片假名 1**。例：`今日は運が悪い。`、
`QT AIも半導体も重要鉱物(レアアース)も…`。因此任何「中文分词」方案都不成立。

### 事实 2：FTS5 内置分词器对 CJK 无效

```
文本: "eBPF 在内核中运行沙箱程序，用于可观测性与网络"

unicode61（默认）切分 → ['ebpf', '在内核中运行沙箱程序', '用于可观测性与网络']
                        ↑ 整段中文成一个 token
  搜 '内核'     → 0 命中
  搜 '沙箱'     → 0 命中
  搜 '可观测性' → 0 命中      （只有 eBPF 这类拉丁词能命中）

trigram 切分 → 需要 ≥3 字才命中
  搜 '内'   (1字) → 0
  搜 '内核' (2字) → 0        ← 2 字词是中文检索主战场
  搜 '内核中'(3字) → 1
```

### 事实 3：重叠 bigram + unigram 是混合语言下唯一可行解

```
预分词后 → ['在','内','核','中',…,'在内','内核','核中',…,'ebpf']
  搜 '内核'    → 命中
  搜 '可观测性' → 命中（切为 ['可观','观测','测性']）
  搜 '沙箱'    → 命中
  搜 'ebpf'    → 命中（大小写归一）
  'どうやったら' → ['どう','うや','やっ','った','たら']  假名可切
  'カタカナ'     → ['カタ','タカ','カナ']               片假名可切
  繁体 '熊日記'  → ['熊','日','記','熊日','日記']       同一套规则
```

孤立 CJK 字回退 unigram 是必要的：仅 bigram 时，单字查询 `神` 只命中字面「神」，
漏掉「神经网络的原理」「日本神户」（实测）。

### 事实 4：bigram 的 AND 语义有假阳性，必须用短语

```
doc1: "可观 测性 是 观测 的 特性"      ← 三 bigram 散落，语义无关
doc2: "可观测性 很好"                 ← 真正相邻
查询 '可观测性' → ['可观','观测','测性']

AND 形式  MATCH "可观" AND "观测" AND "测性"  → 命中 [1,2,3]   ← 1 是假阳性
短语形式  MATCH "可观 观测 测性"              → 命中 [2]      ← 正确
```

### 事实 5：FTS5 的 `rowid` 必须是 INTEGER，而 `item.id` 是 32 位十六进制

```python
insert into t(rowid, a) values ("761534b2156e2977837bada2cadc1ec5", …)
→ IntegrityError: datatype mismatch
```

所以「插入时指定 rowid = item.id」不可行，必须选一种映射方案。

### 事实 6：contentless 表的 DELETE 需要显式选项

```python
create virtual table t1 using fts5(a, content='');  delete from t1 …
→ OperationalError: cannot DELETE from contentless fts5 table: t1
create virtual table t2 using fts5(a, content='', contentless_delete=1);  delete …
→ 成功
```

ADR 0021「删除就是删除」下这条路径必然被走到，所以该选项不是可选项。

### 事实 7：列表查询全表扫 + 临时排序，根因是**从未 ANALYZE**

```
实测（7,000 items）：任何列表/搜索/计数查询都是
  SEARCH item USING INDEX sqlite_autoindex_item_2 (tenant_id=?) | USE TEMP B-TREE FOR ORDER BY
  ↑ tenant_id 单租户下是常量 → 等于全表扫；ix_item_status_published 完全没被用上

加 (published_at desc, id desc) 索引但不 ANALYZE → 仍然不用那个索引（0.58ms 原地不动）
同一索引 + ANALYZE                              → SCAN … USING INDEX（临时排序消失）

耗时： 7,100 条: 0.58ms → 0.03ms (19×)
      100,000 条: 10.89ms → 0.04ms (272×)
按源过滤: SCAN item（无 source_id 前缀索引）
```

### 事实 8：本机规模下**不需要** ANN 索引，也**不需要** numpy

暴力全量点积实测（7,000 × 384 维，**纯标准库**）：

| 实现 | 耗时 |
|---|---|
| `sum(a*b for a,b in zip(r,q))` ← 最初误测的写法 | 224 ms |
| `sum(map(operator.mul, r, q))` | 50 ms |
| **`math.sumprod(r, q)`**（3.12+ 内置 C 函数）| **10.6 ms** |
| `math.sumprod` + `heapq.nlargest` top-20 | 11.1 ms |
| 30,000 × 384（4.3× 数据）| 48.6 ms |

**更正**：本 ADR 初稿写「纯 Python 224 ms，需先引入 numpy」——那是**测量方法错误**
（把生成器表达式的开销当成了语言上限）。`math.sumprod` 是 CPython 的 C 实现，且项目
floor 已是 Python 3.13（`requires-python = ">=3.13,<3.14"`），故它**必然可用且零依赖**。

端到端混合链路实测（FTS5 + 向量全量 + RRF 融合）：**11.4 ms**。
因此：
- **不引入 numpy**：为 10ms 的路径加一个二进制依赖不划算，而且 `math.sumprod` 就在标准库里
- **不上 ANN 索引**：`sqlite-vec` 解决的是 10 万～千万级问题，此处是过早优化

存储形式的权衡（同一次实测）：`list[float]` 每条 ~3 KB 开销但点积 **10.4 ms**；
`array('f')` 紧凑（1.5 KB/条）但点积 **38 ms**（约 3.7× 慢）。float32 存储相对 float64
的 top-20 排序重合 **20/20**，即精度无损。故：**存 float32 紧凑，载入内存时转成
`list[float]`** 供 `math.sumprod` 使用——两边都拿到最优。

### 事实 9：存储代价可以忽略

7,000 篇正文、预分词膨胀 1.72×：普通 FTS5 = **8.8 MB**；contentless + 映射表 = 2.5 MB。
差 6.3 MB，而磁盘 144 GB 可用。

## 决策

### 1. 分词：应用层预分词（重叠 bigram + unigram），入库与查询同一个函数

`core/text_search.py` 暴露唯一的 `tokenize()`：

- CJK 连续段（Han `\u4e00-\u9fff` + 平假名 `\u3040-\u309f` + 片假名 `\u30a0-\u30ff`）
  产出**每个字（unigram）+ 相邻二元（bigram）**
- 拉丁/数字连续段整词保留并小写归一
- **入库与查询必须用同一个函数**——这是本方案唯一不能破的不变量，只由测试守住

**否决**：
- `unicode61` / `trigram`：事实 2 实测无效。
- **jieba**：nixpkgs 里有（0.42.1），但（a）不处理日文（需 MeCab），（b）词典随版本演进，
  **升级会导致索引与查询分词失配**——同一文本切出不同 token 就搜不到，且是静默的。
  bigram 是确定性的、脚本无关的，且与 Lucene/Elasticsearch 的 `cjk_bigram` token filter
  同构（工业界对 CJK 的标准做法）。日后若需更好召回，替换 `tokenize()` 并**全量重建索引**
  即可，接口不变。

### 2. 查询：重叠 token 用**短语**匹配，不用 AND

```
MATCH '"可观 观测 测性"'     ← 靠位置信息强制相邻
```

事实 4 已构造出 AND 形式的假阳性。短语形式语义是「这些 token 依次相邻出现」，正是
「文中有这个词」的正确表达。

### 3. 索引结构：普通 FTS5 + `item_id UNINDEXED`（**不用** contentless）

```sql
CREATE VIRTUAL TABLE item_fts USING fts5(
    item_id UNINDEXED,      -- 过滤/回表用，不参与全文索引
    title_tok,              -- 预分词后的标题
    body_tok,               -- 预分词后的正文
    tokenize='unicode61'
);
```

**理由（权衡后选择「复杂度更低」的一侧）**：
- 事实 5 迫使我们放弃「rowid = item.id」；两个可行替代是 A（本决策）与 B（contentless + 独立
  `(rowid, item_id)` 映射表）。
- B 省 6.3 MB，代价是：删除要**两张表同事务**（漏一个即孤儿行）、**contentless 不存任何列值**
  所以每次取结果都要 JOIN 映射表回 `item`、以及事实 6 的 `contentless_delete=1` 依赖。
- 磁盘 144 GB 可用，6.3 MB 换不来这些复杂度。
- 原文本来就不在 FTS 里（在 CAS filestore，`content_ref`），所以 A **没有**制造第二份原文副本，
  不违反「大产物存文件系统、DB 只存引用」。

若日后确要改 contentless：**必须同时**加 `contentless_delete=1`（事实 6），否则删除运行期报错。

### 4. 闸门：FTS 结果一律 JOIN 回 `item` 过滤 `visible_item_rows()`

FTS 表**包含全部条目**（含 `pending`），可见性只在查询时用现有闸门谓词过滤。

理由与 `items_query.py` 既有注释同一逻辑：「闸门在 SQL 里强制，任何 filter 都不能放宽它」。
若把闸门复制进写入路径（只索引已发布），就出现了**第二个闸门事实源**，且条目
`pending → ready` 时要补索引——两处会漂移。选 JOIN 方案，闸门仍是唯一定义。

### 5. 向量：本地多语言模型，**不用**远程 endpoint

用户决策：不确认 translate endpoint 是否有 `/v1/embeddings`，直接本地跑。

- **模型**：`multilingual-e5-small`（**384 维**）。多语言，覆盖中/日/英，小模型可在 CPU 跑。
- **否决 `bge-small-zh-v1.5`**：纯中文模型，对本语料的日文/英文无效（事实 1）。
- **推理**：ONNX Runtime（nixpkgs 有 1.27.1）或 sentence-transformers（nixpkgs 有）。无 GPU，
  8 核 CPU。embedding 是**离线派生**，不在请求路径上，慢可接受。
- **存储**：SQLite BLOB 列（384 维 × 4 B = 1.5 KB/条；7,000 条 ≈ 10 MB）。载入内存后暴力检索。
- **不做 ANN**：事实 8。

### 6. embedding 走**既有 processor 链机制**，并把模型身份纳入幂等标识

embedding 是派生物，与 translation / fulltext 同类，因此复用 `item_enrichment` +
`ProcessorSpec` + `input_hash` + `backfill` / `retry-enrichments`，不新造一套生命周期。

**但必须修一个既有缺陷**：现有 `uq_enrichment_product` 是
`(item_id, plugin_id, plugin_version, kind, lang, input_hash)`，`model` 列**存在但不参与唯一性、
也不在写入路径填充**。换 embedding 模型时维度与语义空间都变，**旧行会静默失配且不被重算**。

修法：把模型身份纳入标识（模型名 + 维度 + 版本）。**这是 embedding 的前置条件，不是可选优化。**

### 7. 混合检索：两路**全量**并行 + RRF 融合，**不做级联**

```
路径 A: FTS5 BM25（精确关键词，0.3–1.4ms）
路径 B: 向量余弦全量（语义，`math.sumprod` 后 10.6 ms）
融合:   RRF（Reciprocal Rank Fusion, k=60）
```

**否决级联（FTS 粗筛 200 → 向量重排）**：不含查询关键词、只有语义相关的文章**永远进不了
候选**——语义召回被字面召回封顶。这正是「换了说法就搜不到」的成因，而它恰好是向量检索要
解决的问题。

**不引入 numpy、不引入向量扩展**：事实 8 已更正——`math.sumprod`（标准库内置 C 函数，
Python 3.12+，项目 floor 为 3.13）将全量点积做到 10.6 ms，完整混合链路 11.4 ms。
这比引入一个二进制依赖更简单，且零供应链成本。
级联只在规模大 10 倍后再考虑（30,000 条时 48.6 ms，仍在可接受范围）。

### 8. 用「相关文章」替代「实时聚类」

**否决实时聚类**，理由是没有消费方：
- 79 个活跃源、7,000 条，且都是**个人博客**（不是通讯社），同一事件的重复报道很少；去重
  已由 `content_hash` 完成
- 滑动窗口聚类要引入：窗口重算 + 聚类算法 + 簇的持久化与稳定 ID + 增量归簇逻辑——这是
  **另一个子系统**，不是检索的一个特性
- **聚出来给谁？** Reader API 是 Google Reader 协议，没有「事件簇」概念；MCP 工具面也没有。
  没有消费者 = 做了也不会被用
- 与 ADR 0021 冲突：簇成员变动时簇 ID 是否变，会引入新的状态一致性问题

**替代**（覆盖聚类约 90% 价值，且是一个查询）：
- **相关文章**：给定条目，向量找 top-N 相似
- **近重复提示**：阈值化相似度，用于**提示**而非自动合并

### 9. 场景编号与 golden query 集

新增场景从 **AC-78** 起（AC-1..35 已占用；AC-36..77 由 MCP 设计工作流 #63–#76 先占用）。

号段的**单一事实源是仓库根的 `SCENARIOS.md`**：占号先在那里加一行，再照那行编号——
不要照本行推算，本行只是当时的一次快照。本 ADR 原来写「从 AC-36 起」，而 36..54 早已
被 #63–#68 合并占用；当时为检索工作流声明的 72 起也随后整体右移 6 位（已合并块比原布局
多用了 6 个号位），故现在是 **AC-78** 起。来龙去脉见 SCENARIOS.md「一次真实的整体让号」
与 issue #95。

**必须建立 20～30 条中日英三语 golden query 测试集**（含简繁、两字词、假名、英文混排、
跨语言同义），因为「入库与查询同函数」这个不变量**只有测试能守住**：分词函数一改、
模型一换，索引与查询就会静默失配，而系统不会报错。该集合在 S2 交付时跑通，之后每次
动分词或换模型都回归。

**已知限制（记录而非隐藏）**：简繁不互通（实测「內核」只命中自身）——bigram 无法桥接码位
差异。由向量层兜底；若日后需要，入库时做繁→简折叠并作为规范化的一部分写进不变量。

## 已否决（汇总）

| 方案 | 理由 |
|---|---|
| `unicode61` / `trigram` 分词 | 事实 2：中文搜不到 / 需 ≥3 字 |
| jieba 预分词 | 不处理日文；词典演进导致**索引与查询静默失配** |
| AND 语义的 bigram 查询 | 事实 4：构造出假阳性 |
| contentless 表 | 省 6.3 MB 换两张表 + JOIN + 删除事务；磁盘 144 GB |
| rowid = item.id | 事实 5：类型不匹配，不可行 |
| 远程 embedding endpoint | 用户决策：直接用本地模型 |
| `bge-small-zh-v1.5` | 纯中文，对日文/英文无效 |
| ANN 索引（sqlite-vec / pgvector）| 事实 8：7,000 条规模上是过早优化 |
| 级联式混合检索 | 语义召回被字面召回封顶 |
| 实时聚类 | 无消费方；是独立子系统；与 ADR 0021 的簇 ID 稳定性冲突 |
| Elasticsearch / Meilisearch | 数据 8 MB、单用户；引入独立服务违背「运维轻量」 |
| 把 embedding 存成每篇一个文件 | 7,000 小文件的 inode 与备份代价，且失去 SQL 检索 |

## 对架构 §1.3 的边界修订

原非目标写作「**全文搜索引擎**」。修订为：

> - 通用全文搜索引擎（**边界**：pearss 只为自己订阅的条目建检索——FTS5 精确关键词 + 本地
>   向量语义检索 + RRF 融合，规模以万条计；**不**引入独立搜索服务、不做通用网页搜索、
>   不做跨租户检索）

这样 FTS5 与向量在边界内，Elasticsearch 在边界外。

## 影响

- 新增 `core/text_search.py`（`tokenize()`，纯函数，无 I/O——守分层规则）
- 新增 `adapters/repository/search.py`（FTS 写入 / 查询 / 回填）
- 新增 `plugins/processors/embedding/`（复用处理器链）
- `core/enrichment.py`：新增 `EnrichmentKind.EMBEDDING`；**不**加入 `CONSUMABLE_KINDS`
  （向量不是文本输入）
- migration：FTS5 虚表 + 索引 + 模型身份纳入唯一标识
- `app/items_routes.py` / `app/mcp/tools/reads.py`：检索面
- 依赖：`onnxruntime` 或 `sentence-transformers`（本地 embedding 推理），经 `uv add`。
  **向量检索本身零依赖**（`math.sumprod` + `heapq`，均为标准库）
- 架构文档 §1.3 / C2 行同步修订
- 数据集：三语 golden query 集（fixture）

## 交付顺序

1. **S0** 统计与索引（ANALYZE + 排序索引 + 按源索引）——零风险，且不先做就无法干净地测 FTS
2. **S2** FTS5 中日文全文检索 + golden query 集
3. **S2b** 检索面（API / MCP / 闸门）
4. **S3** 本地多语言 embedding + 模型身份纳入标识
5. **S3b** 相关文章 / 近重复提示
6. **S4** RRF 混合检索（**零新依赖**：`math.sumprod` + `heapq`）

各步独立可交付、独立可测，拆分为原子化 issue（见 issue 追踪）。
