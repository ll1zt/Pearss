# TODO — Agent 数据呈现面（L0–L2）

> 状态：**规划中，值得开分支实施**。建议分支名 `feat/agent-view-surface`。
> 来源：2026-09 与用户的讨论——MCP 是很好的入口，但 agent 拿到数据后直接向用户
> 输出原始数据体验不好；如何合适地渲染表格/页面，需要一个系统性方案。
> 实施时遵守 `AGENTS.md`（分层、审计、测试、`nix develop --command just ci`）。

## 1. 问题定义

**两类数据，两条渲染路径，不可混淆：**

| 数据类型 | 正确的渲染面 | 现状 |
|---|---|---|
| 内容本身（item 正文） | NetNewsWire / Reader API（已有，且是最好的） | 无缺口。agent 永远不转述正文（不可信、体积大，且「不得批量返回正文」已是既定规则） |
| 系统运营状态（sources / jobs / error_class 分布 / audit） | **缺失** ← 本 TODO 要解决的 | agent 从 `get_system_snapshot` 等工具拿到 JSON 后即兴拼 markdown 表 |

**为什么「agent 即兴渲染」结构性地不行**（不是模型能力问题）：

1. **不一致**：同一查询每次表格列数、排序都不同；视图需要一致性，模型每次都在重新发明。
2. **不可交互**：排序/筛选/翻页/看单条 detail 都变成「再问 agent 一次」，每轮烧上下文。
3. **上下文双倍消耗**：数据进模型 → 再格式化出模型。这是「列表工具不得批量返回正文」
   （`docs/api/mcp-tool-guide.md`、ADR 0013）同一个反模式上移一层。

**核心原则（应写进 AGENTS.md 的「领域不变量」或 MCP 章节）：**

> Agent 决定**看什么**（哪个源、哪个时间窗、高亮什么异常）；
> 确定性代码决定**怎么看**（布局、排序、交互）。
> Agent 的价值在叙事与诊断（结论 + 建议 + 下一步），不在排版。

## 2. L0 — 聊天内渲染约定（零新表面，任何宿主立刻受益）

**做什么**：让所有 MCP 宿主在聊天里渲染小数据集时保持一致。

- 修改 `src/app/mcp_server.py`：
  - server `instructions` 增加渲染约定段落（表格列、排序、失败行前置等规则）；
  - 读类工具（`list_sources` / `search_items` / `get_system_snapshot` / `list_jobs` /
    `list_runs`）的结果附带 `display_hint` 字段，声明建议的列与排序
    （例如 sources：`title / status / last_run / error_class`，失败行前置）；
  - 更新 `tests/contract/mcp_tools.json` snapshot（`MCP_SNAPSHOT_UPDATE=1 just contract`
    后审查 diff，见 `docs/api/mcp-tool-guide.md`）。
- **适用范围**：≤20 行的小数据集。大集合不进聊天，走 L1 链接。
- **工作量**：约半天。
- **验收**：同一工具在任意宿主（Claude Desktop / 其他 MCP 客户端 / 终端 agent）下
  渲染结构一致；`just ci` 通过（含工具面 snapshot）。

## 3. L1 — 服务端只读视图 + 短时签名链接（核心层，最适合 Pearss）

**关键观察**：Pearss 本来就是常驻 HTTP 服务（`src/app/main.py`）。渲染不必借道宿主，
直接加一组只读 HTML 路由。工具结果携带预填参数的链接：

```json
{
  "summary": "3 sources failing, 2 auth, 1 structure_changed",
  "failing": [ "…前 3 行最小字段…" ],
  "view_url": "https://<host>:8000/views/health?window=24h&token=<短时签名>"
}
```

Agent 的回答变成「一句话结论 + 链接 + 建议的下一步」；排序/筛选/翻页回到真实页面。

**边界澄清（不违反 P1 non-goal「Web 管理 UI」）**：

- 本层是**纯只读视图**；一切写操作仍然 MCP-only + elicitation（ADR 0010）。
- 与发布闸门同构：视图不改变任何状态；读视图页不得写 `first_publish`，
  不得让 `pending` 条目可见（复用 `core.item_state.is_visible`，与 RSS /
  Reader API / MCP 三条既有路径同一规则）。

**实施清单**：

1. 新增 `src/app/views_routes.py`（只读路由，挂 `app/`，不承载领域规则）：
   - `GET /views/health` — 首个页面：system snapshot 可视化。
     §7.7 定义的腐化信号（error_class 分布突变、pending 积压时长、抓取成功率）
     值得一个真实页面而不是每次让 agent 转述；
   - `GET /views/sources`、`GET /views/items`（复用 `adapters.repository.query_items`
     的过滤/分页，HTML 渲染层不新造查询语义）；
   - 模板用服务端渲染（保持零构建链；不引入前端框架）。
2. **短时签名链接**（本层唯一需要新设计的机制）：
   - 服务端用 HMAC 签名 token：绑定路径 + 过期（建议 15 min TTL）+ 可选绑定查询参数；
   - **不要**把长生命周期 token（`PEARSS_API_TOKEN` 等）放进 URL；
   - 失效返回 403 页面并说明「链接已过期，请让 agent 重新生成」；
   - 写 ADR：链接鉴权是新的信任边界决策（按 AGENTS.md 文档规则）。
3. MCP 工具结果增加 `view_url` 字段：
   - 需要 base URL 配置（新增 `PEARSS_PUBLIC_URL` 或从 `PEARSS_BIND_HOST`/`PEARSS_PORT`
     推导；注意与 `PEARSS_MCP_ALLOWED_HOSTS` 同样的「远程访问需显式选择」原则）；
   - agent 侧上下文成本：`view_url` 只有几十字节——配合「fetch 5 行做总结，链接看全部」，
     是 `fields` 过滤（实测省 87%）的自然延伸。
4. 测试：
   - integration：签名 token 的签发/过期/路径绑定/篡改拒绝；
   - integration：视图页不可见 `pending` 条目、不写 `first_publish`；
   - contract：`view_url` 字段进工具面 snapshot。
5. 文档：`README.md` 增加一节；`docs/api/mcp-tool-guide.md` 增补 view_url 语义。

**验收**：用户对 agent 说「看看采集健康」→ 得到一句诊断 + 可点开的 health 页；
链接过期后可再生成；`just ci` 通过。

## 4. L2 — MCP Apps（SEP-1865）：官方路线，渐进增强

**现状（2026-09）**：MCP Apps 已于 2026-01-26 成为 stable 规范（MCP 第一个正式扩展，
并入 2026-07-28 核心规范修订）。工具响应可携带 `ui://` 资源（HTML/CSS/JS），
宿主在沙箱 iframe 内渲染，经 postMessage 上的 JSON-RPC 回调工具。

**本质**：把 L1 的「整页跳转」升级为「聊天内嵌面板」，体验更连贯。

**前置条件（不满足则不启动）**：确认当前主力 MCP 客户端支持 Apps。
**Resource 采用率低的教训（ADR 0013）在此完全适用：必须有降级路径。**

- 渐进增强设计：同一工具既返回数据 + `view_url`（L1 语义），可选附 `ui://` 资源；
  不支持 Apps 的宿主自动降级到 L0/L1，行为不回退；
- 视图模板直接复用 L1 的服务端渲染产物（iframe 只是换宿主渲染同一批 HTML）；
- 安全：iframe 沙箱 + 正文不可信（ADR 0007 入库净化已覆盖）；关注宿主方
  sandbox proxy 的规范要求。

**暂不实施的原因**：宿主支持面仍在演进；绑定年轻生态的churn成本 > 当前收益。
触发条件：主力客户端宣布支持，或用户明确提出「聊天内嵌面板」需求。

## 5. 不做什么（Out of Scope）

- 内容消费进聊天：正文永远走 NNW / Reader API / `item://{id}` Resource；
- 写操作 UI：视图层纯只读，管理面仍是 MCP + elicitation；
- L3（服务端生成静态周报文件）：有「采集健康周报」类需求时再评估；
- 前端框架/构建链：保持服务端渲染的零构建形态。

## 6. 建议实施顺序

```text
分支 feat/agent-view-surface
├── ① L0：渲染约定 + display_hint（半天，独立可先合）
├── ② L1-a：/views/health + 签名链接 + view_url + ADR
├── ③ L1-b：/views/sources、/views/items
└── ④ L2：观望，触发条件见上
```

每一步独立可交付；① 与 ② 无依赖关系，① 可先行。完成的变更按 `AGENTS.md`
Definition of Done 走：测试覆盖 + snapshot 更新 + 文档同步 + `just ci` 通过。
