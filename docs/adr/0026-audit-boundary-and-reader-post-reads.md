# 0026 · 审计边界与 Reader POST 读取认证例外

- 状态：accepted
- 日期：2026-09-18
- 实现：`src/app/middleware.py`（`UNAUDITED_PATHS`、`AUTH_FAILURE_STATUSES`、
  审计 worker 线程）、`src/app/main.py`（中间件顺序）、
  `src/app/reader_routes.py`（`authenticate_any`）、
  `tests/integration/test_audit_responsiveness.py`、
  `tests/e2e/test_failure_paths.py`（认证拒绝不落审计行的钉死断言）、
  `tests/integration/test_app.py::test_a_refused_write_is_not_audited_because_it_never_reaches_the_endpoint`
- 关联：ADR 0016（公开暴露加固——审计泛洪是资源耗尽向量）、
  ADR 0022（agent ingest 与令牌作用域）
- 背景：深度评审（`problem2fix.md` C01/C02）指出仓库规则之间存在冲突：
  一处说「被拒写/认证失败都要审计」，另一处（ADR 0016）指出无条件审计
  每个拒绝是泛洪漏洞，而 Reader API 的 POST 读取用 header 认证又与
  「POST 只认表单 `T`」的成文规则矛盾。本 ADR 一次性裁定三者的边界。

## 裁定 1：审计保证覆盖「到达审计中间件的调用方」

「所有 API 调用方均需经过 Core API 审计中间件」的准确含义是：

1. **每个已认证的写**（2xx/4xx，含校验拒绝 422 与幂等冲突 409）**必须**
   留下一条 actor、operation、correlation_id、脱敏参数齐全的审计行，
   outcome 为 `succeeded` 或 `rejected`。
2. **认证失败的读与写**（401/403）在到达审计中间件时也会被记录
   （`AUTH_FAILURE_STATUSES`）：一阵 401 是撞库在数据里的样子。
3. **EarlyAuth 之外的拒绝**（限流 429、体积 413、未认证即被 EarlyAuth
   拒绝的 401）**不审计**：这些中间件在 CorrelationAndAudit 外侧，
   被拒请求从未成为「API 调用方」。这是有意的泛洪防线——攻击者打满
   审计表不应免费；观测渠道是服务日志与限流计数，不是事实源表。
4. `/healthz`、`/docs`、`/openapi.json`、`/redoc` 与常规读不审计：
   AuditLog 是要备份、保留的事实源，探测与读取的无限增长没有侦查价值。

第 3 点的负面断言由 `test_a_wrong_token_cannot_create_a_source` 钉死
（401 且审计表为空）。若未来把审计移到 EarlyAuth 外侧，该测试会失败，
这个改动必须是带着新泛洪分析的自觉决定。

## 裁定 2：审计写不得阻塞事件循环（A15）

审计行与业务数据同库（SQLite）。审计提交若在共享 asyncio 循环内同步执行，
writer 争用时（采集运行、enrichment worker 持锁是常态）会把**无关请求**
一起拖住最长达 busy timeout。因此：

- 审计写在专属单线程 worker 上执行，fire-and-forget：响应已在网络上时
  审计才发生，等待它只会把响应与请求未造成的争用耦合。
- 会话在线程内创建、关闭。失败降级为 error 级日志（行丢失可见），
  绝不影响调用方已得到的响应。
- 容量上限即单 worker：审计洪峰会排队延迟审计行（日志可见），
  不会放大数据库争用。

## 裁定 3：Reader POST 读取接受 header 认证是文档化的硬例外（C02）

成文规则「POST 只认表单 `T`」服务于**写**端点：`T` 出现在请求体里，
而写必须解析请求体，二者自然同源。但真实客户端（NetNewsWire 的全部
读请求）把参数放查询串、token 放 `Authorization` header，以 POST 发送；
要求 `T` 会让所有文章列表 405/401。因此：

- **写端点**（edit-tag、subscription 管理等）：只认表单 `T`，不认 header。
  这是安全边界，`test_post_does_not_accept_the_token_in_the_header` 钉死。
- **读端点**（`reader_read` 注册的全部路径、`stream/items/contents`）：
  header 或表单 `T` 皆可（`authenticate_any`）。POST 只是传输动词，
  不改变「这是读」的本质。
- 查询串**永远不**作为凭据来源：URL 里的 token 会被访问日志、代理与
  浏览器历史捕获，这个便利没有任何客户端需要。

该例外只放宽 Reader 兼容层的读取认证，Core API（`/v1`）不受影响。

## 后果

- 「认证失败都审计」的旧表述被本 ADR 取代：审计覆盖以中间件位置定义，
  泛洪防线（ADR 0016）优先于记录完备性。
- AGENTS.md 的 Reader API 一节更新为「写 `T`、读 header/T 皆可」的准确表述。
- 审计异步化让「拒绝且落库」类 E2E 断言需要等待 worker 排空
  （`CorrelationAndAuditMiddleware.drain_audit()`），而不是假设行已存在。
