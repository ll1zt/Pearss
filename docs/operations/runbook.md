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

### 两条部署路径，规范的是哪一条（issue #88）

| 路径 | 状态 |
|---|---|
| **systemd 用户服务**（`just install-unit`） | **规范**。它不需要 root，`.env` 由 `just serve` 读取，所以单元里没有主机名、端口与凭据 |
| NixOS 模块（`nix/module.nix`，ADR 0020） | 备选。它从 `cfg.package.passthru.projectRoot` 取路径，本来就是参数化的 |

单元里的 checkout 路径是**占位符** `@CHECKOUT@`，由 `just install-unit` 在安装时替换成
**它自己所在的 checkout**：

```bash
just install-unit      # 从哪个 checkout 跑，单元就指向哪个
```

所以换了目录、换了机器、或用 worktree 部署，都不需要编辑单元文件。
`WorkingDirectory=` 不是装饰：`nix develop` 从工作目录解析 flake，而 nix 2.34 既没有全局也
没有子命令级的 `--directory`。渲染假设由 `tests/unit/test_structure_guards.py` 守住
（它用一个**不是任何人的 home** 的路径来验装配，而不是验本机布局）。

## 调度器

运行中的调度循环和 enrichment sweep 遇到 SQLAlchemy `OperationalError` 时会记录不含 SQL、
参数或异常正文的警告，结束当前轮并按原轮询间隔重试；这不是数据库自动修复，持续失败
仍需检查磁盘、权限和锁竞争。其他异常不会被当作临时存储故障无限重试。
此恢复边界不含启动阶段；`/healthz` 也不代表后台任务健康。
enrichment 认领在执行前提交；每轮最多认领并发槽数，不预先认领在线程队列等待的任务。
processor 现以一次性子进程执行（JSON 输入/正文输出，父进程独占 DB 和正文存储），
默认总预算 360 秒，包含启动、所有分段和模型 fallback；超时或 stop 时强制终止并回收子进程，
然后按 `network` 走既有退避/最多三次策略。失败的必需产品仍保持 Item pending。
`lease_expires_at` 在认领时持久化为预算加 5 秒清理余量；其他 worker 只在租约到期后回收，
不能用自己的超时设置提前回收。升级前 NULL 租约保留原 720 秒回收规则。
A10 owner-token SQL fence 继续拒绝旧 owner 的结果。

执行边界仅接受可 import 的模块或模块级静态 processor 类，不支持闭包/有状态实例，
没有退回线程直接执行的兼容分支。凭据沿用 child_environment allowlist，translate 每次启动
重新读取自己的 `PEARSS_TRANSLATE_*`，不会继承 DB/API 凭据。协议输入输出各上限 32 MiB。
provider 地址仍由部署配置；这不是新增 HTTP 代理或第三方插件安全沙箱。

translate 对每个可翻译块各发一次请求（ADR 0031）：一篇 4 段的文章是 4 次调用，而不是 1 次。
因此 provider 侧看到的请求数随文章块数增长，与条目数不是一对一；估算用量与排查限速时要按这个乘数算。
非文本节点（代码块、图片）不经 provider，所以它们既不产生调用也不会被翻译。

正常 POSIX 环境会终止整个子进程组；禁止进程组信号的 macOS 沙箱退回终止直接子进程。
子进程有同截止时间 watchdog，但不是 OS 级隔离：宿主崩溃且子进程卡在持有 GIL 的 C 扩展、
进程被暂停、机器时钟跳变或恶意脱离进程组等情况不在硬保证内，部署仍需服务级进程组清理。
远端已接受的收费请求不能撤销，重试可能重复计费。清理会 join 真正执行 sweep 的线程，
不会仅取消 Future 后丢弃线程；父进程 DB/文件系统故障的耗时不属于 processor 总预算。
孤儿运行回收和审计裁剪只在数据库提交成功后推进维护计时；写入失败不会消耗重试窗口，
下一轮仍可重试。窗口从本次维护完成时起计算。

调度器和 enrichment worker 的 `stop()` 会消费已失败任务保存的普通异常、只记录异常类型，
并清除任务引用，避免该异常中断后续顺序清理。这不代表运行时会自动重试编程错误。
enrichment 会等待取消后的真实 sweep 完成；scheduler 仍不保证取消 `asyncio.to_thread`
后同步工作已停止。进入 MCP lifespan 后，插件恢复、传输接线、
插件表同步及 worker 启动也纳入同一清理范围；这些启动步骤失败时会执行既有的顺序清理。
更早的构造/MCP 进入失败、各资源独立清理和后台健康信号仍由 A07/C10 跟踪。

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

X 源是**登录型**（ADR 0012），需要会话 cookie，且是**分页型**采集：抓取范围由
`source` 上的两个游标界定 —— `seen_frontier`（上沿，已入库的最新推文 id）与
`floor_cursor`（下沿，该源走过的最深位置）。语义见 data-model 的「抓取区间」。

| 症状 | 查什么 | 说明 |
|---|---|---|
| `error_class=auth` | `PEARSS_X_*` 或 CookieCloud 配置 | 凭据缺失/失效**不会**匿名重试（ADR 0012）—— 若看到 `auth` 但确信配好了，查 CookieCloud 是否返回了 `auth_token`/`ct0` |
| 首轮抓回上千条 | `PEARSS_X_FIRST_PAGES`（默认 3） | 它定义该源的历史深度。想让新源只取最近内容，调小再订阅；对已有源改它**无效** —— floor 已由首轮定格 |
| 每轮请求数远超预期 | `source.floor_cursor` 是否为 NULL | floor 为空说明区间只有上沿，walk 会一直翻到 origin 底部或 200 页护栏。让首轮正常跑完即可建立 floor |
| 日志出现 `timeline gap` | 无处置动作 | 断档是**记录**而非待办：它位于区间内部，追它等于开出第二个爬取方向。frontier 持续推进才是下一步抓什么的依据 |
| CookieCloud 连不上 | 服务日志 `CookieCloud unavailable` | 静态 `PEARSS_X_*` 优先；若两者都无 → `auth`。服务端不可达与“没配置”是**不同处置** |
| X 返回 401 | 服务日志 | 会话已轮换；会绕过缓存重取一次并重试该页（只一次）。若持续 401，说明 CookieCloud 里的仍未更新 —— 需在浏览器重新同步 |
| `structure_changed` | 响应结构 | X 的 queryId 与结构由 X 单方变更；queryId 会自动从 x.com 的 JS bundle 恢复，失败则 human 介入（见采集器 README） |

**手动续期**：CookieCloud 缓存 TTL 默认 300s（`PEARSS_COOKIECLOUD_CACHE_SECONDS`）。
浏览器重登后最多等一个 TTL，或重启服务立即生效。

> ⚠️ `COOKIE_CLOUD_SERVER_PASSWORD` 是**服务端主密钥**，能导出任意 uuid、任意域名的 cookie。
> 它比它取回的那一个会话值强得多 —— 持有它等于持有同步过的全部站点会话（含银行、邮箱）。

## MCP 工具面

- 端点 `/mcp`（Streamable HTTP），与 API 同进程。**不是 `/mcp/mcp`** —— 那个
  双重路径是早期把 SDK 的 `/mcp` 路由挂到 `/mcp` 之下造成的，现已在根挂载并用路径守卫
  收窄；未知路径返回 404，且不再附带 MCP 的鉴权挑战
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
- **删除类工具（ADR 0021）**：`remove_source`（删订阅+内容）、`delete_items`（按 id）、
  `delete_source_items`（清空某源条目、保留订阅）。三者都不可逆，确认文本里会写明
  后果。它们与其它写工具的一个区别：破坏半径不在参数里，所以确认消息必须把
  「删掉什么」说出来，而不只是复述参数

## 第三方插件装载（ADR 0023）

插件放在 `PEARSS_PLUGIN_DIR`（默认 `/var/lib/pearss/plugins`）下的一个目录里，
目录名就是 collector_id：

```text
/var/lib/pearss/plugins/my-collector/
  manifest.json    # id / version / name / features / required_config
  collector.py     # hookimpls，模块名必须能被 import 为 `collector`
  .venv/           # 作者自带的依赖（可选）
```

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/plugins/my-collector:import \
  -H "Authorization: Bearer $PEARSS_API_TOKEN" -H "Idempotency-Key: $(uuidgen)"
```

| 症状 | 查什么 | 说明 |
|---|---|---|
| `plugin_load_failed`，消息指明目录 | `PEARSS_PLUGIN_DIR` 与目录名 | 目录名必须等于 manifest 的 `id` |
| `declares feature 'paged' but implements no …` | 插件的 `source_config()` 与 hookimpl | 声明与实现不符是契约问题（同 ADR 0017 的启动校验），拒绝且不留痕 |
| 装了但不采集 | 该插件的源是否 `active`、URL 是否被 `match_url` 命中 | **`enabled` ≠ 在运行**：真正执行发生在某个 active 源路由到它、调度器 tick 触发延迟 spawn 时 |
| 改了代码不生效 | 是否执行了 `:reload` | `:reload` 杀子进程重 spawn；不 reload 则仍在跑旧代码 |
| `uninstall` 返回 409 | 该插件的活跃源 | 先暂停/删除那些源。卸载**不删磁盘文件**，那是你的目录 |
| 日志出现 `plugin raised: …` | 插件自身的异常 | 插件未捕获的异常被归类为 `parse` 失败（记录在该源的 run 上），不会逃逸成服务级错误 |

**重启恢复（ADR 0025）：** 启动会从数据库恢复已审批安装，不扫描目录自动导入。
卸载后留在磁盘上的目录不会复活；disabled 保持禁用，触发仍按原契约成功跳过采集。
启动校验会在子进程中导入代码（包括 disabled 插件），不等同于执行采集。
若日志提示 `Installed plugin ... unavailable`，检查文件和依赖；恢复缺失/损坏文件
后重启重试。manifest 有变化时必须重新审查并显式 `:import`，不得直接改数据库
checksum。checksum 只覆盖 manifest，不是代码签名。

恢复失败的审批记录仍保存在数据库，但现有 `/v1/plugins` 仅列运行时注册项，
因此不会显示这些失败记录。源没有持久化 collector 绑定，恢复失败后可能落入
其他 URL 匹配器；排查时结合启动日志，不只看源的 parse 错误。

**装载器不判断插件是否安全**（ADR 0023）：它只校验结构与声明一致性。子进程隔离
（env allowlist + 宿主代理出网 + 资源限额）限制的是**故障半径**，不是**恶意行为** ——
信任决定属于运行者。若插件声明了 `PEARSS_COOKIECLOUD_` 前缀，它会拿到 CookieCloud
服务端主密钥（能解密你同步过的全部站点会话），见 ADR 0012 的既有要求。

## Agent 源与主动接入（A3，ADR 0022）

外部 agent（或你手动）向 Pearss 推送内容。与采集完全同源：推送的条目同样经过
存储边界净化、内容寻址、去重与发布闸门。

### 建源与推送

```bash
# 1. 建 agent 源（url 用合成名 agent://<name>，它同时是去重键）
curl -sS -X POST http://127.0.0.1:8000/v1/sources \
  -H "Authorization: Bearer $PEARSS_API_TOKEN" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"url":"agent://newsletter-intake","type":"agent",
       "schedule_policy":{"type":"fixed","interval":"1h"}}'

# 2. 推送（凭据用 ingest token，不是 API token）
curl -sS -X POST http://127.0.0.1:8000/v1/sources/<id>:ingest \
  -H "Authorization: Bearer $PEARSS_INGEST_TOKEN" -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"items":[{"external_id":"post-1","title":"标题",
        "canonical_url":"https://example.com/post-1","content":"<p>正文</p>"}]}'
```

MCP 客户端用工具 `submit_items`（需确认）。

### 凭据

| 变量 | 能做什么 |
|---|---|
| `PEARSS_INGEST_TOKEN` | **只能**向 agent 源推送内容 |
| `PEARSS_API_TOKEN` | 全部（含 ingest） |
| `PEARSS_API_READ_TOKEN` | 只读，推送返回 403 |

ingest token 不配则 ingest 不可用，**其余功能不受影响**。它打不通
`POST/PATCH/DELETE /v1/sources*` 或 `:trigger`（一律 403）——所以给外部 agent
用它，泄漏的损失面仅限「能往 agent 源塞内容」。

### 排错

| 症状 | 原因 |
|---|---|
| 403 `insufficient_scope` | 用了 read token，或用了 ingest token 去调非 ingest 路径 |
| 409 `not_an_agent_source` | 目标是 rss/web 源。推送只接受 `type='agent'`，避免把内容混进另一种来源的 provenance |
| 404 `source_not_found` | 源 id 不对；先建 agent 源 |
| 201 但 `inserted` 小于 `submitted` | **不是错误**，是去重：同源同内容为 `skip_same_source`，同内容已从别源发布过为 `suppress`。**按 `results[].action` 判断，不要按计数** |
| 换了 body 但响应还是旧的 | 复用了同一个 `Idempotency-Key`。重放是刻意的，响应带 `Idempotent-Replay: true` 头；换操作要换 key |
| 条目推送成功但客户端看不到 | 该源配了 `pipeline_chain`，条目滞留 `pending` 等处理器链完成（与采集源同样受闸门约束）。查 `list_enrichments` |

**注意**：agent 源**不进调度器**（它是被推送的，不是被轮询的），所以它没有
JobRun、不产生采集历史。若发现某个 agent 源在 `/v1/jobs` 里出现失败运行，
那是缺陷而非预期。

### 清空某个 agent 源的内容

与普通源相同：`POST /v1/sources/{id}/items:delete`（保留源），或
`DELETE /v1/sources/{id}`（连源一起删）。见「清理条目与订阅」。

## 修改源的处理链与目标语言（A04）

`PATCH /v1/sources/{id}` 对 `pipeline_chain` 和 `target_lang` 分别使用三态语义：

| 请求体 | 结果 |
|---|---|
| `{"target_lang":"de"}` | 只改默认目标语言，原链（含顺序、逐项 `lang` / `required`）保持不变 |
| `{"pipeline_chain":[{"plugin_id":"translate","kind":"translation"}]}` | 替换链，保留已有 `target_lang` |
| `{"pipeline_chain":null}` 或 `{"pipeline_chain":[]}` | 显式清空链，保留目标语言；之后新采集条目无需等待处理链 |
| `{"target_lang":null}` | 只清空默认语言，不清空链；逐项显式 `lang` 不受影响 |
| `{"pipeline_chain":null,"target_lang":null}` | 同时清空两项配置 |

省略不是 `null`。服务先将请求中明确提供的字段与存储中的未提供字段合并，
再校验最终链；未知处理器或非法 kind 返回 `422 invalid_pipeline_chain`，
整个 PATCH（包括同时发送的 folder / schedule 等字段）不落库。
清空默认语言不等于保证翻译处理器能运行：当前链校验允许空语言，
使用翻译时应保留默认目标语言或逐项设置 `lang`。

写请求仍需 API 鉴权和 `Idempotency-Key`，不同修改使用不同 key。
变更后的最小冒烟检查（使用测试源，避免改写已发布条目）：

1. 先 GET 源配置，记录原 `pipeline_chain` 与 `target_lang`。
2. 只 PATCH `target_lang`，再 GET 确认原链逐项不变。
3. 手动触发采集新条目；必需处理尚未完成时，条目仍为 `pending`，
   `/v1/sources/{id}/enrichments` 显示相应任务及目标语言。
4. 对测试源提交非法 kind 并同时修改 folder；确认收到 422，GET 后两者均未变。

## 日常健康检查

检查以下信号：

- 各 Source 最近成功时间与连续失败次数
- ErrorClass 分布；特别是 `structure_changed` 是否突然增加
- `pending` Item 的数量与最久等待时长
- JobRun 队列深度、超时率、重试率
- 插件子进程信号：`GET /v1/plugins` 看启用状态；日志中
  `plugin child … failed; restarting once`（崩溃重启）、
  `plugin child … environment changed; rotating`（凭据轮换）、
  `PEARSS_* environment changed` 出现频率异常 = 凭据在频繁变动
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

## 作业与批量投递（`just work`）

批量入口在 CLI，不在模型上下文里：把目标写成文件，命令值由 CLI 构造并发到 `/v1/works`。

```bash
just work submit --kind fetch --source <source_id> --targets-file urls.txt   # 打印 work_id
just work status <work_id>                                                   # 状态 + 折叠 counts
just work units <work_id> [--outcome failed]                                 # 逐目标账本（TSV）
just work retry-failed <work_id>                                             # 只重发失败目标
```

- 列表**不回显**：stdout 只有 work_id（可 `WORK=$(just work submit …)` 捕获），说明走 stderr。
- 凭据从环境读（`PEARSS_API_TOKEN`；justfile 会自动 source `.env`），**从不打印**。API 地址默认
  `http://127.0.0.1:8000`，用 `PEARSS_API_URL` 指向别处。
- 同一命令值（kind + 有序 targets + params）在 API 的幂等窗口内是**同一个作业**；窗口之后重新提交
  是新的一次运行。目标文件里的空行与 `#` 注释会被跳过。
- 六个 kind（`fetch` / `delete` / `enrich` / `retry` / `subscribe` / `ingest`）都已有执行器。将来新增的
  kind 若还没有执行器，提交后会停在 `queued`（不认领也不标失败）——`just work status` 看得到，不会静默失败。
- 超时丢结果的旧形态（`:fetch-items` 一次几百个地址）已被限制为 ≤ 20 个地址的兼容壳（ADR 0046）；
  大批量一律走作业。


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
| 沙箱插件日志出现 `child_error` / `failed; restarting once` | `journalctl --user -u pearss` 中 child 的 traceback（stderr 已继承宿主流） | 单次崩溃会被自动重启自愈；反复出现 = 插件代码/环境问题，先看 traceback 再决定是否 disable 该插件 | 在宿主进程内"顺手修"插件代码 |
| 翻译链失败，但 journal 里看不出是哪个模型 | `journalctl -u pearss \| grep '\[translate '` —— 处理器子进程的日志已按行转发，带 `[plugin_id item_id]` 前缀（issue #229；此前 stderr 是 `/dev/null`，所以「日志里没有」本身当时是事实） | 按日志里的模型名与 `error_class` 处置；健康登记表会自动冷却/降级（ADR 0048），持续失败再查 provider 与 `PEARSS_TRANSLATE_*` | 在插件里自己加 `logging.basicConfig`：宿主已转发子进程 stderr，重复配置只会改变格式 |
| 沙箱插件持续 `egress_denied` | 部署是否经 app lifespan 启动（egress 由 lifespan 注入） | 正常 systemd/`just run` 启动即已接线；自查脚本/手工调用 collector_registry 时需手动注入 transport | 给 child 直接放开网络（绕过 SSRF 拦截） |
| 插件相关错误里 `auth` 但凭据已配置 | 凭据前缀是否在 `_COLLECTOR_ENV_PREFIXES` 登记（ADR 0017 env allowlist） | 补登记后重启服务 | 把宿主全量环境透传给 child |

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

## 清理条目与订阅（ADR 0021，不可逆）

删除是**物理删除**：行、正文文件、已读状态、译文产物、抓取记录一并消失。
误删只能从数据库备份恢复（见「备份与恢复」），AuditLog 只记录「删了什么」，
不保留内容。

### 抓错了内容，但订阅要保留

用 `POST /v1/sources/{id}/items:delete`（MCP：`delete_source_items`）。
源与其 `seen_frontier` / `floor_cursor` 保留，下一轮从这里继续——
**不要**用「删订阅再重新订阅」代替，那会让采集重新采样历史深度。

- 先看规模：`GET /v1/items?source_id=<id>&limit=1` 的 `total` 字段
- 只想清未对外发布的部分：传 `include_published=false`
- **时间窗（`since` / `until`）只选择有发布时间的条目**。没有发布时间的条目
  不属于任何窗口，两个边界都会排除它——所以「清掉某日期之前的」不会顺带删掉
  无日期的条目。若确实要清它们，不要带窗口参数
- 响应里的 `files_removed` 是实际 unlink 的文件数，可能小于可回收引用数
  （文件已不存在时如此）；`bodies` 是判定为可回收的引用数

### 删除指定条目

`DELETE /v1/items/{id}`（单条）或 `POST /v1/items:delete`（批量，
body `{"item_ids": [...]}`）。MCP：`delete_items`。
id 从 `search_items` 或 Reader 的 items 流里取。

### 删除整个订阅

`DELETE /v1/sources/{id}`（MCP：`remove_source`）。**不可逆**，
且重新订阅同一 feed 会得到一个新源、从首轮页数策略重新采样。
若不确定，先用 `set_source_status` 置为 `disabled`——它停止采集但保留一切。

### 共享正文的坑（已由实现处理，排障时需知道）

正文按 sha256 内容寻址，**正文相同的两个条目共享一个文件**。删除时只有
当没有存活的 Item 行引用该摘要才会删文件。若你手工用 SQL 删条目，
**不要**顺手删 `cas://` 文件——`read_body` 对缺失文件只返回空串并记一行
warning，另一篇活文章的正文会静默变空。

### 客户端仍会显示已删内容

Reader/NNW 缓存有自己的时效，服务端删掉后客户端可能仍显示到缓存过期或
重新拉取为止。这是已知且接受的：删除的目标是服务端不再提供它。

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

## 备份与恢复

**日常**：`pearss-backup.timer` 每日运行 `just backup`，产物落在
`backups/`（gitignored）。审计日志按 ADR 0016 是 30 天滚动事实源，所以
**备份周期必须远小于 30 天**——日度留足了漏跑余量。

**归档内容**：SQLite 在线快照（`.backup` API，服务运行中一致）+ 文件存储
整树（条目正文、翻译产物）。`raw_fetch` 报文不入档：它们是可重建的诊断
数据（data-model.md 的分级）。

**恢复**：

```bash
just restore backups/pearss-backup-<stamp>.tar.gz /tmp/restore-drill
# 把 PEARSS_DATABASE_URL / PEARSS_STORAGE_DIR 指向还原目录，起服务验证
```

`restore` 会拒绝覆盖已存在的数据库文件（防误操作），并在解包后跑
`pragma integrity_check` + 行数核对——一个能打开但空的库比失败更危险。

**演练记录（2026-09-11，首次）**：9 源 / 127 条归档还原成功，Reader API
登录、订阅列表、条目正文、译文合并全部正常。演练价值实证：过程中两次
「疑似 bug」都是测量伪影（httpx data 传参形式、归档内 db 命名），真实
curl 复核后端点行为正确——这正是 runbook 强调「用真实客户端复核」的原因。

## 本地 embedding 模型（ADR 0035 / 0040）

向量能力需要模型文件（约 135MB），不在仓库里：

```bash
just fetch-model     # 装到 PEARSS_EMBEDDING_MODEL_DIR（默认 /var/lib/pearss/models/multilingual-e5-small）
```

每个字节都对着源码里钉住的 sha256 校验 ✓，所以重复执行只做一次比对 ✓（内容一致
就不重下 ✓）；被篡改或缺一个文件都会失败且**不留残骸** ✓。

到国际站点线路慢或不稳时设 `HF_ENDPOINT`（例如 `https://hf-mirror.com`）✓ ——
镜像只能改变「字节从哪来」✓：产物是按哈希钉住的 ✓，供错字节会被拒绝、什么都装不上 ✓。
CI 在跑门禁前先用镜像装一次 ✓，所以那份模型也在被持续验证 ✓。

源的链里出现 `embed` 处理器之前必须先装好 ✓，否则**配置期**就会拒绝 ✓（一行永远
无人认领、每轮 sweep 静默跳过，比早失败更糟 ✓）。

需要**真模型**的测试（多语言与语义，见 `tests/integration/test_embedding_semantics.py`）
在没有模型时**跳过** ✓，跳过是看得见的 ✓；CI 里它们是真跑的 ✓。

## 循环存活与告警（ADR 0034 / 0039）

两个后台循环（scheduler、enrichment worker）**互相**通过告警通道报告对方的死亡：
循环无法报告自己的死亡，所以发现它停掉的必须是另一个还在跑的循环。卡死的处理链
（某源待处理行全部 `failed` 且 ≥3 条）走同一条通道，文案与行动建议随条件分叉。

**残余风险（ADR 0039）**：两个循环同时死时，告警通道保持静默——没有任何进程内的
机制能覆盖进程整体死亡。此时唯一的信号是被动面，而它由 HTTP 层回答，不由这两个
循环回答（ADR 0019）：

```bash
curl -fsS http://127.0.0.1:8000/healthz   # 非 200 即需要人看
```

部署侧应把这一步做成外部轮询。若实际出现「两个循环同时死而无人察觉」，按 ADR 0039
的触发条件改用进程外观察者，而不是继续在进程内加代码。

## 手工冒烟清单

大改动（新客户端接入、插件架构变更、部署形态变化、Reader/采集器修改）
合入后走一遍。自动化测试覆盖不了真实客户端与真实传输边界——2026-09 的
六个实战缺陷全部只被这张单能抓到的路径暴露。

**NNW（FreshRSS API）**：

1. NNW 登录 `http://<host>:8000/api/greader.php`，订阅列表出现全部源
2. 刷新拉取新条目，正文完整（WordPress 源应为全文而非摘要）
3. 标一条已读、加一条星标 → `read_state` 表落库
4. **再次刷新不产生 401**（token 稳定性；曾因凭据同步重哈希互踢）

**MCP（agent 面）**：

5. 经 MCP `add_source` 添加一个非 feed URL（如 zhihu 频道），确认弹窗
   类型正确、采集 run 成功
6. `get_system_snapshot` 数字与库内一致

**传输边界**：

7. 一个 HTTPS 源经沙箱出网采集成功（gzip 双重解压回归点）
8. 日志无 `zlib` / `AttributeError: handle_request` / 连续 401

**配套**：

9. 备份 `just backup` 成功且能恢复（改部署后必做恢复演练）
10. 隧道环境：XFF 传递、401 限速按真实客户端地址计数

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

> NixOS 部署形态、隧道双入口（serve :8443 整应用 / funnel :10000 仅 Reader API）
> 与 UID 约束见 `docs/adr/0020-nixos-deployment-and-tunnel.md`；
> 部署后逐条验证见 `docs/operations/tunnel-drill.md`。

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
