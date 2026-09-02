# 错误分类与诊断语义

错误分类法是采集、调度、诊断、监控和 agent 建议共同依赖的领域契约。错误类在 `core.ErrorClass` enum 中单一来源定义；不得以任意字符串代替。

## ErrorClass

| 值 | 含义 | 常见证据 | 默认安全建议 |
|---|---|---|---|
| `network` | DNS、连接、TLS、超时、临时 5xx 等传输层失败 | connect timeout、DNS error、502/503 | 延迟重试；检查 URL、网络和对方服务状态 |
| `parse` | 收到内容但 XML/HTML 无法按其声明格式解析 | malformed XML、编码错误、非法 HTML | 保存脱敏快照；检查 feed/解析器容错；谨慎重试 |
| `structure_changed` | 页面可解析，但原有选择器/提取规则不再得到预期字段或条目 | 必填标题/链接缺失、选择器 0 命中、断言失败 | 停用该采集器自动发布；保存 fixture；修复 parser 后回归测试 |
| `blocked` | 对方明确限制、拒绝或挑战自动请求 | 403、429、CAPTCHA、反爬页面、robots 禁止 | 尊重限制，指数退避；不要绕过验证码或访问控制 |
| `auth` | 来源需要且未提供/已失效的合法认证 | 401、登录页替代内容、token 失效 | 请求用户更新合法凭据；不得把凭据写入日志或 fixture |

## 分类优先级

当一次失败同时满足多个信号时，从上至下按以下优先级归类：

```text
blocked > auth > network > parse > structure_changed
```

理由：访问被明确禁止时必须先停止；认证问题不应误判为结构变化；传输失败时没有足够证据分析内容。

`structure_changed` 只在以下条件都满足时使用：请求成功、内容格式可处理、采集器已运行、但原有结构契约不再满足。不要把所有 parser exception 都归类为 structure_changed。

## 领域错误与 HTTP 错误

| 场景 | HTTP API | JobRun | MCP Tool |
|---|---|---|---|
| API 参数无效 | 422 | 不创建或标记拒绝 | `invalid_input`，说明修正项 |
| 抓取超时 | 202 已创建 JobRun；最终查询 run | `failed + network` 或 `timed_out` | 返回 run ID；诊断显示分类 |
| 网页 429/CAPTCHA | 202；最终 run | `failed + blocked` | 明示不可自动绕过，建议等待/停用 |
| 页面结构变化 | 202；最终 run | `failed + structure_changed` | 建议保存 fixture、更新采集器 |
| 用户无权限 | 403 | 不适用 | `permission_denied`，不泄漏资源信息 |

HTTP 状态码表达“请求处理结果”；`ErrorClass` 表达“采集领域的失败原因”。两者不能互相替代。

## 错误记录结构

JobRun 或错误事件至少保存：

```json
{
  "error_class": "structure_changed",
  "code": "required_field_missing",
  "safe_summary": "The collector found no article links matching its expected structure.",
  "evidence": {
    "http_status": 200,
    "selector": "article a.headline",
    "matched_count": 0
  },
  "correlation_id": "..."
}
```

规则：

- `safe_summary` 不含 URL query secret、Cookie、Authorization、完整 HTML、外部错误正文或内部 stack trace
- 详细异常、原始响应仅存受控诊断存储；agent 读取时以 `untrusted_data` 隔离
- `evidence` 是结构化、可审计、可测试数据，优先于自然语言错误描述

## 重试与调度后果

| ErrorClass | 自动重试 | 调度策略 | 发布行为 |
|---|---|---|---|
| `network` | 是，有限次数 + 抖动 | 指数退避，后续恢复 adaptive/fixed 策略 | 不产生新 Item |
| `parse` | 至多一次短暂重试 | 保持或温和退避 | 不产生新 Item |
| `structure_changed` | 否 | 暂停该 collector 的自动抓取或降频并告警 | 不产生新 Item；不改已发布条目 |
| `blocked` | 否 | 明显退避/暂停，遵守 Retry-After | 不产生新 Item |
| `auth` | 否，等待合法凭据更新 | 暂停直至用户处理 | 不产生新 Item |

任何重试都必须创建新的 JobRun 并以 `retry_of` 关联；不得覆写第一次失败记录。

## 诊断输出规则

`diagnose_source` 必须基于最近 JobRun 的结构化分类与 evidence，输出：

1. 当前健康状态与最近成功时间
2. 主错误分类和证据
3. 影响范围：仅某来源、某采集器，还是系统性网络问题
4. 已自动采取的动作（例如退避、暂停）
5. 只读的建议 `next_actions`

示例：

```json
{
  "status": "degraded",
  "primary_error_class": "structure_changed",
  "evidence": {"matched_count": 0, "http_status": 200},
  "automatic_action": "paused automatic runs for this collector",
  "next_actions": ["view_latest_fixture", "test_collector"]
}
```

诊断不应自动启用插件、改变采集规则、降低安全限制或 force publish。此类写操作必须由 agent 提议并经用户确认。

