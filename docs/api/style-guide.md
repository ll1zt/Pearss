# HTTP API / OpenAPI 风格规范

本规范适用于 Core API 的所有 HTTP endpoint。OpenAPI 文件是机器可读契约；本文件定义其命名、行为和演进的一致性规则。

## 原则

- API-first：先更新 OpenAPI，再实现 endpoint
- 稳定优先：面向未来 MCP、Swagger、脚本和 Reader API 调用方，兼容性比内部实现方便更重要
- 资源优先：常规操作以资源 URL 表达；工作流/诊断等复合操作可使用动作 endpoint
- 明确优先：时间、分页、幂等性、错误与副作用必须在 schema 中可见

## 基础约定

| 项目 | 规则 | 示例 |
|---|---|---|
| 版本 | 所有 Core REST API 使用路径版本 | `/v1/sources` |
| 命名 | URL 用复数 kebab-case 名词 | `/v1/feed-outputs` |
| JSON 字段 | `snake_case` | `publish_status` |
| ID | UUIDv7（或项目唯一批准的 ID 格式） | `018f...` |
| 时间 | RFC 3339 / ISO 8601，统一 UTC | `2026-09-02T05:20:00Z` |
| 内容类型 | JSON 为 `application/json`；RSS/OPML 用对应 XML MIME type | `application/rss+xml` |
| 认证 | Bearer token 或会话机制；权限显式声明 | `Authorization: Bearer ...` |
| 请求追踪 | 接受/生成 `X-Request-ID`，响应回显；内部映射到 `correlation_id` | |

## 资源与动作

优先用资源：

```text
GET    /v1/sources
POST   /v1/sources
GET    /v1/sources/{source_id}
PATCH  /v1/sources/{source_id}
DELETE /v1/sources/{source_id}
```

仅在行为无法自然表达为资源 CRUD 时使用动作：

```text
POST /v1/sources/{source_id}:trigger    # 立即触发抓取          ✅ 已实现
POST /v1/sources/{source_id}:ingest     # agent 推送内容（A3）    ✅ 已实现（ADR 0022）
POST /v1/feed-discovery                 # 输入 URL，返回候选 feed  ⏸ A5 暂缓
POST /v1/jobs/{job_id}:pause            # 改变任务生命周期        ❌ 未实现
                                          （暂停走 PATCH /v1/sources/{id} {status}）
POST /v1/items/{item_id}:force-publish  # 需显式授权与审计        ❌ P3（处理器链）
POST /v1/items:delete                   # 批量删除条目（不可逆）     ✅ 已实现（ADR 0021）
DELETE /v1/items/{item_id}              # 删除单条（不可逆）         ✅ 已实现（ADR 0021）
POST /v1/sources/{id}/items:delete      # 清空某源条目、保留订阅      ✅ 已实现（ADR 0021）
```

POST /v1/plugins/{plugin_id}:enable    # 生命周期：启用插件       ✅ P2
POST /v1/plugins/{plugin_id}:disable   # 生命周期：禁用插件       ✅ P2
POST /v1/plugins/{plugin_id}:import    # 安装第三方插件           ⏸ 第三方 loader（独立 ADR）
DELETE /v1/plugins/{plugin_id}         # 卸载                    ⏸ 第三方 loader（独立 ADR）
POST /v1/plugins/{plugin_id}:reload    # 子进程重载（杀进程重 spawn）⏸ 随 loader
```

已实现的 `/v1` 面：`GET|POST /v1/sources`、`GET|PATCH|DELETE /v1/sources/{id}`、
`POST /v1/sources/{id}:trigger`、`POST /v1/sources/{id}:ingest`、
`POST /v1/sources/{id}/items:delete`、
`GET /v1/items`、`GET /v1/items/{id}`、`GET /v1/items/{id}/body`、
`DELETE /v1/items/{id}`、`POST /v1/items:delete`、
`GET /v1/jobs`、`GET /v1/jobs/{id}/runs`、
`GET /v1/system/snapshot`、`GET /v1/plugins`、`GET /v1/plugins/{id}`、
`POST /v1/plugins/{id}:enable|:disable`。

**删除语义（ADR 0021）**：`DELETE /v1/sources/{id}` 删除源行及其全部 Item、
正文、已读状态与产物——**不可逆**；它过去是软删除，语义已变更。
删除一个不存在的 id 返回 200 与计数 0，**不是 404**：重试与首次执行必须对调用方
不可区分（写操作幂等）。批量删除用 POST + `:delete` 后缀，因为 DELETE 带 body
在各客户端/代理上支持不一致。

动作 endpoint 必须：说明副作用、接受幂等键、返回 correlation ID；如果是异步操作，返回 `202 Accepted` 和 job/run 标识。

### 幂等与冲突（写动作的两条 no-op 语义）

写动作的幂等键决定重复请求的语义，两种情况必须区分：

- **同 key 重试**：返回**首次结果的重放**（200，`Idempotent-Replay: true`）——
  重试不是错误；
- **新 key 的重复操作**（资源已处于目标状态）：409 `*_unchanged`——调用方
  在没有重放凭证的情况下要求执行一个已完成的动作，是请求本身的冲突。

不得把前者改写成 409，也不得让后者伪装成 200：前者保护网络重试，后者
暴露真实状态。参考实现：`/v1/plugins/{id}:enable|:disable`。

## 请求与响应

### 写操作幂等性

所有 `POST`、`PATCH`、`DELETE` 与动作 endpoint 都要求：

```http
Idempotency-Key: <client-generated-uuid>
```

服务端以 `actor + endpoint + key` 去重并在合理窗口内复用首次结果。重复请求不可生成重复 Source、Job 或发布行为。

### 异步任务

耗时抓取、导入、处理、批量操作不得阻塞 HTTP 请求：

```http
HTTP/1.1 202 Accepted
Location: /v1/job-runs/{run_id}
```

```json
{
  "job_run_id": "...",
  "status": "queued",
  "correlation_id": "..."
}
```

### 分页与排序

集合 endpoint 使用 cursor 分页，禁止新 endpoint 使用 offset：

```text
GET /v1/items?limit=50&cursor=<opaque>&sort=-published_at
```

- `limit` 默认 50，最大 100
- cursor 不透明，客户端不得解析
- `sort` 的白名单字段必须在 OpenAPI 写明
- 响应提供 `next_cursor`；无下一页时为 `null`

```json
{
  "data": [],
  "next_cursor": null,
  "correlation_id": "..."
}
```

### 字段选择与大内容

列表默认返回摘要字段。支持显式字段选择：

```text
GET /v1/items?fields=id,title,url,published_at
```

HTML 正文、原始 payload、音频及大型翻译结果不得在列表中内嵌。通过独立 endpoint、签名资源 URL 或 MCP Resource URI 获取。

## 错误模型

所有错误响应使用同一 envelope：

```json
{
  "error": {
    "code": "source_unreachable",
    "message": "The feed URL could not be reached.",
    "error_class": "network",
    "suggestion": "Check the URL or retry later.",
    "details": [],
    "correlation_id": "..."
  }
}
```

规则：

- `code`：稳定、机器可读的 `snake_case` 标识；不要让客户端依赖 `message`
- `message`：面向用户的短说明；不泄漏 token、Cookie、内部堆栈或不可信网页内容
- `error_class`：仅可取 `network`、`parse`、`structure_changed`、`blocked`、`auth`；非采集错误可省略
- 校验失败（`RequestValidationError`）也走同一 envelope：不接处理器时 Starlette 会返回另一种形状，同一 API 出现两套错误格式，客户端只能分支适配
- `suggestion`：提供下一步可执行的安全建议
- `details`：仅为结构化校验字段或安全的补充信息

HTTP 状态码：`400` 请求无效、`401` 未认证、`403` 无权限、`404` 不存在、`409` 状态冲突/幂等键冲突、`422` schema 校验失败、`429` 限流、`5xx` 服务端失败。

## 状态、删除与发布

- API 状态字段必须使用领域 enum；不可由客户端提交任意字符串
- 对 `Item` 的内容、GUID 与首次对外发布是不可逆边界；见 `docs/domain/state-machines.md`
- 已发布条目被源改写正文时，返回 `skip_published_content_changed` 语义（保持不动并告警），**不得**重写或另插一条
- `DELETE` 对事实源数据默认软删除或标记停用；物理删除需要单独的、可审计的维护流程
- `force-publish` 等越过安全默认值的动作必须要求显式理由并写审计日志

## OpenAPI 编写规则

- 每个 operation 必须有 `operationId`、summary、说明、请求 schema、成功和失败响应
- 所有 enum 在共享 schema 中定义，尤其 `ErrorClass`、`PublishStatus`、`JobRunStatus`
- 为每个写操作提供含 `Idempotency-Key` 的示例
- schema 变更先修改规范，再改代码；CI 使用 oasdiff 阻断破坏性变更
- 新字段应可选且有安全默认值；删除/重命名字段必须先 deprecate

## 兼容与弃用

- `/v1` 内不得静默移除字段、收紧可选字段、改变字段语义或变更成功/错误状态码
- 必须破坏兼容性时：新增字段/endpoint → 标记旧字段 deprecated → 文档写替代方案和截止时间 → 观察调用量 → 再在 `/v2` 删除
- 一项破坏式变更必须附 ADR 和迁移说明

## Reader API 边界

P3 的 Google Reader 兼容端点 `/api/greader.php` 是兼容性接口，不与 `/v1` REST 资源混用：

- 单独实现 adapter，内部仍调用 core 应用服务
- 兼容 API 不得把 FreshRSS 的数据格式泄漏进 `core/`
- 对已读/星标写入仍走审计、状态机与幂等策略
