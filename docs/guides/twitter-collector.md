# X（Twitter）采集器实现说明

本文是 `src/plugins/collectors/twitter/` 的**实现文档**：模块如何分工、数据流
怎么走、每个反直觉的写法对应哪次线上失败。

- **运维视角**（怎么配、怎么排障、风险边界）：先读采集器目录里的
  [`README.md`](../../src/plugins/collectors/twitter/README.md)
- **为什么允许登录型采集**：ADR [0012](../adr/0012-authenticated-collectors.md)
- **采集器通用合约**：[collector-guide.md](collector-guide.md) 与
  `core/plugins/hooks.py`（ADR [0017](../adr/0017-p2-plugin-freeze.md)）

本文不重复上述内容，只写**代码层面**的事实。

## 模块分工

四个 Python 模块，按"能不能离线测"切分而不是按"代码多少"切分。这个切分是
刻意的：X 的响应结构只能靠录制载荷验证，所以**网络、凭据、翻页决策全部分离出去**，
`collector.py` 只剩 bytes 到 `ItemDraft` 的纯函数。

```text
collector.py   831 行  纯解析：bytes → ItemDraft；无网络、无凭据、无状态
client.py      438 行  GraphQL 请求、凭据解析、queryId 恢复、错误分类
paging.py      237 行  翻页算法：frontier / 断档 / 预算 / 部分成功（无 I/O）
walk.py        238 行  三者粘合 + 沙箱 RPC 入口（注册表 `paged=True` 的实现）
```

依赖方向是单向的：`walk.py → {client, collector, paging}`，
`collector.py` 不 import `client.py`（解析器看不到凭据，所以凭据不可能进 fixture）。

### 各模块的公开面

| 模块 | 导出 | 谁调用 |
|---|---|---|
| `collector.py` | `parse`、`match_url`、`source_config`、`collector_id`、`backfill`、`collect_pages`（`HOOKIMPL`）；`page_ids`、`page_cursor`、`page_sort_span`（解析助手） | 注册表（经 `HOOKIMPL`）、`walk.py` |
| `client.py` | `fetch_page`、`Credentials`、`QueryIdRejected`、`CredentialError`、配置读取函数 | 只被 `walk.py` 调用 |
| `paging.py` | `walk`、`Page`、`WalkResult`、`to_int`、`truncate_at_seen` | 只被 `walk.py` 调用 |
| `walk.py` | `collect_pages`、`PagedWalkResult` | 注册表经 `plugin_runtime` 的 RPC 代理 |

`collector.py` 里的 `collect_pages` 是一个**薄转发**（`collector.py:773`，转发体 6 行），
存在的唯一理由是注册表的 paged 派发要在采集器模块上找到这个 hookimpl——
`collector_id` 指向的是 `collector.py`。实现在 `walk.py`。

## 数据流

一次 paged 采集的完整路径：

```text
调度器 / trigger_job
  └─ app/pipeline.collect_paged            ← 宿主：读 paging_state，写入新游标
       └─ registration.collector.collect_pages   （子进程 RPC 代理，plugin_runtime）
            └─ plugins.collectors.twitter.walk.collect_pages
                 ├─ client.credentials_configured()   ← 无凭据 → auth，不发请求
                 ├─ client.Credentials.from_env()     ← 静态 env → CookieCloud
                 └─ paging.walk(fetch_page=fetch_with_recovery, ...)
                      │   fetch_with_recovery 包装 client.fetch_page：
                      │   400 → refresh_query_id 后重试一次
                      │   401/403 → Credentials.refresh 后重试一次
                      └─ 每页 collector.parse() → ItemDraft
       └─ normalise_and_store                   ← 宿主：去重、净化、写库
       └─ save_paging_state                     ← 宿主：frontier 只前移，floor 只加深
```

**关键边界**：采集器**从不碰 SQLAlchemy**。翻页状态以
`core.paging.WalkState`（`seen_frontier`、`floor_cursor` 两个字段）传入，
结果以 `PagedWalkResult`（`drafts`、`newest_id`、`floor_candidate`、`gap_cursor`、
`error_class` 与 `detail`）返回。宿主持久化，采集器只读这两个字段——这也是
沙箱子进程能在不 import `adapters` 的前提下工作的原因。

## 三个反直觉的请求/解析决策

### 1. 永不发送 `seenTweetIds`

`page_body()`（`client.py`）构造的请求体**没有** `seenTweetIds`。X 把它读作
"排除这些"，于是本地停止锚点永远碰不到已摄入的 id，翻页只能翻满预算或翻到
历史尽头——上游实现记录这是真实上线过的回归：每轮摄入几百条从未见过的旧推文。

回归测试：`test_client.py::test_seen_tweet_ids_are_never_sent` 直接断言请求体
里没有这个键。

### 2. `{"data": null}` 是 `blocked`，不是"没有新内容"

风控抑制的响应在 HTTP 层是 **200**。解析层若返回空列表，调用方读作
"无新内容"，一个被永久封住的源会**连续数周报告健康运行**。

`_instructions()`（`collector.py`）在三条路径上强制分类：

| 载荷形状 | 分类 | 理由 |
|---|---|---|
| `data` 不是 dict（含 `null`） | `blocked` | 风控 stub，不是历史尽头 |
| `data.errors` 非空 | `structure_changed` | 端点级错误 |
| JSON 解不开 / 不是对象 | `parse` | 载荷损坏 |

回归测试：`test_risk_control_stub_is_blocked_not_a_quiet_success`、
`test_endpoint_errors_are_structure_changed`、`test_truncated_json_is_parse_error`。

### 3. 实体区间不盲信

X 文档说 `indices` 是码点偏移，上游某处注释说 UTF-16 而代码按码点数。
**错误区间不是崩溃**，而是把链接安到错误的词上且看起来一切正常。

`_resolve_span()` 的三步策略：

1. 按 X 同时给出的 `t.co` 文本比对区间；相符即采用
2. 不符则 `str.find()` 定位字面量
3. 仍不符则**丢弃该实体**——保留文字不链接，而不是把链接安错

上游那个"UTF-16 分支"在本移植里试过又删了：没有数据需要它，也没有测试能到达它。
`_resolve_span` 的 docstring 记录了这一点——**没有测试能证伪的分支比没有分支更糟**，
因为它读起来像已处理。

配套的 `_split_segments()` 对**重叠/乱序**区间做钳制（`low = max(low, position)`），
不让畸形实体反向切片。

## 翻页模型：区间，不是续采

这是本采集器最容易被误改的部分。**区间模型**（`paging.py` 的 `walk`）：

```text
seen_frontier ──────────────────────────► 顶部（每轮从这里开始）
      │                                       ▲
      │  每轮抓取的范围                        │ 抓到 frontier 即停
      ▼                                       │
floor_cursor（该源走过的**最深**位置）       每轮的起点
```

- **上界 `seen_frontier`**：已入库的最新推文 id。X 的 id 随时间递增，所以
  单个整数就够——不需要 seen id 集合，也不需要向 origin 发送任何改变其返回的东西
- **下界 `floor_cursor`**：**停止符，不是续采起点**。把它当 resume 会从历史最深处
  往更旧爬——实测把一条时间线拖回 2023 年、单轮上千条

### 首轮是唯一有"页数策略"的一轮

| 轮次 | 预算 | 依据 |
|---|---|---|
| 首轮（无 frontier、无 floor） | `PEARSS_X_FIRST_PAGES`（默认 3） | 预算耗尽处即 floor——定义该源的历史深度，是操作者的选择 |
| 后续每轮 | `CATCH_UP_PAGE_CEILING`（200） | **失控护栏**，不是上限 |

`CATCH_UP_PAGE_CEILING` 只在 origin 反复用同一个 cursor 回应时生效（另有
显式检查直接判底：`page.next_cursor == cursor`）。正常**永远不会碰到**。

**为什么后续轮的预算不能是 `first_pages`**：请求数应当取决于该源的真实产出，
而不是某个策略数字。在触及 frontier 前因预算耗尽而停下，正是跨轮丢内容的方式——
溢出的部分会在下一 tick 之前沉到顶部页以下。回归测试：
`test_a_busy_catch_up_walks_past_the_first_walk_page_count`。

### `floor_candidate` 只在一种结局下产生

`WalkResult.floor_candidate` 仅当**预算耗尽且未穿过 frontier**时设置。其余结局
（穿过 frontier / origin 见底 / 到达 floor / 失败）都不产生候选，已存的 floor 保持不动。

**为什么由 walk 报告而不是宿主比较**：cursor 是不透明字符串，宿主无法比较位置。
让 walk 说"这次是预算用尽而结束"才使候选有意义。回归测试：
`test_a_budget_stop_never_moves_the_floor`、`test_the_floor_persists_when_a_walk_stops_at_the_frontier`。

### 断档检测：记录并告警，不追

时间线是活的：两条请求之间可能插入推文，导致下一页起点**高于**上一页终点，
被跳过的内容**不在任何一页里**。去重无能为力——它只防重复存储，不知道"漏了"。

连续性靠每页 `sortIndex` 区间判定：**下一页的 max 必须低于本页的 min**。
只记录**第一处**断裂（分页从新到旧，那是最新的缺失区），作为 `gap_cursor` 返回。

断档**不会被追**：检测与追讨是两件事，追断档等于开出第二个爬取方向。
`walk.py` 记 `LOGGER.warning` 后继续从 frontier 走。

> ⚠️ **已确认缺陷**：`page_sort_span()` 统计 `_entries()` 里**所有**带 `sortIndex`
> 的条目，而游标条目（`cursor-top-*` / `cursor-bottom-*`）**也带** `sortIndex`。
> 仓库 fixture 里能看到这一点：`fixtures/valid.json` 的
> `cursor-bottom-4000000000000000001` 带 `sortIndex: "9999999999999999999"`；
> `fixtures/no_posts.json` 的纯游标页只有 `cursor-bottom-1` 带 `"5"`。
>
> **真实流量下的严重性（2026-09-14 实测两页后修正）**：真实载荷里
> `cursor-bottom` 的 `sortIndex` 只比最旧推文**小 1**，`cursor-top` 只比
> 最新推文大 1——游标紧贴推文区间，不是 fixture 里那种悬殊差距。因此
> 污染带宽只有约 1 个单位，相邻页被误判需下一页 max 恰好落入其中，
> **实测未触发**。
>
> 所以这是**低概率的潜在缺陷，不是活跃故障**。判据仍然错（语义要求比较
> "推文区间"，它比较的是"含游标的区间"），且 fixture 量纲与真实不符使测试
> 在守护错误行为，故仍应修，但不紧急。
>
> 修复方向：`page_sort_span` 只统计 `entryId` 以 `tweet-` 开头的条目，
> 纯游标页返回 `None`（"无法证明连续性"，已有测试
> `test_pages_without_sort_index_cannot_prove_continuity` 覆盖该语义）。
> 详见 [twitter-improvements.md](twitter-improvements.md) 的 P0-2 与 P0-3。

## 凭据解析

优先级与自愈路径（`client.py`）：

```text
Credentials.from_env()
  ├─ PEARSS_X_AUTH_TOKEN + PEARSS_X_CT0 都在 → 用静态值（显式钉住优先）
  └─ 否则 cookiecloud.config_from_env(domain=COOKIE_DOMAIN)
       ├─ 未配置服务 → CredentialError（消息列出两条路径）
       └─ 取到但无 auth_token/ct0 → None → 回落 CredentialError
```

**静态优先的理由**：显式钉住一个值就是明确要求用那个值，不该被一个在跑的同步
服务悄悄覆盖。测试：`test_static_credentials_win_over_cookiecloud`。

`Credentials.refresh()` 是 401 之后的路径，**绕过缓存**重取一次：
X 在登录时轮换 `ct0`，缓存里的那份正是被拒的那份。若凭据是静态钉住的，
`refresh()` 直接抛 `CredentialError` 并说明"要么重新导出会话，要么 unset 以回落到
CookieCloud"——同一个值再读一遍只会空转。

**`COOKIE_DOMAIN = "x.com"` 由采集器声明**，不读全局配置：域名是站点的固有属性
（下一站的知乎声明 `zhihu.com`），共享值会让第二个登录型采集器静默拿到第一个的
cookie。ADR 0012「实现约束」记录了这条从全局环境变量改过来的原因。

### 三次重试的上限

| 触发 | 恢复动作 | 次数 |
|---|---|---|
| HTTP 400 | `refresh_query_id()` 从 x.com 自己的 JS bundle 抽新 queryId，重试该页 | 1 |
| HTTP 401/403 | `Credentials.refresh()` 绕过缓存重取会话，重试该页 | 1 |
| CookieCloud 挂掉 | 记 `LOGGER.info` 后返回 `None`（不等于"没有凭据"） | — |

**只重试一次**：对着拒绝反复重试是在轰炸对方，而真正的问题是没人能给我们授权。
测试：`test_a_persistent_rejection_does_not_retry_forever`（断言 `calls == 1`）、
`test_a_rejected_session_is_refreshed_and_the_page_retried`（断言恰好一次恢复）。

`refresh_query_id()` 失败返回 `None` 而非抛错：调用方还留着旧值，
发现不了新 queryId 不该看起来像凭据问题。

### 凭据的密封性

四条测试守住"凭据不外泄"：

- `test_credentials_never_appear_in_a_fetch_error_detail`
- `test_policy_headers_are_redacted_by_the_audit_allowlist`（`cookie`、`authorization`、
  `x-csrf-token` 在脱敏列表里）
- `test_no_credential_appears_in_any_draft`
- 全部 fixture 不含凭据——解析器根本看不到凭据

`PUBLIC_BEARER` 是公开值（每个浏览器都发这个），不是秘密；凭据是那对 cookie。

## 与上游实现的三处有意差异

| # | 上游行为 | 本实现 | 理由 |
|---|---|---|---|
| 1 | 非 `+0000` 的 `created_at` 一律返回无时间戳 | 换算成 UTC | 因为一个没预料到的时区就丢掉真实时间，比换算更糟 |
| 2 | 区间不校验（注释说 UTF-16，代码按码点） | 比对 `t.co` 文本 → 定位字面量 → 丢弃 | 错误区间不是崩溃，是把链接安到错误的词上 |
| 3 | 头像用 inline `style` | **不渲染头像**（见下） | 无法可靠呈现为圆形，且正文不需要它 |

### 头像：试过、撤销了

上游把头像内联在正文里（`style` 做圆形）。本移植先照做，改用
`width`/`height` 属性 + `class="pearss-avatar"`（净化器唯一放行的类，按值
校验于 `adapters.sanitize`），让阅读器/主题挂 `border-radius: 50%` 自行做圆。

**在真实阅读器里验收后撤销**：NNW 不注入自定义 CSS，头像就显示成方形；
而服务端做圆是**不可能的**——inline `style` 是 XSS 面，净化器（ADR 0007）
全面禁止。与其发一个注定不像头像的图片，不如不发。

因此署名就是纯文字 `名字 @handle`，正文里的**图片与引用媒体不受影响**
（它们是内容，不是装饰）。`AVATAR_CLASS` 常量保留在 `adapters.sanitize`：
它是通用样式钩子，其他采集器可能用，且有自己的测试。

同理，`published_at` 已解析并入库，但**不在正文里渲染**：阅读器自己就显示
到达时间，每条都重复是噪声。

## 身份与字段映射

| `ItemDraft` 字段 | 来源 |
|---|---|
| `external_id` | `twitter:<rest_id>` |
| `canonical_url` | `https://x.com/<screen_name>/status/<rest_id>` |
| `title` | 首行、去掉 `t.co` 链接、按词边界截断到 `TITLE_MAX`（100） |
| `published_at` | `legacy.created_at`，换算为 UTC |
| `content` | 按序拼接：转推横幅 → 作者行 → 正文 → 媒体 → 引用 → 卡片 |
| `metadata["author"]` | `Name (@screen)` |
| `metadata["retweet"]` | 转推时为 `"true"` |

**`rest_id` 出站一律是文本**：API 发字符串、录制 fixture 发整数，两者都接受，
出口统一 `str`。

> ⚠️ **已确认缺陷**：作者无法解析时 `_screen_name()` 返回字面量 `"unknown"`，
> 于是 `_tweet_link()` 生成 `https://x.com/unknown/status/<id>`——一个**指向不存在
> 账号的合成 URL**（`rest_id` 存在但作者节点缺失时触发）。这违反 collector-guide
> 「`canonical_url` 不可为空；不应使用追踪 URL」的精神：URL 非空却指向错误位置。
> `_quoted_html()` 已对同一常量做了防护（`screen != "unknown"`），说明作者知道
> 这个哨兵值的存在，但 `_tweet_link()` 未做同样检查。

### 转推的同一性检查

`HomeLatestTimeline` 常把**原推**放在顶层，同时带一个 `retweeted_status_result`
**指向同一条推文**。若不比对身份，每条普通推文都会渲染出"某人转推了"横幅并指向自己。

判据是双重的（id 与 screen_name 都相同才算自指），`_retweet_parts()` 返回 `None`。
测试：`test_a_self_referencing_retweet_entry_renders_as_an_ordinary_tweet`。

### NoteTweet 优先于截断预览

超过 280 字符时 X 把全文放在 `note_tweet`，`legacy.full_text` 只剩以省略号结尾的预览。
只读 legacy 会**发布一条截断的推文且看起来完全成功**。`_resolve_full_text()` 先读
note 再回落到 legacy。

配套：`_content_urls()` 按**正在渲染的文本**选取实体集——NoteTweet 的实体索引
指向 note 文本，legacy 的指向 `full_text`；配对错了就会链接错误的区间。

### 同一事实的多条路径

X 在不同时期把同一份数据放在不同位置，**只接受一种会静默丢内容**（解析正常，
只是内容没了）。三处已知分叉，各自有 fixture：

| 数据 | 路径 A | 路径 B | fixture |
|---|---|---|---|
| 引用推文 | `quoted_status_result.result`（顶层） | `legacy.quoted_status_result.result` / `legacy.quoted_status` | `quoted.json` |
| `binding_values` | map | `[{key, value}]` 数组 | `cards.json` |
| 可见性包装 | `TweetWithVisibilityResults.tweet` | 直接是 `Tweet` | `visibility.json` |

### 第四个分叉：`home-conversation` 模块（已处理）

`_tweets()` 曾经只接受 `entryId` 以 `tweet-` 开头的条目，于是
`home-conversation-*`（`TimelineTimelineModule`）里的推文被整块丢弃。
这些模块的 `content.items[].item.itemContent.tweet_results.result`
是**完整推文**——有 `legacy`、作者、时间戳，内容是回复串。

**2026-09-14 实测**：两页各丢 11–13 条，占该页推文总数约 **14%**，
且全是对话回复（信息密度最高的部分）。

危险在于它和推广推文（`promoted-tweet-*`）此前靠**同一条规则**
（"不以 `tweet-` 开头就跳过"）被排除——规则偶然正确：广告确实该丢，
对话不该。展开模块后广告会意外入库。

**但当时的修复只往白名单里又加了一个前缀**（`home-conversation-`），
陷阱原样留着：下一个 X 换个名字的模块仍会被整块丢弃。

### 第五个分叉：模块前缀是任意字符串（已修，2026-09-16）

`_tweets()` 与 `page_sort_span()` 现在按**结构**判定条目，不再看 `entryId`
前缀——`_is_module(content)` 认 `__typename == "TimelineTimelineModule"`
**或** `content.items` 是个列表（二者任一即可；推文的 `content` 两者皆无，
不会误判）。

| 条目 | 处理 |
|---|---|
| 任意前缀 + 模块形（`items[]`） | 展开 `content.items[].item.itemContent`，每条一推文 |
| 其余带 `tweet_results.result` 的条目 | 一条推文（**不因名字被丢**） |
| `itemContent.promotedMetadata` 或 id 含 `promoted` | 排除 |
| `cursor-*` | 只取游标值 |

**判据**：带 `tweet_results.result` 的条目就是推文，不管 X 把它叫什么；
只有**显式排除**（推广、游标）才允许丢弃。`page_sort_span` 用同一套分类，
两者必须描述同一批推文——span 漏掉 `_tweets` 保留的推文会报出假断档，
反之会报出低于真实值的 floor。

**回归测试**：`test_a_module_under_an_unrecognized_prefix_is_still_expanded`
（解析层）与 `test_a_module_under_an_unknown_prefix_is_stored_too`
（走完整 walk 并读回 `item` 表——回答"为什么阅读器里没有"必须看到存储层）。

**推广判定是 item 级，不是 entry 级**（`_is_promoted(item_content)`）。
这一条是 review 自己代码时发现的：初版让 `_is_promoted` 递归进模块、
任一 item 是广告就跳过**整个模块**——那正是本分支要修的那类静默丢内容，
只是移到了另一层。

`fixtures/conversation.json` 是脱敏语料（结构、字段组合、sortIndex 量纲
照抄真实载荷；文本、用户名、头像 URL 为合成），同时含这三种条目形状。

完整改进规划见 [twitter-improvements.md](twitter-improvements.md)。

## 标题类型前缀（`RT ` / `QT `）

阅读器的列表视图**只显示标题**，所以正文里的 `↻ 某人 转推了` 横幅在那里
完全不可见。真实页面上转推 + 引用合计约 **44–46%**，此前在标题层面无法区分。

| 类型 | 前缀 | 标题主体 |
|---|---|---|
| 转推 | `RT ` | 被转推原推的文本 |
| 引用 | `QT ` | 自身正文 |
| 原创 | （无） | 自身正文 |

三个设计点：

- 前缀加在 `_title_from_text()` 的**输出**上，不加在正文（会污染 content）
- 前缀从 `TITLE_MAX` 里**扣**，不是加在上面——否则每个带标记的标题都超限
- 两者同时成立时取 `RT `：顶层形态是转推，引用已在正文 blockquote 里呈现

**刻意不带 handle**：上游的 `RT @user: text` 形态仍然被拒——它重复
`metadata["author"]`，还让人分不清读到的是谁的话。`test_retweet_keeps_the_retweeter_and_uses_the_original_body`
继续断言不含该形态，新测试断言裸 `RT ` 存在，两条 docstring 写清区别。

## 图片尺寸

图片用 `original_info.width/height` 写 `width`/`height` 属性，让阅读器在图片
加载前占住正确高度、消除布局跳动（真实页面上 29/29 张图都带这个字段）。
**注意**：`bool` 是 `int` 子类，`isinstance(True, int)` 为真，所以守卫显式
排除布尔值——否则会渲染出 `width="True"`。图片 URL 用原图，不取缩略图。


## 测试布局

```text
src/plugins/collectors/twitter/
  test_collector.py    57 个  解析（含 6 个实体区间、条目形状、标题前缀、图片尺寸）
  test_client.py       29 个  请求形状、凭据解析、错误分类、queryId 恢复
  test_paging.py       21 个  frontier / 断档 / 预算 / 部分成功 / floor 候选
tests/integration/
  test_twitter_paging.py 13 个  真实仓库写入 + 真实 walk（MockTransport，离线）
```

集成层覆盖单元层到不了的部分：首轮定义 floor、第二轮停在 frontier、
catch-up 越过 `first_pages`、401 自愈恰好一次、持续拒绝不无限重试、
frontier 只前移、floor 不被预算停止覆盖。全部离线——HTTP 层是
`httpx.MockTransport`。

`fixtures/valid.json` 是上游 Elixir 实现记录的**真实形态**，共享断言同时充当
移植等价性检查。其余 fixture 覆盖上游注释称为生产必需但从未 pin 的形状
（顶层引用推文、数组形 `binding_values`、可见性包装、风控 stub）——
**这些 shim 正是没人断言时最容易腐烂的代码**。

`tests/unit/test_structure_guards.py` 的 fixture 引用守卫保证每个 fixture
至少被同目录一个测试引用（防死语料）。

## 风险与维护

- **违反 X 服务条款**（禁止未授权自动化访问，含逆向私有接口）；账号风控、
  限流、验证、封号。风险由运行者承担，ADR 0012 记录了知情决定的全部条件
- **私有接口无兼容承诺**：`queryId` 与响应结构由 X 单方变更。所以
  `refresh_query_id()` 与 `structure_changed` 分类是**必需件而非优化**
- **凭据不可重放恢复**：库丢了能重建条目，`auth_token` 丢了要用户重新导出。
  CookieCloud 的存在正是为了把"重新登录"变成自动动作
- **`CookieCloud` 的 token 强于它取回的凭据**：`COOKIE_CLOUD_SERVER_PASSWORD`
  是服务端主密钥，能解密导出任意 uuid、任意域名的 cookie。部署应使用只含
  阅读器所需站点的专用 uuid

改动本目录时值得先跑的检查：

```bash
just test        # 含本目录（test_collector 57 + test_client 29 + test_paging 21）
                 #   + tests/integration/test_twitter_paging.py 13 个
                 #   + tests/unit/test_cookiecloud.py，全部离线
just lint        # import-linter 守四层依赖方向
```
