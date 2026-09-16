# 0023 · 第三方插件加载器：用户自负其责

- 状态：accepted（2026-09-17 部分修订：安装来源判定与「无源插件永不执行」由 [0025](0025-restore-approved-plugin-installations.md) 取代）
- 日期：2026-09-16
- 关联：ADR 0017（hookspec 冻结 / pluggy 注册表 / P2b 子进程沙箱）、ADR 0016（公网暴露收口）、ADR 0012（登录型采集器，风险承担范式）、ADR 0020（NixOS 部署）
- 前置：ADR 0017 把 `:import` / `:uninstall` / `:reload` 留为 409 占位（"P2b 的 loader 才有意义"），本 ADR 交付该 loader

## 背景

`_DECLARATIONS`（`src/app/collector_registry.py`）是一份硬编码元组，列出五个采集器
及其沙箱属性与能力。新增一个采集器的现状是：改元组 → 重新构建 → 重新部署（Y700
上还要跑一次 `nixos-rebuild`）。加载器要消除的是最后一步。

ADR 0017 已完成 P2b，交付了完整隔离：四个第三方采集器跑在
`python -m plugins.collectors <id>` 子进程里，宿主只持 `SandboxedCollector` 代理；
子进程 env 是 allowlist（`child_environment`）；egress 经宿主代理（SSRF 拦截仍生效）；
RLIMIT_CPU/RLIMIT_AS 生效；分类失败是值、只有协议失步才重启。**加载器的架构风险
因此大幅降低：新插件的破坏半径已被现有沙箱限定。** `:reload` 的语义也已定
（杀进程重 spawn，天然安全），不需要 `importlib.reload`。

## 决定

### 信任模型：用户自负其责

本 ADR 的核心立场，与 ADR 0012 同源（"本项目单用户、自部署，风险由运行者个人承担；
ADR 的作用是让这个承担是**知情的**"）：

**加载器不试图判断插件是否安全。** 它不沙箱能力、不审查代码、不限制来源。用户下载
并安装一个插件，就是用户对该插件的行为负责。机制的责任是**如实告知**——把插件要
什么权限、会碰什么数据说清楚——而不是**阻止**。

由此确定：

| 决策点 | 选择 | 理由 |
|---|---|---|
| 依赖策略 | **插件自带 venv** | 依赖集由作者声明、放在插件目录内，Pearss 不需要理解它，与 ADR 0020 §10.1 的职责线（Nix 管解释器、uv 管 Python 依赖图）不冲突 |
| 来源 | **允许从 URL 装载** | 用户对自己下载的东西负责。这是本 ADR 唯一与 ADR 0016 方向相反的决定，见「明确接受的风险」 |
| 凭据 | **manifest 声明 `required_config`，宿主按声明放行前缀** | 复用既有字段；不引入审批流程 |
| 装载校验 | **子进程真 import** | 与 ADR 0017 的 startup 双向校验同源，保证"装得进就一定跑得起来" |
| `:import` 确认 | 走 elicitation（MCP 侧） | 非安全边界，是 AGENTS.md 既有规则"写工具默认需要用户确认"，约束误操作 |

### 装载形态与 `sys.path`

插件装到 `/var/lib/pearss/plugins/<collector_id>/`：

```
/var/lib/pearss/plugins/my-collector/
  manifest.json      # id / version / name / features / required_config
  collector.py       # HOOKIMPL 实现（模块名固定为 collector）
  .venv/             # 作者自带的依赖环境
```

宿主 spawn 时把插件目录加入子进程的 `sys.path`，插件模块以 `collector` 之名
import。**子进程解释器仍是 Pearss venv 的**（不换解释器）：RPC 协议、`core.plugins.hooks`
的契约类型都由宿主侧提供，插件只提供 hookimpl 函数。这保持了与现有沙箱同构，
改动局限于 spawn 参数。

插件自带 venv 的依赖通过 `PYTHONPATH` 追加（宿主组装：`<插件>/.venv/lib/python3.X/site-packages`
+ `plugins.collectors` 的父目录），使插件既能 import 自己的依赖，又能 import 契约模块。

### 「已装载」是数据库状态，`_DECLARATIONS` 变为内置 + 表

这是唯一的结构性改动：

```
_BUILTIN_DECLARATIONS    # 不变，随 Pearss 发布
_build_registry()        # 遍历：内置 ∪ plugin 表中 trust=third-party 的行
```

`plugin` 表因此成为**代码加载的输入**——能写这张表就等于能加载代码。故：

- 表只由 `:import` / `:uninstall` 写，受写鉴权 + 审计覆盖（与所有写操作一致）
- 装载时校验磁盘文件的 checksum 与表中记录一致；不匹配则拒绝
  （防"登记后掉包"，这是**正确性**检查而非安全判断——用户当然可以自己改插件，
  那就重新 import）

### 生命周期语义

| 操作 | 语义 |
|---|---|
| `:import` | 校验 manifest → 子进程 import 校验双向一致 → 算 checksum → 写表 → 进入 registry |
| `:uninstall` | 有 active 源时 409 拒绝（说明哪些源）；否则删表行 + 摘除 registry |
| `:reload` | 杀 child，下次使用时按新代码 spawn（ADR 0017 已定性） |
| `:enable` / `:disable` | 已存在（只改状态位，不加载代码） |

**`:uninstall` 不删磁盘文件**：插件目录是运维放置的，宿主删除文件会让"撤销装载"
（可逆）与"删除数据"（不可逆）混在一起。文件由用户决定何时删。

**`enabled` ≠ 在运行**：它只表示"允许被调度使用"。代码真正执行是在某个 active 源
路由到它、调度器 tick 触发延迟 spawn 时（ADR 0017 的 `PluginChild` 首次使用时 fork）。
一个装了但没有源在用的插件，永远不会被执行。

## 明确接受的风险

按 ADR 0012 的范式逐条写下，不藏起来：

- **加载器允许任意代码在您的服务里运行**。现有隔离（子进程 + env allowlist +
  宿主代理出网 + 资源限额）限制的是**故障半径**，不是**恶意行为**。插件能读写其
  子进程可触及的一切、能通过宿主代理访问被允许的网络、能看到它被授权的环境变量。
  信任来源即信任代码。

- **`PEARSS_COOKIECLOUD_*` 是插件系统里最危险的一处凭据扩散**。该 token 是
  CookieCloud **服务端主密钥**，能解密导出**任意 uuid、任意域名**的 cookie——
  持有它等于持有您同步过的全部站点会话（含银行、邮箱）。若一个登录型插件声明了
  `PEARSS_COOKIECLOUD_` 前缀，它就拿到这把主密钥。ADR 0012 的既有要求因此对
  插件同样适用：非回环明文 http 必须告警；部署应使用只含阅读器所需站点的专用 uuid。
  `:import` 的 elicitation 文本必须列出插件请求的凭据前缀。

- **来源不设限意味着来源即信任**。从任意 URL 装载 = 信任那个 URL。
  **这是本 ADR 唯一与 ADR 0016 方向相反的决定。** ADR 0016 把公网面收敛到只有
  Reader API（隧道 + 路径过滤），加载器在应用内部重新开了一个"让服务获取并执行
  代码"的通路。它不扩大公网**攻击面**（`:import` 需要写鉴权，与所有写端点同一边界），
  但它确实意味着"服务的可信代码集"不再只由部署决定。选择接受，理由是单用户系统里
  用户本就是唯一的信任源。

- **本 ADR 不对插件做任何能力限制**。没有网络白名单、没有文件系统限制（除进程本身
  的 cwd/权限）、没有 API 配额。这与 ADR 0012 对登录型采集器的处理一致：风险陈述的
  目的不是消除风险，而是让承担是知情的。

## 后果

- `plugin` 表进"代码加载输入"的角色，其写路径的重要性上升（审计必须完整）
- `_DECLARATIONS` 拆为 `_BUILTIN_DECLARATIONS` + 表读取；registry 构建从纯常量
  变为读表，启动顺序上需在 DB 可用之后
- `child_environment` 的凭据前缀来自插件 manifest 的 `required_config`，不再是
  硬编码的 `_COLLECTOR_ENV_PREFIXES`（后者保留为内置采集器的声明来源）
- 插件的 `trust` 字段是**纯描述性**的（区分"随 Pearss 来"与"你自己装的"），
  **不产生行为差异**——与 `TrustLevel`（`core/enums.py`）的既有处理同构：
  存下来、展示出来、不给它效果
- OPML 导入导出（A6）与本 ADR 无关，仍独立

## MCP 侧暂不暴露（记录裁剪）

本 ADR 只实现 HTTP 面（`POST /v1/plugins/{id}:import`、`DELETE /v1/plugins/{id}`、
`POST /v1/plugins/{id}:reload`）。MCP 工具面**不加**对应工具，理由：

- 装载插件是**运维动作**（放置文件、检查来源），不是 agent 对话里发生的操作。
  给它加 elicitation 确认的意义有限——真正需要确认的是"这个插件从哪来"，
  而那发生在文件系统上，不在工具调用里
- `list_plugins` 与 `set_plugin_status` 已在 MCP 面，agent 能看见和启停已装载的
  插件；装/卸留给人

若后续确实需要 agent 参与装载，那应是一个独立决定：MCP 的 `:import` 确认文本
**必须列出该插件请求的凭据前缀**（ADR 0012 的 CookieCloud 主密钥风险）。

## 不做什么（记录裁剪）

- **不做插件的安全审查、签名验证、来源白名单**。见「信任模型」。
- **不做插件的依赖解析**。自带 venv 即全部；Pearss 不跑 pip/uv。
- **不删插件的磁盘文件**。`uninstall` 只改内部状态。
- **不做内置采集器的热重载**。ADR 0017 已永久裁剪（进程内 reload 撞四堵墙），
  本 ADR 不反转该决定。
