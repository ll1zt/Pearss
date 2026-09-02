# 个人信息流系统

单用户优先、Agent-First 的 RSS+ 系统：采集 RSS/网页内容，规范化与存储后向 NetNewsWire 提供 RSS 信息流；AI agent 在用户指令下通过 MCP 管理采集、调度、诊断和配置。

> 架构总纲见根目录 `architecture-spec.md`。本文只解决“如何开始开发与验证”。

## 当前范围

P1 提供：

- RSS/Atom 增量采集（ETag / Last-Modified）
- 网站 Feed 自动发现
- OPML 导入导出
- 2–3 个准插件网页采集器
- 固定与自适应调度策略
- 标准 RSS 输出，供 NetNewsWire 订阅
- FastAPI Core API 与 FastMCP 操作面
- 发布闸门、错误分类、审计、fixture 回归测试

P1 不提供：完整 Web 管理 UI、全文搜索、多用户、Reader API 已读同步、第三方插件市场、翻译/TTS。

## 快速开始

### 前置条件

- 已安装 [Nix](https://nixos.org/download/)
- 已安装 [direnv](https://direnv.net/)，并启用 shell hook
- Docker（仅运行 `just e2e` 或 P3 服务时需要）

### 首次启动

```bash
git clone <repository-url>
cd <repository-name>
direnv allow
just sync
just migrate
just run
```

浏览器打开 `http://127.0.0.1:8000/docs` 查看 Swagger UI。

开发环境由 Nix Flake 提供；Python 项目依赖由 uv 安装。不要在项目目录外创建或提交 `.venv`。

### 最小验收

1. 用 Swagger UI 或 MCP 调用 `discover_feed(url)`，获取网站的候选 RSS/Atom 地址
2. 调用 `add_source` 添加一个候选 feed
3. 调用 `trigger_job` 立即抓取
4. 打开对应的 RSS 输出地址，在 NetNewsWire 中订阅
5. 用 `list_jobs`、`search_items` 或 Swagger UI 检查结果

## 常用命令

所有开发、测试和维护命令都应通过 `justfile` 运行：

```text
just shell        # 验证/进入 Nix 开发环境
just sync         # uv sync
just run          # 启动 FastAPI + MCP
just test         # 单元测试与 fixture 回归测试
just lint         # ruff、mypy、import-linter
just contract     # OpenAPI diff 与 MCP 工具 snapshot
just migrate      # alembic upgrade head
just e2e          # Docker Compose 全链路测试
just ci           # 本地完整质量门禁
```

提交前必须运行：

```bash
nix develop --command just ci
```

## 数据与密钥

- 开发数据目录、SQLite 数据库、原始抓取内容、TTS 产物、`.venv`、Nix store 一律不提交 Git
- 密钥仅通过未提交的 `.env` 或部署平台 Secret 注入
- 不得将 token、Cookie、认证 Header、私有 URL、用户数据写入日志、fixture 或 AuditLog
- 事实源数据（Source、Item、ReadState、FeedOut、AuditLog 等）需要备份；派生物可以通过重放管道重建

## 文档导航

| 文档 | 内容 |
|---|---|
| `architecture-spec.md` | 总体需求、架构、路线图、选型与决策 |
| `AGENTS.md` | 编码 agent 的项目规则与完成标准 |
| `docs/api/style-guide.md` | REST/OpenAPI 设计与演进规范 |
| `docs/api/mcp-tool-guide.md` | MCP Tool/Resource 语义与安全规范（待编写，见 P1 路线） |
| `docs/domain/data-model.md` | 领域实体、关系、保留与备份策略 |
| `docs/domain/state-machines.md` | Item、JobRun、Plugin 等状态机 |
| `docs/domain/error-taxonomy.md` | 错误分类、错误 envelope 与诊断语义 |
| `docs/guides/collector-guide.md` | RSS/网页采集器与 fixture 编写指南 |
| `docs/guides/testing.md` | 测试层次、fixture、契约与 e2e 规则 |
| `docs/operations/runbook.md` | 故障处理与恢复操作手册 |
| `docs/integrations/netnewswire.md` | NetNewsWire 集成与缓存约束 |
| `docs/adr/` | 架构决策记录 |

## 贡献原则

- 先读 `AGENTS.md`，再改代码
- 新增或修改行为时，优先添加可验证的测试和 fixture
- 领域规则放在 `core/`；框架、网络、数据库、MCP 放在 `adapters/`
- 破坏 API、MCP 工具或数据语义的改动必须写 ADR，并明确迁移路径
