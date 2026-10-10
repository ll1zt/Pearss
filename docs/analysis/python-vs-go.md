# Pearss 的 Python 生态依赖评估与 Go 迁移取舍

> 状态：分析文档（只读研究，不构成迁移决定）。
> 方法：静态审计 —— 逐依赖盘点 + 分层耦合分析 + 迁移面量化。
> 数据来源：`pyproject.toml`、`src/**`、`migrations/**`、`docs/adr/**`，数字均为本次在
> 仓库内实测（命令见各节脚注），可复现。
> 结论先行见 §5。

---

## 0. 一句话结论

Pearss 对 Python 生态的依赖**广度大、深度浅**：15 个直接依赖里 14 个能在任意语言里
用标准库或成熟库得到（剩下 1 个 `pluggy` 只是没有等价库，而它是机制问题——
interface + 显式注册表即可）。真正的语言锁定只有**一处、一个文件、307 行**（本地
embedding 的 `onnxruntime + tokenizers`，占生产代码 0.8%）。而这个仓库是**单用户优先**
系统（README、ADR 0038），迁移到 Go 换来的是**运维形态**（单二进制、冷启动、内存），
不是**吞吐**。因此没有"必须迁移"的技术理由；真正值得做的不是换语言，而是把 embedding
推理拆成独立进程边界（这一步不需要 Go，且现有插件运行时已经提供了那条边界）。

---

## 1. Python 生态依赖全景

### 1.1 依赖清单：15 个直接依赖 + 2 个关键传递项

下表逐条给出：用途、在仓库里的真实引用位置（grep 证据）、分层归属、Go 侧替代、
**迁移阻断等级**。前 15 行是 `pyproject.toml` 的 `[project].dependencies`；`starlette`
与 `pydantic` 是随 `fastapi` 进来的传递依赖，单独列出因为迁移时必须当成一等对象处理。

| 依赖 | 用途 | 主要引用位置 | 分层 | Go 侧对应 | 阻断等级 |
|---|---|---|---|---|---|
| `fastapi` | HTTP 路由 / DI / 中间件 | `src/app/*_routes.py`、`main.py`、`middleware.py`、`reader/routes.py`（14 个模块） | app | `net/http` + chi/gin | 可替代 |
| `starlette`（传递） | ASGI 基座、`Form/Query` 解析 | 由 fastapi 引入 | app | `net/http` | 可替代 |
| `uvicorn` | ASGI server | `src/app/http_timeout.py` | app | `net/http.Server` | 可替代 |
| `pydantic`（传递） | 请求/响应模型与校验 | `src/app/*_routes.py`（6 模块，`BaseModel`/`Field`/`field_validator`） | app | struct tag + validator | 可替代 |
| `sqlalchemy` | ORM + Core，SQLite | **38 个 `src/` 文件** + 全部 29 个 migration | adapters/app | `sqlc`/`ent`/`database/sql` | 可替代（高成本） |
| `alembic` | schema 迁移 | `migrations/env.py` + `migrations/versions/*.py`（29 个 revision） | migrations | `golang-migrate`/`goose` | 可替代（需重写历史） |
| `httpx` | 同步+异步 HTTP 客户端、ETag 条件请求 | `src/adapters/http.py`、`deadline_transport.py`、`alerting.py`、`cookiecloud.py` + 采集器（20+ 模块） | adapters/plugins/app | `net/http` + `go-retryablehttp` | 可替代 |
| `lxml` | HTML/XML 解析与序列化 | `src/adapters/article.py`、`sanitize.py`、`rss_out.py`、`zhihu`、`telegram`、`embed`、`translate`（7 文件） | adapters/plugins | `x/net/html` + `goquery`、`encoding/xml` | 可替代 |
| `cssselect` | 为 lxml 提供 CSS 选择器 | `src/plugins/collectors/telegram/collector.py`（`node.cssselect(...)`，8 处） | plugins | `goquery` cascade | 可替代 |
| `nh3` | HTML 消毒（Rust ammonia 绑定） | `src/adapters/sanitize.py`（1 文件） | adapters | `bluemonday` | 可替代（策略需重写） |
| `feedparser` | 容错 RSS/Atom 解析 | `src/plugins/collectors/rss/collector.py`（**1 文件**） | plugins | `gofeed` | 可替代 |
| `mcp` | MCP server SDK | `src/app/mcp_server.py`、`mcp/rendering.py`、`mcp/tools/{reads,writes,resources}.py` | app | `mcp-go` / 官方 Go SDK | 可替代 |
| `pluggy` | hookspec/hookimpl 注册表 | `src/core/plugins/hooks.py`、`src/app/collector_registry.py`（2 文件） | core/app | 无直接等价；interface + 显式注册表 | **需重新设计**（ADR 0017 冻结契约） |
| `onnxruntime` | 本地 embedding 推理 | `src/plugins/processors/embed/encoder.py`（**1 文件**） | plugins | `yalue/onnxruntime_go`（cgo + 原生库） | 可替代（cgo） |
| `tokenizers` | HF 分词器（Rust 绑定） | `src/plugins/processors/embed/encoder.py`（**1 文件**） | plugins | 无成熟等价（`hugot` 部分覆盖） | **阻断**（唯一真正的语言锁定） |
| `numpy` | 向量池化/数学 | `src/plugins/processors/embed/encoder.py`（**1 文件**） | plugins | `gonum` | 可替代 |
| `python-multipart` | Reader API 表单 `T=` 解析 | Reader 写端点（表单认证） | adapters | `r.ParseForm()` | 可替代 |

> 证据脚注：
> - 直接依赖列表 = `pyproject.toml` 的 `[project].dependencies`（15 项）。
> - 引用位置 = `grep -rl --include="*.py" -E "^\s*(import|from)\s+<dep>\b" src`。
> - `sqlalchemy` 计数 = `grep -rl ... src | wc -l` = 38；`feedparser` = 1；`onnxruntime`
>   = `tokenizers` = `numpy` = 1（均在 `encoder.py`）。
> - `pydantic` 不在 `dependencies` 里，它由 `fastapi` 引入；`starlette` 同理。

### 1.2 依赖集中度：广度大，深度浅

按层统计"有多少生产文件 import 了任一第三方库"：

| 层 | 引用第三方库的文件 / 生产文件 | 说明 |
|---|---|---|
| `core` | **1 / 19** | 只有 `core/plugins/hooks.py` 用 `pluggy`（其本身无 I/O，见该文件 docstring）。领域层事实上是纯标准库 |
| `adapters` | 29 / 38 | `sqlalchemy` + `httpx` + `lxml` 是主力 |
| `plugins` | 15 / 17 | 采集器与处理器几乎都碰解析/HTTP |
| `app` | 37 / 44 | `sqlalchemy` + `fastapi` + `pydantic` |

这张表是整份分析的核心事实：**依赖几乎全部集中在"管道"（I/O、框架、解析）里，
而不是"领域"里**。`src/core/` 的 3657 行领域逻辑（`ErrorClass` 分类法、发布状态机、
normalize/去重、调度纯函数、幂等账本折叠）只依赖标准库 + 一个无 I/O 的 `pluggy`。
这意味着迁移的语言风险与领域规则的复杂度是**解耦**的。

### 1.3 两个常被误判为"深度绑定"的地方，实际都很浅

- **翻译管线不是本地模型。** `src/plugins/processors/translate/processor.py`（1354 行）
  是一个 **OpenAI 兼容 HTTP 客户端**：`PEARSS_TRANSLATE_BASE_URL` / `_API_KEY` /
  `_MODEL` 从环境读取，正文经 `httpx` POST 到 chat-completions 端点。它不 import 任何
  ML 库。**翻译能力与宿主语言完全无关**——换 Go 只是换一个 HTTP 客户端。
- **feedparser 只在一个文件里。** 5 个采集器中只有 `rss` 用 `feedparser`；其余 4 个
  （telegram/twitter/zhihu/zsxq）本来就是 `lxml` + `httpx` 手写解析。而 `rss` 采集器
  带 5 套 fixture 回归（见 §4），替代品 `gofeed` 的容错差异**可被现有 fixture 证伪**，
  不是盲替换。

**唯一的深度绑定**是 `src/plugins/processors/embed/encoder.py`：`onnxruntime` 跑 ONNX
图、`tokenizers` 分词、`numpy` 池化，三者在同一个 209 行文件里（+ 同目录 98 行
processor）。这是全仓库唯一"Go 没有等价物"的能力，也是唯一需要 cgo 或进程外服务化的
地方。

---

## 2. 分层迁移面量化与框架耦合点

### 2.1 四层规模（实测）

| 层 | 生产 LOC | 文件数 | 第三方依赖 | 该层迁移要重写什么 |
|---|---|---|---|---|
| `core` | 3 657 | 21 | 仅 `pluggy`（无 I/O） | 领域规则本身：`ErrorClass` 分类法、发布状态机、normalize/去重、调度纯函数、幂等账本折叠、文本分词 |
| `adapters` | 9 304 | 40 | `sqlalchemy`、`httpx`、`lxml`、`nh3` | 全部持久化（20 个 repository 模块，5 305 行）、HTTP 抓取、RSS 输出、消毒、Reader API 兼容层（`reader_api.py` 256 行）、审计 |
| `plugins` | 7 941 | 28（其中生产模块 17） | `lxml`、`httpx`、`feedparser`、`cssselect`、`onnxruntime`、`tokenizers`、`numpy` | 6 个采集器（rss/telegram/twitter/zhihu/zsxq）与 4 个处理器（translate/fulltext/embed/…） |
| `app` | 18 858 | 48 | `fastapi`、`pydantic`、`sqlalchemy`、`mcp`、`uvicorn` | 53 个路由、31 个 MCP 装饰器（30 `tool` + 1 `resource`）、调度器（923 行）、worker、中间件、插件装载器 |
| **合计** | **39 760** | **137** | 15 个直接依赖 | — |

> 命令：`find src/<层> -name "*.py" ! -name "test_*" -exec cat {} + | wc -l`。
> `plugins` 的 7 941 行不含同目录的 17 个 fixture 测试（`src` 内 `test_*.py` 共 8 416 行）。

### 2.2 测试套件的规模（迁移成本的另一半）

| 位置 | 文件数 | LOC | 内容 |
|---|---|---|---|
| `tests/` | 181 | 59 343 | unit 60、integration 98、contract 4、e2e 14 |
| `src/**/test_*.py` | 17 | 8 416 | 与采集器同目录的 fixture 回归（快照与解析器成对） |
| **合计** | **198** | **67 759** | **测试 : 生产 ≈ 1.70 : 1** |

这个比例既是**成本**（重写规模是生产代码的 1.7 倍）也是**保障**（领域不变量已被 67k 行
测试钉死，任何迁移都有可对照的规格）。但要注意：这些测试大量使用真 SQLite、真 HTTP、
`monkeypatch`、以及 Python 内部结构断言（repository 返回的 dataclass、SQLAlchemy 行对象），
**不能机械翻译**，只能按行为重新表达。

### 2.3 框架耦合点清单

| # | 耦合点 | 规模 / 证据 | 迁移影响 |
|---|---|---|---|
| 1 | **SQLAlchemy ORM** | 38 个 `src/` 文件 + `adapters/db_models.py`（804 行）+ 29 个 migration | 声明式模型、`Session` 生命周期、`select()` 组合、FTS5 虚拟表、SQLite 局部索引（`sqlite_where`）、enum CHECK 约束全部重写 |
| 2 | **Alembic 迁移链** | 29 个 revision + `migrations/env.py`；`just migrate-check` 门禁 upgrade/downgrade 往返 | 迁移文件是 **Python 代码**且按 AGENTS.md **内联枚举取值**（不 import `core.enums`）——必须逐条重写为等价 SQL，并保留 downgrade |
| 3 | **FastAPI 路由与 DI** | 53 个 `@router.{get,post,put,patch,delete}` | `Depends`、`Form(...)`/`Query(...)` 默认值、异常处理器（把 pydantic 错误压成统一 envelope）、lifespan 全部重写 |
| 4 | **ASGI 中间件** | `src/app/middleware.py` 写成**纯 ASGI**（非 `BaseHTTPMiddleware`），依赖 contextvar 跨中间件→端点传播 | 审计与 correlation id 的传播保证必须在 Go 里重新论证（`net/http` 的 context 语义不同） |
| 5 | **Pydantic 校验** | 6 个路由模块的 `BaseModel`/`field_validator`/`model_validator`/`model_fields_set` | 请求/响应契约与错误 envelope 需重建 |
| 6 | **pluggy 插件契约** | `core/plugins/hooks.py`（冻结契约，ADR 0017）+ `app/collector_registry.py`（998 行） | Go 无等价物：需 interface + 显式注册表，且**第三方插件协议**（`manifest.json` + `collector.py`，目录名即 collector_id）目前硬绑定 Python 模块名 |
| 7 | **asyncio 并发模型** | `app` 4 文件 / `adapters` 1 文件；同步领域代码经 `asyncio.to_thread` 卸载；`scheduler.py:447` 用 `ThreadPoolExecutor` | Go 的 goroutine 模型更简单，但同步 SQLAlchemy/lxml 的线程池语义（busy timeout、writer 竞争）要重新设计 |
| 8 | **MCP SDK** | 30 个 `@server.tool` + 1 个 `@server.resource`；`just contract` 双 snapshot 门禁 | 工具 schema 与描述可移植，但 snapshot 基线要重建 |
| 9 | **stdlib 安全原语** | `hmac`、`secrets`、`hashlib.scrypt`（Reader API 凭据派生，ADR 0026） | Go 有 `crypto/hmac`、`crypto/rand`、`x/crypto/scrypt`；但"token 由存储哈希派生、跨请求稳定"这一**行为契约**必须重新证明 |

### 2.4 必须在目标语言里重新证明的领域不变量

这些规则与语言无关，但目前**只被 Python 实现和 198 个测试文件编码**。迁移时它们是正确性
回归的高风险面（全部来自 AGENTS.md / `docs/domain/`）：

- 去重匹配**身份（id/guid）优先于内容**，`content_hash` 由正文派生，两者会分叉
- `source.canonical_url` **NOT NULL**（migration 0004；可空列的 UNIQUE 对 NULL 不生效）
- 时间戳**定宽** `%Y-%m-%dT%H:%M:%S.%f` + `Z`（migration 0005 专门归一化历史数据）
- 失败必须用唯一 `ErrorClass` enum，DB 层 CHECK 约束强制
- **`next_run_time` 不落库**，从 policy + 历史推导（ADR 0009）
- 复数操作 = **作业 + 账本**（ADR 0046）：`202 + work_id`，逐条结果只在 `work_unit`，`counts` 折叠不落库，`rejected` 与 `failed` 分离
- 已发布条目不可改内容；`pending` 对一切输出（含 Reader API）不可见

---

## 3. 「不可替代」与「商品化」的划分

判据：**能不能在不损失行为契约的前提下，用目标语言的标准库或成熟库直接得到**。

### 3.1 商品化能力（13 项，覆盖 15 个依赖中的 14 个）

任意语言都有对应物，迁移成本是"重写调用点"而非"重新发明"。第 13 项（hookspec）是
唯一没有等价库的，但它是**机制问题**而非能力缺失（interface + 显式注册表即可）。
15 个依赖里未被本节覆盖的只剩 `feedparser`，见 §3.3。

| 能力 | 现实现 | Go 对应 | 真实资产（与语言无关的部分） |
|---|---|---|---|
| HTTP 服务 / 路由 / DI | `fastapi` + `starlette` + `uvicorn` | `net/http` + chi/gin | 53 个路由的**语义**（错误 envelope、scope 鉴权、审计中间件） |
| HTTP 客户端 | `httpx` | `net/http` | `adapters/http.py`（623 行）：超时、响应体上限、ETag/Last-Modified 条件请求回路、SSRF 域名拦截、deadline 传播 |
| ORM / 持久化 | `sqlalchemy` | `sqlc` / `database/sql` | 22 个 repository 模块的**查询语义**与闸门谓词 |
| 迁移 | `alembic` | `goose` / `golang-migrate` | 29 条 schema 演进的**历史与 downgrade** |
| 请求校验 | `pydantic` | struct tag + validator | 请求/响应契约（含 `model_fields_set` 的"缺失 vs 显式 null"语义） |
| HTML/XML 解析 | `lxml` + `cssselect` | `x/net/html` + `goquery` | `adapters/article.py` 的正文抽取启发式、`rss_out.py` 的 XML 转义规则 |
| HTML 消毒 | `nh3` | `bluemonday` | `sanitize.py` 的**白名单策略**（安全面，必须差分验证） |
| MCP server | `mcp` | `mcp-go` / 官方 Go SDK | MCP 工具面的**描述、schema、annotations** |
| 向量数学 | `numpy` | `gonum` | 池化与归一化的公式 |
| 表单解析 | `python-multipart` | `r.ParseForm()` | Reader API 的 `T=` 表单认证不对称规则 |
| 哈希/随机/编码 | stdlib `hmac`/`secrets`/`hashlib` | stdlib + `x/crypto` | scrypt 参数与"token 由存储哈希派生"的行为契约 |
| 并发 | `asyncio` + `ThreadPoolExecutor` | goroutine + channel | 同步领域代码卸载、writer 竞争与 busy timeout 的处理 |
| hookspec 机制 | `pluggy` | 无直接等价（interface + 显式注册表） | 冻结契约（ADR 0017）+ manifest↔hookimpl 双向校验 |

### 3.2 真正的语言锁定（只有 1 项，且窄）

| 能力 | 真实职责 | 体量 | Go 侧现状 | 可否降级为外部服务 |
|---|---|---|---|---|
| **`onnxruntime` + `tokenizers` + `numpy` 的本地 embedding 管线** | 把正文切块、分词、跑 ONNX 图、池化成向量，写入 `item_enrichment` 供 FTS5+向量混合检索（ADR 0035/0040） | `src/plugins/processors/embed/encoder.py` 209 行 + `processor.py` 98 行（**唯一真正阻断点**） | `onnxruntime_go`（yalue，cgo + 原生库）可用；`tokenizers` 无成熟等价（`hugot` 部分覆盖） | **可以**。它已经是一个插件，已有子进程运行时（`app/plugin_runtime.py`）与"处理器 = 外部 HTTP 调用"先例（translate）。改成 embedding sidecar 后**宿主语言完全无关** |

注意这里的关键区分：`onnxruntime`/`tokenizers` 是**原生（Rust/C++）库的绑定**，不是"Python 生态"。
它们本来就是 Python 之外的资产，只是经 Python 绑定暴露。所以"换 Go"不能消除它们，
只能把绑定换成 cgo —— **把 40MB 原生库换一个调用它的语言，不是收益**。

### 3.3 两个"看似锁定、实为伪命题"的项

- **`feedparser` 的容错语义**：只在 1 个文件里，且有 5 套 fixture 回归。`gofeed` 在畸形
  feed、日期格式、编码嗅探上的容错面与 `feedparser` 不同——但这是**可用现有 fixture 差分
  验证**的工程问题，不是不可替代的能力。
- **`lxml`/`cssselect` 的选择器栈**：`goquery`（cascadia）已覆盖 CSS 选择器；`x/net/html`
  是符合规范的解析器（与 libxml2 的"宽容修复"行为不同）。差异集中在畸形 HTML 上，而
  `article.py` 的抽取启发式**本来就是针对真实页面调参的**，同样有 fixture 可对照。

### 3.4 这一节的结论

> **"Python 生态依赖程度"这个提法容易误导。** 真正不可替代的不是"Python"，而是
> "一个原生推理运行时"。而它只占 39760 行生产代码中的约 **300 行（0.8%）**，
> 且已经是可隔离的插件边界。

换言之：**把 15 个依赖里的 14 个换掉，换来的不是架构收益，而是把 39.7k 行语言无关的
逻辑用另一种语言重抄一遍。**

---

## 4. 三方案 trade-off 矩阵

> 代价与收益的**量级**均为估算，不是实测（本目标的口径为静态审计）。每个数字下面附
> 推导依据，可被实测推翻。

### 4.1 方案 A —— 留在 Python

| 维度 | 评估 |
|---|---|
| 迁移成本 | **0** |
| 收益 | **0**（但保留 64 个 ADR、198 个测试文件、29 条迁移链的全部既得利益） |
| 已知痛点 | ① 冷启动需加载解释器 + 48 个 app 模块；② `onnxruntime` 40MB 原生库 + numpy 抬高常驻内存；③ 部署依赖 Python 运行时（但 Nix + systemd 已闭环，`just install-unit`） |
| 不构成痛点 | **并发**。工作负载是 I/O 密集（HTTP 抓取、SQLite）+ 偶发原生计算（ONNX、numpy 都释放 GIL）；真正的天花板是 **SQLite 单写者**，换 Go 也是同一个限制 |

### 4.2 方案 B —— 全量迁 Go

**成本推导**（按产物数量，而非仅按 LOC）：

| 工作包 | 估算（人天） | 依据 |
|---|---|---|
| 29 条 Alembic 迁移重写为等价 SQL + downgrade | 15–29 | 迁移文件是 Python 代码且内联枚举值（AGENTS.md），无法机械转换；`just migrate-check` 要求 upgrade/downgrade 往返 |
| 22 个 repository 模块（SQLAlchemy → sqlc/database/sql，含 FTS5、局部索引、闸门谓词） | 30–50 | `src/adapters/repository/` 20 个模块、5 305 行 |
| 53 个路由 + pydantic 契约 + 错误 envelope + 审计中间件 | 15–25 | 含 contextvar 传播、scope 鉴权、幂等键 |
| `core` 领域层（3 657 行，但受 mutmut 变异测试守护） | 15–30 | 不变量密度高，逐条对应 7 条领域规则（§2.4） |
| 6 采集器 + 4 处理器（含 fixture 回归） | 10–20 | 含登录型采集器的凭据/签名协议 |
| 调度器、两个 worker、插件装载器（子进程沙箱）、hardening、backup、reindex | 30–50 | `app` 层 18 858 行中除去路由的主体 |
| MCP 工具面（30 个 `tool` + 1 个 `resource`）+ snapshot 基线重建 | 10 | 工具描述与 schema 可移植 |
| Reader API 兼容层（Google Reader 形态） | 10 | `adapters/reader_api.py` + `app/reader/` 共 1 753 行 |
| **测试重新表达**（198 文件 / 67 759 行，按行为而非机械翻译） | 40–80 | 测试:​生产 = 1.70:1；大量断言绑在 Python 内部结构上 |
| 不变量差分验证（content_hash、定宽时间戳、幂等账本、闸门可见性） | 20 | 需要独立 oracle，不能只靠新实现的自己的测试 |
| **合计** | **195–314 人天 ≈ 9–15 人月** | 对 107k 行（生产+测试）忠实移植：≈ 340–550 行/天，对机械部分偏乐观、对不变量部分偏悲观 |

**收益（估算）**：

| 项 | Python（估） | Go（估） | 说明 |
|---|---|---|---|
| 进程冷启动 | 0.3–1.0 s | 5–20 ms | 但**首次 embedding 都要加载 40MB 模型**，那才是主导项——换语言不消除它 |
| 常驻内存 | 150–400 MB | 60–120 MB | 差值 ≈ 100–300 MB（模型内存在两边相同）；小 VPS 上有意义，工作站上无意义 |
| 吞吐 | — | 提升不显着 | 天花板是 SQLite 单写者 + 外部站点限流，不是解释器 |
| 部署形态 | venv/uv lock + Nix | 单一静态二进制 | **真实收益**：可直接分发给非技术用户 |

**风险**：

1. **正确性回归面重新打开**。AGENTS.md 记录本项目六个实际缺陷**全部**产生于"该路径的第一次真实使用"（会话存续、传输边界、用户输入真实形态）。重写会把这三类边界重新变回"未经真实使用"状态——包括 gzip 双重解压、代理/重定向适配、token 派生稳定性。
2. **测试与实现同时重写 = 失去安全网**。现在 198 个测试是**独立**于新实现的口径；重写时它们既是成本也是 oracle，两者叠在同一个人身上。
3. **64 个 ADR 中与实现绑定的会失效**：ADR 0023/0025（插件装载器）、0026（Reader 凭据）、0040（embedding 跨层依赖）、0017（冻结 hookspec）都需重开并重新论证。
4. **分层不变量会失去强制手段**。现在 `import-linter` 在 `just lint` 里硬性卡住 `app → plugins → adapters → core`；Go 需自建 `depguard`/自写 analyzer 才能平替。
5. **第三方插件协议硬绑定 Python**（最强论据）。`PEARSS_PLUGIN_DIR` 下每个插件必须是含 `collector.py` 的 Python 包。Go 宿主要么废弃现有插件协议，要么**在 Go 宿主里仍然运行 Python 子进程**——后者意味着你已经打包了一个 Python 运行时，换语言的主要收益（单二进制、无 Python）当场消失。

**与现有架构约束的兼容性**：❌ 破坏 import-linter 强制手段；❌ 使实现绑定的 ADR 失效；❌ 破坏第三方插件协议；✅ 分层**方向**本身可保留（Go 包也可单向依赖）。

### 4.3 方案 C —— 混合架构

需区分两个很不同的变体：

**C1：Go 做采集热路径（调度 + 拓取 + 解析），Python 保留 core + ML + 插件**

| 维度 | 评估 |
|---|---|
| 成本 | 2–3 人月（Go 服务 15–25 人天 + IPC 协议 + 跨语言类型同步 + 插件协议适配） |
| 收益 | 几乎为零：抓取是 I/O 密集，瓶颈在外面站点与 SQLite，不在解释器 |
| 新风险 | 你得到了**两个运行时、两个部署单元、两个测试套件、一个跨进程协议**。而 AGENTS.md 记录的六个缺陷中，至少两个（egress gzip 双重解压、Client/Transport 类型错配）**就发生在进程/传输边界上**——增加一条边界就是增加这类缺陷的滋生面 |

**C2：把 embedding 推理拆成 sidecar（不改宿主语言）**

| 维度 | 评估 |
|---|---|
| 成本 | **3–5 人天** |
| 收益 | 消除唯一的"语言锁定"；40MB 原生库可独立升级/重启/限内存；主进程卸载后内存下降 |
| 为何这么便宜 | 这条边界**已经存在**：处理器已经是插件，`app/plugin_runtime.py` 已在跑子进程隔离，translate 处理器已经演示了"处理器 = 外部 HTTP 调用" |
| 风险 | 低。失败契约已经是 `ProcessorError(ErrorClass)`，与 translate 同型 |

> **C2 才是真正的"混合架构"。** 它用 3–5 天买到 C1/B 想要的那部分收益（隔离、可独立演进、卸载内存），
> 且不引入新语言、新运行时、新分层不变量。

### 4.4 矩阵总览

| | A 留下 Python | B 全量迁 Go | C1 Go 拓取 + Python | C2 embedding sidecar |
|---|---|---|---|---|
| 成本 | 0 | 9–15 人月 | 2–3 人月 | 3–5 人天 |
| 吞吐 | 不变（受 SQLite 限制） | 微小提升 | ≈ 不变 | 不变 |
| 内存 | 不变 | −100~300 MB | ≈ 不变 | −（模型移出主进程） |
| 冷启动 | 不变 | −0.3~1 s（**首次 embed 仍加载模型**） | ≈ 不变 | 主进程变轻，模型启动移到 sidecar |
| 单二进制分发 | ❌ | ✅（但要连带分发原生库 + 插件运行时问题） | ❌ | ❌ |
| 分层不变量强制 | ✅ import-linter | ❌ 需重建 | ❌ 两条边界 | ✅ 不动 |
| 第三方插件协议 | ✅ | ❌ 或带 Python 子进程 | ❌ | ✅ |
| 现有 198 测试作为 oracle | ✅ | ❌ 与实现同时重写 | ⚠ 部分 | ✅ |
| 真实新风险 | 无 | 重新打开六类历史缺陷 + ADR 重论证 | 新增进程/传输边界 | 低（同 translate 型） |

---

## 5. 推荐与可证伪的翻转条件

### 5.1 推荐

> **方案 A（留在 Python）为默认；唯一的架构投资是 C2（把 embedding 推理拆成 sidecar），
> 而不是换语言。不推荐 B（全量迁 Go），也不推荐 C1（Go 拓取 + Python 宿主）。**

**理由（按权重排序）：**

1. **定位约束**。README 与 ADR 0038 明确这是**单用户优先**系统。而 Go 的主要收益
   （高并发、低内存、单二进制）对应的是**多租户、高吞吐、面向分发**的场景。在一个
   单用户、I/O 密集、**SQLite 单写者**的系统上，这些收益大多不可兑现。
2. **语言锁定的体量与其消解方式**。真正不可替代的只有 `tokenizers`（连带 `onnxruntime`），
   共 307 行（0.8%），且已经是可隔离的插件边界。**用 C2 消除它只需 3–5 天**，不需要
   换语言。
3. **成本-收益不对称**。全量迁 Go 需要 9–15 人月 + 重写 67.8k 行测试 + 重论证 64 个 ADR
   + 破坏现有插件协议，换来的主要是分发形态与 100–300 MB 内存。除非**部署/分发形态
   发生真实变化**，否则不对等。
4. **迁移成本的真实构成不是"Python 依赖"，而是"语言无关的逻辑"**。39.7k 行生产代码里
   只有 0.8% 真正绑定 Python 生态；剩下 99.2% 换语言只是重拄。
5. **C1（混合）是最差选项**。它付出两个运行时/两个部署单元/一个跨进程协议的代价，
   而 AGENTS.md 记录的六个历史缺陷里至少两个就发生在进程/传输边界上。增加一条边界
   = 增加这类缺陷的滋生面，却换不到吞吐。

**如果一定要为"将来可能的迁移"做准备，唯一值得现在做的动作是 C2**：把
`processors/embed` 的推理搬到独立进程（复用已有的插件子进程运行时），让宿主只通过
HTTP/管道拿向量。这样：① 消除唯一的语言锁定；② 为内存/崩溃隔离买到真实收益；
③ 不引入新语言，可逆。

> 反过来说：**如果连 C2 都不需要，那就不需要任何动作。**

### 5.2 翻转条件（出现哪一条，就应推翻本推荐）

每条都要求**可观测信号**，不接受"感觉 Go 更好"：

| # | 触发条件 | 可观测信号 | 为何它真能翻转结论 |
|---|---|---|---|
| 1 | **分发形态变化** | 出现"不想装 Python/Nix"的真实用户诉求；需要双击即用的单二进制/桌面产物 | 单二进制是 Go 相对于 Python 唯一无争议、无法用其他手段替代的收益 |
| 2 | **部署目标无法承载 Python** | 主部署形态迁到 serverless/边缘（Cloudflare Workers、冷启动敏感的 Lambda） | Python 在这些平台上要么不可用要么冷启动不可接受，属硬约束 |
| 3 | **单实例 CPU 长期饱和且 profile 指向解释器** | CPU 持续 >80%，且 `py-spy top` / `perf` 显示时间花在 Python 字节码而非 I/O 或原生库 | 这是唯一能证明"解释器本身是瓶颈"的证据 |
| 4 | **多租户/高并发成为真实需求** | 出现第二个真实用户/团队；或 QPS 增长到 SQLite 写锁成为可测量瓶颈 | 直接推翻 ADR 0038 的单租户前提，也推翻"I/O 密集"假设 |
| 5 | **插件生态需要非 Python** | ≥1 个外部贡献者要求用 Go/Rust 写采集器，且维持 Python 子进程协议不可接受 | 当前协议硬绑定 `collector.py`；一旦要支持多语言插件，宿主语言的决策就不再由内核决定 |
| 6 | **embedding 换成远程服务** | `PEARSS_EMBED_*` 指向远程 embedding API（与 translate 同型），本地 ONNX 栈下线 | 唯一语言锁定消失，全量迁 Go 的成本-收益比**变好**（但仍不构成"必须迁"） |
| 7 | **维护者能力结构变化** | 连续多个变更周期无人能安全修改 `core`；团队只有 Go 维护者 | 语言决策最终由能维护它的人决定，与代码本身无关 |

### 5.3 明确不构成迁移理由的论点

| 常见论点 | 为何无效 |
|---|---|
| "Python 比 Go 慢" | 本系统瓶颈在外部站点、SQLite 写锁、原生库，不在解释器。需先证明 §5.2 #3 |
| "Go 并发模型更好" | 负载是 I/O 密集；ONNX 与 numpy 都释放 GIL；真正的串行点是 SQLite 单写者，换语言也是同一个限制 |
| "onnxruntime 太占内存" | 换 Go 不消除那 40MB 原生模型（`onnxruntime_go` 同样加载它）。应做 C2 或换小模型，而不是换语言 |
| "Go 生态更稳定/更好部署" | 部署已由 Nix + systemd 闭环（`just install-unit`），不是当前痛点 |

### 5.4 如果将来真要迁，正确的顺序

1. **先做 C2**（3–5 天）：把 embedding 移出主进程，验证进程边界是否真的带来收益。
2. **再实测** §5.2 #3（py-spy/perf）与 #4（SQLite 写锁），确认瓶颈在解释器而非存储。
3. 若确认，**按 §4.2 的工作包逐块 strangler**：先迁无状态、无领域规则的层（采集/拓取），
   `core` 领域层最后迁且必须带独立 oracle。
4. **不可 strangler 的部分**：领域规则不能两份实现（会变成两个事实源，正是 ADR 0009
   拒绝存 `next_run_time` 的同一类错误）。`core` 迁移必须是一次性的、带差分测试的切换。

---

## 6. 附录

### 6.1 本文数字的复现命令

```bash
# 依赖数量与清单
python3 -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies'])"

# 每层生产 LOC（不含同目录 fixture 测试）
for d in core adapters plugins app; do
  echo -n "$d: "; find src/$d -name '*.py' ! -name 'test_*' -exec cat {} + | wc -l
done

# 每层生产文件数（含 __init__.py）
for d in core adapters plugins app; do echo -n "$d: "; find src/$d -name '*.py' ! -name 'test_*' | wc -l; done

# 依赖集中度（引用任一第三方库的生产文件数 / 生产文件数）
for d in core adapters plugins app; do
  tot=$(find src/$d -name '*.py' ! -name 'test_*' ! -name '__init__.py' | wc -l)
  dep=$(grep -rlE '^\s*(import|from)\s+(fastapi|starlette|sqlalchemy|alembic|feedparser|httpx|lxml|cssselect|nh3|mcp|onnxruntime|tokenizers|numpy|pluggy|uvicorn|pydantic|multipart)\b' --include='*.py' $(find src/$d -name '*.py' ! -name 'test_*') | wc -l)
  echo "$d: $dep / $tot"
done

# 测试规模
tests:   find tests -name '*.py' | wc -l            # 181
fixture: find src -name 'test_*.py' | wc -l         # 17
loc:     find tests -name '*.py' -exec cat {} + | wc -l   # 59343

# 框架耦合点规模
ls migrations/versions/*.py | wc -l                                    # 29
find src/adapters/repository -name '*.py' ! -name '__init__.py' | wc -l # 20
grep -rhoE '@router\.(get|post|put|patch|delete)' src/app | wc -l       # 53
grep -rhoE '@server\.(tool|resource)' src/app/mcp | sort | uniq -c      # 30 tool / 1 resource
grep -rl --include='*.py' -E '^\s*(import|from)\s+sqlalchemy' src | wc -l # 38
ls docs/adr/*.md | wc -l                                               # 64

# 唯一语言锁定点的体量
wc -l src/plugins/processors/embed/encoder.py src/plugins/processors/embed/processor.py  # 209 + 98
```

### 6.2 审计中发现的文档/代码漂移（不属本目标修复范围）

- **MCP 工具数**：`README.md` 写"22 个工具（10 读 + 12 写）"，而 `src/app/mcp/tools/`
  实测 **30 个 `@server.tool` + 1 个 `@server.resource`**（`reads.py` 12 + `writes.py` 18）。
  两者不一致，建议后续以 `just contract` 的 snapshot 为准确认是 README 滞后还是
  工具注册方式不同（本目标只读，不修）。
- 内置采集器的 manifest 声明在 `app/collector_registry.py` 中（`manifest_json=json.dumps(...)`），
  只有**第三方**插件才在磁盘上放 `manifest.json`（`app/plugin_loader.py:MANIFEST_NAME`）。
  这与 AGENTS.md 的表述一致，但容易被误读为"所有插件都有 manifest.json"。

### 6.3 本目标的边界

- 未执行任何性能基准或 Go 原型；§4 的性能/内存数字均标注为估算。
- 未修改 `src/`、`pyproject.toml`、`uv.lock`、`migrations/` 或任何测试。
- 未新增 ADR。若本结论需要固化为长期决策，应另开 ADR（本目标范围外）。

