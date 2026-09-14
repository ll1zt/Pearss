# 0008 · source.canonical_url 必须 NOT NULL

- 状态：accepted
- 日期：2026-09-04

## 背景

`source` 的唯一约束是 `uq_source_url (tenant_id, canonical_url, type)`，意图是"同一租户内同一 URL 同一类型只能有一个源"。

但 `canonical_url` 建表时可空。SQL 标准里 **NULL 彼此不相等**，所以：

```sql
-- 两条都插入成功，约束完全不拦截
insert into source (id, url, canonical_url, type) values ('a', 'https://x/f', NULL, 'rss');
insert into source (id, url, canonical_url, type) values ('b', 'https://x/f', NULL, 'rss');
```

实测确认过这个行为。也就是说：**把去重键放在可空列上，等于没有约束** —— 而且不是"约束弱一点"，是对这些行彻底失明。

## 触发方式

这个洞不是理论推出来的，是做出来的。为验证 Reader API 而用裸 SQL 手灌的两个源，`url` 和 `canonical_url` 都是 NULL —— 它们能通过，恰恰证明约束没在工作。而这两行后来导致 `:trigger` 必然失败（没有 URL 可抓）。

AGENTS.md 的原则是「靠工具不靠自觉」。应用层 `create_source()` 确实会正确填 `canonical_url`，但唯一约束存在的意义正是兜住绕过应用层的写入 —— 兜不住就是失职。

## 决策

`source.canonical_url` 改为 **NOT NULL**（migration 0004）。

各类型的键：

| type | canonical_url |
|---|---|
| `rss` | 规范化后的 feed URL |
| `web` | 规范化后的页面 URL |
| `agent` | 合成键 `agent://<name>` |

agent 源是被推送而非轮询的，本没有 URL；但它的键**仍不能为空** —— 否则又回到约束看不见这些行的状态。所以给它一个每行唯一、且明显是合成的键。

## migration 回填而非删除

这是本 ADR 的第二条决定，也是更重要的那条。

把可空列改 NOT NULL 时，存量 NULL 行必须被处理。删除是最直接的做法，但：

```
source ──CASCADE──> item ──CASCADE──> read_state
```

删一个源会连带删掉它的所有条目和已读状态。而这些条目可能**已经发布** —— GUID 冻结、客户端已缓存，`data-model.md` 与 `netnewswire.md` 都规定不得删除已发布 GUID 后复用。

更关键的是：**migration 是要在任何环境跑的**。一段"升级时删除已发布内容"的逻辑，写进迁移就等于给每一次部署埋了数据摧毁的雷。

> **后续（ADR 0021，2026-09-14）**：级联删除现在是一个**显式的、经用户确认的
> API 动作**（`DELETE /v1/sources/{id}`），不再是迁移行为。本 ADR 的结论因此
> 依然成立且更精确了：不该在**迁移**里删数据，但删除本身是正当的运维需求，
> 由 ADR 0021 以可确认、可审计的形式提供。其中「不得删除已发布 GUID 后复用」
> 被澄清为「不得把该 GUID 复用到不同内容上」——删除行并不违反它。

所以 migration 0004 只做回填，**一行都不删**：

```sql
-- 1. 有 url 的，复制过去
UPDATE source SET canonical_url = url WHERE canonical_url IS NULL AND url IS NOT NULL;
-- 2. 两者都无的，给合成键
UPDATE source SET canonical_url = 'legacy://' || id WHERE canonical_url IS NULL;
-- 3. 再改 NOT NULL
```

第 1 步存的是原始 url 而非规范化结果 —— 因为规范化会剥追踪参数、排序 query，这套逻辑属于应用层；在 migration 里重写一遍，一旦两边有分歧，同一个源会被静默拆成两条。这类行由应用下次写入时自然修正。

那 2 个已知的坏行（我手灌的）作为**一次性运维动作**单独修正，不进 migration。

## 被否决的备选

| 方案 | 否决理由 |
|---|---|
| 保持可空，只在应用层保证 | 数据库不再拦截绕过应用层的写入；而那 2 个坏行正是这类写入的产物。与 AGENTS.md「靠工具不靠自觉」直接冲突 |
| 新增非空 `dedupe_key` 列承载约束 | 约束能生效，但两列表达同一件事，容易不同步；`canonical_url` 仍会是那个"看起来是键但没人保证"的列 |
| migration 里删除无法解析的行 | 级联删除已发布条目与已读状态；且该逻辑在任何环境重跑都会掉数据 |

## 后果

- 唯一约束对每一行真正生效，直接 SQL 写入也无法产出重复源
- 三个测试钉住：`canonical_url` 不可为空、重复 `canonical_url` 被拒、0004 回填不删行
- `legacy://` 前缀让历史坏行在数据里**可见**，而不是被静默抹平 —— 能查出来才能修
- agent 源必须提供名字，否则 `source_canonical_key` 拒绝；未知 `source_type` 同样拒绝（否则会静默获得 rss 的 URL 语义）

## 可逆性

downgrade 只放开 NOT NULL，**保留回填值**：回退一个约束不该重写数据，而 `legacy://` 键比 NULL 更好（至少仍在约束之内）。
