# 0016 · 公网暴露形态：移除公开 RSS 输出，隧道优先，防御内移

- 状态：accepted
- 日期：2026-09-06
- 修订：`README.md`（NetNewsWire 接入、启动服务）、`architecture-spec.md`
  §1.2 / §5 / §6 C1、§2.1（AuditLog 备份等级）、§11、`docs/operations/runbook.md`
- 关联：ADR 0012（登录型采集器）、ADR 0014/0015（fetch 钩子）

## 背景

Pearss 原按「本机 / Tailscale 内网单用户」设计。服务现在需要公网可达，而一次
全面安全审计（实证探测全部 21 个端点）确认：应用层的净化、注入抵抗、日志卫生、
常量时间比较都站得住，但四个前提被公网击穿——

1. 无 TLS 终止方案：三类凭据（Core API token、Reader 密码、MCP token）明文传输；
2. 全站无限速：Reader `ClientLogin` 与 Core API 401 路径可在线暴力破解（实测
   60 连发无阻尼），且每个 401 写一行**永久**审计——10 次/秒的扫描 ≈ 86 万行/天，
   取证表本身成为磁盘耗尽通道；
3. 无请求体上限：无凭据 10MB 请求体被完整接收；
4. `/docs` `/redoc` `/openapi.json` 公开：21 个端点的完整 schema 白送。

另有一个产品决策：`/feeds/default.xml` 无鉴权返回最近 200 条**完整正文**，且
GET 带冻结内容的副作用。

## 决定

| 决策点 | 选择 | 说明 |
|---|---|---|
| 入口形态 | **隧道优先**（Cloudflare Tunnel / Tailscale Funnel），应用只绑 `127.0.0.1`，不开入站端口 | TLS 由隧道终结；其他形态（自建反代等）由操作者自行搭建、自担其责 |
| 公开 RSS 输出 | **移除** `/feeds/{feed_id}.xml`；RSS 客户端一律走已鉴权的 Reader API（FreshRSS 协议） | NNW 原生支持 FreshRSS 账号，无功能损失；`adapters/rss_out.py` 模块保留，供 P3 处理器链/FeedOut 复用 |
| first_publish | 由 Reader API contents 与 `/v1/items/{id}/body` 两条路径记录 | 输出面的不可变保证不变，只是输出面变了；「渲染即冻结」的语义跟到 body 端点 |
| 暴露边界 | 默认仅 Reader API 公网；`/v1` 与 `/mcp` 是否暴露由隧道路由规则决定（操作者自选） | 控制面有 token + elicitation + 限速，但默认不暴露；cloudflared 按路径路由 |
| Swagger 兜底 | `PEARSS_DOCS_ENABLED`（默认开）；公网部署置 0 后 `/docs` `/redoc` `/openapi.json` 一律 404 | 兜底 UI 需求保留在可信网内；公网不送 schema |
| 审计保留 | **30 天滚动**（`PEARSS_AUDIT_RETENTION_DAYS`，0 = 永久），调度器每小时清理 | 这是对「AuditLog 永久事实源」的原则修订：公网 401 洪水让无界保留变成资源耗尽面。30 天内仍可取证 |
| 暴力破解 | 应用内按客户端地址限速：**携带凭据的** 401/403 计入滑动窗口（默认 50 次 / 5 分钟），超限 429 + Retry-After 15 分钟；`PEARSS_AUTH_RATELIMIT_MAX=0` 关闭 | 内存计数、单进程，重启即清零——目标是让在线爆破变慢，不是入侵检测。「携带凭据的」限定（2026-09-20，issue #8）：无 `Authorization` 头的 401（CORS 预检等）不是「失败尝试」——没有猜就没有失败，不计入预算。`/healthz` 整体豁免限速与阻断（issue #6），liveness 语义不被 429 顶替。全站无 CORS 中间件是有意设计：MCP 面（tailnet-only）不在浏览器场景内，跨源预检本就拿不到 `Access-Control-Allow-Origin` |
| 请求体 | `PEARSS_MAX_BODY_BYTES`（默认 1MB）超限 413，在应用运行前拒绝 | 基于 Content-Length；无长度的分块体由隧道缓冲兜底 |
| SSRF | 默认拒绝解析到私网/环回/链路本地地址的抓取目标，重定向逐跳复查且跨宿主剥离凭据头；`PEARSS_ALLOW_PRIVATE_FETCH=1` 关闭（订阅内网源的场景） | 已知局限：先解析后连接存在 DNS rebinding 的 TOCTOU 窗口；威胁模型是写 token 泄露后的内网探测，不是与攻击者竞速解析器 |
| Reader token 有效期 | **维持永不过期**（风险已知并接受），改密即全端轮换（ADR 0005） | 泄露处置写入 runbook |

## 后果

- `architecture-spec.md` §6 C1 从「已交付」改记为「已移除」；内容消费路径收敛到
  C4 Reader API——外部兼容面减少一个。
- `README.md` 的 NetNewsWire 接入只剩 FreshRSS 账号一种方式。
- 审计的备份语义从「永久事实源」变为「30 天滚动事实源」；备份节奏需覆盖该窗口
  内的取证需要。
- 限速以隧道代理上报的 `X-Forwarded-For` 为准（uvicorn `--proxy-headers`，仅信任
  本机隧道进程）；伪造 XFF 需要先控制隧道主机，与威胁模型一致。
- 若 P3 重新需要公开 RSS（如 enclosure 音频 feed），应在新 ADR 下以
  `FeedOut.token_hash` 做 URL token 化，而不是恢复无鉴权端点。
