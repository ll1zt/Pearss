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

## 插件结构与执行模型

> **实现现状**：已交付 5 个采集器（ADR 0011/0012/0014/0015），均为
> `collectors/<name>/collector.py` 包形态：
> - `rss`（RSS 2.0 + Atom，**内置，进程内**）：5 套 fixture，fetch 钩子含 WordPress 全文升级
> - `telegram`（公开频道预览页）：5 套 fixture，声明 backfill
> - `twitter`（X 时间线，**登录型**，见 ADR 0012）：9 套 fixture，paged 走读
> - `zhihu`（**登录型 + 签名**，见 ADR 0014）
> - `zsxq`（**登录型**，见 ADR 0015）
>
> 选择在 `src/app/collector_registry.py`：按 URL 选采集器（`match_url`），但
> **声明类型优先于 URL 猜测**（`select_for`）——`hnrss.org/frontpage` 是真
> RSS feed 却不含任何 feed 形状的路径，纯 URL 路由会拒绝采集一个运维明确
> 建成 `rss` 的源。

合约冻结于 `core/plugins/hooks.py`（ADR 0017）：每个采集器以 `@HOOKIMPL`
声明 `collector_id` / `match_url` / `parse` / `source_config`，可选声明
`fetch` / `collect_pages` / `backfill`。manifest 的 features 与实际
hookimpl 在启动时双向校验——声明了没有的能力即启动失败。失败契约：
`fetch` 只抛 `FetchError(ErrorClass)`，`parse` 只抛
`CollectorError(ErrorClass)`（从 `core.plugins.hooks` 导入，不得自定义）。

### 执行模型：进程内 vs 子进程沙箱（ADR 0017）

| | rss（内置） | telegram / twitter / zhihu / zsxq（第三方） |
|---|---|---|
| 进程 | 宿主进程内直接调用 | `python -m plugins.collectors <id>` 独立子进程 |
| 通信 | 函数调用 | JSON-RPC 2.0 over stdio（合约：`core/plugins/protocol.py`） |
| 网络 | `adapters/http.py` | 经反向调用代理回宿主，走同一 SSRF 拦截与超时/限额 |
| 凭据 | 环境变量 | env allowlist 注入（见下） |
| 崩溃影响 | 宿主 | 宿主自动重启 child 一次；分类失败（auth/blocked/network/parse/structure_changed）是**值**，不重启 |

写新第三方采集器时的硬性约束：

- **凭据前缀必须登记**：`app/plugin_runtime.py` 的
  `_COLLECTOR_ENV_PREFIXES` 按采集器 id 列出它可读的 `PEARSS_*` 前缀。
  未登记的变量不会进入子进程——child 读不到凭据会归 `auth`，这是
  allowlist 生效的表现，不是 bug。
- **child 不 import `adapters`/`app`**：子进程只从请求参数与环境取值，
  不碰数据库与文件存储。SQLAlchemy 等宿主持久化机器不进沙箱。
- **child 的 stderr 继承宿主流**：崩溃 traceback 由 journald 收集，排查
  插件故障先看 `journalctl --user -u pearss`。
- **网络一律经代理**：child 内的 `httpx` 客户端由入口注入（反向调用回
  宿主），child 不直接出网；fixture 测试经 `set_http_transport` 注入。

### 各站点的环境事实（为什么代码长这样）

这些是真实订阅暴露后沉淀的约束。改动相关代码前先读这里——每一条都对应
一次线上失败和一个回归测试：

1. **telegram 的预览页重写**：订阅者写 `t.me/<name>`，可读页面在
   `t.me/s/<name>`。裸 URL 是应用安装引导页，没有任何消息——fetch 钩子
   必须做这个重写（回归测试：child 必须请求 `/s/` 路径）。
2. **zsxq 接受两种 URL 形式**：`/dweb2/index/group/<id>`（应用内部路由）
   与 `/group/<id>`（地址栏形式）指向同一页面，`resolve_source` 两者都要
   认；scope 查询参数在两种形式下都保留。只认一种会让 MCP 类型推断回落
   成 rss，rss 采集器再把登录页当 feed 解析。
3. **zhihu 的签名绑定 d_c0**：x-zse-96 签名以 `d_c0` cookie 为输入，
   CookieCloud 导出的 session 必须含它；签名与发送的 percent-encoding
   必须逐字节一致。
4. **Reader API 的 token 由存储哈希派生**：scrypt 盐随机，凭据同步若在
   密码未变时重哈希，每次登录都会铸造新 token、作废旧 token（NNW 的
   登录后并发请求互相 401）。同步只在密码真正变化时重哈希；改密码踢掉
   全部客户端是 ADR 0005 的语义，不是缺陷。
5. **沙箱 egress 的解压语义**：`HostClientTransport` 把宿主 client 适配成
   transport 时，httpx 的 `send` 已解压 body 而 `content-encoding` 头还在——
   必须剥掉传输态头再交给子进程，否则子进程 httpx 对明文做 gunzip 直接
   zlib -3。
6. **MCP `add_source` 的类型推断边界**：推断只查非 rss 采集器的
   `match_url`；它认不出的 URL（新站点、新 URL 形式）会回落 rss。新增
   URL 形式时同步扩展对应采集器的 `resolve_source`/`match_url` 测试。

### 小助手函数的重复是有意的

`_text` / `_name_of` / `_id_of` 这类三五行的小助手在各采集器里**各有一份**，
不要抽公共模块。原因有二：其一，每个采集器是独立的信任边界（沙箱子进程），
共享模块会把它们重新耦合成一个发布单元，破坏 ADR 0017 的隔离前提；其二，
各站点的取值细节（trim、HTML 反转义、嵌套解包）看似相同实则各有分叉，
过早统一会在第三次差异出现时逼出条件分支堆叠。第三个采集器重复同一段
逻辑时，先怀疑是相似而非相同——除非确定共享，否则继续各写各的。

目录与 fixture 约定不变：

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

- 使用 httpx；超时/大小上限由 `adapters/http.py` 统一实施，采集器自带
  的 HTTP 层不得绕过（沙箱采集器的出网经宿主代理，天然受限）
- 对 RSS 使用 ETag / Last-Modified 条件请求；网页可按站点能力使用。
  已知缺口：ETag 写回 RawFetch 的持久化回路未接线
  （ADR 0017「已知缺口」），条件重放暂不可达
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



## WordPress 摘要 feed 的全文升级（rss 采集器内置）

WordPress 站点把 feed 输出配成「摘要」时，feed 里只有 ~200-500 字符的截断
description（`&#8230;` + `more-link` 截断标记）且没有 `content:encoded`。
该站点的 REST API（`content.rendered`）不受这个设置影响，默认返回完整正文。

rss 采集器的 fetch 钩子已内置升级路径（ADR 0016 之后的增量，见
`plugins/collectors/rss/wordpress.py`）：

1. 普通 fetch 先行（保留 ETag/Last-Modified 条件请求语义）；
2. 载荷字节内检测截断标记（`more-link` / `screen-reader-text`）；未命中直接
   返回，零额外请求；
3. 命中则从 feed URL 推导 REST 端点（`{install_prefix}/wp-json/wp/v2/posts`，
   子目录安装自动带上前缀），按条目数定窗口取 `content.rendered`；
4. **按 permalink 匹配**回 feed 条目（REST id 与 feed GUID 是不同身份体系），
   替换截断 description；纯文本长度不优于摘要的条目保留摘要；
5. REST 任何失败（404/禁用/非 JSON）静默降级回摘要 feed——升级路径绝不把
   能工作的源变成失败源。

给新采集器的启示：当源存在「同一内容的更完整公开通道」时，优先换数据源而非
抓页面做正文提取——后者脆弱且贵。合并回 feed 载荷（而不是改 parse）意味着
normalize/dedupe/闸门零改动，条目身份保持稳定。
