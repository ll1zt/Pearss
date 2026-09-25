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
- 发布闸门（`pending` 对一切输出不可见）；FastAPI 应用，`just run` 可启动
- **Reader API（FreshRSS/Google Reader 兼容）**：NNW 可用 FreshRSS 账号登录，已读/星标多端同步（原定 P3，提前到 P1，见 ADR 0004）
- **Core API `/v1`**：`/v1/sources` 增删改查 + `:trigger` 立即采集；`/v1/items` 紧凑查询（keyset 分页、字段选择、sort 白名单）+ `/v1/items/{id}/body` 独立取正文；写操作强制 `Idempotency-Key`，统一错误 envelope，审计中间件与 scope 分离的 token 鉴权
- **调度器**：每轮从 `schedule_policy` + 历史推导到期集合（不存 next_run_time），fixed/adaptive 两种策略、失败指数退避、并发 claim 保护；随 systemd 服务常驻，`PEARSS_SCHEDULER_ENABLED=0` 可关
- **采集器（5 个）**：`rss`（RSS 2.0 + Atom，**进程内**，含 WordPress 摘要 feed 的 REST 全文升级）、`telegram`（公开频道预览页）、`twitter`（X Following 时间线，**登录型**，见 ADR 0012）、`zhihu`（知乎用户动态/回答/文章，**登录型**，Cookie 或 CookieCloud，见 ADR 0014）、`zsxq`（知识星球群组，**登录型**，见 ADR 0015）。后四个为第三方插件，**各运行于独立子进程沙箱**（ADR 0017）：崩溃与内存失控不伤宿主，凭据经 env allowlist 注入，出网经宿主代理（SSRF 拦截生效）。按 URL 选，但**声明类型优先**于 URL 猜测
- **插件生命周期 API**：`GET /v1/plugins`、`POST /v1/plugins/{id}:enable|:disable`（幂等写、全程审计）；manifest 经启动双向校验
- **第三方插件装载器（ADR 0023）**：`:import` / `:uninstall` / `:reload` 已实现。插件放在 `PEARSS_PLUGIN_DIR`（默认 `/var/lib/pearss/plugins`）下的一个目录里，目录名即 collector_id，需含 `manifest.json` + `collector.py`（模块名固定为 `collector`），依赖自带 `.venv`。装载在**子进程**里 import 并校验「manifest 声明 ↔ 实际 hookimpl」双向一致，不一致即拒绝且不留痕。**装载器不判断插件是否安全**：子进程隔离限制的是故障半径，信任决定属于运行者
- **MCP 控制平面**：22 个工具（10 读 + 12 写）经 Core API 调用（审计天然覆盖），写操作走 elicitation 确认且校验确认参数==执行参数；**每个工具声明 `ToolAnnotations`**（读/破坏性/开放世界），写操作成功返回带 `audit_id`；正文优先走 `item://{item_id}` Resource，不支持 Resource 的客户端可用 `get_item_body`（带截断，ADR 0013）；工具面 snapshot 由 `just contract` 校验
- **systemd 用户服务**：`just install-unit`，开机自启 + 崩溃重启

### 验证与测试

以下命令均已接线，没有占位：

- `just contract`：MCP 工具面 snapshot + OpenAPI 全文档 snapshot 双校验（OpenAPI 用 snapshot 手法替代 nixpkgs 缺失的 oasdiff）
- `just test-e2e`：单进程全链路（真 uvicorn + MCP 客户端 + 本地 fixture origin）；Compose 多容器拓扑暂不需要

其余待实现项：OPML（A6）、`discover_feed`（A5，已暂缓）、`create_feed`/FeedOut CRUD、
**备份与恢复演练**（§7.5）、**条件请求持久化回路**（`record_raw_fetch` 生产路径
未接线——ETag/Last-Modified 未写回，304 路径对所有采集器不可达，见 ADR 0017
已知缺口）。见 `architecture-spec.md` §5/§6。

## 快速开始

### 前置条件

- 已安装 [Nix](https://nixos.org/download/)，并启用 `experimental-features = nix-command flakes`
- 已安装 [direnv](https://direnv.net/)，并启用 shell hook
- Docker（仅 P3 的多服务容器化拓扑需要；当前 `just test-e2e` 不需要）

### 首次启动

```bash
git clone <repository-url>
cd <repository-name>
direnv allow
just sync
just migrate
cp .env.example .env && chmod 600 .env   # 然后填 PEARSS_API_TOKEN 等
```

验证环境：`just ci`（或在 CI 之外复现 CI 的完整命令：`just ci-nix`）。

`.env.example` 是全部 `PEARSS_*` 变量的完整清单，每项标注默认值与影响，并由
`tests/unit/test_structure_guards.py` 强制与源码同步：代码读了但模板漏掉的变量会让
测试失败，模板里没人读的变量同样失败。

开发环境由 Nix Flake 提供（`pkgs.mkShell`，见 ADR 0002）；Python 项目依赖由 uv 安装。不要在项目目录外创建或提交 `.venv`。

启动服务（需先在 `.env` 里配置 `PEARSS_API_TOKEN`，否则 Core API 会 fail-closed 拒绝所有请求）：

```bash
just run                      # 仅本机 127.0.0.1:8000
just run 100.64.0.1 8000  # 绑到某个地址，例如 Tailscale
```

### 最小验收

`architecture-spec.md` §5 的 P1 验收标准是「agent 完成发现→加源→抓取→出 feed」。
**「发现」环节（A5 `discover_feed`）已决定暂缓**，故当前可走通的是后半段：

1. `POST /v1/sources`（或 MCP `add_source`⚠）添加一个 feed
2. `POST /v1/sources/{id}:trigger`（或 MCP `trigger_job`⚠）立即抓取；此后由调度器自动续抓
3. 用 NNW 的 FreshRSS 账号连 `/api/greader.php`（公开 RSS 输出已移除，见 ADR 0016）
4. `GET /v1/items` / MCP `search_items` 查条目；正文走 `item://{id}` Resource
5. `GET /v1/jobs/{id}/runs` / `GET /v1/system/snapshot` 检查采集健康

OPML 迁移（A6）尚未实现。

## 常用命令

所有开发、测试和维护命令都应通过 `justfile` 运行：

```text
just shell            # 打印环境解析到的工具链版本
just sync             # uv sync
just run [host port]  # 启动 API + Reader + MCP + 调度器（默认 127.0.0.1:8000）
just serve            # 同上，但地址与端口取自 .env（systemd 单元用的就是它）
just test             # 单元测试与 fixture 回归测试
just test-integration # SQLite / 临时服务集成测试
just lint             # ruff、mypy、import-linter
just fix              # 自动修复可修复的 lint 与格式问题
just contract         # MCP 工具面 + OpenAPI 双 snapshot
just migrate          # alembic upgrade head
just migrate-check    # 验证 upgrade / downgrade / upgrade 往返
just revision "msg"   # 生成新 migration
just test-e2e         # 单进程全链路
just mutate           # core 变异测试（mutmut）
just ci               # 本地完整质量门禁
just ci-nix           # CI 实际执行的命令，含 Nix 环境入口

# 长期运行（systemd 用户服务，无需 root）
just linger           # 开机自启 + 存活于登出
just install-unit     # 安装并启用 deploy/pearss.service
just service-status   # 状态 + 最近日志
just service-restart  # 改完 .env 或代码后
just uninstall-unit   # 停用并移除
```

提交前必须运行：

```bash
nix develop --command just ci
```

## 部署到 NixOS 服务器

本仓库的 flake 同时提供可部署的包与 NixOS module：

```nix
# flake.nix inputs
pearss.url = "git+https://github.com/lllzt/Pearss";
pearss.inputs.nixpkgs.follows = "nixpkgs";

# 主机配置
imports = [ inputs.pearss.nixosModules.default ];
services.pearss = {
  enable = true;
  environmentFile = "/run/secrets/pearss-env";   # 密钥不进 store
};
```

隧道双入口（tailnet 整应用 + 公网仅 Reader API）见 `nix/tunnel.nix`。
完整步骤、UID 约束与已实测证据见 `nix/DEPLOY.md`；
部署后验证清单见 `docs/operations/tunnel-drill.md`。

## 长期运行（systemd 用户服务，开发机）

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
PEARSS_BIND_HOST=127.0.0.1  # 隧道部署（ADR 0016）只绑本机，由隧道代理对外
PEARSS_PORT=8000
```

## 公网暴露（隧道部署，ADR 0016）

公开 RSS 输出已移除：公网只暴露已鉴权的 Reader API（`/api/greader.php`），
RSS 客户端一律用 NNW 的 FreshRSS 账号连接。`/v1` 与 `/mcp` 默认不暴露，
是否把 agent 面放公网由隧道路由规则自行决定（有 token 与限速保护）。

**Cloudflare Tunnel**（推荐，按路径只路由 Reader API）：

```ini
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: pearss.example.com
    path: ^/api/greader\.php
    service: http://127.0.0.1:8000
  - service: http_status:404
```

**Tailscale Funnel** 注意：Funnel 按主机名整源暴露，不支持路径路由——
必须同时设置 `PEARSS_DOCS_ENABLED=0` 关闭 Swagger/openapi.json，
否则完整 API schema 会对公网披露。

应用侧已内置（无需反代配置）：401/403 失败按地址限速（默认 50 次/5 分钟，
超限 429 十五分钟）、请求体上限 1MB（超限 413）、全响应 `nosniff`、
审计行 30 天滚动保留。`just serve` 已带 `--proxy-headers`，限速按
X-Forwarded-For 的真实客户端地址计数。相关开关：

```bash
PEARSS_DOCS_ENABLED=0              # 关闭 Swagger/redoc/openapi.json
PEARSS_AUDIT_RETENTION_DAYS=30     # 审计保留天数，0 = 永久
PEARSS_MAX_BODY_BYTES=1048576      # 请求体上限，0 = 不限
PEARSS_AUTH_RATELIMIT_MAX=50       # 失败预算，0 = 关闭限速
PEARSS_ALLOW_PRIVATE_FETCH=0       # 1 = 允许采集私网地址的源
```

服务跑在 `nix develop` 里，与 CI 用同一套解释器与工具链；崩溃自动重启，
但有次数上限（超出则放弃而不是无限刷日志）。单元文件见 `deploy/pearss.service`。

## Core API 鉴权

`/v1` 使用 bearer token，与 Reader API 凭据互不相通：

```bash
PEARSS_API_TOKEN=<随机>        # 读 + 写 + ingest
PEARSS_API_READ_TOKEN=<随机>   # 可选：仅读
PEARSS_INGEST_TOKEN=<随机>     # 可选：仅可向 agent 型源推送内容（ADR 0022）
```

生成：`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`

```bash
curl -H "Authorization: Bearer $PEARSS_API_TOKEN" http://127.0.0.1:8000/v1/items
```

行为约定：

- **fail-closed**：未配置 `PEARSS_API_TOKEN` 时所有 Core API 请求被拒，启动日志会提示
- **scope 分离**（AGENTS.md）：read-only token 写操作返回 403；ingest token 只能
  向 agent 型源推送，不能删源或改配置
- **审计**：写操作与 401/403 拒绝记入 `audit_log`（含 actor 与 correlation id）；
  普通读不记录，因为 AuditLog 是永久保留的事实源
- **关联**：响应回显 `X-Request-ID`；入站值不安全（含 CRLF 等）时换用生成的值

## 添加订阅源

```bash
TOKEN=$(grep '^PEARSS_API_TOKEN=' .env | cut -d= -f2)

# 1. 建源（写操作必须带 Idempotency-Key）
curl -sS -X POST http://127.0.0.1:8000/v1/sources \
  -H "Authorization: Bearer $TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://planetpython.org/rss20.xml",
    "schedule_policy": {"type": "adaptive", "min": "15m", "max": "24h"}
  }'

# 2. 立即采集一次（返回入库条数）
curl -sS -X POST "http://127.0.0.1:8000/v1/sources/<id>:trigger" \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)"

# 3. 看列表
curl -sS "http://127.0.0.1:8000/v1/sources?limit=20" -H "Authorization: Bearer $TOKEN"
```

行为要点：

- **重试安全**：同一个 `Idempotency-Key` 重发不会建第二个源，响应会带 `Idempotent-Replay: true`
- **URL 去重**：`canonical_url` 剔掉追踪参数后作为唯一键，同一 feed 带不同 `?utm_*` 不会重复入库
- **`schedule_policy` 严格校验**：未知键会被拒（`interwal` 这种拼错不会静默变成无间隔）
- **删除是软删**：已发布条目的 GUID 冻结且客户端已缓存，不能随源一起消失
- **WordPress 摘要 feed 自动升级全文**：源站把 feed 配成摘要输出时（`more-link`
  截断特征），采集器自动改从该站点的 REST API 取完整正文（`content.rendered`），
  条目身份不变、不会产生重复；REST 不可用时静默降级回摘要。见
  `docs/guides/collector-guide.md` 的 WordPress 小节

## MCP 控制平面

MCP server 挂在同一应用的 `/mcp`（Streamable HTTP）。工具：

| 工具 | 需确认 | 作用 |
|---|---|---|
| `list_sources` | | 列出来源 |
| `search_items` | | 搜索条目（**不含正文**） |
| `get_system_snapshot` | | 状态快照：错误分布、pending 积压、近期成功率 |
| `list_jobs` / `list_runs` | | 任务与执行历史 |
| `get_item_body` | | 取单条正文（截断，默认 4000、上限 20000 字符） |
| `add_source` | ⚠ | 加源 |
| `update_schedule_policy` | ⚠ | 改调度策略 |
| `set_source_status` | ⚠ | 暂停 / 恢复 |
| `set_source_folder` | ⚠ | 归类到侧边栏文件夹（一订阅一分类） |
| `remove_source` | ⚠ | 软删除来源 |
| `trigger_job` | ⚠ | 立即抓取 |
| `list_enrichments` | | 某源的处理器链产物状态 |
| `retry_enrichments` | ⚠ | 重排失败的产物（会再次消耗 provider 额度） |
| `backfill_source` | ⚠ | 为启用链之前的存量条目补排队 |

**取正文**：优先用 Resource `item://{item_id}` —— 部分客户端把它当附件，不占模型上下文。
客户端不支持 Resource 时用 `get_item_body`（ADR 0013），它带截断并自述 `truncated` /
`total_chars`。

> 「Resource 不进上下文」是常见误解：`resources/read` 的返回同样进上下文，MCP 没有
> “读取但不物化”的语义。规则的真实依据是**不得批量返回正文** —— 列表工具一次带 N 篇
> 正文才是必然的上下文爆炸。

**过滤在服务器端做**：`search_items` 的 `fields` 是最省的单一杠杆。实测（真实库 4677 条）：

```text
search_items(limit=100)                        32,434 字符
search_items(limit=100, fields=id)              4,323 字符   (-87%)
search_items(limit=5, fields=id,title,
             source_id=…, q=…)                      66 字符
```

### 鉴权

MCP 端点使用**独立的** `PEARSS_MCP_TOKEN`，与 Core API token 分开（撤销客户端不该连带登出 API）：

```bash
PEARSS_MCP_TOKEN=<随机>
# 客户端要连非回环地址时，还需显式放行 Host（SDK 的 DNS-rebinding 防护）
PEARSS_MCP_ALLOWED_HOSTS=127.0.0.1:*,100.64.0.1:*,192.168.1.10:*
```

- **fail-closed**：未配置 `PEARSS_MCP_TOKEN` 时端点返回 503 并拒绝一切，启动记 warning
- 不设 `PEARSS_MCP_ALLOWED_HOSTS` 则**只有回环可达**（SDK 默认），远程访问需要两个显式选择

写操作由 **server 侧 elicitation** 确认；客户端不支持 elicitation 时**拒绝执行**而非照做。确认表单预填待执行的值，且执行前校验确认值与实际参数一致。

## NetNewsWire 接入

Reader API 是唯一的内容消费路径（公开 RSS 输出已移除，ADR 0016）：

| 路径 | 地址 | 用途 |
|---|---|---|
| Reader API | `http://<host>:8000/api/greader.php` | 内容消费 + 已读/星标多端同步（公开 RSS 输出已移除，ADR 0016） |

Reader API 需要设置凭据（写入未提交的 `.env`）：

```bash
PEARSS_READER_USERNAME=reader
PEARSS_READER_PASSWORD=<your-password>
```

然后在 NNW 中添加 **FreshRSS** 类型账号，服务器地址填 `http://<host>:8000/api/greader.php`，
用户名与密码填上面的值。库内只保存 scrypt 哈希，明文密码只存在于环境变量。

> **改密码会让 token 失效**，所有已登录客户端需要重新登录——token 由哈希派生，见 ADR 0005。

同一地址也适用于 Android 的 **Read You**（FreshRSS 账号类型）。它在 NNW 的只读能力
之上还能在 App 内加订阅、建/改/删分组、改名订阅、退订（真删，与 `DELETE /v1/sources`
同语义）。唯一已知缺口：新建空分组刷新后消失（分组是派生值，无实体）。见 ADR 0024。

## X（Twitter）源：登录型采集

X 源读取**你自己的** Following 时间线，需要会话 cookie（ADR 0012 的条件与风险）。两种配法：

```bash
# 1) 静态（简单，但重登后失效，需重启）
PEARSS_X_AUTH_TOKEN=<你的 auth_token cookie>
PEARSS_X_CT0=<你的 ct0 cookie>

# 2) CookieCloud 自动续期（推荐）
PEARSS_COOKIECLOUD_URL=http://127.0.0.1:4000
PEARSS_COOKIECLOUD_UUID=<你的同步 uuid>
PEARSS_COOKIECLOUD_TOKEN=<COOKIE_CLOUD_SERVER_PASSWORD 的值>
```

解析顺序：**静态优先**于 CookieCloud（显式钉住值就是明确要求用那个值）。X 返回 401 说明
会话已轮换，此时绕过缓存重取一次并重试该页（只一次）。

> ⚠️ `COOKIE_CLOUD_SERVER_PASSWORD` 是**服务端主密钥**，能导出任意 uuid、任意域名的
> cookie —— 持有它等于持有你同步过的全部站点会话（含银行、邮箱）。用**只含阅读器所需
> 站点的专用 uuid**，别指向整个浏览器档案。详见 ADR 0012。

## 知乎与知识星球源：登录型采集

两个源都走 fetch 钩子自取 API 页（ADR 0014/0015），加源时 `type` 填 `web`。
凭据写入 `.env`，与 X 共用同一 CookieCloud 部署（按 domain 过滤）：

```bash
# 知乎：Cookie 须含 d_c0（x-zse-96 签名绑定该值，本地计算）
PEARSS_ZHIHU_COOKIES="d_c0=…; z_c0=…"
# 或走 CookieCloud（domain=zhihu.com）

# 知识星球：裸 token 即可
PEARSS_ZSXQ_ACCESS_TOKEN="…"
# 或完整 Cookie：PEARSS_ZSXQ_COOKIE="zsxq_access_token=…; …"
```

源 URL 与路由：

```text
https://www.zhihu.com/people/<url_token>           # 动态（裸 URL 的默认路由）
https://www.zhihu.com/people/<url_token>/answers   # 回答
https://www.zhihu.com/people/<url_token>/posts     # 文章（org/<name>/posts 为机构）
https://wx.zsxq.com/dweb2/index/group/<group_id>                       # 最新
https://wx.zsxq.com/dweb2/index/group/<group_id>?scope=digests         # 精华等栏目
```

行为要点：

- **知乎**动态会丢弃「关注了…」「提出了问题」类噪音，其余条目打动作标签
  （[赞同] [创作] [收藏]）；401 时若走 CookieCloud 自动刷新重试一次，403 是
  `blocked` 不重试。
- **知识星球**的裸 URL 就是 `scope=all` 的规范形态——不要用 `?scope=all`
  显式订阅，那会给同一个 feed 建第二个身份；业务码 1059（风控）按上游惯例
  退避重试 3 次，耗尽即 `blocked`。
- 两者凭据只存在于 `.env` 与内存；会话失效表现为 `auth` 分类的失败 run，
  `get_system_snapshot` 可见。

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
| `docs/api/mcp-tool-guide.md` | MCP Tool/Resource 语义与安全规范 |
| `docs/domain/data-model.md` | 领域实体、关系、保留与备份策略 |
| `docs/domain/state-machines.md` | Item、JobRun、Plugin 等状态机 |
| `docs/domain/error-taxonomy.md` | 错误分类、错误 envelope 与诊断语义 |
| `docs/guides/collector-guide.md` | RSS/网页采集器与 fixture 编写指南 |
| `docs/guides/testing.md` | 测试层次、fixture、契约与 e2e 规则 |
| `docs/operations/runbook.md` | 故障处理与恢复操作手册 |
| `docs/integrations/netnewswire.md` | NetNewsWire 集成与缓存约束 |
| `docs/adr/` | 架构决策记录（0000 为索引） |
| `deploy/pearss.service` | systemd 用户服务单元 |

## 贡献原则

- 先读 `AGENTS.md`，再改代码
- 新增或修改行为时，优先添加可验证的测试和 fixture
- 领域规则放在 `src/core/`；框架、网络、数据库、MCP 放在 `src/adapters/`
- 破坏 API、MCP 工具或数据语义的改动必须写 ADR，并明确迁移路径
