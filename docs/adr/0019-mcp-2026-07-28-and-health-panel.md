# 0019 · MCP 协议升级至 2026-07-28 与 L2 健康面板

- 状态：accepted
- 日期：2026-09-12
- 关联：ADR 0010（MCP 经 Core API）、ADR 0013（get_item_body）、ADR 0016（公网暴露）
- 实测依据：全部结论来自对真实服务的线级探测（raw JSON-RPC POST + SDK 客户端
  连接），不是静态阅读代码的推断；探测中三个"疑似服务端缺陷"最终均归因于探针
  自身构造错误（`_meta` 嵌套位置、缺失 `jsonrpc`/`id`、缺失 auth 头）。

## 背景

对 MCP 规范 2026-07-28 修订与 Pearss MCP 面的对照审计发现两类问题：

**协议层**（根因相同）：服务经 `streamable_http_app()` 默认参数构建，协商到
2025-11-25（握手时代）。在该路径上，现行规范的若干 MUST 结构性不可达——

- `server/discover`（MUST）：除 `ping` 外全部方法被握手门禁拦截，实测返回
  `400 Missing session ID`；
- `ttlMs`/`cacheScope`（六个可缓存方法 MUST）：legacy 线格式没有 `resultType`
  信封可承载。SDK 静默填充默认值，看似符合实则不是我们声明的事实；
- elicitation 的服务端反向请求机制：无状态传输根本没有反向通道，升级后立即
  失效（`no back-channel for server-initiated requests`）。

**业务层**（独立缺陷）：

- `item://{id}` 资源在条目缺失时返回空内容（200 + `""`）。规范 resources 章节
  明文禁止：空 contents 无法区分"存在但无正文"与"不存在"，agent 会把后者读成
  前者并转述给用户；
- `Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version` 镜像头不校验。这些头存在的
  意义是让中间层不解析 body 即可路由；头与 body 分叉意味着"代理放行的请求"
  与"服务器执行的请求"不是同一个——SDK 不做此校验，责任在应用。

另：agent 数据呈现面的 L1（服务端只读视图 + 短时签名链接）在此期间被重新评估。

## 决定

| 决策点 | 选择 | 说明 |
|---|---|---|
| 协议版本 | **升到 2026-07-28**：`stateless_http=True` | 六个 MUST 可达；MRTR 就位；代价是不再响应握手时代客户端——单用户、客户端自控，接受 |
| 缓存提示 | tools/list 等 5 分钟 private；resources/read 0 | 工具面只随发布变化；pending 条目随时可能变为可读，缓存"不存在"会活过闸门 |
| 镜像头校验 | 新增 `MCPHeaderValidationMiddleware`，400 + `-32020` | 有镜像头才校验：legacy 客户端一个都不发，拒了会破坏传输层自己要求的向后兼容；缺失 ≠ 分歧 |
| 资源缺失 | `-32602` + `uri` 数据 | `body_not_stored`（存在但不可读）与 `item_not_found` 同码但消息区分；`-32603` 兜底 |
| elicitation | **迁 MRTR**：SDK resolver（`Resolve`/`Elicit`） | 一套代码双协议兼容；拒绝语义变更见下 |
| 无能力客户端 | `-32021 MissingRequiredClientCapability`（不再返回友好 `{"declined": true}`） | 旧形态需要向未声明能力的客户端发请求，规范禁止且无状态传输无通道；错误数据指明缺失能力 |
| L1（签名链接视图） | **砍掉** | 其独有价值仅剩"链接可分享/跨设备"——单用户系统不存在；Apps 路线免掉全部新信任边界（无新路由、无签名、无公开 URL 配置、无新 ADR 决策面） |
| L2（健康面板） | **落地**：`get_system_snapshot` 绑定 `ui://pearss/health` | 渐进增强：工具结果不变，降级路径天然存在 |

## L2 面板的边界

- **只读**：面板是渲染，不触碰任何状态；写操作仍 MCP-only + elicitation。
- **可见性同构**：面板数据来自 `system_snapshot`，与 RSS / Reader API / MCP
  三条既有路径同一可见性规则，不新增第四套。
- **自包含是契约**：无 script、无外部引用（测试断言）。沙箱 iframe 默认 deny，
  不给 CSP 留谈判空间。
- **渲染在服务端、读取时求值**：模型不复述计数；`data-*` 属性让测试与宿主
  结构化读取面板，而非刮取散文。
- **注册时序**：Apps 绑定必须在 `MCPServer` 构造前登记——extension 在
  `__init__` 即被消费，迟到的绑定永远不会出现在 `tools/list`。

## 后果

- `just ci` 全绿（1037+ 测试）；契约快照现在包含工具 `_meta`，UI 绑定的变更
  必须作为审查过的 diff 进入版本库。
- 客户端影响：仅支持握手时代的第三方 MCP 客户端将无法连接。单用户部署可接受；
  若未来需要兼容，按规范的 backward-compatibility 规则另行评估，不默认回退。
- 变异测试视角的注记：本轮所有服务端修复均由先红后绿的线级测试驱动；三个
  探针误报的教训（先怀疑自己的测试工具）记入 testing.md 的"实际验证过的手法"。
