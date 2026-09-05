# Telegram channel collector (P1 quasi-plugin)

Parses the public web preview of a Telegram channel into `ItemDraft`.

```text
https://t.me/s/<username>
```

No Bot API token and no login: it reads the same page a browser sees. It is
this project's first *web* collector, and the second of the two-to-three the
guide wants before P2 freezes a hookspec.

## 位置与约定

```text
src/plugins/collectors/telegram/
  collector.py
  test_collector.py
  fixtures/
    valid.html       # 真实频道预览页（与上游 Elixir 实现同一份）
    empty.html       # 频道存在但无消息
    changed.html     # 改版后的标记，选择器全部落空
    media_only.html  # 纯图片消息 + 纯 emoji 消息
    legacy_ids.html  # data-post 只有数字 id 的旧式页面
```

与 `rss` 同构：都是包，都在 `app/collector_registry.py` 显式注册。

## 接口

```python
match_url(url: str) -> bool
parse(raw: RawPayload) -> list[ItemDraft]
before_url(url: str, cursor: str) -> str | None   # 可选：回溯
oldest_cursor(drafts) -> str | None               # 可选：回溯
```

`fetch` 不在这里 —— 抓取统一由 `adapters/http.py` 负责（超时、大小上限、条件请求、
User-Agent）。后两个方法是**可选能力**：`collector_registry.backfiller()` 探测它们，
RSS 采集器不实现，所以不会被要求回溯。

## 回溯（`?before=`）

默认只抓最新一页，与上游实现 v0.1 一致 —— 每多一页都是对别人服务器的一次请求。
需要回溯历史（首次回填、或核对某次采集是否漏了东西）时由调用方传 `max_pages`：

```python
collect_source(session, source=source, client=client, max_pages=5)
```

循环终止有三个条件，缺一个都可能变成死循环或原地踏步：

1. 取到 `max_pages`
2. 某一页解析失败（保留已抓到的，不假装成功）
3. cursor 不前进 —— 空页、或起点忽略了 `before` 返回同一页

**cursor 取页面第一条，不是最后一条。** 预览页按 id **升序**排列（最老在上），
所以最后一条是最新的；拿它当 `?before=` 会返回与当前页重叠的一页，表现为「翻页
在推进但其实在重复读同样的消息」。这个错误在真实频道上验证过：`durov` 频道取末条
时三页有重叠，取首条后 60 条无重叠。

## 与上游实现的两处有意差异

移植自 `earss_source_telegram`（Elixir）。共享的 fixture 与断言保持等价，
以下两点是有意偏离：

1. **清掉正文里的 inline emoji 外壳**。Telegram 把 emoji 渲染成
   `<i class="emoji" style="background-image:url(//telegram.org/img/emoji/...)">`，
   上游只过滤它自己提取的媒体 URL，不管正文里的这些。本项目正文会进 RSS 与
   Reader API，一条消息能带上百个装饰性 background-image，所以这里剥掉外壳、
   保留 Unicode 字符本身（文本里本来就有）。
2. **目录结构为包**。上游是单文件模块；本仓库按 collector-guide 用
   `collectors/<name>/collector.py`，`rss` 也一并改成同构。

## 错误分类

| 情况 | 分类 |
|---|---|
| 空 payload、HTML 无法构建 DOM | `parse` |
| HTTP 200 但选择器全部落空 | `structure_changed` |
| 消息节点都在但全部缺 `data-post` | `structure_changed` |

「频道没有消息」不是 parse 失败：空频道仍会渲染页面外壳，所以选择器落空意味着
标记变了或需要登录预览，两种情况都需要人来看，不能静默当成零新消息。

## 边界

- 仅公开频道的 `/s/` 预览；私有频道、需要登录的频道不在范围内
- 默认一页（约 20 条），无全量历史
- CDN 链接可能过期或防盗链；emoji 与头像已过滤，正文图片仍可能有此问题
- 抓取第三方 HTML，站点改标记即失效，故 `structure_changed` 必须落库并可见
- 使用条款与合规风险由部署者承担

## 运行测试

```bash
just test        # 含本目录的 fixture 回归测试
```

fixture 不访问公网。真实频道验证（`durov`）曾用于确认翻页语义，但不进 CI。
