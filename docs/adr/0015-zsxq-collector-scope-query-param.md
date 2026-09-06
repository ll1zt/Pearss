# 0015 · 知识星球采集器：scope 即 URL 查询参数

- 状态：accepted
- 日期：2026-09-06
- 关联：ADR 0012（登录型采集器）、ADR 0014（fetch 钩子与知乎采集器）

## 背景

知识星球群组 API（`api.zsxq.com/v2`）以 `zsxq_access_token` Cookie 鉴权，
无请求签名；响应包裹在 `{"succeeded": true, "resp_data": …}` 信封内，业务码
1059（风控限流）按上游惯例退避重试。群组的栏目（scope：all | digests |
by_owner | questions | tasks）在 Web 应用里是同一 URL 的应用内切换，不存在
天然的每栏目 URL。参考实现：`earss_source_zsxq`（移植自 RSSHub
`lib/routes/zsxq/group.ts`）。

## 决定

1. **scope 编码为源 URL 的查询参数**：`…/group/<id>?scope=digests`；
   `canonicalize_url` 不剥 `scope`（非追踪参数），故不同 scope 是不同源、
   互不冲突。裸 URL 即 `scope=all` 的规范形态——**不要**用 `?scope=all`
   显式订阅，那会给同一个 feed 建第二个身份。
2. **复用 ADR 0014 的 fetch 钩子**：`Registration.fetches=True`；一次采集
   发起两个 API 请求（群组元数据 → feed 名称；topics 列表 → 内容），在
   钩子内合并为单载荷 `{"group": …, "topics": […]}` 交给 parse，维持
   「一钩子一载荷」契约。`{"group": …}` 包装层在此处解包。
3. **信封与重试语义**：1059 退避重试至多 3 次（0.15s × n，pause 可注入），
   耗尽即 `blocked`；其他业务拒绝码同为 `blocked`；信封残缺为 `parse`；
   401 重试一次（仅 CookieCloud 来源），与知乎客户端同一纪律。
4. **鉴权**：静态 `PEARSS_ZSXQ_COOKIE`（完整头，优先）或
   `PEARSS_ZSXQ_ACCESS_TOKEN`（裸 token，包成 cookie），否则共享
   CookieCloud（domain `zsxq.com`）；无论来源，Cookie 必须含
   `zsxq_access_token`。
5. **条目整形对齐参考实现**：topic_id 缺失的条目丢弃；显式 topic.title 优先
   于首行派生；问答条目内容为「提问 blockquote + 可选回答」，作者用回答者、
   缺省提问者；未认识的 type 只取标题不造内容。

## 后果

- 第三个登录型采集器复用既有 fetch 钩子与凭据纪律，未新增注册表机制。
- 与知乎一样共享 CookieCloud 部署：`zsxq.com` 的 Cookie 须与 `x.com`、
  `zhihu.com` 同在一个 uuid 内（按 domain 过滤），或等将来引入 per-collector
  uuid 覆盖。
- `resp_data` 缺失（`succeeded` 为真但无数据）退化为空 topics 列表——这是
  上游「星球暂无内容」的真实形态，不是结构变更。
