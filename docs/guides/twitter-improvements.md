# X（Twitter）采集器改进规划

本文件基于**真实载荷分析**（2026-09-14，经 CookieCloud 取到的实际
`HomeLatestTimeline` 两页）制定，不是推测。取样方法与原始观察记录在文末。

> **进度（2026-09-15）**：P0 全部完成（`c994dd3` `3952b76` `8ab6c19`）；
> P1-4 标题前缀（`4471b8a`）、P1-7 图片尺寸（`c80ed9a`）、P1-6 引用媒体（`c80ed9a`）完成；
> P1-5 正文时间**已做后按实测撤销**（`482dd23`，见其小节）；
> P1-8 视频嵌入待 ADR（用户决定搁置）；P1-9 经确认为「维持现状，不改」。
> 各条小节内另有状态标注。

前置阅读：实现现状见 [twitter-collector.md](twitter-collector.md)，
运维视角见插件目录的 `README.md`，登录型的授权边界见
ADR [0012](../adr/0012-authenticated-collectors.md)。

## 取样事实

一页 95 个条目，其中：

| 类别 | 数量 | 当前处理 |
|---|---|---|
| `tweet-*` | 79 | 解析入库 |
| `home-conversation-*` 模块 | 5 个模块 / **13 条推文** | **整块丢弃** |
| `promoted-tweet-*` | 9 | 跳过（正确） |
| `cursor-*` | 2 | 只取 cursor 值 |

79 条已解析推文的构成：

| 特征 | 数量 | 占比 | 处置 |
|---|---|---|---|
| 带媒体（图片/视频） | 28 | 35% | 图片维持原图（P1-7）；视频待嵌入（P1-8） |
| 引用推文 | 29 | 37% | 加 `QT ` 前缀（P1-4）+ 补头像媒体（P1-6） |
| 转推 | 15 | 19% | 加 `RT ` 前缀（P1-4） |
| 长推（`note_tweet`） | 17 | 22% | 已正确读全文，标题维持截断（P1-9） |
| 带 `views` 计数 | 77 | 97% | **不做**（见「明确不做」） |

**关键结论**：一页里 14% 的推文（13/92）从未进入系统，且它们全是回复串——
往往是对话里信息密度最高的部分。转推 + 引用合计 **56%**，而这些在标题层面
目前完全无法区分（P1-4）。

## P0：确认的内容丢失

### 1. `home-conversation` 模块内的推文被整块丢弃 ✅ 已修（`c994dd3`）

`_tweets()` 只接受 `entryId` 以 `tweet-` 开头的条目：

```python
for entry in _entries(body):
    if not str(entry.get("entryId", "")).startswith("tweet-"):
        continue  # home-conversation-* 在这里被丢掉
```

但 `home-conversation-*` 是 `TimelineTimelineModule`，其
`content.items[].item.itemContent.tweet_results.result` 里是**完整推文**。
实测每页丢 11–13 条（占该页推文总数约 14%）。

**为什么这不是"只丢上下文占位"**：抽样到的模块内容是真实对话，例如一条
关于美元叙事的分析推及其两条回复。它们有完整的 `legacy`、作者、时间戳。

**修复**：`_tweets()` 增加对 `TimelineTimelineModule` 的展开——遍历
`content.items`，对每个 `item.itemContent` 走与顶层相同的取法。
`page_ids()` 与 `page_sort_span()` 必须同步（它们共用 `_tweets()`/`_entries()`，
前者自动受益，后者见 P0-2）。

**注意**：`items[]` 内的 `entryId` 已经是 `tweet-<id>` 形式，所以判据可以统一。
实现时应把"从 itemContent 取 result"抽成一个小函数，两条路径共用。

**回归测试**：用一个含 `home-conversation` 模块的 fixture，断言模块内推文
出现在 `page_ids()` 与 `parse()` 结果里。当前 `fixtures/` 里**没有任何**
`home-conversation` 语料——这解释了为什么这个缺陷一直没被发现。

### 2. `page_sort_span` 把游标条目算进区间 ✅ 已修（`3952b76`）

`page_sort_span()` 遍历 `_entries()` 取所有带 `sortIndex` 的条目，而游标条目
（`cursor-top-*` / `cursor-bottom-*`）**也带** `sortIndex`。

**真实载荷的布局**（两页复核一致）：

```text
cursor-top    sortIndex = 2099389805031849985
tweet (最新)  sortIndex ≈ 209938980503184989x
...
tweet (最旧)  sortIndex = 2099389805031849892
cursor-bottom sortIndex = 2099389805031849891   ← 只在最旧推文下方 1 个单位
```

**影响评估（与上一轮判断不同，这里更正）**：上一轮我基于仓库 fixture 认为
这会导致"每页都误报断档"。真实数据显示游标与推文区间**仅相差 1**，
污染带宽只有 1 个单位，相邻页误判需要下一页 max 恰好落在这 1 个单位内。
实测相邻两页（`page1.min=…891`，`page2.max=…890`）**没有**触发误报。

所以这是**低概率的潜在缺陷，不是活跃故障**。仍应修，理由有二：

1. 判据本身是错的——它比较的是"含游标的区间"，而语义要求比较"推文区间"
2. 仓库 fixture 与真实载荷**量纲不符**，测试因此在守护错误行为（见 P0-3）

**修复**：`page_sort_span()` 只统计 `entryId` 以 `tweet-` 开头的条目，
纯游标页返回 `None`（"无法证明连续性"——该语义已有测试
`test_pages_without_sort_index_cannot_prove_continuity` 覆盖）。

### 3. fixture 与真实载荷不符 ✅ 已修（`3952b76`、`b742516`）

| fixture | 现状 | 真实 |
|---|---|---|
| `valid.json` | 推文 sortIndex `10^30`/`9×10^18`，游标 `9999999999999999999` | 均为 **19 位推文 id 量级**，游标与推文相邻 |
| `no_posts.json` | 游标 `sortIndex: "5"` | 真实游标 sortIndex 是推文 id 量级的 19 位数 |

`valid.json` 的量纲错误（1e30 vs 1e19）让游标落在推文区间**内部或下方很远**，
这在真实流量里不会发生。`no_posts.json` 的 `"5"` 更是让它报告的区间 `(5,5)`
成为一个在现实中不可能出现的值。

**修复**：把两个 fixture 的 sortIndex 改成从真实载荷量纲派生的合成值
（保持不含真实内容的脱敏要求，但量纲与相对关系必须真实）。
同时补一个含 `home-conversation` 的 fixture（P0-1）。

> **脱敏要求**：新 fixture 不得包含真实推文文本、真实用户 id 或头像 URL。
> 用合成内容，但**结构、量纲、字段组合**照抄真实载荷。

## P1：展示效果改进

这一节是你提到的重点。当前渲染能用，但丢掉了很多已在载荷里的信息。

### 4. 标题加类型前缀 ✅ 已实现（`4471b8a`）

**需求**：不同类型的内容在标题开头就要能一眼分辨。

```text
RT <被转推者的标题>          # 转推
QT <被引用者的标题>          # 引用
```

**现状**：转推只有正文里的 `↻ <strong>某人</strong> 转推了` 横幅——
在阅读器的列表视图里**完全看不到**，因为列表只显示标题。
引用推文则连标记都没有，标题就是自身正文。

**为什么这对本条时间线特别重要**：79 条已解析推文里 **19% 是转推、
37% 是引用**——超过一半的内容不是作者原创，但标题层面完全无法区分。

**实现要点**：

- 前缀在 `_title_from_text()` 的输出上加，不能在 `full_text` 上加
  （正文里的前缀会污染 `content`）
- 转推：用被转推原推的文本做标题主体，前缀 `RT `
- 引用：用自身正文做标题主体，前缀 `QT `
- 长度约束：前缀计入 `TITLE_MAX`（100）后再截断，否则标题会超预算
- 两者同时出现时（转推了一条引用推文）需定优先级——**建议 `RT` 优先**，
  因为外层形态是转推；引用的信息在正文 blockquote 里已呈现

**注意**：`RT`/`QT` 是**英文标记**，与现有中文正文混排。若要更一致可评估
`转推`/`引用`，但前缀的价值在于**短且固定宽度**，中文两字在列表视图里
比 `RT` 更宽。**建议用 `RT`/`QT`**。

**⚠️ 与现有测试的冲突必须先处理**：
`test_collector.py::test_retweet_keeps_the_retweeter_and_uses_the_original_body`
第 117 行断言 `not retweet.title.startswith("RT @")`。这条断言**不能直接删**——
它的理由是对的：上游格式 `RT @user2: <正文>` 里的 `RT @user2:` 是**冗余**的
（`metadata["retweet"]` 与正文横幅已表达同一事实），而且会让人分不清
读到的是谁的话。

本需求是**裸 `RT ` 前缀**，语义不同：它只标记类型，不带作者名，
不遮蔽正文归属。因此正确的做法是：

- 保留"不得出现 `RT @<handle>:` 形态"的断言（改成断言不含 `@` 与冒号）
- 新增断言"转推标题以 `RT ` 开头"
- 在测试 docstring 里写清两者的区别，否则下一个人会把它们混为一谈

### 5. 推文时间可见 ❌ 已撤销（`482dd23`）

**实现后在真实阅读器里验收，判断为噪声并撤销**：客户端自己就显示条目到达
时间，每条正文再重复一遍是冗余。`published_at` 仍然解析、入库、经 API 提供，
只是不再渲染进正文。

（以下为当初的提议，保留以记录推理。）
X 的原生 UI 里时间是重要
定位信息；读者在 RSS 阅读器里看到的只有条目顺序。

**建议**：作者行加上时间（`<time datetime="...">` 最合适——净化器已放行
`time[datetime]`，见 `ALLOWED_ATTRIBUTES`）。

### 6. 引用推文补媒体 ✅ 已实现（`c80ed9a`）；头像已撤销

当前引用块只有：作者名 + 140 字预览 + "查看被引推文"链接。
**引用的媒体 0% 呈现**（实测 29 条引用里没有一条带 `<img>`）。

引用推文里带图片/视频是常见的（"看图"型引用）。纯文本预览会让人
误以为引用内容只是一句话。

**建议**：引用块的作者行加头像（与主推文同规格），若有媒体则加缩略图。
保持 `<blockquote>` 结构。

### 7. 图片：维持原图，但补上尺寸 ✅ 已实现（`c80ed9a`）

**决定：不改成缩略图，继续用 `media_url_https` 原图。** 原图能保留细节，
且 X 的图片 CDN 本身响应够快。

**仍建议做的**：用 `original_info.width/height` 写 `width`/`height` 属性。
载荷里已有这两个真实值，写上能让**图片加载前就占住正确高度**，
消除阅读器里的布局跳动——与头像写 `width`/`height` 是同一个理由。

**已验证**：净化器保留 `width`/`height`（`img` 属性白名单里有），
可直接实施，无需改净化器。

### 8. 视频：支持嵌入（**需先扩净化器，见下方警告**）

**决定：支持 `<video>` 嵌入。** 载荷里 `video_info.variants` 含可直接播放的
mp4 URL（**已实测确认**：HLS 主播放列表 + 三档 mp4，256k/832k/2176k，
及 `duration_millis`），当前完全没用。

> 🚫 **前提缺口（必须先解决，否则此改动无法落地）**：
> **净化器目前完全丢弃 `<video>`** —— 实测
> `<video src="...">` 与 `<video><source .../></video>` 过 `sanitize_html`
> 后都变成**空的 `<p></p>`**，播放器连同来源一起消失。
>
> 这不是加一行白名单就完事的改动：ADR 0007 第 54 行**明确禁止**
> `iframe`/`object`/`embed`，而 `tests/unit/test_properties_sanitize.py`
> 有属性测试守护"净化输出不含执行或追踪向量"。放宽媒体标签白名单是
> **扩大攻击面**，必须：
>
> 1. **写新 ADR**（ADR 0007 是 accepted 状态，不能改写）
> 2. **按宿主约束 URL**：只允许 `https://video.twimg.com/` —— 这是与
>    现有 `img` 放行 `pearss-avatar` class 同类的思路：放行的不是标签本身，
>    而是一个受限的、可枚举的来源
> 3. **明确不放行 `autoplay`**（自动播放是骚扰且可能绕过用户流量偏好），
>    `controls` 放行
> 4. 加对抗性测试：`<video>` 携 `javascript:` URL、携 `onerror`、
>    指向非白名单宿主——三者都必须被剥掉

**降级路径（必须先于嵌入实现）**：保留现有的"缩略图 + 外链"作为
video 标签被剥离时的形态。这样即使净化器改动被否决，视频也不会消失。

**建议同时做**：缩略图旁显示时长（`duration_millis`），让读者知道
是短片还是长视频。

### 9. 长推标题：维持现状

22% 是长推，已正确读到 `note_tweet` 全文。标题取首句或截断到 100 字符
**是合理的，不需要改动**——长标题在列表视图里并不更好读。

**唯一相关改动**是 P1-4 的类型前缀要计入这 100 字符预算。

## 明确不做

以下项经讨论后决定不做，记录在案以防重新提出：

| 项 | 决定 | 理由 |
|---|---|---|
| 点赞/转推/回复/浏览量计数 | **不做** | 计数是快照且会腐烂；时间线的价值在内容不在热度指标 |
| 图片改用 `?name=` 缩略图 | **不做** | 原图保留细节，CDN 响应可接受 |
| 长推标题加长 | **不做** | 首句截断已合理 |
| 视频改为 `<iframe>` 嵌入 | **不做** | 与 ADR 0007 的禁令冲突，且 `<video>` 已够用 |

## P2：结构性改进

### 10. `promoted-tweet` 的显式处理 ✅ 已实现（`c994dd3`、item 级见 `8ab6c19`）

当前靠"`entryId` 不以 `tweet-` 开头"跳过，这是**偶然正确**而非显式设计。
若 P0-1 改成遍历 `itemContent`，推广推文会**意外被解析入库**（它有完整的
`tweet_results`）。

**必须在 P0-1 的实现里同时加显式排除**：按 `entryId` 前缀
`promoted-tweet-` 或 `itemContent.promotedMetadata` 存在与否判断。
**这是 P0-1 的强制配套项，不是可选项。**

### 11. `views` 之外的分析字段

载荷里还有 `lang`（语言检测）、`possibly_sensitive`、`source`
（发布客户端）、`conversation_id_str`（线程归属）、`edit_control`。

`possibly_sensitive` 尤其值得考虑：敏感内容标记来自来源，入库时应保留，
让下游能据此过滤。

### 12. 回复/线程的归属

`conversation_id_str` 与 `id_str` 不同即为回复。实测这一页恰好全是
顶层推文（`conversation_id == id`），但 `home-conversation` 模块里的
就是回复（P0-1 修好后会入库）。

有了它，未来可以做"把同一对话聚合成一条 Item"——**不建议现在做**，
记录方向即可。

### 13. 条目分类不能再依赖 `entryId` 前缀 ✅ 已修（2026-09-16）

**背景**：P0-1 的修复是往白名单里加了 `home-conversation-` 这一个前缀。
陷阱没变——**下一个 X 换个名字的模块仍会被整块丢弃**，且不报错、不记日志。

**2026-09-16 实网复核**（经 CookieCloud，取样一页）：

| 条目前缀 | 条数 | 是否带 `tweet_results` |
|---|---|---|
| `tweet-` | 81 | 是（81 条推文） |
| `home-conversation-` | 9 | 是（模块内共 **20 条**推文） |
| `promoted-tweet-` | 8 | 是（**正确排除**） |
| `header-image-prompt-ad-` | 1 | **否**（无推文，忽略正确） |
| `cursor-top-` / `cursor-bottom-` | 各 1 | 否 |

采集器实际保留 **101** 条 = 81 + 20，推广 8 条全部排除。算术闭合。

**关键观察**：这一页出现了**旧代码从未见过的新前缀**
`header-image-prompt-ad-`。它当前不带推文，因此旧代码"碰巧"没事；
但如果 X 让它带上模块形内容，旧的白名单实现会再次静默丢弃。

**修复**：`_tweets()` 与 `page_sort_span()` 改为按**结构**判定——
`_is_module(content)` 认 `__typename == "TimelineTimelineModule"` **或**
`content.items` 是列表；其余带 `tweet_results.result` 的条目一律当推文。
判据变成"带 `tweet_results` 就是推文，只有显式排除（推广/游标）才丢"。

**测试**：4 个解析层（`test_a_module_under_an_unrecognized_prefix_is_still_expanded`
等）+ 2 个入库层（`test_a_module_under_an_unknown_prefix_is_stored_too`）。

## 建议实施顺序

1. **P0-1 + P0-2 配套**（`home-conversation` 展开 + 显式排除推广推文 +
   `page_sort_span` 只算推文）——一个 PR，含新 fixture
2. **P0-3**（fixture 量纲修正）——与 1 同 PR 或紧随，因为新测试依赖它
3. **P1-4**（标题 `RT `/`QT ` 前缀）——**单独的展示层 PR**，
   因为它是最容易被看到改动、也最需要肉眼验收效果的一项
4. **P1-6 + P1-7 尺寸**（引用补头像/媒体、图片补 `width`/`height`）
   ——展示层，与 3 可同 PR
5. **P1-5**（时间可见）——小改动，可并入 3 或 4
6. **P1-8**（视频嵌入）——**必须先写 ADR 扩净化器白名单**，
   独立 PR，含对抗性测试与降级路径
7. **P2**——按需

每一项都应附带**用真实量纲 fixture** 的回归测试。P0-1 的测试必须
能证伪"模块内推文被丢弃"——即断言数量，而不只是断言"能解析"。
P1-4 的测试必须同时覆盖"前缀存在"与"不含 `RT @handle:`"两侧。

**明确不做**的项（计数、图片缩略图、长标题、iframe）已在上文列表记录，
不要因为"数据里有"就顺手加回来。

## 取样方法（可复现）

```bash
set -a && source .env && set +a
uv run python - <<'PY'
import httpx
from plugins.collectors.twitter import client as x
creds = x.Credentials.from_env()
with httpx.Client(timeout=30) as c:
    p = x.fetch_page(client=c, credentials=creds,
                     query_id=x.current_query_id(), count=10)
    open("/tmp/x_page1.json","wb").write(p.body)
PY
```

**凭据与隐私**：取样载荷含真实推文、作者与头像 URL。`/tmp/` 下的样本
不入库、不进 git，用后应删除。任何新增 fixture 必须脱敏（见 P0-3 的
脱敏要求）。
