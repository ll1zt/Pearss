# 测试策略

测试的目标不是追求覆盖率数字，而是守住领域不变量、外部内容解析、API/MCP 契约和真实部署链路。测试必须离线、确定、可重复。

## 测试金字塔

| 层级 | 位置 | 测什么 | 是否访问网络/真实服务 | 频率 |
|---|---|---|---|---|
| 单元测试 | `tests/unit/` | core 纯函数、状态机、去重、调度统计、错误分类、属性测试、结构守卫 | 否 | 每次改动 |
| Fixture 测试 | 插件目录（`src/plugins/collectors/*/test_*.py`） | RSS/HTML 快照到 ItemDraft 的解析行为 | 否 | 每次改动 |
| 集成测试 | `tests/integration/` | adapter 与 SQLite、文件存储、FastAPI 的集成 | 不访问公网；可用临时本地服务 | 每次改动 |
| 契约测试 | `tests/contract/` | MCP Tool 面 snapshot（`mcp_tools.json`）+ OpenAPI 全文档 snapshot（`openapi.json`） | 否（起本地 uvicorn 线程） | 每次改动 |
| E2E | `tests/e2e/` | 单进程全链路：真 uvicorn 线程 + 真 MCP 客户端 + 本地 fixture origin，覆盖 testing.md 的 P1 七步场景 | 仅 127.0.0.1（`PEARSS_ALLOW_PRIVATE_FETCH=1` 是守卫的文档化逃生门） | 每次改动 |
| Agent eval | `tests/evals/`（P2） | golden 指令的 Tool 选择、确认、安全边界 | 使用 mock MCP 或受控环境 | MCP 改动时 |

### 契约层（tests/contract/）

- MCP tool 面：`test_tool_surface_matches_the_snapshot` 经 Streamable HTTP 连活服务取 tool 列表，与 `mcp_tools.json` 比对。更新：`MCP_SNAPSHOT_UPDATE=1 just contract`，审查 diff 后提交
- OpenAPI：`app.openapi()` 全文档与 `openapi.json` 比对（oasdiff 在 nixpkgs 不可用，用与 MCP 相同的 snapshot 手法保证破坏式变更必须显式接受）。更新：`OPENAPI_SNAPSHOT_UPDATE=1 just contract`
- 契约层自带 conftest（空迁移库 + 真 HTTP 服务），不依赖 integration 套件的任何 fixture

### E2E（tests/e2e/）

- 拓扑：一个 app 实例、一个 uvicorn 线程；REST 与 MCP（挂载于同一 app 的 `/mcp`）走同一端口；origin 是本地 `ThreadingHTTPServer` 供 RSS fixture
- 覆盖七步：无 elicitation 拒绝写 → 确认 add_source → trigger_job 同步等 run 成功 → item 过闸（ready/published）→ Reader API 登录/ids/contents 稳定 → RSS+ 输出层（库级，`render_feed` 尚无 HTTP 路由）GUID 稳定 → 幂等重放只产生一个 source 且审计落行
- discover_feed 不存在（A5 backlog），feed URL 由测试直接给定；工具落地后补第 2 步

### 属性测试（hypothesis）

- `tests/unit/conftest.py` 注册 derandomize profile：testing.md 禁止依赖随机数，derandomized 的失败样例永远可复现；扩大 `max_examples` 是对配置文件的可见 diff
- 现有属性：sanitize 输出不含执行/追踪向量且幂等；定宽时间戳字典序==时间序且往返稳定；content_hash 只看散文（markup/空白/零宽不敏感）、确定性且对规范化后不同的输入给出不同 hash；item 身份 tenant 隔离、guid 与内部 id 不同命名空间；scrypt verify 对任意输入不抛错；merge_state 幂等
- 安全边界（sanitize）的属性测试验证过有牙：放宽 scheme 白名单的注入回归立刻变红

### 结构守卫（tests/unit/test_structure_guards.py）

守卫测的是**项目自身结构的约定**，不是业务逻辑：

- enum ↔ DB CHECK 同步：对迁移后库的真实 DDL 断言每个 DB 侧枚举列的 CHECK 允许集合 == enum 成员集合（DDL 而非迁移源码，天然排除部分索引谓词）；缺 CHECK 本身也失败
- migration downgrade：upgrade 改 schema 的 migration，downgrade 必须有真实 op 调用；纯数据迁移允许有文档的 no-op（0005 的先例）
- fixture 引用：每个 collector fixture 必须被同目录至少一个测试引用，否则是死语料
- ADR 引用：docs/README/AGENTS 中每个 `ADR NNNN` 必须能解析到 `docs/adr/NNNN-*.md`

### 变异测试（mutmut）

覆盖率不等于断言强度：一个测试可以"执行"了代码却验证不了行为（本仓库修过十余个这类假测试）。变异测试系统性地回答"这些测试能不能被证伪"。

- 范围：只对 `src/core/`（`only_mutate = ["src/core/*"]`，其余 src/ 原样复制以保证 import 完整）。core 是纯函数、毫秒级，变异结果可解释；对 app/plugins 全量变异要数小时，测的是集成测试速度而非断言强度
- 运行：`just mutate`（几分钟量级）。**改了测试后必须 `rm -rf mutants` 重跑**——mutmut 缓存按配置 hash 失效，不感知测试文件变更，缓存会给出一模一样的旧数字
- 首轮基线（本分支落地前）：1184 变异，killed 703 / survived 255 / no-tests 226
- 针对性补测后：killed 807 / survived 198 / no-tests 178（score ≈ 80%，基线 ≈ 73%）。补的测试在 `test_rpc_protocol.py`（RPC 解码边界，杀了 ~46）、`test_core_guards.py`（naive-datetime / policy-bound / 处理闸门守卫的单元级断言）、`test_dedupe.py` 尾部三个 pin（identity-or 语义 + matched_id 审计指针）
- 读法：存活的变异分两类——**测试缺口**（补一个能杀它的断言）与**等价/文案变异**（如 reason 字符串加 XX、防御性 `or` 改写后行为真不变），后者记录并放过。剩余存活集中在 normalize 的 reason 文案（84）与 models 的 dataclass 管道（no-tests 154）——前者多为等价类，后者只有集成路径执行，两者都不是断言缺口优先级。新增 core 代码时跑一次，作为 review 的断言强度证据

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

## 实际验证过的手法

这些不是建议，是仓库里已经在跑的做法，新增测试应沿用：

- **注入式验证**：每条安全/不变量测试都要确认它**会失败**。做法是临时注入对应回归，跑测试，看它红，再恢复。本项目靠这个抓出多个「测试通过但功能不工作」的情形
- **假测试自查**：断言恒真（`x or True`）、辅助函数从未被调用、`or` 短路顺序导致先对 `None` 求值 —— 都出现过。写完问一句「什么回归会让它失败」
- **MCP 用真实客户端**：经 Streamable HTTP 连活服务，而不是直接调工具函数。测的是线上契约（schema 生成、elicitation、审计覆盖）。elicitation 回调要**在同一 task 内进出**，故用 helper 上下文管理器而非 async fixture（anyio cancel scope 限制）
- **调度器注入时钟**：`run_tick(now=...)` 可传时钟；不传时生产用真实时间。注意**时钟必须贯穿** —— 用挂钟写 run 时间戳而用注入值判到期，会让 elapsed 为负、被时钟偏移规则判为「永远到期」，退避静默失效
- **对外契约以对端实现为准**：Reader API 的 item id 格式与 GET/POST 动词，两次都是「读文档/读源码得出的结论」被**真实客户端流量**推翻（见 ADR 0006 与 netnewswire.md）
- **先怀疑自己的探针**：MCP 2026-07-28 协议升级时，三个「服务端缺陷」（`server/discover` 不可达、discover 内部错误、auto 模式回退）最终全部归因于探针脚本自身的错误——`_meta` 嵌套位置、缺 `jsonrpc`/`id`、缺 auth 头。静态读代码判断协议符合度不可靠，必须以实测协商结果为准；而实测结果反常时，第一嫌疑人是测试工具的构造，不是被测系统

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
