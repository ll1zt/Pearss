# 0010 · MCP 工具面经 Core API 调用，用官方 mcp SDK

- 状态：accepted
- 日期：2026-09-04
- 修订：`architecture-spec.md` §9 的「FastMCP 3.x」一行、§8「FastMCP 从 OpenAPI 生成」的执行方式

## 背景

§9 写「MCP | FastMCP 3.x，Streamable HTTP | 官方 SDK 上层封装」。实现时两个事实与它不符：

1. **版本号已过时**。独立 `fastmcp` 包现为 4.0.2；官方 `mcp` SDK 现为 2.1.1，其内置的
   `mcp.server.fastmcp` 是当年 FastMCP 并入官方 SDK 的那份代码。"3.x" 两边都对不上
2. **§8 说「FastMCP 从 OpenAPI 生成」，但全自动生成会违反 AGENTS.md 的硬约束**

第 2 点是真正的冲突。`from_openapi()` 生成的工具：

- **不支持 elicitation** —— 而 AGENTS.md 要求「需要用户确认的 MCP 动作必须走 elicitation」
- **把响应原样返回** —— 而 §2.3 明确「正文走 `item://{id}` Resource」，非功能要求「返回紧凑」
- 无法把 Core API 的错误 envelope 映射成 §2.3 规定的
  `{error, error_class, suggestion, next_actions}`

## 决策

### 1. 用官方 `mcp` SDK（内含 FastMCP），不用独立 `fastmcp` 包

§9 的原话是「官方 SDK 上层封装」—— FastMCP 正是以 `mcp.server.fastmcp` 的形式存在于官方
SDK 内。选官方包同时满足「用 FastMCP」与「用官方 SDK」，且少一个第三方上游。

### 2. 工具经 Core API 调用，而非直调仓储，也非从 OpenAPI 自动生成

```text
agent → MCP tool → httpx ASGITransport → /v1/... → 审计中间件 → repository
```

决定性理由是 AGENTS.md 第 39 行：

> 所有 API 调用方均需经过 Core API 审计中间件；不得只在 MCP 层做审计

直调仓储会绕过审计、鉴权与校验。而"从 OpenAPI 自动生成"虽然字面最符合 §8，却拿掉
elicitation 和截断控制 —— 那是把两条硬约束换掉一条。

经 HTTP 调自己的 `/v1` 同时满足全部：

| 约束 | 自动生成 | 直调仓储 | 经 /v1 |
|---|---|---|---|
| 审计覆盖所有调用方 | ✅ | ❌ | ✅ |
| 写操作走 elicitation | ❌ | ✅ | ✅ |
| 大内容不进返回值 | ❌ | ✅ | ✅ |
| 单一契约不重复 | ✅ | ❌ | ✅ |
| 复用 /v1 鉴权与校验 | ✅ | ❌ | ✅ |

§8「MCP 是 OpenAPI 的投影」在**语义层**保持：工具与 `/v1` endpoint 一一对应、错误码同源、
命名对齐 `operationId`。投影由手写的薄封装完成，而非代码生成器。

### 3. 工具集范围

映射 §2.3 P1 清单中**已有 Core API 支撑**的部分，并补三个只读端点：

```
GET /v1/jobs
GET /v1/jobs/{job_id}/runs
GET /v1/system/snapshot
```

工具：`list_sources`、`add_source`⚠、`update_schedule_policy`⚠、`pause_source`⚠、
`resume_source`⚠、`remove_source`⚠、`trigger_job`、`search_items`、`get_system_snapshot`、
`list_runs`。Resource：`item://{item_id}`。

⚠ = 需 elicitation 确认。

**暂缓**：`discover_feed`（A5，用户已决定不做）、`import_opml`/`export_opml`（A6 未实现）、
`create_feed`（FeedOut 尚无 CRUD 端点）。这些在 §2.3 的 P1 清单里，本次不交付。

## 理由与代价

代价，如实记录：

- **多一层进程内 HTTP**。开销是微秒级，但确实存在；换来的是审计与校验不被绕过
- **MCP server 需持有写权限 token**。它作为可信内部客户端调用 `/v1`，因此审计里 actor
  记为 `api:full` 而非具体 agent —— agent 身份通过 session/correlation id 关联，
  不通过伪造 actor 表达。这是一个真实的可归因性弱化，写进 mcp-tool-guide 第八节
- **手写封装意味着工具面可能与 `/v1` 漂移**。缓解：snapshot 测试（§7.1）+ 每个工具
  一个 endpoint 的对应关系 + 验收清单

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| `from_openapi` 全自动生成 | 无 elicitation、响应原样返回，违反 AGENTS.md 与 §2.3 |
| 工具直调 repository | 绕过审计中间件，直接违反 AGENTS.md 第 39 行 |
| 用独立 `fastmcp` 4.x | 多一个第三方上游，且 §9 原意是「官方 SDK 上层封装」 |

## 后果

- 新增动作的正确顺序变成「先有 `/v1` endpoint，再包工具」，写进 mcp-tool-guide 第一节
- `/v1` 是唯一的实现点，工具只做呈现层控制（截断、字段、elicitation、错误映射）
- §9 的「FastMCP 3.x」需按本 ADR 修订为「官方 `mcp` SDK 的 `mcp.server.fastmcp`」
- P3 拆子模块 server + Gateway 时本决策继续适用，只是挂载点变化

## 可逆性

- 选官方 `mcp` 包：可逆，独立 `fastmcp` 的 API 形状相近
- 经 `/v1` 而非直调仓储：**技术上可逆但不应做** —— 回退即违反审计覆盖要求
- 工具集范围：可逆，补暂缓项是增量
