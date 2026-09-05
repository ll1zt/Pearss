# RSS/Atom collector (P1 quasi-plugin)

Parses standard RSS 2.0 and Atom feeds into `ItemDraft`.

## 位置与约定

按 `docs/guides/collector-guide.md` 的准插件结构放置：

```text
src/plugins/collectors/rss/
  collector.py                      # 采集器本体
  test_collector.py                 # fixture 回归测试
  fixtures/
    valid.xml          # 正常 RSS 2.0，含 guid / 日期 / content:encoded / 无日期三种条目
    valid_atom.xml     # Atom，验证 rel="alternate" 取链与 content 优先于 summary
    empty.xml          # 结构有效但零条目 —— 合法，不是错误
    changed.xml        # 站点改版：条目不再提供 <link>
    malformed.xml      # 截断的 XML
```

P1 不做动态装配置；采集器在 `app/` 显式注册。P2 依 Rule of Three 冻结 hookspec。

## 接口

```python
match_url(url: str) -> bool
parse(raw: RawPayload) -> list[ItemDraft]
```

`fetch` 不在采集器内：抓取由 `adapters/http.py` 统一负责（超时、大小上限、条件请求、User-Agent），采集器只解析。这样「怎么拿」和「怎么读」分开，网页采集器也能复用同一套抓取策略。

## 职责边界

| 做 | 不做 |
|---|---|
| 从 `RawPayload` 提取 `ItemDraft` | 发网络请求（`adapters/http.py` 负责） |
| 结构断言并分类失败 | 写数据库、发布 RSS、调度重试 |
| 生成 `external_id` | 生成对外 RSS GUID（`core.normalize` 负责） |
| 保留原始日期证据 | 猜测时区 |

## 错误分类

`parse` 抛 `CollectorError`，携带 `ErrorClass`：

| 情况 | 分类 |
|---|---|
| 空 payload、XML 截断 | `parse` |
| 可解析但不是 feed（无 version） | `structure_changed` |
| 条目全部缺少可用链接 | `structure_changed` |
| 结构有效但零条目 | **不是错误**，返回空列表 |

两个易错点，都由 fixture 钉住：

1. **`urn:uuid:` 之类的 guid 不是 URL**。它可作为 `external_id`，但绝不能当 `canonical_url`——否则 feed 里会出现一个点不开的 `<link>`。只接受 `http(s)://` 开头的链接。
2. **feedparser 故意宽容**：截断的 XML 也能捞出残缺条目，`bozo` 被置位但 `entries` 非空。只判断「bozo 且无 entries」会把半截内容当正常文章发布。本采集器额外检查标签是否闭合来识别截断。

## 运行测试

```bash
just test        # 含本目录的 fixture 回归测试
```

fixture 不访问公网；HTTP 层用 `httpx.MockTransport` 替换传输层（`tests/unit/test_http.py`）。
