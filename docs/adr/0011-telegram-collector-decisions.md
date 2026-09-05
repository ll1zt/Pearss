# 0011 · Telegram 采集器：t.me 原生 URL、CSS 取图、可选回溯

- 状态：accepted
- 日期：2026-09-05
- 关联：`docs/guides/collector-guide.md`、`docs/adr/0003-rss-cdata-passthrough-and-lxml.md`

## 背景

A2 要求的第一个网页采集器选定为 Telegram 公开频道预览（`https://t.me/s/<username>`），
移植自有相同用途的 Elixir 实现 `earss_source_telegram`。三个决定需要记录，因为它们都
偏离了「照抄上游」这个最省力的选项。

## 决定

### 1. 源 URL 用 t.me 原生形态，不引入 `earss://` scheme

上游用 `earss://telegram/channel/<username>` 作为源标识。本项目不采用：

- `source.canonical_url` 是源的去重键（ADR 0008），要求 NOT NULL 且可比较；
  `canonicalize_url()` 已有 http(s) 的规范化规则，自定义 scheme 要另开分支
- `canonical_url` 同时是**可抓取地址**（采集器直接 fetch 它）。`earss://` 不可抓，
  采集器得再维护一张 scheme → URL 的映射表，等于两个事实源
- 现有 UI/API/MCP 都假设源 URL 是 http(s)

代价：源标识与上游不通用。可接受 —— 本项目不与上游共享数据库。

条目侧：`external_id = telegram:<channel>/<id>`，`canonical_url =
https://t.me/<channel>/<id>`（后者可点开，符合 collector-guide「canonical_url
不可为空、不应使用追踪 URL」）。

### 2. 图片从 CSS `background-image` 提取，并过滤 emoji

预览页把图片放在包裹元素的 `background-image` 里，不是 `<img src>`。只找 `<img>`
的解析器会在图片密集型频道里静默丢掉全部图片。

同时过滤两类噪声：

- `telegram.org/img/emoji`、`/emoji/` —— 装饰，不是内容
- **正文内的 inline emoji 外壳**（上游未处理，见下）

### 3. 回溯实现但默认关闭

`?before=<id>` 支持翻页，实现为采集器的可选能力（`before_url` / `oldest_cursor`），
由 `collector_registry.backfiller()` 探测，RSS 采集器不实现。默认 `max_pages=1`，
与上游 v0.1 一致：每多一页都是对别人服务器的一次请求。需要时调用方传 `max_pages`。

## 与上游实现的两处有意差异

### inline emoji 外壳：清掉

上游只过滤它**自己提取**的媒体 URL，不管正文里 Telegram 渲染的
`<i class="emoji" style="background-image:url(...)">`。它的 fixture 断言也只覆盖
`<img>` 标签，所以这个疏漏不会让它自己的测试变红。

本项目必须处理：正文会进 RSS、Reader API 与内容寻址存储，一条消息能带上百个
装饰性 background-image（真 fixture 里 175 处）。做法是用 `drop_tag()` 剥掉外壳、
保留里面的 Unicode 字符 —— 字符本来就在文本里，所以信息无损。

### 目录结构：包而非单文件

collector-guide 规定 `collectors/<name>/collector.py`。上游是单文件模块。
本仓库采用包结构，并**把已有的 `rss` 一并从 `rss.py` 改成 `rss/collector.py`**：

两个采集器形态不一致会导致 pytest 的 basename 冲突（两个 `test_collector.py`
在无 `__init__.py` 的目录里无法区分），而加 `__init__.py` 让 `rss` 变成空包后
`from plugins.collectors import rss` 拿到的不再是模块本身。统一为包同时解决两点，
且符合既有指南。

## 实测发现：cursor 取首条而非末条

预览页按消息 id **升序**排列（最老在上）。`?before=N` 返回「截止到 N 的最新 20 条」。

因此回溯游标必须是页面**第一条**。取末条（最新）会让 `?before=` 返回与当前页
重叠的一页 —— 表现为翻页在推进、实际在重复读同样的消息。

在 `durov` 频道实测：取末条时三页有重叠且 id 非递减；改取首条后三页 59 条无重叠、
id 连续递减。该错误由测试钉住（`test_backfill_cursor_is_the_oldest_message_on_the_page`）。

## 后果

- A2 从「0/2–3」推进到「1/2–3」；P2 冻结 hookspec 的 Rule of Three 前提仍未满足
- 采集器选择从 pipeline 硬编码 `rss.parse` 改为 `collector_registry.select(url)`
- 新依赖 `cssselect`（lxml 的 CSS 选择器需要；XPath 可免但选择器显著更噪）
- 无注册采集器命中 URL 时归 `structure_changed` 而非 `parse`：抓取成功了，
  但没人认得返回的内容，这与「解码失败」的重试语义不同
