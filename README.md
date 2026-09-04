# 个人信息流系统

单用户优先、Agent-First 的 RSS+ 系统：采集 RSS/网页内容，规范化与存储后向 NetNewsWire 提供 RSS 信息流；AI agent 在用户指令下通过 MCP 管理采集、调度、诊断和配置。

> 架构总纲见根目录 `architecture-spec.md`。本文只解决“如何开始开发与验证”。

## 当前范围

P1 目标提供：

- RSS/Atom 增量采集（ETag / Last-Modified）
- 网站 Feed 自动发现
- OPML 导入导出
- 2–3 个准插件网页采集器
- 固定与自适应调度策略
- 标准 RSS 输出，供 NetNewsWire 订阅
- FastAPI Core API 与 FastMCP 操作面
- 发布闸门、错误分类、审计、fixture 回归测试

P1 不提供：完整 Web 管理 UI、全文搜索、多用户、Reader API 已读同步、第三方插件市场、翻译/TTS。

### 已完成

- 可复现开发环境：Nix Flake + direnv（`pkgs.mkShell`），默认 shell 与 `.#crawler` profile
- `justfile` 命令入口与本地质量门禁（`just ci`）
- 四层包结构（`src/core` / `src/adapters` / `src/plugins` / `src/app`）与 import-linter 分层契约
- core 领域层：`ErrorClass` 分类法与优先级、Item 发布状态机、adaptive 调度纯函数、normalize 与去重规则
- P1 数据库 schema 与 Alembic migration（0001 七张表，含 enum CHECK 约束）
- RSS/Atom 采集：HTTP 抓取（超时/大小上限/ETag 条件请求）+ 五套 fixture 回归
- RSS 2.0 输出与发布闸门（`pending` 不可见）；FastAPI 应用，`just run` 可启动
- **Reader API（FreshRSS/Google Reader 兼容）**：NNW 可用 FreshRSS 账号登录，已读/星标多端同步（原定 P3，提前到 P1，见 `docs/adr/0004`）

### 尚未接线

### 尚未接线

以下 `just` 命令是显式占位，运行会说明缺少什么而不是报“无此命令”：

- `just contract`：OpenAPI diff 与 MCP tool snapshot 未接线
- `just e2e`：需要 `compose.yaml` 与 P1 服务镜像

MCP 工具面、网页采集器、OPML、`discover_feed` 与调度器接线均待实现，见
`architecture-spec.md` §5 的 P1 验收标准。

## 快速开始

### 前置条件

- 已安装 [Nix](https://nixos.org/download/)，并启用 `experimental-features = nix-command flakes`
- 已安装 [direnv](https://direnv.net/)，并启用 shell hook
- Docker（仅运行 `just e2e` 或 P3 服务时需要）

### 首次启动

```bash
git clone <repository-url>
cd <repository-name>
direnv allow
just sync
just migrate
```

验证环境：`just ci`（或在 CI 之外复现 CI 的完整命令：`just ci-nix`）。

开发环境由 Nix Flake 提供（`pkgs.mkShell`，见 ADR 0002）；Python 项目依赖由 uv 安装。不要在项目目录外创建或提交 `.venv`。

> `just run` 尚未接线：FastAPI 应用还没创建。当前可验证的是 `just sync` → `just migrate` → `just ci`。

### 最小验收

1. 用 Swagger UI 或 MCP 调用 `discover_feed(url)`，获取网站的候选 RSS/Atom 地址
2. 调用 `add_source` 添加一个候选 feed
3. 调用 `trigger_job` 立即抓取
4. 打开对应的 RSS 输出地址，在 NetNewsWire 中订阅
5. 用 `list_jobs`、`search_items` 或 Swagger UI 检查结果

## 常用命令

所有开发、测试和维护命令都应通过 `justfile` 运行：

```text
just shell            # 打印环境解析到的工具链版本
just sync             # uv sync
just run              # 启动 FastAPI + MCP（尚未接线）
just test             # 单元测试与 fixture 回归测试
just test-integration # SQLite / 临时服务集成测试
just lint             # ruff、mypy、import-linter
just fix              # 自动修复可修复的 lint 与格式问题
just contract         # OpenAPI diff 与 MCP 工具 snapshot（尚未接线）
just migrate          # alembic upgrade head
just migrate-check    # 验证 upgrade / downgrade / upgrade 往返
just revision "msg"   # 生成新 migration
just e2e              # Docker Compose 全链路测试（尚未接线）
just ci               # 本地完整质量门禁
just ci-nix           # CI 实际执行的命令，含 Nix 环境入口
```

提交前必须运行：

```bash
nix develop --command just ci
```

## 长期运行（systemd 用户服务）

无需 root。先启用 linger，否则登出后服务会停：

```bash
just linger          # loginctl enable-linger，开机自启 + 存活于登出
just install-unit    # 安装并启动 ~/.config/systemd/user/pearss.service
just service-status  # 状态 + 最近 30 行日志
just service-restart # 改完 .env 或代码后重启
just uninstall-unit  # 停用并移除
```

监听地址与端口写在 `.env`，unit 本身不含这些细节：

```bash
PEARSS_BIND_HOST=0.0.0.0   # 127.0.0.1 仅本机；0.0.0.0 同时开放局域网与 Tailscale
PEARSS_PORT=8000
```

服务跑在 `nix develop` 里，与 CI 用同一套解释器与工具链；崩溃自动重启，
但有次数上限（超出则放弃而不是无限刷日志）。单元文件见 `deploy/pearss.service`。

## Core API 鉴权

`/v1` 与 `/internal/*` 使用独立的 bearer token，与 Reader API 凭据互不相通：

```bash
PEARSS_API_TOKEN=<随机>        # 读 + 写
PEARSS_API_READ_TOKEN=<随机>   # 可选：仅读
```

生成：`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`

```bash
curl -H "Authorization: Bearer $PEARSS_API_TOKEN" http://127.0.0.1:8000/v1/items
```

行为约定：

- **fail-closed**：未配置 `PEARSS_API_TOKEN` 时所有 Core API 请求被拒，启动日志会提示
- **scope 分离**（AGENTS.md）：read-only token 写操作返回 403
- **审计**：写操作与 401/403 拒绍记入 `audit_log`（含 actor 与 correlation id）；
  普通读不记录，因为 AuditLog 是永久保留的事实源
- **关联**：响应回显 `X-Request-ID`；入站值不安全（含 CRLF 等）时换用生成的值

## NetNewsWire 接入

RSS 输出与 Reader API 都已可用，两条路径用途不同：

| 路径 | 地址 | 用途 |
|---|---|---|
| RSS 输出 | `http://<host>:8000/feeds/default.xml` | 内容投递 |
| Reader API | `http://<host>:8000/api/greader.php` | 已读/星标多端同步 |

Reader API 需要设置凭据（写入未提交的 `.env`）：

```bash
PEARSS_READER_USERNAME=reader
PEARSS_READER_PASSWORD=<your-password>
```

然后在 NNW 中添加 **FreshRSS** 类型账号，服务器地址填 `http://<host>:8000/api/greader.php`，
用户名与密码填上面的值。库内只保存 scrypt 哈希，明文密码只存在于环境变量。

> **改密码会让 token 失效**，所有已登录客户端需要重新登录——token 由哈希派生，见 ADR 0005。

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
- 领域规则放在 `src/core/`；框架、网络、数据库、MCP 放在 `src/adapters/`
- 破坏 API、MCP 工具或数据语义的改动必须写 ADR，并明确迁移路径
