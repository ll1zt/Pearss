# RSS 与网页采集器开发指南

本指南规定如何增加和维护 RSS/Atom 采集与网页采集器。目标是优先使用稳定标准、尊重内容提供方、并用 fixture 防止网站改版悄悄破坏系统。

## 选择采集方式

按以下顺序选择，不要一开始就写网页爬虫：

1. 官方 RSS/Atom feed：首选，低维护、语义稳定
2. Feed 自动发现：解析网站 HTML 的 `<link rel="alternate">`
3. 官方 API：若有合法、稳定、符合使用条款的 API
4. 网页采集器：仅在无 feed/API 或 feed 内容明显不足时使用

**匿名采集（默认）**：采集器不得绕过 robots.txt、登录限制、CAPTCHA、付费墙、访问控制或站点明确的反自动化要求。遇到此类情况归类为 `blocked` 或 `auth`，而非尝试规避。

**登录型采集（例外，需显式配置）**：当且仅当凭据由用户本人提供、账号属于该用户、且采集范围不超出该账号自身可见的数据时，允许携带登录态。全部条件与风险见 ADR 0012。凭据缺失或失效时仍归 `auth` 并停止，不得尝试规避。

登录型采集器**自己声明它需要哪个域名**（如 `COOKIE_DOMAIN = "x.com"`），不读全局配置：域名是站点的固有属性，一个共享值会让第二个采集器静默拿到第一个的 cookie。

## P1 准插件结构

> **实现现状**：已交付 3 个准插件，均为 `collectors/<name>/collector.py` 包形态：
> - `rss`（RSS 2.0 + Atom）：5 套 fixture
> - `telegram`（公开频道预览页）：5 套 fixture
> - `twitter`（X 时间线，**登录型**，见 ADR 0012）：9 套 fixture
>
> 三个齐了，§3「第三个爬虫接入且未改动前两个」这一 P2 hookspec 冻结前提
> （Rule of Three）**已满足**——但需实际核对第三个是否迫使前两个改过代码。
>
> 选择在 `src/app/collector_registry.py`：按 URL 选采集器（`match_url`），而非 pipeline
> 硬编码调某一个。`fetch` 不在采集器内，由 `adapters/http.py` 统一负责。

P1 不做动态安装/manifest；每个采集器仍按统一目录和接口形态编写：

```text
plugins/collectors/<pattern_name>/
  collector.py
  README.md
  fixtures/
    valid.html
    empty.html
    changed.html
    expected-items.json
  test_collector.py
```

在 `app/collector_registry.py` 的显式注册表硬编码注册，不使用动态 import。P2 依据多个真实采集器的共同模式冻结 hookspec，再引入 pluggy、manifest 与插件生命周期。

注册表按 URL 选采集器，但**声明类型优先于 URL 猜测**（`select_for`）：`hnrss.org/frontpage`
是真 RSS feed 却不含任何 feed 形状的路径，纯 URL 路由会拒绝采集一个运维明确建成 `rss` 的源。

## Collector 职责

草案接口：

```python
class Collector:
    def match_url(self, url: str) -> bool: ...
    def fetch(self, source, config) -> RawPayload: ...  # 可选
    def parse(self, raw: RawPayload, config) -> list[ItemDraft]: ...
```

职责边界：

| 方法 | 应做 | 不应做 |
|---|---|---|
| `match_url` | 判断 URL/域名/路径是否适用 | 发网络请求、访问数据库 |
| `fetch` | 获取必要页面；设置超时、大小限制、合法 Header | 解析业务内容、创建 Item、调度重试 |
| `parse` | 从 RawPayload 提取 `ItemDraft`；执行结构断言 | 写数据库、发布 RSS、调用翻译/TTS、修改 Source |

所有入口最终统一走：

```text
RSS / Web Collector / Ingest -> ItemDraft -> normalize -> Item
```

## 抓取规则

- 使用 httpx；设置连接/读取/总超时，禁止无限等待
- 限制最大响应体大小，流式读取并在超限时停止
- 对 RSS 使用 ETag / Last-Modified 条件请求；网页可按站点能力使用
- 每域名设置并发上限和速率限制；读取并遵守 `Retry-After`
- 使用清晰、可联系的 User-Agent（由部署配置提供）
- URL 需 canonicalize：移除已知追踪参数、规范化 host/scheme、保留确有语义的 query
- 日期解析必须保留原始值与解析后 UTC 值；无法确定时区时记录证据，不可凭本地时区猜测

## ItemDraft 最低质量要求

每个 ItemDraft 至少应尽力提供：

```text
external_id/guid, title, canonical_url, published_at, content, source_id, raw_fetch_id
```

规则：

- `external_id` 缺失时由稳定字段组合派生，但必须说明算法并保证重复抓取一致
- `canonical_url` 不可为空；不应使用追踪 URL
- `title` 缺失时可使用安全短摘要，但必须明确标记
- `content` 可以为空，但采集器要明确是正文不可得还是解析异常
- 不在采集器中生成对外 RSS GUID；GUID 由核心 normalize/publish 逻辑决定

## 错误分类

只能使用 `ErrorClass` enum。详细定义见 `docs/domain/error-taxonomy.md`。

| 情况 | 分类 |
|---|---|
| DNS、TLS、连接/读取超时、临时 5xx | `network` |
| XML/HTML 格式无法解析、编码损坏 | `parse` |
| HTTP 200 但选择器不再匹配/必填字段缺失 | `structure_changed` |
| 403、429、CAPTCHA、robots 禁止 | `blocked` |
| 401、认证过期、返回登录页 | `auth` |

不要将所有 exception 粗暴归类为 `parse`；保留结构化 evidence（HTTP 状态、选择器、命中数），但不可把完整外部页面/错误正文写进普通日志。

