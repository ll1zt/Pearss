# ADR 0045：内容级发现——地址由外部提供，采集器按地址形状抓取（`POST /v1/sources/{id}:fetch-items` + MCP `fetch_source_items`）

- 状态：accepted
- 日期：2026-09-30
- issue：#167、#168、#169、#170、#171、#172（本 ADR 是 #172 的交付物）
- 相关 / 修订：ADR 0017（能力声明与双向校验）、ADR 0022（`submit_items` 与 `source_id` 溯源边界）、ADR 0044（批量确认先例）、ADR 0029（抓取礼貌）、ADR 0010（MCP 经 `/v1`）、ADR 0028（fulltext 处理器）、ADR 0036（工具面是 `/v1` 的投影）

**新增场景从 **AC-167** 起**（AC-167..176，见 `SCENARIOS.md`）。

## 背景

**用户实测**：源是 `https://kogumagoya.com/blog/feed`（WordPress）。feed 只有 10 条；站点有
568 篇历史文章（例如关于 NAIST 的 12 篇）。**历史文章不在 feed 里**，而 agent 能枚举它们
（归档页、站内搜索、sitemap）。

现状（读代码 + 实测，非推断）：

- 采集器只能解析**自己的 listing**：五个采集器的 `parse` 都吃源文档（feed / 预览页 /
  listing JSON）。**没有"按地址取一条"的入口。**
- `collect_source` 只抓源 URL，且 `max_pages` 不对外暴露（只有 scheduler 用默认值 1）。
- **feed 分页不是标准**：本 feed 无 `rel=next`/`rel=prev`，`?paged=58` 直接 404。用 feed 抓全量
  实测要 57 次请求，而且只拿到摘要（摘要长度 186 < fulltext 的 `MIN_PLAIN_CHARS=200`，等于白抓）。
- 同站 WP REST：`/blog/wp-json/wp/v2/posts?include=<id>&_fields=…` 返回全文与 `guid`；
  `per_page=100&page=1..6` 覆盖 568 篇（**6 次请求**）。
- 站点知识（怎么抓、怎么解析、身份怎么定）**已经在采集器里**。"发现"是另一件事；缺的是把
  "发现"变成一等输入。

## 决策

### 1. 发现是显式输入

调用方（agent）提供一批**地址**，部署去抓。v1 只有这一种生产者；采集器自己声明的 `discover()`
推迟（决策 9）。地址的形状 v1 只有一种：**item 形状（1 地址 → 1 条）**；listing 形状
（1 地址 → N 条，即今天的 `fetch`+`parse`）留作未来。

### 2. 新可选能力 `collect_item(*, source_id, url, client) -> list[ItemDraft]`

与 `fetch`/`parse` 同契约：只抛 `FetchError`/`CollectorError`，**不引入新的 result 类型**。
能力**声明而非探测**（ADR 0017）：未声明该能力的源在**调用采集器之前**被拒
（`structure_changed`），**不发任何请求**。

### 3. 抽取器唯一

`adapters/article.py` 是"页面 HTML → 文章正文"的**唯一实现**；fulltext 处理器与 rss 的地址路径
共用它（#167 提出并迁移，#169 接入第二个调用方）。文章页的抓取界限
（`ARTICLE_FETCH_POLICY`：超时/大小上限）也放这里，两个调用方共用一套数字。

### 4. WordPress 走 REST 加速，失败回落通用抽取

地址形状能指出 post 时（`?p=<id>`、结尾数字段如 `/archives/2391`）先试
`/{install}/wp-json/wp/v2/posts?include=<id>&_fields=…,guid`：install 根先按**地址自己的首段**
猜（`/blog/archives/2391` → `/blog`），再退域名根，常见情形一次请求命中。返回的 post 必须
**验证**（id 相同，或 link 回该地址）——"结尾是数字"只是猜测（可能是分类页），猜错必须回落，
而不是把别人的文章存到这个地址下。任何失败（无 REST、被禁用、验证不成立）都回落通用抽取。

### 5. 身份与 feed 对齐

- WordPress：`external_id = guid.rendered` —— 与 feed 的 `<guid>` 是**同一个字符串**，所以按地址
  抓到的文章落在 feed 已经产生的**那一条**上，而不是旁边多一条。
- 通用：`external_id = link:<canonical_url>`（canonical 取页面自己的 `rel=canonical`，缺失时用
  请求 URL）——与 `parse` 对"无 guid 条目"的规则一致。

### 6. 同源硬校验（抓取之前）

地址 host 必须等于源 `canonical_url` 的 host（允许 `www.` 差异）；不满足即拒绝该条、
**不发请求**。`source_id` 是溯源边界（ADR 0022）：一串地址不是"可以对本源从未同意过的服务器
发请求"的许可。**不得**用 `rss.match_url` 校验文章 URL（它只认 feed 形状片段）。

### 7. 端点与工具

- `POST /v1/sources/{id}:fetch-items`：`write_required` + `Idempotency-Key` + 审计中间件；
  **200 + 逐条结果**（`{url, action, item_id, error_class, detail}`，`action` ∈
  `stored`/`existing`/`rejected`/`failed`）。与 `:ingest` 的区别：那里是**调用方抓、部署存**
  （`agent` 型源）；这里是**调用方只给地址、部署抓**。
- MCP `fetch_source_items`：一次确认覆盖整批。确认文本 = 条数 + URL 预览 + **列表指纹**
  （elicitation 表单只能携带原始类型，列表进不去；只比条数会被"换掉一个地址"的列表骗过——
  #67 的教训）。

### 8. 节拍由调用方指定、服务端夹取，部署下限优先

`batch_size` clamp 1..50（默认 20）、`min_interval_seconds` clamp 0..60（默认 2）；**部署的
`PEARSS_FETCH_MIN_INTERVAL` 是下限**（操作者对"这个部署能多用力打别人的服务器"的声明，
请求方不能把它谈下来）。响应回传**生效值**，调用方分得清"按你说的做了"与"被悄悄改过"。
`blocked`（403/429）之后余下地址不再尝试（报 `rejected`）——那是源在要求"停"，继续正是
抓取礼貌（ADR 0029）存在的理由。

### 9. 采集器声明的 `discover()` 推迟（附触发条件）

RSS 的"历史枚举"没有可实现的 listing 形状（feed 不分页；REST 分页是平台特定的 listing，
需要另一套 `parse`）。**不声明零实现的能力**——registry 的双向校验会让它启动失败。
**触发条件**：出现**第二个**"需要无 agent 枚举、且有可解析 listing 形状"的站点类别。
（第一个不足以为一个钩子定型。）

### 10. 无新增持久化

不做续跑游标、不建新表；失败由调用方重发（去重保证幂等）。

### 11. 明确不做

feed 分页做全量、只支持 WordPress REST、agent 抓 + `submit_items` 替代本方案、跨源/跨域抓取、
登录型单条（twitter/zhihu/zsxq）、源级发现 A5。

## 已知缺口

**通用站点无法保证与 feed 身份对齐**：只能用 `link:<canonical>`。若同一篇文章此前已由 feed 以
guid 身份入库，地址路径按内容哈希去重（而不是身份），**可能留下两行**（一行 guid 身份、
一行 link 身份）。缓解：优先走有稳定 id 的源（WordPress 的 `guid`）。`link:` 兜底是 `parse`
的既有规则，不是本方案新引入的。

## 被否决

| 方案 | 否决理由 |
|---|---|
| feed 分页抓全量 | RSS/Atom 无标准分页；本 feed 无 `rel=next`、末页 404；实测 57 次请求且只有摘要（vs REST 6 次） |
| 只支持 WordPress REST | 不通用；已被"通用抽取 + WP 加速"取代 |
| agent 抓 + `submit_items` 替代 | 目标是**已有采集源**（站点知识在采集器里），且 agent 侧抽取会与 fulltext 的抽取器分叉（A4） |
| 声明一个没有实现的 `discover()` | 违反 ADR 0017 的双向校验（声明无实现 = 启动失败） |
| 新写一套限流器 | `app/fetch_pacing.py` 已有 per-host interval/cooldown；重复实现会让两处规则分叉 |

## 回滚

纯增量：一个端点 + 一个可选能力 + 一个工具，**无 schema 变化**。删路由/删钩子即回滚；
已入库条目是合法 Item，不回收。

## 验收

AC-167..176（`SCENARIOS.md`）。实现 PR：#175（抽取器共享件）、#178（`collect_item` 能力）、
#180（rss 实现）、#181（端点）、#182（MCP 工具）。
