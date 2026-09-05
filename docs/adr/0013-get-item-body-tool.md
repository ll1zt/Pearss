# 0013 · 允许 get_item_body 工具，以截断为条件

- 状态：accepted
- 日期：2026-09-05
- 修订：`AGENTS.md` 第 99 行、`docs/api/mcp-tool-guide.md` §二 硬性规则 1
- 关联：ADR 0010（MCP 工具面经 Core API）

## 背景

AGENTS.md 第 99 行与 mcp-tool-guide §2 规则 1 明文规定：

> 大正文、HTML、音频不得塞进 Tool 返回值；返回 item/enrichment ID 或 Resource URI

正文因此只经 `item://{item_id}` Resource 提供。

## 这条规则此前被错误地论证过

一种常见说法是"Resource 不进上下文，工具结果进"。**这是错的**：
`resources/read` 返回的 `text` 与工具结果一样进入对话上下文，MCP 没有
"读取但不进上下文"的语义。

规则真正的依据是另外两条，而且都成立：

1. **粒度**。列表工具一次带 N 个正文是必然的上下文爆炸
   （`search_items(limit=50)` × 数 KB = 数百 KB）。强制逐条获取，把成本变成
   每次一条、由 agent 主动决定。
2. **可选的客户端处理**。部分客户端把 Resource 当附件/可切换上下文
   （Claude Projects、Cursor 的 @ 引用），此时正文可以不在 prompt 里。
   这是**客户端能力，不是协议保证**。

所以规则的精神是"不要批量、要显式"，不是"Resource 免费"。

## 现实约束

Resource 是 MCP 中**采用率最低**的特性之一。客户端不支持时，agent 完全取不到
正文 —— 而且不报错，只表现为"它说没有正文可取"。这等于功能缺失。

## 决定

新增 `get_item_body(item_id, max_chars)` 工具，**以截断为强制条件**：

| 约束 | 值 | 理由 |
|---|---|---|
| 默认 `max_chars` | 4000 | 一篇正文通常够读，又不至于一次吃掉窗口 |
| 上限 | 20000 | 超过即 422，防止 agent 用它绕过粒度约束 |
| 返回必须自述 | `truncated` / `total_chars` / `max_chars` | 静默截断会让 agent 以为读到了全文 |
| 标注不可信 | `untrusted: true` | 正文是远程 HTML，AGENTS.md 要求不得当指令 |
| 闸门照旧 | `pending`/不存在 → 404 错误 envelope | 与 Resource、HTTP 三条路径一致 |

Resource **保留**，不删。工具描述里写明"优先用 Resource"，让支持的客户端仍走
附件路径。

原规则改写为：**不得批量返回正文**；单条、显式、带截断的获取允许。

## 后果

- 上下文经济从"结构上不可能"降级为"默认便宜、显式可选贵的"。这是本次决定
  的真实代价，写下来而不是淡化。
- 缓解：上限 20000 使最坏情况有界；`truncated` 让 agent 知道自己拿到的是片段。
- 工具面 snapshot 需更新（`tests/contract/mcp_tools.json`）。
- 若将来接入 `ResourceLink`（工具结果只放指针，由客户端决定是否取），
  可考虑收回本工具 —— 那才是协议层真正"不进上下文"的路径。
