# Pearss 环境变量完整清单
#
# 用途：部署到 NixOS（`services.pearss.environmentFile`）或本地 `.env`。
# 格式：KEY=value，每行一条，`#` 开头为注释。systemd 的 EnvironmentFile 也读这个格式。
#
# 生成随机值：openssl rand -base64 32
#
# ⚠️ 这个文件本身含密钥，只应存在于：
#     · sops 加密的 secrets.yaml（NixOS 部署）
#     · 项目根的 .env（gitignored，本地开发）
#   绝不提交、绝不进 Nix store（store 全局可读）。

# ===========================================================================
# 一、部署必需（NixOS 上这四项是必填的）
# ===========================================================================

# Core API（/v1）的 bearer token，read + write + ingest 皆可。
# 未设置时所有 Core API 请求被拒（fail-closed），启动日志会警告。
PEARSS_API_TOKEN=

# 可选：只读 token（scope=read）。持它写操作返回 403。
PEARSS_API_READ_TOKEN=

# 可选：A3 主动接入专用 token（scope=ingest，ADR 0022）。只允许向 agent 型源
# 推送内容，不能删源/改配置/读已读状态。给外部 agent 用它而不是 API_TOKEN：
# 泄漏时的损失面仅限「能往 agent 源塞内容」。
PEARSS_INGEST_TOKEN=

# MCP 工具面的边界 token。独立于 API_TOKEN：撤销一台笔记本的 MCP 客户端
# 不应把其他调用方一起登出。
PEARSS_MCP_TOKEN=

# Reader API 凭据——NetNewsWire 用「FreshRSS 账号」类型连接。
# 库内只存 scrypt 哈希，明文永不落库（ADR 0005）。
# ⚠️ auth token 由 HMAC(username + 密码哈希) 派生：改密码会使所有已登录
#    客户端失效。NNW 没有「改密码」入口，只能删账号重加。
PEARSS_READER_USERNAME=reader
PEARSS_READER_PASSWORD=

# 0 = 关闭 Swagger/redoc/openapi.json。
# 公网或 tailnet 可达时建议设 0：否则完整 API schema 对任何可达者披露。
PEARSS_DOCS_ENABLED=0


# ===========================================================================
# 二、NixOS module 已设置（勿在此重复，否则行为不一致）
# ===========================================================================
#
# PEARSS_BIND_HOST=127.0.0.1        # module 固定为 loopback，对外走隧道
# PEARSS_PORT=8000
# PEARSS_DATABASE_URL=sqlite+pysqlite:////var/lib/pearss/pearss.db
# PEARSS_STORAGE_DIR=/var/lib/pearss/bodies
# PEARSS_PLUGIN_DIR=/var/lib/pearss/plugins   # 默认值；放状态目录旁，一次备份覆盖
# PEARSS_FORWARDED_ALLOW_IPS=127.0.0.1


# ===========================================================================
# 三、运行行为（都有默认值，按需覆盖）
# ===========================================================================

# 日志级别：DEBUG / INFO / WARNING / ERROR。默认 INFO。
# PEARSS_LOG_LEVEL=INFO

# 轮询间隔（秒）。到期与否每轮从 schedule_policy + 历史推导，不落库（ADR 0009）。
# PEARSS_TICK_SECONDS=60

# 一轮 tick 内并发采集的源数。默认 3。
# PEARSS_TICK_CONCURRENCY=3

# enrichment worker（翻译等处理链）的并发数。默认 3。
# PEARSS_ENRICHMENT_CONCURRENCY=3

# 0 = 只跑 API 不跑调度（不采集、不清理审计、不跑处理链）。
# 容器资源紧张或只想对外提供 Reader API 时可设 0。
# PEARSS_SCHEDULER_ENABLED=1

# 审计行保留天数，0 = 永久。默认 30。
# 公网部署下每个未授权探测都写一行审计，取证窗口即此值。
# PEARSS_AUDIT_RETENTION_DAYS=30

# 第三方插件目录（ADR 0023）。其下每个子目录名即 collector_id，须含
# manifest.json 与 collector.py（模块名固定为 collector）；插件自带依赖放
# 同目录的 .venv/，Pearss 不解析插件依赖。装载走
# POST /v1/plugins/{id}:import，它在子进程里 import 插件并校验声明与实现一致。
#
# ⚠️ 装载器**不判断插件是否安全**：子进程隔离（env allowlist + 宿主代理出网 +
#    资源限额）限制的是故障半径，不是恶意行为——信任决定属于运行者。
#    若插件声明了 PEARSS_COOKIECLOUD_ 前缀，它会拿到 CookieCloud 服务端主密钥，
#    而那把密钥能解密你同步过的全部站点会话（见 ADR 0012）。
# 本变量是 host-side：绝不下发给子进程，插件无权知道其他插件在哪。
# PEARSS_PLUGIN_DIR=/var/lib/pearss/plugins


# ===========================================================================
# 四、公网暴露与限速
# ===========================================================================

# 401/403 失败预算：窗口内失败超过 MAX 次，该客户端地址被阻断 BLOCK 秒。
# 按客户端地址计数，地址取自 X-Forwarded-For（隧道/代理需正确传递该头）。
# 0 = 关闭限速。
# PEARSS_AUTH_RATELIMIT_MAX=50
# PEARSS_AUTH_RATELIMIT_WINDOW_SECONDS=300
# PEARSS_AUTH_RATELIMIT_BLOCK_SECONDS=900

# 请求体上限（字节），超限 413。0 = 不限。默认 1MB。
# PEARSS_MAX_BODY_BYTES=1048576

# 半开请求头超时（秒）。连接建立后在该时长内未收到完整请求头即关闭；
# 每个收到的字节重置窗口（针对「无进展」而非「总时长」）。
# 默认 15，上限 300；0/负数/nan/inf 等坏值回落到默认，无法关闭。
# 经 `uvicorn --http app.http_timeout:HeaderTimeoutProtocol` 安装。
# PEARSS_HTTP_HEADER_TIMEOUT_SECONDS=15

# 远程 MCP 客户端需在此放宽 DNS-rebinding 保护，逗号分隔。
# loopback 始终放行；端口必须用 host:* 通配（浏览器会带端口）。
# PEARSS_MCP_ALLOWED_HOSTS=pearss.example.com:*

# ⚠️ 1 = 允许采集解析到私网地址的源。默认 0。
# 打开它会同时**关掉 SSRF 守卫**（ADR 0016）——云元数据、RFC1918、loopback
# 都会变成可抓取目标。仅在你确实订阅内网源时开启，并明白这个代价。
# PEARSS_ALLOW_PRIVATE_FETCH=0
# （沙箱子进程同样继承此开关；早期版本会在子进程里被静默重置为「拒绝」，
#   导致本地/内网源的采集全部报 blocked，而父进程以为已放行。）


# ===========================================================================
# 五、备份与恢复（NixOS 上用 systemd timer，见 deploy/）
# ===========================================================================

# 备份归档目录（SQLite 在线快照 + 文件存储整树的单个 tar.gz）。
# NixOS 上建议放到状态目录之外并单独挂卷。
# PEARSS_BACKUP_DIR=/var/lib/pearss/backups

# 以下两项由 CLI 从命令行参数设置，不要写进 env 文件：
# PEARSS_RESTORE_ARCHIVE / PEARSS_RESTORE_TARGET
# PEARSS_BACKFILL_SOURCE_ID / PEARSS_BACKFILL_LIMIT


# ===========================================================================
# 六、翻译处理器（按源配置 pipeline_chain 后生效）
# ===========================================================================

# 任何 OpenAI-compatible chat completions 端点。
# ⚠️ 填到 /v1 为止，**不要**自带 /chat/completions——代码会自行拼接。
# PEARSS_TRANSLATE_BASE_URL=https://api.openai.com/v1
# PEARSS_TRANSLATE_API_KEY=
# 主模型。**只放一个模型名**，与 MODELS 二选一，后者优先。
# PEARSS_TRANSLATE_MODEL=gpt-4o-mini
# 备选链，逗号分隔。主模型遇到 429/5xx/不可达时依次降级。
# PEARSS_TRANSLATE_MODELS=gpt-4o-mini,gpt-4o,deepseek-chat
# ⚠️ 把逗号分隔的多模型写进**单数**变量会让 provider 直接 400
#    （`unknown provider for model "a,b"`）——它被当成了一个模型名。
#    实测踩过：表现为「翻译全部失败」。400 现已归类为 `parse`（不可重试），
#    所以它会立刻显形，而不是伪装成网络抖动重试三次。


# ===========================================================================
# 七、采集器凭据（按需；未配置的采集器报 auth 而非崩溃）
# ===========================================================================

# --- 知乎 ---
# PEARSS_ZHIHU_COOKIES=          # 整条 Cookie 串；也可经 CookieCloud
# PEARSS_ZHIHU_USER_AGENT=       # 可选，默认内置一个

# --- 知识星球（cookie 与 access token 二选一）---
# PEARSS_ZSXQ_COOKIE=
# PEARSS_ZSXQ_ACCESS_TOKEN=
# PEARSS_ZSXQ_USER_AGENT=

# --- X / Twitter（静态值优先于 CookieCloud）---
# PEARSS_X_AUTH_TOKEN=           # 与 CT0 必须成对
# PEARSS_X_CT0=
# PEARSS_X_QUERY_ID=             # 可选：X 轮换 queryId 时覆盖代码内默认值
# PEARSS_X_COUNT=20              # 每页推文数，1–40，默认 20
# 首轮抓取页数，默认 3。它定义该源的**历史深度**：首轮走到哪，floor 就定在哪，
# 此后每轮只在 floor 之上补新。后续轮不受它约束（否则会漏）。
# PEARSS_X_FIRST_PAGES=3
# PEARSS_X_USER_AGENT=

# --- CookieCloud（X / 知乎 / 知识星球共用一套凭据分发）---
# 三者齐备才启用；按 domain 过滤，一个部署服务多个采集器。
# PEARSS_COOKIECLOUD_URL=
# PEARSS_COOKIECLOUD_UUID=
# PEARSS_COOKIECLOUD_TOKEN=
# PEARSS_COOKIECLOUD_CACHE_SECONDS=300


# ===========================================================================
# 八、绝不要在此设置
# ===========================================================================

# PEARSS_SANDBOXED    沙箱子进程的运行时标记，由 env allowlist 自行注入。
# PEARSS_TEST_DNS     测试专用。设了会让所有 fetch 走假解析，等于关掉 SSRF 守卫。
# PEARSS_ENV          devShell 设置（development），不是运行时配置。
