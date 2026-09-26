# 0028: Fulltext 处理器——摘要型 feed 的网页正文抽取

Status: accepted
Date: 2026-09-23
Context: issue #28。blog 文件夹 13 个订阅源只有摘要（Dejavu、绅士喵、yihong0618、
EGOIST、Erzbir、FooFish、回声、I'm Kai、Peng Liu、Mozi、苏洋、kxxt、Hank），
逐一探测确认**没有一个是 WordPress**——引擎覆盖 Hugo、Halo、Astro、Quartz、
Gatsby 与自建站。现有全文能力（`plugins/collectors/rss/wordpress.py`）靠
WordPress 摘要指纹 + REST `content.rendered` 升级，对这些站点永远不会触发，
且这是正确行为：它们是博主主动选择只输出摘要。

## 决策

### 1. 通用全文走处理器链，不进采集器

`fulltext` 作为第二个处理器插件（`src/plugins/processors/fulltext/`），与
translate 同构：源通过 `pipeline_chain: [{"plugin_id": "fulltext",
"kind": "fulltext"}]` 显式启用。理由：

- 采集器不该为「feed 之外的第二网络请求」负责——WordPress 路径特殊在
  REST API 与 feed 同源同信任，而任意网页抽取是独立的信任与适配问题；
- 按源 opt-in 是唯一礼貌的量纲：启用链的源才产生逐条原文页抓取，运算量
  与源站压力都由 operator 的显式决定兜底。全局默认开启等于替所有博主
  决定被爬。

### 2. 新增 `EnrichmentKind.FULLTEXT`，产物即正文、结构对齐翻译

kind 枚举扩展（core.enrichment）。产物（artifact）是经 ADR 0007 消毒的
HTML 片段，保留原文块级标记（§4）；闸门语义不变——required spec 成功才
放行。产物 pin 在 `input_hash` 上，内容变更加新行，永不回写 Item（ADR 0018
的「产物不改写条目」不变量原样适用）。

Reader API 不展示 fulltext 产物的初始决定已修订（2026-09-24，issue #29
灰度第一天）：产物入库但对唯一消费路径不可见，等于读者永远拿不到全文，
验收判据「NNW 显示全文」不可能达成。现在 Reader API contents 沿 ADR 0018
的 merge 模式输出产物栈：fulltext 包住摘要（全文在上、原摘要留分隔线下），
译文再叠于其上——链序 fulltext → translate 对应阅读栈 译文 → 全文 → 摘要。
产物照旧不回写 Item。原文合并视图的其余部分维持本篇其余决策不变。

### 3. 阈值与回退语义

- `MIN_PLAIN_CHARS = 200`（可见文本，纯文本长度比较，标记不计重）：
  条目正文已达标 → 不发请求、空产物成功、闸门照常打开。阈值刻意低——
  误跳过的代价（读者看到 stub）小于误抓取的代价（对博主多一次请求）。
- `MIN_ARTICLE_CHARS = 120`：抽取结果低于此为探测器失手（付费墙 stub、
  JS 壳），抛 `parse` 而非假装成功——假产物会把闸门开在空内容上。
- 抓取失败分类对齐错误分类法：5xx/超时 → `network`（worker 现有退避
  重试），SSRF 拒绝 → `blocked`，200 但无可抽取正文 → `parse`（不自动
  重试）。任何失败路径都保留原摘要——「摘要是底线」与 WordPress 路径
  的回退语义一致。
- title-only 条目：空产物成功（translate 的先例）；对没有正文的条目
  失败只会把闸门死锁在链永远无法处理的内容形态上。

### 4. 抽取器：结构性算法，无站点规则

lxml 解析 → 丢弃非内容子树（script/style/nav/aside/header/footer/form/
svg/iframe…）→ 在剩余容器级元素中按可见文本密度取胜者 → 保留胜出容器的
子节点标记。不引入 readability-lxml/trafilatura 依赖：博客页的结构足够
规整，结构算法已够，而第三方抽取器引入的是一整条供应链与不可解释的
启发式。

**产物形态：保留原文标记，不拍平为 `<p>`（2026-09-26 修订，issue #32）。**
原实现把抽取结果降为纯文本再逐段包 `<p>`，实测（Dejavu、kxxt 等摘要源）
丢失标题、图片、代码块、列表、引用、表格与链接，嵌套块（如
`<blockquote><p>`）重复输出——博客的排版、配图与代码在阅读器里全部消失。
现在序列化胜出容器的子节点：块级标记保留原标签，经 ADR 0007 的
allowlist（已含 h1-h6/img/pre/code/ul/li/blockquote/table/a/figure）消毒；
`pre` 内空白不折叠（缩进是代码语义）；相对 `href`/`src` 以条目 URL 为基
绝对化（产物渲染在阅读器/手机上，相对路径在那里即断链），`img alt` 保留。
容器自身的 `<article>`/`<main>` 标签不输出——那是包装，不属于内容。

### 5. 内容门槛按可见文本度量，不按标记

`_plain_length` 一律折算为可见文本（lxml 取 `text_content()` 后折叠空白）
再计数，解析失败则退回按空白切词。理由：门槛用于「这条路要不要抓」与
「抓到的算不算文章」两个决定，两者问的都是内容体量；标记权重若能凑数，
一个短摘要配上几十个空 `<p>`/`<br>` 就能伪装成达标。这与全文产物的形态
决策（§4，保留标记）是两条独立的线：存储保留结构，判定只看可见文本。

### 6. 信任边界：产物在进程内消毒后才落存储

原文页与 feed 正文同等不可信（AGENTS.md 安全规则）。抽取结果过
`adapters.sanitize.sanitize_html`（ADR 0007 的同一 allowlist）再交给
`store`，所以任何输出路径读到的产物与读 feed body 的信任等级相同。
抓取经 `adapters.http.fetch`（超时、2.5MB 上限、SSRF 守卫、手动重定向
逐跳检查），运行于 enrichment 子进程沙箱内，出网走宿主代理。

## 已否决

- **进 RSS 采集器做通用升级**：把「要不要抓原文页」的决定权从 operator
  手里拿走，且采集器无法知道 operator 愿意为哪些源付这个成本。
- **复用 WordPress 指纹思路做「通用摘要指纹」**：非 WP 站点没有可靠
  指纹（desc 长度不可靠，正文摘要 feed 的合法形态太多），误判会把
  请求打到不该打的站点。显式 opt-in 才是诚实的选择。
- **backfill 时全量回填**：存量回填是逐条抓原文页，对博主站点是真实的
  爬取压力。该决定归 #29 的灰度节奏管（每源至多一次、限速、避开高峰），
  插件本身不提供任何「批量补抓」捷径。

## 影响

- `core/enrichment.py`：`EnrichmentKind.FULLTEXT`；无 migration（enum
  存 VARCHAR，DB CHECK 不约束 kind 取值，历史 migration 不随枚举演进）。
- `app/enrichment_worker.py`：`default_processors()` 注册 fulltext。
- `app/sources_routes.py` 与 MCP `add_source` 描述：kinds 清单补 fulltext。
- contract snapshot（mcp_tools.json）随描述更新。
