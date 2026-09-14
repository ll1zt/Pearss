# X (Twitter) timeline collector (P1 quasi-plugin, login-type)

Reads the **operator's own** Following timeline through X's private GraphQL
endpoint (`HomeLatestTimeline`). No Bot/API key: the session is the operator's.

This is a **login-type collector** and exists only under the conditions in
**ADR 0012**. Read that first. In short: the credentials must be yours, the
scope must not exceed what your account can already see, and a missing or
expired credential stops as `auth` rather than being worked around.

本文是**运维视角**：配置、行为、排障与风险。代码层面的实现在
[`docs/guides/twitter-collector.md`](../../../../docs/guides/twitter-collector.md)
（模块分工、数据流、翻页模型、已知缺陷）。

## 位置与结构

```text
src/plugins/collectors/twitter/
  collector.py       # 纯解析：bytes → ItemDraft（无网络、无凭据）
  client.py          # GraphQL 请求、凭据、queryId 恢复
  paging.py          # 翻页：frontier / 断档检测 / 区间下界
  walk.py            # 三者粘合 + 沙箱 RPC 入口
  test_collector.py  # 35 个解析测试
  test_client.py     # 29 个请求与分类测试
  test_paging.py     # 21 个翻页测试
  fixtures/
    valid.json         # 上游实现记录的真实形态（RT / 图片 / 长推 / 截断）
    quoted.json        # 引用推文在顶层与在 legacy 下两种形态
    cards.json         # 投票卡（binding_values 为 map 与为数组）+ 链接卡
    visibility.json    # TweetWithVisibilityResults 包装
    self_retweet.json  # 外层即原推（HomeLatestTimeline 的常见形态）
    no_posts.json      # 只有游标、无推文 —— 合法，不是错误
    empty_response.json# 风控空响应 {"data": null}
    errors.json        # 端点级 errors
    malformed.json     # 截断 JSON
```

## 配置

只从环境读，**不入库**，不作为 `source` 字段：

```bash
PEARSS_X_AUTH_TOKEN=<你的 auth_token cookie>   # 登录凭据
PEARSS_X_CT0=<你的 ct0 cookie>                 # 必须与 x-csrf-token 一致
PEARSS_X_QUERY_ID=<可选，固定 queryId>
PEARSS_X_COUNT=<可选，每页条数，≤40>
PEARSS_X_USER_AGENT=<可选>
```

建源时 URL 必须是 `https://x.com/home` —— 这是**显式声明**，不因 URL 形状自动启用
（否则一次普通加源就会悄悄带上登录态请求）。

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/sources \
  -H "Authorization: Bearer $PEARSS_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" -H 'Content-Type: application/json' \
  -d '{"url":"https://x.com/home","type":"web","schedule_policy":{"type":"fixed","interval":"60m"}}'
```

## CookieCloud（凭据自动续期）

浏览器重新登录后 X 会轮换 `ct0`，静态值就失效了。配了 CookieCloud 就不必重启：

```bash
PEARSS_COOKIECLOUD_URL=http://127.0.0.1:4000   # 自部署 cookie_cloud_server
PEARSS_COOKIECLOUD_UUID=<你的同步 uuid>
PEARSS_COOKIECLOUD_TOKEN=<COOKIE_CLOUD_SERVER_PASSWORD>
PEARSS_COOKIECLOUD_CACHE_SECONDS=300          # 可选
```

**解析顺序**：静态 `PEARSS_X_*` > CookieCloud。静态优先 —— 显式钉住值就是明确要求
用那个值，不该被一个在跑的同步服务悄悄覆盖。

**没有 `PEARSS_COOKIECLOUD_DOMAIN` 这个配置项。** 要导出哪个域是**采集器自己的事实**
（X 声明 `COOKIE_DOMAIN = "x.com"`，未来的知乎采集器声明 `"zhihu.com"`），不是部署选项。
它曾经是个全局环境变量，那样第二个登录型采集器会静默拿到第一个的 cookie。

**被拒时自愈**：X 返回 401 说明会话已轮换，此时绕过缓存重取一次并重试该页。
只重试一次 —— 对着拒绝反复重试是在轰炸对方，而真正的问题是没人能给我们授权。

> ⚠️ **`COOKIE_CLOUD_SERVER_PASSWORD` 不是"某个站的 cookie"**。它是服务端**主密钥**，
> 能解密导出**任意 uuid、任意域名**的 cookie。`domain=` 只缩小本次响应，不缩小
> token 权限。持有它 = 持有你同步过的全部站点会话（含银行、邮箱）。
>
> 因此：**用一个只含阅读器所需站点的专用 uuid**，别指向整个浏览器档案。
> 明文 http 指向非回环地址时启动会告警（不记 token）。

客户端是通用适配器 `src/adapters/cookiecloud.py`，下一个登录型源直接复用。

## 三个必须记住的行为

### 1. 绝不发送 `seenTweetIds`

X 把它解释为"排除这些"。于是本地停止锚点永远碰不到已摄入的 id，翻页只能翻满预算
或翻到历史尽头 —— **每轮摄入几百条从未见过的旧推文**。上游实现记录这是它真实上线过的
回归。测试直接断言请求体里没有这个键。

### 2. `{"data": null}` 是 blocked，不是"没有新内容"

风控抑制的响应在 HTTP 层是 **200**。若解析层返回空列表，调用方读作"无新内容"，
一个被永久封住的源就会**连续数周报告健康运行**。所以它显式报错。

### 3. 断档（gap）优先于"已追上"

时间线是活的：两条请求之间可能插入推文，导致下一页起点**高于**上一页终点，被跳过
的内容**不在任何一页里**。去重对此无能为力 —— 它只防重复存储，不知道"漏了"。
连续性靠每页 `sortIndex` 区间判定。断档会被**记录并告警，但不会被追** ——
检测与追讨是两件事，追断档等于开出第二个爬取方向。

## 抓取区间：frontier 与 floor

每轮抓取的上界是 `seen_frontier`（已入库的最新推文 id），下界是 `floor_cursor`
（该源走过的**最深**位置）。两者之间的区间就是要抓的范围。

- **首轮**是唯一有「页数策略」的一轮：走 `PEARSS_X_FIRST_PAGES`（默认 3）页，
  预算耗尽时它停在哪，那里就成为 floor —— 这定义了该源的历史深度，是操作者的选择。
- **后续每轮**从时间线顶部开始，抓到 frontier 即停。**请求数取决于该源的真实产出**，
  而不是某个策略数字；`first_pages` 不约束后续轮，否则「预算在触及 frontier 前用尽」
  正是跨轮丢内容的方式。
- `CATCH_UP_PAGE_CEILING`（200）是**失控护栏**，不是上限：它只在 origin 反复用同一个
  cursor 回应时生效（另有显式检查直接判底）。正常永远不会碰到。

floor 是**停止符**，不是续采起点。把它当 resume 会从历史最深处往更旧爬 ——
实测把一条时间线拖回 2023 年、单轮上千条。区间模型没有「欠账」概念，因此也
没有再引入 `resume_rounds` 之类的老化计数器。

**页间 sleep 不在这里**：限流策略属应用层（见 collector-guide 的职责边界）。

## 与上游实现的三处有意差异

1. **非 UTC 的 `created_at` 偏移被换算而非丢弃**。上游对不是 `+0000` 的一律返回无时间戳。
   X 实际发 UTC，但因为遇到一个没预料到的时区就丢掉真实时间，比换算更糟。
2. **实体区间不盲信**。X 文档说码点，上游一处注释说 UTF-16、代码却按码点数。这里拿
   X 同时给出的 `t.co` 文本比对区间，对不上就定位字面量，再对不上就**丢弃该实体** ——
   错误区间不是崩溃，而是把链接安到错误的词上且看起来一切正常。
   上游那个"UTF-16"分支在本移植里试过又删了：没有数据需要它，也没有测试能到达它。
3. **头像用 `width`/`height` 属性而非 inline `style`**。入库净化（ADR 0007）会丢弃
   `style` 与 `class`，写 style 等于没写。

## 风险与边界

- **违反 X 服务条款**（禁止未授权自动化访问，含逆向私有接口）。风险由运行者承担。
- **账号风控**：限流、要求验证、乃至封号。
- **私有接口无兼容承诺**：queryId 与响应结构由 X 单方变更。故 queryId 自动恢复与
  `structure_changed` 分类是必需件。
- 凭据属**不可重放恢复**的东西：库丢了能重建条目，`auth_token` 丢了要用户重新导出。
  本项目尚无备份方案（§7.5），这是启用本源前该知道的。

## 运行测试

```bash
just test        # 含本目录 85 个 + tests/integration/test_twitter_paging.py 13 个
                 #   + tests/unit/test_cookiecloud.py 35 个，全部离线
```

无 fixture 含凭据；HTTP 层一律用 `httpx.MockTransport`。
