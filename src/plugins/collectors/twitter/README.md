# X (Twitter) timeline collector (P1 quasi-plugin, login-type)

Reads the **operator's own** Following timeline through X's private GraphQL
endpoint (`HomeLatestTimeline`). No Bot/API key: the session is the operator's.

This is a **login-type collector** and exists only under the conditions in
**ADR 0012**. Read that first. In short: the credentials must be yours, the
scope must not exceed what your account can already see, and a missing or
expired credential stops as `auth` rather than being worked around.

## 位置与结构

```text
src/plugins/collectors/twitter/
  collector.py       # 纯解析：bytes → ItemDraft（无网络、无凭据）
  client.py          # GraphQL 请求、凭据、queryId 恢复
  paging.py          # 翻页：frontier / 断档检测 / 可续采回填
  test_collector.py  # 31 个解析测试
  test_client.py     # 20 个请求与分类测试
  test_paging.py     # 18 个翻页测试
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

## 上游未做的两件事

CookieCloud（凭据经自部署同步服务下发）**未移植**。静态 cookie 已覆盖核心需求，
引入一个第三方凭据分发通道值得单独决策，不该作为移植的附带项。

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
连续性靠每页 `sortIndex` 区间判定，断档游标的优先级高于一切其他续采点。

## 翻页与预算

`max_pages`（默认 20）是**限流保险丝**。存在未完成 resume 时，最新页阶段让出约 30%
预算给回填，否则高活跃账号每轮都吃满预算、自己的旧账永远追不上。

回填**故意忽略 frontier**：在那里截断会让重走一碰到已见 id 就停，正好丢掉它本该找回
的内容；重叠交给核心层去重。

`resume_rounds` 连续未解决到 `MAX_RESUME_ROUNDS`（3）即丢弃游标、下轮从最新页重来。
比 frontier 更深的欠账没有自然停止锚点，不加上界就会每轮向历史滑动、永不收敛 ——
接受一个**有界的**深历史缺口，好过无界的滑动。

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
just test        # 含本目录 69 个测试，全部离线
```

无 fixture 含凭据；HTTP 层一律用 `httpx.MockTransport`。
