# 0014 · 知乎采集器与注册表的可选 fetch 钩子

- 状态：accepted
- 日期：2026-09-06
- 修订：`src/app/collector_registry.py`（Registration 增 `fetches` 字段）、
  `src/app/pipeline.py`（`_collect_one_page` 先选采集器再取数）
- 关联：ADR 0012（登录型采集器）、architecture-spec §3（hookspec 草案的
  `fetch(source, config) -> RawPayload` 可选钩子）

## 背景

知乎 Web API 要求登录 Cookie（必须含 `d_c0`）+ `x-zse-96` 请求签名，签名
以「API 路径 + d_c0」为输入逐请求计算。普通采集路径 `adapters.http.fetch`
无法携带这类每请求头——这是继 X（ADR 0012）之后第二个登录型源，但 X 的
解决方式是 pipeline 里的一条专用路径（`collect_authenticated_pages`），
不可复制：知乎不是分页时间线，套 `paged=True` 是语义错位。

参考实现：`earss_source_zhihu`（Elixir，移植自 RSSHub
`lib/routes/zhihu/*`，含 `x-zse-96-v3` 签名与确定性测试向量）。

## 决定

1. **注册表增加可选 fetch 钩子**，落实 architecture-spec §3 hookspec 草案
   里早已预留的 `fetch(source, config) -> RawPayload`：

   - `Registration` 增 `fetches: bool = False`。与 `paged` 同理**声明而非
     探测**：模块碰巧定义了同名函数不代表采集方式应当改变。
   - `fetch_page_with(registration, …)` 统一分发：声明了钩子的采集器自取
     页面，其余走普通适配器。条件请求状态（ETag/Last-Modified）只属于
     普通路径——签名 API 不回 304，钩子签名里没有这个参数。
   - pipeline 改为先选采集器再取数。`match_url` 的契约本就是纯 URL 判断，
     无采集器认领的 URL 现在不再浪费一次请求，分类仍是 `structure_changed`。

2. **新增 `zhihu` 采集器**（`plugins/collectors/zhihu/`），三类路由：
   `people/<token>[/activities]`（动态）、`/answers`、`/posts`、
   `org/<token>/posts`。源类型仍为 `web`，靠 URL 匹配选择。

3. **签名与凭据边界**：

   - `x-zse-96` 移植为纯函数（`zse96.py`），以 RSSHub 生成的 4 组确定性
     向量钉住（注入固定随机前导字节）；随机字节默认 `secrets.randbelow(127)`。
   - Cookie 解析顺序与 ADR 0012 一致：静态 `PEARSS_ZHIHU_COOKIES` 优先，
     否则共享的 CookieCloud（`PEARSS_COOKIECLOUD_*`，domain `zhihu.com`）。
     缺 `d_c0` 视为无会话。
   - 401 重试一次（仅 CookieCloud 来源，强制刷新），与 X 的自愈同构；
     403 保持 `blocked` 不重试——重试显式拒绝在两种解读下都是错的。
   - 凭据只存在于环境与内存，不落库、不进日志（审计 redact 名单已含
     `cookie` 头）。

4. **内容整形对齐参考实现**：丢弃「关注了…」「提出了问题」类动态；其余打
   动作标签（[赞同] [创作] [收藏] [动态]）；pin 标题取摘要或正文并截断 60
   字符；条目作者用内容原作者而非动作发起者。两处有意偏离：

   - pin 的 `video` 元素转为链接——存储边界净化器（ADR 0007）的 allowlist
     不含 `video`，为一个采集器重新开洞是错的交易；
   - 动态作者卡片不带头像图——zhimg 外链会失效，净化器本就剥 `style`。

## 后果

- 准插件接口从「match_url + parse（+ 分页探测）」演进为三能力形态
  （fetch 声明 / 分页声明 / backfill 探测）。**P2 冻结 hookspec 时，fetch
  钩子进入正式合约**，签名与凭据语义一并冻结。
- 第三个登录型采集器到位，Rule of Three 前提进一步巩固。
- `CredentialError → FetchError(AUTH)` 的映射发生在采集器边界：pipeline
  只认 FetchError，这是钩子契约的一部分，新登录型采集器必须遵守。
- Zhihu 的反爬与签名版本（`x-zse-93: 101_3_3.0`、`x-api-version`）随网页
  客户端演进，失败模式是 `structure_changed`/`auth`，由既有诊断路径消化。
