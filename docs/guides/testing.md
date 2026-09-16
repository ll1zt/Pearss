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
- `test_full_loop.py` 覆盖七步：无 elicitation 拒绝写 → 确认 add_source → trigger_job 同步等 run 成功 → item 过闸（ready/published）→ Reader API 登录/ids/contents 稳定 → RSS+ 输出层（库级，`render_feed` 尚无 HTTP 路由）GUID 稳定 → 幂等重放只产生一个 source 且审计落行
- discover_feed 不存在（A5 backlog），feed URL 由测试直接给定；工具落地后补第 2 步

#### 客户端同步旅程（`test_client_sync_journey.py`）

**这个系统有两条用户腿，e2e 曾经只长了 agent 那条。** 2026-09 的五轮修复
（`it=`、`xt` 截断、`continuation`、quickadd/rename-tag/disable-tag、
长式 id 零填充）全部发生在**阅读器客户端的状态同步旅程**上，且全部由用户
报告、事后才补测——因为 e2e 的 Reader API 用法只有登录 + ids + contents
三个只读步骤。下面是这条旅程的验收场景清单，测试按此逐条验收；
**新客户端兼容问题先落到这里成为场景，再写代码**（场景先行，不是事后回归）。

**状态（2026-09-16）**：J1–J10 已全部实现为 e2e（`test_client_sync_journey.py`，
10 个场景）。**该套件在编写过程中就抓到一个此前任何层都没发现的真 bug**：
`s=user/-/state/com.google/read` 流（NNW 的 Google-Reader 形态与 Read You 的
非 FreshRSS 分支使用）命中 `visible_ids_for_stream` 的 unhandled 分支，恒返回
空——已读条目对所有按名查询已读流的客户端不可见。已修复并经变异验证
（撤掉修复分支，J3/J5/J6 等 4 个场景立刻变红）。

旅程的前提约束：两个客户端用**各自真实的请求形态**连同一个服务器
（NNW 与 Read You 的形态差异是已记录的契约：`s=` vs `it=`、header vs `T`、
十进制 vs 长式 id），走真实 uvicorn 端口——ASGI 直连测不出中间件顺序回归，
而本仓库在中间件顺序上真实出过事。

| # | Scenario（user 开头，Given/When/Then） | 拦过的 bug |
|---|---|---|
| J1 | 用户在客户端 A 登录并看到订阅与条目（NNW 形态：十进制 id、`s=read`） | 长式 id 未填充（0006 修订） |
| J2 | 用户在客户端 A 读了一篇 → 客户端 B 同步后它也是已读（Read You 形态：`it=read` + 翻页拉全） | `it=` 忽略（ADR 0024）、`continuation` 恒 null |
| J3 | 用户在客户端 B 把它改回未读 → A 同步后也是未读（反向必须同样成立，方向性是排障的决定性线索） | 同上 |
| J4 | 用户给一篇加星 → 两个客户端同步后都看到星标 | 星标此前无跨客户端 e2e |
| J5 | 用户的未读数在无操作时两次同步结果一致（read+unread 恰好划分全集，不重不漏） | 未读数震荡（`it=` 全集） |
| J6 | 用户的条目超过一页时，同步能取全（翻页跟 `continuation`，深度上限终止） | `continuation` 缺失；`xt` LIMIT 后过滤 |
| J7 | 用户在客户端 App 里加一个订阅（quickadd），刷新后订阅与条目都在 | quickadd 404 |
| J8 | 用户在 App 里把订阅移入分组 / 改组名 / 删组，两个客户端的侧边栏都跟着变 | rename-tag / disable-tag 404 |
| J9 | 用户在 App 里删订阅被拒绝，且源与条目原样还在（删除的不可逆性不暴露给同步路径） | `ac=unsubscribe` 决策（ADR 0024） |
| J10 | 用户订阅了带翻译链的源，Reader API 里译文在分隔线之上、原文保存在下（对两种请求形态都成立） | 翻译链此前无 e2e（provider 用本地 OpenAI 兼容真 origin） |

失败模式备注（来自真实教训，写给将来的排查者）：

- **方向性症状先当约束用**："A→B 不行但 B→A 行"立即把范围缩到一条读取路径
  （J2/J3 成对存在就是这个原因）
- **"没有下一页"是肯定性断言**：`continuation: null` 与返回错误集合同罪
  （J6）
- **同一客户端的两条路径结论可以相反**（Read You 的 syncFeed 对、sync 错），
  所以 J2–J6 断言的是**服务端响应**在两种请求形态下都正确，不模拟客户端内部
  集合运算——那是它的实现，不是我们的契约

#### 失败路径（`test_failure_paths.py`）

**拒绝之后系统有没有被改动**，是这里唯一的主题。一个"拒绝了但仍然写入"的缺陷在本仓库最难发现——响应看起来完全正确，损害只在之后显形（未鉴权者建出了源、未确认的 MCP 写落地、pending 条目在 enrichment 跑完前进了客户端缓存）。happy path 永远不会暴露这类问题，所以它必须是一个文件。

覆盖的四个场景：

| 场景 | 断言的可观测结果 |
|---|---|
| 错误/缺失 token 的写请求 | 401；**source 行数为 0**（状态码本身证明不了没落库） |
| 错误 token + 畸形 body | **401 而非 422**：body 在鉴权前不被解析 |
| MCP 无 elicitation / 明确 decline | 拒绝；source 行数为 0 |
| pending 条目 | RSS、`/v1/items`、Reader API **三面**都看不到；且该行确实存在（否则测的是"没存进去"） |
| 幂等键复用但 body 不同 | 库里仍只有 1 个源，且是首个 |

三个实测出来的、与本仓库设计相反的直觉，都写进了测试的 docstring：

1. **401 不写审计行**——不是疏漏。`EarlyAuthMiddleware` 在 `CorrelationAndAudit` **外层**，未鉴权请求在审计写入前就被拒。`_should_audit` 本来会记录 401（鉴权失败是可审计事件），所以"审计为空"这条断言钉的是中间件顺序。
2. **被拒的写请求会记 `outcome=rejected`**——4xx 是"我们替调用方拒绝了"，5xx 才是"我们失败了"。所以 `audit_log == 0` 是错的断言，应为 `outcomes == []`（鉴权层）或"不含 succeeded"（路由层）。
3. **decline 由 SDK 抛成 tool error**（`is_error=True`），根本到不了工具自己的 `{"declined": True}` 分支。测的是这个可观测事实，不是代码里那条看起来会走的分支。


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
- **测试卫生**（5 条，机器可判定的规则落成守卫而非文档）：无 `assert_called`/`call_count`；无 `unittest.mock` import；无无界 `sleep`；无 `assert ... or True`；每个含测试的模块必须声明 marker

### 每个测试都必须带 marker（否则等于不存在）

`just` 的每个目标按 marker 选择（`just test` 跑 `-m "unit or fixture"`），而 **pytest 的 `-m` 表达式会 deselect 未标记的测试**——不是跳过，是根本不运行。所以一个没写 marker 的测试**本地通过、CI 通过、永远不跑**。

本仓库实际发生过：`test_deletion.py` 整个文件 32 个测试没有任何 marker（它是 class 组织的，绕过了模块级 `pytestmark` 的惯例），`test_core_guards.py` 12 个里只有 1 个带装饰器，`translate/test_processor.py` 19 个全裸——`just ci` 收集 1326、实跑 1264，差的 62 个全是"通过因为不存在"。其中 `test_deletion` 覆盖的是共享正文规则（删条目不得删掉别的条目还在引用的文件），而该文件自己的文档字符串就写着这种失败是静默的。

新增测试模块时：模块级 `pytestmark = pytest.mark.<level>`，或在每个测试上写 `@pytest.mark.<level>`。守卫 `test_every_test_module_declares_a_marker` 会拦下遗漏。

自查命令（应当为空）：

```bash
uv run pytest --collect-only -q | grep "::" | sort > /tmp/all.txt
uv run pytest --collect-only -q -m "unit or fixture or integration or e2e or contract" \
  | grep "::" | sort > /tmp/ran.txt
comm -23 /tmp/all.txt /tmp/ran.txt
```

### 变异测试（mutmut）

覆盖率不等于断言强度：一个测试可以"执行"了代码却验证不了行为（本仓库修过十余个这类假测试）。变异测试系统性地回答"这些测试能不能被证伪"。

- 范围：只对 `src/core/`（`only_mutate = ["src/core/*"]`，其余 src/ 原样复制以保证 import 完整）。core 是纯函数、毫秒级，变异结果可解释；对 app/plugins 全量变异要数小时，测的是集成测试速度而非断言强度
- 运行：`just mutate`（几分钟量级）。**改了测试后必须 `rm -rf mutants` 重跑**——mutmut 缓存按配置 hash 失效，不感知测试文件变更，缓存会给出一模一样的旧数字
- **`also_copy` 必须覆盖单测读到的所有仓库文件**。缺一个条目的后果不是"某个变异存活"，而是 *stats collection 阶段直接抛异常*，`just mutate` 以 `failed to collect stats` 退出、什么都不报告。`justfile` 与 `.env.example` 都曾缺失（结构守卫读这两个文件），整个门禁因此静默失效
- 当前基线：1291 变异，killed 1064 / survived 207 / no-tests 20（score ≈ 82%）
- 历史：本轮起点为 killed 884 / survived 206 / **no-tests 201**。补测后 no-tests 降到 20（剩下的全是 `hooks.py` 异常类 `__init__` 与 `with_content_ref` 这类管道），killed 升到 1064
- 读法：存活的变异分两类——**测试缺口**（补一个能杀它的断言）与**等价/文案变异**，后者记录并放过。207 个存活里约 112 行是异常消息文案（`raise X("...")` → `raise X(None)` / `X("XX...XX")` / 全大写），测试断言的是异常**类型**，这是契约，不该为文案写断言；其余多为不可达分支（如 `host` 已 `.lower()`，集合里的大写成员永远不匹配；`SchedulePolicy.__post_init__` 已校验的重复守卫）

#### 三类真实的断言缺口（都实际修过）

1. **`is not None` 系列**：`assert d.matched_id is not None` / `assert r.audit_note is not None` 看不见"某个分支丢了指针"。循环里只要有一个决策有值，整个循环就通过。正确写法是**按分支断言确切值**
2. **只测相对性质**：`derive_item_id` 只断言"确定、租户隔离、与 guid 不同"时，**丢掉某个输入字段的变异仍然通过**——因为丢字段同时保留全部四条性质。持久化标识（item id / guid / content_hash / 密码哈希格式）必须**钉死字面值**，并注明"改这个值需要数据迁移"
3. **mutation 视界 ≠ 覆盖率视界**：只被 integration 调用的 core 纯函数，在 mutation 报告里显示为 `no tests`（mutmut 只选 `tests/unit`），数字虚高。纯函数应在 `tests/unit` 直接测输入输出

### 覆盖率：先看数字是什么意思，再看改动的行

覆盖率是**诊断工具**，本仓库刻意**没有**全局 `fail_under`：在这个体量下，全局阈值最省力的达成方式是给最容易碰到的代码补测试，而剩下那 800 多句未覆盖行，百分比根本区分不了它们属于哪一类：

- **进程外入口**：`plugins/collectors/__main__.py`（28%，子进程 RPC 入口）、`enrichment_backfill.py`（0%，`just enrichment-backfill` CLI）、`twitter/walk.py`（30%，经沙箱 RPC 才走到）——它们的正确性由沙箱集成测试和手工验收覆盖，不是缺口
- **真实缺口**：失败路径与错误分支

测量的前提是数字要诚实。`[tool.coverage.run]` 的 `source` **加上** `omit` 才能去掉 `src/plugins/collectors/*/test_*.py` 这类"就住在被测代码旁边、且按定义必然执行"的测试模块——`source` 单独不够，pytest-cov 会测量所有被导入的东西。去掉之后总数从 89% 降到 87%，那才是生产代码的覆盖率。

**真正守住新代码的是 `just coverage-diff`**（`scripts/coverage_diff.py`）：它取零上下文 diff 里**本次新增/修改**的行，逐个问 coverage 是否被覆盖。一个改动只需为自己写的行负责，不必背上一个"顺手碰过"的模块的旧账。默认阈值 20% 未覆盖即失败。

```bash
just coverage              # 全量测量（生产代码，排除测试模块）
just coverage-diff         # 相对 origin/main 只检查新增行
just coverage-diff HEAD~3  # 或指定 base
```

未覆盖且**确实到不了**的行，在 commit message 里说明理由，而不是写成凑数的测试——`// unreachable: <为什么>` 是允许的，垃圾测试不允许。

### 测试驱动：先场景，后实现

新功能与缺陷修复的默认顺序（本仓库已在用，写入此处以免遗忘）：

1. **先写验收场景**，用 Given/When/Then 描述"用户能看到什么"，不是"调用了哪个函数"
2. **先跑一遍确认它是红的**。对缺陷修复尤其如此：先写一条**能复现 bug** 的测试，看它因预期原因失败，再改代码
3. 实现后确认变绿，然后**注入式验证**（见下节）确认这条测试真的会因为该回归而变红

例外：纯重构不需要新测试，但**断言一行都不许改**。断言是业务契约；重构时改断言，说明它测的是实现。

### 断言强度：写完要问一句

写完每条测试，问："什么回归会让它失败？" 答不上来就是假测试。以下模式在本仓库实际出现过：

- **恒真断言**：`assert x.isprintable() or True` —— 后半句让整条永真。删掉重写
- **自反断言**：`assert a != a.replace("a","b")` —— 只证明了字符串不等于自己的替换，与被测逻辑无关
- **循环里的存在性断言**：见上文缺口 1
- **只断言长度/类型**：`assert len(drafts) == 1` 在需要确认"是哪一个 draft"时不够

补完断言后，**用变异或手改源码确认它变红**——本仓库每条安全/不变量测试都这么验过。这一步不能省：本轮修复中，先写的两条断言（自反的 scheme 测试、`or`→`and` 的守卫）实测**杀不死对应变异**，其中一条是措辞误导（改写为诚实描述），另一条证实为等价变异（`""` 与 `$` 串 split 后必然少于 6 段，六段检查已覆盖）。


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
- **写失败路径测试时，先确认"拒绝"的机制再断言**：本轮补 E2E 失败路径时，三条最自然的直觉断言全错——401 不写审计行（中间件顺序使然）、被拒的写记 `rejected` 而非空、decline 被 SDK 抛成 tool error 而非走工具自己的分支。**测试揭示的机制与源码读起来的印象不一致时，以实测为准，并把差异写进 docstring**；若差异说明的是别处的文档错了（见下条），记录它而不是改测试去迎合
- **手写 INSERT 是脆弱测试**：本轮两次因为绕过 repository 直接写 SQL 而失败（漏 `created_at`、猜了一个不是 repository 派生出来的 id）。**种子数据一律走 repository**：它复现的是生产写入路径，且 schema 加列时不会碎
- **模块级全局状态必须由 teardown 清理，且清理要是幂等的**：本轮引入「运行时装载的插件」后，`_INSTALLED_PLUGINS` / `_SANDBOX_CHILDREN` 是模块全局，`conftest` 的 autouse teardown 一度只是 `stop()` 子进程而没清插件表——于是一个测试装的插件在下一个测试里仍然注册，且**优先于内置采集器匹配 URL**，把 `test_zhihu_collector` / `test_twitter_paging` 全弄红。更隐蔽的是第二次尝试：teardown 里做了 `_REGISTRY = None` 强制重建，反而让「持有 registry 引用」的测试（`test_scheduler`）莫名其妙地失败。**判据**：teardown 只在「确实有东西要清」时动状态（早返回），且清完后的状态要与从未装载过一致——不要用重建来"顺便"重置，重建会波及没被污染的部分
- **跨文件运行时依赖：单独跑通 ≠ 全量跑通**。`pytest 单独一个文件` 与 `pytest tests/integration/` 结果不同时，几乎总是全局状态或导入副作用；发现后不要满足于"单独跑是绿的"，要定位是哪个先跑的测试留下的状态（二分：把可疑文件与受害文件放一起跑即可复现）

## 本地与 CI 命令

```bash
just test            # unit + fixture
just test-integration
just test-e2e
just contract        # OpenAPI + MCP snapshot
just ci              # 以上全部 + lint + migration checks
just coverage        # 全量覆盖率（生产代码）
just coverage-diff   # 只检查本次新增行的覆盖（默认相对 origin/main）
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

## 警告即错误，与唯一例外

`pyproject.toml` 的 `filterwarnings = ["error"]` 把每条警告升级为失败。理由：
一条被忽略的警告（未处理的 deprecation、未关闭的资源）会在日后变成难查的 bug，
而测试期是代价最低的发现点。

**唯一例外**：`unclosed database in <sqlite3.Connection:ResourceWarning`。
CPython 的 sqlite3 在连接对象**被回收**时才发这条警告；在 SQLAlchemy 下这属于
正常情形——连接归连接池所有、由 `engine.dispose()` 关闭，但最后一个引用在解释器
退出阶段被丢弃的 engine，会把它的池化连接留给 GC 回收。

这条 filter 用词刻意很窄（只此一条消息、只 sqlite3），所以**真正的问题仍会暴露**：
连接池被打爆表现为 `OperationalError`，而其他所有 `ResourceWarning` 依旧致命。

排查此类"失败落在无辜测试上"的现象时，注意警告的**归属**取决于 GC 时机，与真实
成因无关；不要据此去"修"被点名的那个测试。诊断建议：

```bash
# 记录分配栈：注意 tracemalloc 记录的是「分配」而非「存活」，
# 它报告的对象可能早已被正常释放，不能作为泄漏证据
PYTHONTRACEMALLOC=25 uv run pytest tests/integration -q
```

要判断是否真有对象存活，必须**先 `gc.collect()` 再计数**，否则数到的是尚未回收的
临时对象。

## 未覆盖行的分类（2026-09-15 实测）

全量测量（unit + fixture + integration，生产代码）后未覆盖 774 句，逐块看过后分四类。**这份分类是"覆盖率差在哪"的答案，也是"该往哪补"的依据**——它随代码移动，重新测量后应更新。

### 一、进程外入口（~170 句，不补测试）

测试进程永远走不到的代码路径，它们的正确性由别的机制保证：

| 模块 | 未覆盖 | 为什么不补 |
|---|---|---|
| `plugins/collectors/__main__.py`（92 句，28%） | 子进程 RPC 服务循环 | 被 `test_plugin_runtime` 以**真实子进程**方式驱动（崩溃恢复、冻结超时），coverage 看不到子进程内部 |
| `app/enrichment_backfill.py`（31 句，0%） | `just enrichment-backfill` 的 CLI 壳 | 真逻辑（`backfill_chain`）已测；壳是参数解析，`just enrichment-backfill` 手工验收过 |
| `app/backup.py` 的 `main()`（~10 句） | CLI 参数分发 | `backup()`/`restore()` 本体含拒绝覆盖、integrity_check 均已测 |

### 二、平台受限分支（少量，标注而非测试）

`plugin_runtime.py:213-225` 的 `RLIMIT_AS` 分支：`sys.platform == "linux"` 下才执行，macOS 上**运行时不可达**。注释（及 `SandboxLimits` 的 docstring）已说明。同理 `hardening.py` 里平台相关的 rlimit 差异。这类分支的正确归属是**部署环境验证**（y700 是 Linux），不是单测。

### 三、真实缺口（值得补，按价值排序）

1. **调度器周期维护**——本轮已补（`test_scheduler_maintenance.py`，6 条）
2. **MCP 工具的 elicitation 分支**（`mcp_server.py:488-499` 等，~70 句）：`_confirmation_verdict` 的 `action != "accept"`、`data` 非 Confirmation 形态等防御分支只在 `-32021` 拒绝路径里间接经过。**失败方向是"确认被当成功"**，值得直接单测
3. **`adapters/http.py:113-132`**（20 句）：`PEARSS_TEST_DNS` 的 JSON 解析失败回退、DNS 解析失败返回空集。**这是 SSRF 守卫的输入解析**，failure direction 是"解析坏了退回真实 DNS"——安全相关
4. **twitter 展示分支**（`collector.py` 47 句）：视频/动图 label 分支、`photo` 无 `expanded` 等。纯展示，按 PR1-8 的验收逐个补 fixture 即可

### 四、防御性分支（~200 句，不为它们写测试）

各 collector 解析器里"字段缺失/类型不对就返回 None"的卫语句簇（zhihu 43 句、telegram 27 句、zsxq 23 句里的多数）。这些**已经在**做正确的退化，且 fixture 测试覆盖了主形态；给每个卫语句造一个"字段恰好缺失"的 fixture 是在为覆盖率凑数——skill 第五条明确反对。例外：**安全方向**的卫语句（如 SSRF 的 `_is_private_ip` 对不可解析地址返回 True）不在此列，必须测。

### 规则

- 新增进程外入口时，在本节加一行说明由什么保证
- 想补"真实缺口"类时，先确认它的失败方向（安全相关优先）
- 不为第四类写测试；发现某防御分支其实可观测时（如同 `is_visible` 那样），先接线再测

### coverage-diff 的已知局限：代码搬移

`coverage-diff` 以"新增行"为单位，因此**纯搬移**（把一段代码从 A 文件挪到 B
文件）会把所有搬移行都计为新增——即便它们的覆盖状况与搬移前分毫不差。重构
PR 跑 `just coverage-diff` 时若失败，先比对搬移前后的覆盖：

```bash
# 搬移前的行在 main 上是否同样未覆盖
git show main:src/<old>.py | sed -n '<old_start>,<old_end>p'
uv run coverage report -m | grep <old-file>
```

逐行核实等价（同代码、同覆盖状况）后放行是正当的；这不是"绕过门禁"，
而是门禁口径的边界——它衡量的是"你新写并被执行的行"，搬移的行不是新写的。
