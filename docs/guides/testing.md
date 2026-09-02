# 测试策略

测试的目标不是追求覆盖率数字，而是守住领域不变量、外部内容解析、API/MCP 契约和真实部署链路。测试必须离线、确定、可重复。

## 测试金字塔

| 层级 | 位置 | 测什么 | 是否访问网络/真实服务 | 频率 |
|---|---|---|---|---|
| 单元测试 | `tests/unit/` | core 纯函数、状态机、去重、调度统计、错误分类 | 否 | 每次改动 |
| Fixture 测试 | `tests/fixtures/` 或插件目录 | RSS/HTML 快照到 ItemDraft 的解析行为 | 否 | 每次改动 |
| 集成测试 | `tests/integration/` | adapter 与 SQLite、文件存储、FastAPI 的集成 | 不访问公网；可用临时本地服务 | 每次改动 |
| 契约测试 | `tests/contract/` | OpenAPI 兼容性、MCP Tool schema/描述 snapshot | 否 | 每次改动 |
| E2E | `tests/e2e/` | Compose 启动后的“发现→添加→抓取→发布”闭环 | 仅本地 Compose 服务/测试 fixture server | PR/合并前 |
| Agent eval | `tests/evals/`（P2） | golden 指令的 Tool 选择、确认、安全边界 | 使用 mock MCP 或受控环境 | MCP 改动时 |

## 必测领域不变量

每条以下规则都必须有至少一个直接单元测试：

- `published` Item 不能修改内容、GUID、URL、标题或 content_hash
- `pending` Item 不出现在 RSS/Reader API 视图
- 处理链成功才将 `pending -> ready`；失败保持 pending
- `force_publish` 没有确认/理由时失败，确认后写 AuditLog
- JobRun 终态不可回退；重试创建新的 JobRun
- ErrorClass 只能来自 enum；错误分类优先级固定
- 相同 content_hash 的条目按既定规则去重或关联
- adaptive 调度只依据输入历史与策略参数，结果确定且受 min/max 约束
- 每个写 API 在相同 actor + endpoint + Idempotency-Key 下只产生一次副作用

## Fixture 测试

- 每个 RSS 类型和网页采集器必须有 valid、empty、changed fixture
- fixture 测试绝不访问公网；HTTP 层使用 mock、录制的安全响应或本地 fixture server
- 线上出现 `structure_changed` 后：先提交可脱敏的新 fixture 使测试复现问题，再修 parser
- fixture 是可审查测试语料，不是原始网页归档；只保留实现解析所需的最小页面片段
- expected ItemDraft 输出应断言关键字段，避免因无关 HTML whitespace 造成脆弱失败

## API 与 MCP 契约测试

### OpenAPI

- 规范文件保存在版本控制中
- CI 使用 oasdiff 比较基线和当前 spec，破坏式变化须失败
- endpoint 的示例、enum、错误响应必须可由测试解析/验证
- 对外操作至少有成功、422、403、409/幂等和外部任务失败路径测试

### MCP

- Tool 名称、描述、输入 JSON schema、输出 JSON schema 做 snapshot
- 每个 Tool 测试成功、无权限、无效输入、外部/领域失败
- 写工具测试：没有 elicitation 确认时不得产生副作用；确认参数必须与实际执行参数完全相同
- Tool 返回大内容时测试失败；正文只能通过 Resource 或受控内容 endpoint 读取

## 数据库与迁移测试

- 每一 migration 必须在空库测试 `upgrade head`
- 每一 migration 必须验证可 downgrade 至前一版本，再 upgrade 回 head
- SQLite 测试使用临时文件库而不是共享开发数据库
- 含并发语义的发布/Job claim 至少有一个测试验证条件更新/乐观锁不产生重复副作用

## E2E 最小验收

P1 的 E2E 场景：

1. Compose 启动 Core API、MCP 与受控 fixture HTTP server
2. 调用 `discover_feed` 找到测试 feed
3. 确认并 `add_source`
4. `trigger_job`，等待对应 JobRun 成功
5. 断言 Item 已入库且 `publish_status=ready/published`
6. 获取 RSS 输出，断言 GUID 稳定、条目存在、pending 条目不存在
7. 重复同一 Idempotency-Key 的添加/触发请求，断言无重复副作用

P3 加：翻译闸门、TTS enclosure、Reader API 已读/星标同步。

## 本地与 CI 命令

```bash
just test       # unit + fixture + integration
just contract   # OpenAPI diff + MCP snapshot
just e2e        # Compose E2E
just ci         # 以上全部 + lint + migration checks
```

CI 唯一入口：

```bash
nix flake check
nix develop --command just ci
```

禁止测试依赖开发者机器上的公网、时钟、随机数、已有 SQLite 数据库、真实 token 或真实 LLM provider。

## 失败处理

- 先最小化并复现失败；不要通过跳过测试、扩大 timeout 或吞掉 exception 让 CI 变绿
- 外部页面变化：添加/更新 fixture 和 parser；不要删除 changed fixture
- 需要更新 OpenAPI/MCP snapshot：审查其语义变化，必要时附 ADR，而不是盲目接受新 snapshot
- flaky E2E：优先修同步/可观测性，使用显式健康检查与确定性 polling；不得无限重试
