# 运维与故障恢复手册

本手册按症状提供安全、可重复的运行操作。所有直接 API 操作仍需鉴权和审计；写操作若从 MCP 发起，必须遵守用户确认规则。

## 基本原则

1. 先收集 `correlation_id`、JobRun、结构化日志和影响范围，再修改配置
2. 不要通过修改已发布 Item 内容修复输出问题；客户端可能已经缓存
3. 不要把 token、Cookie、认证 Header、完整不可信网页正文复制进工单、日志或 Git
4. 变更前先备份事实源数据；变更后运行最小验证
5. 不确定时优先暂停特定 Source/Plugin，避免扩大错误输出

## 服务形态与观察

当前由 **systemd 用户服务**托管（`deploy/pearss.service` + linger），不是容器：

```bash
just service-status     # 状态 + 最近 30 行日志
just service-restart    # 改完 .env 或代码后
journalctl --user -u pearss.service -f
```

三个进程内组件共用一个进程：Core API、Reader API、MCP 工具面，外加 asyncio 调度任务。
`PEARSS_SCHEDULER_ENABLED=0` 可只跑 API 不跑调度；`PEARSS_TICK_SECONDS` 控制轮询间隔。

## 调度器

**到期时间不落库**，每轮从 `source.schedule_policy` + `job_run`/`item` 历史推导（ADR 0009）。
所以「为什么没抓」要按推导的输入去查，而不是找一张 next_run 表：

| 症状 | 查什么 | 说明 |
|---|---|---|
| 某源一直不抓 | `select status from source` | 只有 `active` 会被抓；paused/disabled 是运维的显式决定 |
| 失败后间隔变长 | `job_run` 最近状态 | 连续失败按 2^n 退避，上限 24h；**这是预期行为**，不是卡住 |
| adaptive 抓得太稀 | `item.published_at` 间隔 | 更新节奏按**条目发布时间**算，不是按抓取时间 |
| 重启后大批 `timed_out` | 启动日志 | 上次进程被杀时留下的 `running` 行被回收，属正常清理 |
| 同一源被并发抓 | 不应发生 | claim 是条件更新（`WHERE status='queued'`），恰好一个赢 |

**手动触发**：`POST /v1/sources/{id}:trigger`（或 MCP `trigger_job`）。它走与调度器同一条
`collect_now`，因此**同样写 JobRun** —— 手动触发不可见曾是真实 bug。

## X（Twitter）源：登录型采集

X 源是**登录型**（ADR 0012），需要会话 cookie，且是**分页型**采集：每轮走多个页面直到
前端（seen frontier）或预算耗尽，游标状态存在 `source` 上（`seen_frontier` /
`resume_cursor` / `resume_rounds`）。

| 症状 | 查什么 | 说明 |
|---|---|---|
| `error_class=auth` | `PEARSS_X_*` 或 CookieCloud 配置 | 凭据缺失/失效**不会**匿名重试（ADR 0012）—— 若看到 `auth` 但确信配好了，查 CookieCloud 是否返回了 `auth_token`/`ct0` |
| 每次采到很深的历史 | `source.resume_rounds` | 首次采集无 frontier，20 页预算会走很深（实测一轮 ~1500 条）。护栏在连续 3 轮仍有 resume 时丢弃游标，之后只取新推文 |
| CookieCloud 连不上 | 服务日志 `CookieCloud unavailable` | 静态 `PEARSS_X_*` 优先；若两者都无 → `auth`。服务端不可达与“没配置”是**不同处置** |
| X 返回 401 | 服务日志 | 会话已轮换；会绕过缓存重取一次并重试该页（只一次）。若持续 401，说明 CookieCloud 里的仍未更新 —— 需在浏览器重新同步 |
| `structure_changed` | 响应结构 | X 的 queryId 与结构由 X 单方变更；queryId 会自动从 x.com 的 JS bundle 恢复，失败则 human 介入（见采集器 README） |

**手动续期**：CookieCloud 缓存 TTL 默认 300s（`PEARSS_COOKIECLOUD_CACHE_SECONDS`）。
浏览器重登后最多等一个 TTL，或重启服务立即生效。

> ⚠️ `COOKIE_CLOUD_SERVER_PASSWORD` 是**服务端主密钥**，能导出任意 uuid、任意域名的 cookie。
> 它比它取回的那一个会话值强得多 —— 持有它等于持有同步过的全部站点会话（含银行、邮箱）。

## MCP 工具面

- 端点 `/mcp/mcp`（Streamable HTTP），与 API 同进程
- **需要鉴权**：`PEARSS_MCP_TOKEN`（独立于 Core API token，便于单独撤销）。未配置时端点
  返回 **503** 并拒绝一切 —— 这是 fail-closed，不是“未鉴权”。无凭据请求返回 **401**
- **远程访问需放行 Host**：`PEARSS_MCP_ALLOWED_HOSTS`（如 `100.64.0.1:*`）。不设则
  **只有回环可达**，因为 MCP SDK 默认开 DNS-rebinding 防护，会对未知 Host 返回 **421**
  `Invalid Host header`。

  **排错要点**：`421` 不是鉴权失败，别去找 token；是 Host 不在白名单里。
  另外白名单条目需要 `host:*` 形式（浏览器总带端口），裸 `host` 匹配不上 `host:8000`。
- **写操作需 elicitation**。客户端不支持时**拒绝执行**并返回 `{"declined": true}`，
  不会静默照做。若用户报告「工具说 declined」，是客户端缺 elicitation 能力，不是故障
- 工具经 `/v1` 调用，所以审计与鉴权与任何客户端一致；审计里 actor 是 `api:full`
  （服务持有的 token），**agent 身份靠 correlation_id 关联，不体现在 actor 上**（ADR 0010 记录了这个可归因性弱化）
- 工具面变更会撞 snapshot：`MCP_SNAPSHOT_UPDATE=1 just contract` 后**审查 diff 再提交**，
  不要为了让测试绿而重写

## 日常健康检查

检查以下信号：

- 各 Source 最近成功时间与连续失败次数
- ErrorClass 分布；特别是 `structure_changed` 是否突然增加
- `pending` Item 的数量与最久等待时长
- JobRun 队列深度、超时率、重试率
- 插件崩溃/超时率（P2）
- Reader API 可访问性、最近 GUID 是否稳定
- 备份最近一次成功时间与最近恢复演练日期

可使用 `GET /v1/system/snapshot`、`GET /v1/jobs`、`GET /v1/jobs/{id}/runs`、MCP 的
`get_system_snapshot` / `list_runs`，或 Swagger UI。`diagnose_source` 属 P2，尚未实现。

快照刻意报告**衰减信号**而非一堆计数器：`runs_by_error_class` 里 `structure_changed`
扎堆意味着网站改版；`recent_run_success_rate` 只统计最近 50 次完成运行，因为刚坏的源
应该立刻反映出来。

**所有状态都会列出（含 0）**。此前省略零值导致一次真实误读：快照显示 4576 条 `ready`
而没有 `pending` 键，客户端把整批标成「待处理」—— 唯一能反驳这个读法的状态不在报文里。

三个状态的含义：

| 状态 | 含义 |
|---|---|
| `pending` | 闸门未放行，**对所有输出路径不可见** |
| `ready` | 闸门已放行、可发布；**尚未被任何客户端看到**，内容未冻结 |
| `published` | 已被某条输出路径发出；**内容与 GUID 冻结**，不可回退 |

「通过 Reader API 拉取正文后 `ready` 变 `published`」是预期行为（首次输出即冻结，ADR 0016），不是数据修复。

## 故障处置表

| 症状 | 先看什么 | 安全动作 | 不要做什么 |
|---|---|---|---|
| 某 RSS 源连续失败 | 最近 JobRun、error_class、correlation_id | `network`：等待退避或确认后 trigger；检查 URL | 反复高频触发，绕过对方限流 |
| `structure_changed` 增加 | 采集器 ID、selector evidence、fixture | 暂停该 Source/collector；保存脱敏快照；先复现 fixture 再修 parser | 把异常一律改成 parse 或删除 fixture |
| `blocked` / 429 / CAPTCHA | HTTP 状态、Retry-After、robots 规则 | 暂停或大幅退避；联系内容方/使用合法 API | 绕过 CAPTCHA、伪造身份、提高并发 |
| `auth` 错误 | 凭据状态、认证过期时间 | 用户更新合法 secret；轮换旧 token | 把凭据贴进日志、fixture、代码 |
| pending 堆积 | enrichment JobRun、超时策略、处理器健康 | 重试；确认后 force publish；必要时暂时 hold | 直接把 pending SQL 批量改为 published |
| RSS 内容显示错误 | Item publish_status、GUID、首次发布时间、FeedOut 配置 | 未发布：修正后再 ready；已发布：创建正确的新 Item/版本策略 | 修改已发布 Item 的正文或 GUID |
| Reader 已读不同步（P3） | Reader API 请求日志、ReadState、认证 | 校验 endpoint/认证；按幂等事件重放 | 直接修改 Item 内容/发布状态 |
| SQLite 损坏/无法打开 | 最近备份、磁盘、应用日志 | 停服务、保留故障副本、恢复到新文件、验证 | 在原损坏库上继续写入 |
| 怀疑 token 泄漏 | AuditLog、访问日志、部署 Secret | 立刻撤销/轮换 token，评估暴露范围，清理受控日志 | 只改代码或只删除一条日志 |
| 条目数不涨 | `source.status`、`job_run` 最近状态、启动日志有无 scheduler 行 | 见「调度器」一节按推导输入排查 | 去找 next_run 表 —— 不存在，是设计如此 |
| 文章正文与源不一致 | `job_run` 是否有 `skip_published_content_changed` 告警 | 已发布内容按设计冻结；需要更正只能作为新条目 | 改已发布 Item 的正文或 GUID |
| MCP 写操作全被拒 | 客户端是否实现 elicitation | 换支持的客户端，或直接用 `/v1` | 让工具在无确认时继续执行 |
| NNW 里条目**全被标星** | `read_state.is_starred`、`s=.../starred` 流的返回 | 检查 Reader API 的状态流是否被正确过滤；数据库里 0 条星标却全部显示星标 = 状态流返回了全库 | 把「客户端显示」当成「数据库状态」 |
| 经 NNW 消费的条目**永不冻结** | `item.publish_status` 是否停在 `ready` | 三条输出路径都必须写 `first_publish`；此前只有 RSS 写，Reader API 不写 → 不可变性保证在客户端实际缓存的那条路径上失效 | 只修 RSS 渲染 |
| 应用日志**突然全部消失** | 是否执行过 alembic 迁移 | `fileConfig` 默认 `disable_existing_loggers=True`；已改为 `False` | 以为是代码没打日志 |

## Source/采集器问题操作步骤

### 网络失败

1. 获取最近 JobRun 的 correlation ID 和安全错误摘要
2. 检查是否为单一 Source、同域多个 Source 或全局网络问题
3. 读取调度器是否已自动退避；有退避时不要人工密集触发
4. URL 更正需要用户确认与审计；更正后触发一次任务验证
5. 成功后确认 adaptive 策略恢复，失败则保持退避

### 页面结构变化

1. 暂停受影响 Source 或采集器的自动运行，防止无效请求积累
2. 从受控诊断存储取最小、可合法保存的页面片段
3. 删除敏感信息后加入新的 `changed`/`valid` fixture，使问题可离线复现
4. 修改 parser；运行 fixture、单元和完整 CI
5. 部署后对单一 Source dry-run 验证
6. 用户确认恢复 Source；观察后续 JobRun

## 发布闸门与 NNW 缓存

- `pending` Item 不应出现在任何输出（Reader API、`/v1/items`、MCP 工具）；先查处理链和 enrichment JobRun
- `ready` Item 进入 feed 后，首次被输出即成为 `published`
- 不要“先发原文，后改译文”：NNW 可能缓存第一次内容
- 已发布内容错误时，按明确的补发/更正策略创建新的稳定 Item；不要复用或修改旧 GUID
- `force_publish` 仅在用户确认、理由记录、理解将无法自动撤回时执行

## SQLite 恢复流程

1. 停止 Core API 和 worker，避免继续写入
2. 复制当前数据库文件到带时间戳的隔离位置，保留取证副本
3. 验证最近备份存在、大小合理、可读取
4. 将备份恢复到**新的**数据库文件路径，禁止覆盖唯一故障副本
5. 启动一次离线完整性检查和 migration version 检查
6. 使用只读查询验证 Source、Item、FeedOut、AuditLog 数量与抽样记录
7. 启动服务，执行受控 E2E：发现/添加测试源、抓取、检查 RSS
8. 记录恢复点、丢失窗口、操作人和 correlation ID；安排事后复盘

每季度至少执行一次完整恢复演练；未演练的备份不视为可靠备份。

## 安全事件响应

遵循简化生命周期：准备 → 发现与分析 → 遏制/清除/恢复 → 复盘改进。

1. **遏制**：撤销泄漏 token、暂停有风险的插件/Source、限制入口
2. **保全证据**：保留最小必要日志和 AuditLog，访问受控存储；不公开不可信内容或密钥
3. **分析**：确认 actor、时间范围、目标资源、是否产生对外发布或数据泄漏
4. **恢复**：使用轮换后凭据、干净配置、受测 parser/插件恢复；验证 RSS/Reader API
5. **复盘**：补 fixture、测试、监控阈值、AGENTS.md 规则或 ADR；不修改历史审计记录

## 轮换 Reader API 凭据

Reader API 的密码只存在 `.env` 里，库内是 scrypt 哈希。auth token 由
`HMAC-SHA256(username + 哈希)` 派生，所以**改密码会使所有已登录客户端的 token 失效**。

```bash
# 1. 生成新密码
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"

# 2. 更新 .env 的 PEARSS_READER_PASSWORD（不要写进任何提交、日志或 issue）

# 3. 重启服务，启动时会从环境重新同步哈希
just service-restart

# 4. 验证新密码可用、旧密码已失效
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:8000/api/greader.php/accounts/ClientLogin \
  -d "Email=reader&Passwd=<新密码>"        # 期望 200
```

然后**必须在每个客户端重新输入密码**：NNW 的 FreshRSS 账号类型没有“改密码”入口，
只能删除账号重新添加。旧 token 不会报错提示原因，只会持续收到 401。

若怀疑密码泄露，按本文“安全事件响应”走：轮换优先于清理痕迹。

## 升级条件

暂停自动写入并优先人工处理，当出现以下任一情形：

- 同一域名连续出现 `blocked` 或 CAPTCHA
- `structure_changed` 在短时间内影响多个来源
- pending 积压超过既定上限且存在发布压力
- 已发布 feed 出现批量错误或 GUID 重复
- 数据库完整性检查失败或无可用备份



## 公网运行（ADR 0016）

服务经隧道暴露公网后（仅 Reader API 路由；`/v1` 与 `/mcp` 是否放行由隧道路由
自行决定），以下信号与处置是新的：

- **429 `rate_limited`**：某个客户端地址的 401/403 在 5 分钟内超过 50 次，
  已被阻断 15 分钟。若是你自己的客户端——它拿的是过期凭据，先改 `.env` 再
  重启，等阻断自然过期；若是陌生地址——忽略即可，它已经被限速。
- **审计 30 天滚动**：`audit_log` 每小时由调度器清理一次（
  `PEARSS_AUDIT_RETENTION_DAYS`，0 = 永久）。取证窗口是 30 天，需要长期留存
  的在清理前导出。调度器关闭（`PEARSS_SCHEDULER_ENABLED=0`）时清理不运行。
- **Reader token 永不过期**（ADR 0016 决定，风险已知并接受）：怀疑泄露时
  立即修改 `PEARSS_READER_PASSWORD` 并 `just service-restart`——所有已登录
  客户端的 token 随哈希轮换而全部失效，需重新登录。
- **凭据泄露总处置**：改 `.env` 中对应 token → `just service-restart` →
  检查 `audit_log` 中该 token 名下的操作记录（30 天窗口内）。
