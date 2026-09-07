# 0017 · P2 插件化：hookspec 冻结、pluggy 注册表、manifest 与生命周期

- 状态：accepted
- 日期：2026-09-06
- 修订：`architecture-spec.md` §3、§6 E1–E4；`docs/guides/collector-guide.md`；
  `AGENTS.md`（采集器规则）
- 关联：ADR 0012/0014/0015（登录型采集器）、architecture-spec §1.2 E1–E4、§2.3 D5

## 背景

Rule of Three 前提早已满足：五个采集器（rss、telegram、twitter、zhihu、zsxq）
在跑，且 hookspec 草案的三个可选能力各有 ≥2 个真实使用者——fetch 钩子
（zhihu/zsxq/rss）、paged 声明（twitter）、backfill（telegram）。泛化依据充分，
按 §3 的既定承诺冻结合约、引入 pluggy 注册表、manifest 与生命周期。

用户裁决（本 ADR 的分类基础）：**telegram/twitter/zhihu/zsxq 归为第三方插件**，
rss 内置。划分依据是信任与耦合，不是代码放哪个目录：这四个采集器承载脆弱的、
站点特定的知识（逆向 API、签名算法、会话 cookie），失败模式相互独立且必然
发生，不应与宿主共享崩溃半径；rss 处理宿主核心协议本身，用宿主核心依赖，
无凭据无脆弱知识。

## 决定

### 合约（`src/core/plugins/hooks.py`，frozen）

- hookspec 定义放 **core**：hookspec 是领域合约而非装配细节，方向
  `app → plugins → core` 两头都经它；pluggy 无 I/O，不破 core 纯度。
- 必需：`collector_id` / `match_url` / `parse` / `source_config`（返回 Manifest）。
- 可选：`fetch`（签名/cookie/升级路径）、`collect_pages`（分页游标走读）、
  `backfill()`（返回 BackfillHooks 或 None——**从 hasattr 探测改为声明**，
  消除"声明/探测"双机制）。
- **失败契约进合约**：`fetch` 只许抛 `FetchError(ErrorClass)`，`parse` 只许抛
  `CollectorError(ErrorClass)`——`CollectorError` 移入 hooks.py 单一定义，
  五份重复与 registry 的五向 except 翻译删除。
- **manifest 启动双向校验**：features 里的结构性能力（fetch/paged/backfill）
  必须有对应 hookimpl，描述性能力（signed/cookie/wordpress-upgrade）必须有
  fetch hook；声明了不存在的 feature 即启动失败。校验在首个开发日就抓出
  三处真实不一致（telegram 缺 backfill 声明、twitter manifest 声明了没有的
  fetch、capability 命名错位）——这正是它存在的意义。

### Manifest 与 Plugin 表

- `Manifest`（id/version/name/description/example_url/features/
  required_config/checksum）进 hooks.py；checksum 为 canonical JSON 的
  sha256，registry 计算并随 Registration 携带。
- migration 0010 建 `plugin` 表（manifest_json/checksum/status/trust/...），
  status ∈ {enabled, disabled}，trust ∈ {builtin, third-party}。
- **启动 sync 不覆盖运维决定**：upsert 只刷新 manifest 与 checksum，
  status 留给操作者；`:enable/:disable` 在行缺失时以 registry 的权威
  manifest 建行（操作先于 sync 到达是合法时序）。

### 生命周期

| 操作 | P2a 状态 | 面 |
|---|---|---|
| list | ✅ `/v1/plugins` + MCP `list_plugins` | 读 |
| enable/disable | ✅ `:enable/:disable` + MCP `set_plugin_status`（⚠） | 写，审计覆盖 |
| import/uninstall/reload | ✅ 显式 409 占位（"P2b 的 loader 才有意义"） | — |

### 注册表

- pluggy `PluginManager("pearss")`，spec 挂 core；五个 builtin 模块注册其上，
  `Registration` 由注册过程派生（paged/fetches/manifest）。
- 路由语义不变：`source_type == collector_id` 命中 → 否则 `match_url` 轮询，
  声明序即优先序（telegram 在 rss 前——t.me 比 RSS 路径片段更明确）。

### 明确不做（记录裁剪）

- **内置插件热重载：永久裁剪，不是顺延**。Python 进程内 reload 撞四堵墙：
  模块级缓存/锁清不干净、singleton 引用遍布宿主、绕过全部 CI 门禁形成
  无验证的运行时变更通道，而收益仅是省一次亚秒级 `just service-restart`。
  子进程插件（P2b）的 `:reload` 是杀进程重 spawn，天然安全，届时提供。
- 第三方加载器/进程沙箱 → **P2b**（用户已裁决四采集器为第三方，P2b 交付
  L2 子进程隔离 + 凭据显式注入 + 网络白名单 + 完整 install/uninstall）。
- Processor hookspec → P3（随翻译/TTS 参考实现）。

## 后果

- pipeline 实现宿主零采集器 import（`grep` 可验收）；X 的分页走读搬入
  `plugins/collectors/twitter/walk.py`，凭据恢复等脆弱知识离开宿主。
- `add_source` 自检、MCP 暴露、诊断所需的元数据全部来自 manifest——
  停车场"注册表元数据层"兑现。
- plugin 表进事实源备份清单；item_enrichment 仍是唯一缺席的 P3 表。
- P2b 边界：四采集器迁子进程后，未信代码零进程内 import 才算兑现 E4。
