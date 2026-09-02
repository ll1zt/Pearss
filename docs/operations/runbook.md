# 运维与故障恢复手册

本手册按症状提供安全、可重复的运行操作。所有直接 API 操作仍需鉴权和审计；写操作若从 MCP 发起，必须遵守用户确认规则。

## 基本原则

1. 先收集 `correlation_id`、JobRun、结构化日志和影响范围，再修改配置
2. 不要通过修改已发布 Item 内容修复输出问题；客户端可能已经缓存
3. 不要把 token、Cookie、认证 Header、完整不可信网页正文复制进工单、日志或 Git
4. 变更前先备份事实源数据；变更后运行最小验证
5. 不确定时优先暂停特定 Source/Plugin，避免扩大错误输出

## 日常健康检查

检查以下信号：

- 各 Source 最近成功时间与连续失败次数
- ErrorClass 分布；特别是 `structure_changed` 是否突然增加
- `pending` Item 的数量与最久等待时长
- JobRun 队列深度、超时率、重试率
- 插件崩溃/超时率（P2）
- RSS 输出可访问性、最近 GUID 是否稳定
- 备份最近一次成功时间与最近恢复演练日期

可使用 `get_system_snapshot`、`list_jobs`、`diagnose_source`（P2）或 Swagger UI。

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

- `pending` Item 不应出现在 RSS 输出；先查处理链和 enrichment JobRun
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

## 升级条件

暂停自动写入并优先人工处理，当出现以下任一情形：

- 同一域名连续出现 `blocked` 或 CAPTCHA
- `structure_changed` 在短时间内影响多个来源
- pending 积压超过既定上限且存在发布压力
- 已发布 feed 出现批量错误或 GUID 重复
- 数据库完整性检查失败或无可用备份

