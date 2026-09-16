# 排障记录：NNW 标记的已读传不到 Read You

- 日期：2026-09-16
- 现象（用户报告）：**在 NetNewsWire 里读到一些文章 → 刷新同步；Read You 里这些
  文章**没有**变成已读。反向（Read You 标记 → NNW 可见）正常。**
- 结论：**Read You 客户端的全局同步逻辑有缺陷，但它之所以被触发，是因为
  我们少实现了一个协议能力（`continuation`）。服务端已补上，问题消除。**
- 状态：**已修复**。修复为 `stream/items/ids` 实现 `continuation` 游标；
  回归测试 `test_a_read_item_marked_beyond_the_first_page_still_propagates`
  （1200 条规模，标记最早一条，验证其出现在已读集合里）。

证据档位：以下"实测"均为 A 档（对一个已迁移实例回放对端真实请求序列）。

---

## 结论摘要（先看这个）

| 层 | 状态 |
|---|---|
| 服务端的已读**存储** | ✅ 一直是对的 |
| 服务端的 `it=` 已读**查询** | ✅ 正确（ADR 0024 已修） |
| 服务端的**翻页** | ❌ 恒返回 `continuation: null` → **已修复** |
| Read You 的全局 sync | ⚠️ 依赖翻页；有游标后不再漏 |
| Read You 的 per-feed sync | ✅ 本来就对（自洽推导，不依赖翻页） |


## 一、先证明服务端是对的

回放完整跨客户端序列（临时探针，已删）：

```
all ids: 3
NNW edit-tag: 200 'OK'                       ← NNW 写入已读，成功

Read You 全局 sync 会发三个查询：
2a unread  s=reading-list&xt=read   -> 2 条（正确排除了已读的那条）
2b starred s=starred                -> []
2c read    s=reading-list&it=read&ot=<一个月前>  -> 1 条 [590966536867573008]
                                                      ↑ 正是 NNW 标记的那条

结论：服务端三条查询全部正确。已读状态确实存在于服务端，
      且 Read You 的"已读集合"查询如实返回了它。
```

补充验证 `ot` 的影响（因为 Read You 每次都传 `ot=<一个月前>`）：

```
read with ot=<lastMonth>   -> ['590966536867573008']
read with ot=<far future>  -> ['590966536867573008']
两者相同 ⇒ ot 被忽略（我们文档里已记录）。方向是"放宽"，对本问题无害。
```

**所以不是"服务端没记住已读"，也不是"已读查询漏了"** —— 两个曾经的真 bug
（ADR 0024 修的 `it=` 与 `xt` 截断）都已修复，当前服务端这条链路是对的。

---

## 二、问题在 Read You 的全局 sync：集合时序

Read You 的 `sync()`（`GoogleReaderRssService.kt:270-500`）关键顺序：

```kotlin
// ① 拉三个远程集合（正确）
val remoteUnreadIds = async { getUnreadItemIds() }        // s=reading-list & xt=read
val remoteStarredIds = async { getStarredItemIds() }
val remoteReadIds = async { getReadItemIds(useIt = isFreshRss) }  // it=read & ot=

// ② 快照本地状态 —— 在插入任何新条目之前
val localAllItems = articleDao.queryMetadataAll(accountId)      // :305
val localUnreadIds = localAllItems.filter { it.isUnread }...    // :306

// ③ 并行：把"远程有、本地没有"的条目拉正文并插入
val toBeSync = (remoteUnread ∪ remoteStarred ∪ remoteRead) - localItemIds  // :426
fetchItemsContentsDeferred(toBeSync, unreadIds = remoteUnreadIds)          // :435
//   ↑ 插入时 isUnread = unreadIds.contains(articleId)  (:728)  —— 正确

// ④ 并行：把本地未读、但远程已读的翻成已读
val toBeReadLocal = remoteReadIds ∩ localUnreadIds              // :350
articleDao.markAsReadByIdSet(toBeReadLocal, isUnread = false)
```

### 关键缺陷在 ② 与 ④ 的配合

`localUnreadIds` 是**步骤 ② 的快照**，而条目是在**步骤 ③**才插入库。
两者并行（各自的 `launch`），且 ④ 用的是 ② 的快照。

于是存在这条路径：

1. 某条目在 NNW 被标为已读，Read You **本地还没有这条**（它还没同步过这条）
2. Read You 全局 sync：
   - ④ 的 `toBeReadLocal = remoteReadIds ∩ localUnreadIds`：该条目**不在
     `localUnreadIds`**（本地没这条，快照里自然没有）→ 交集为空 → **不处理**
3. 该条目**本应**由 ③ 插入，且 `isUnread = unreadIds.contains(id)` 会把它
   正确插成**已读**……

所以单看 ③，新条目应该是对的。**问题出在 ③ 的输入**：

```kotlin
val toBeSync = (remoteUnread ∪ remoteStarred ∪ remoteRead) - localItemIds
```

`remoteRead` 受 `n=1000` 上限约束（`MAXIMUM_ITEMS_LIMIT = "1000"`，且
`fetchItemIdsAndContinue` 靠 `continuation` 翻页——**我们返回的 `continuation`
恒为 `null`**，即一次只给 1000 条）。

`n` 的作用位置在**已读集合上**：`s=reading-list&it=read&n=1000` 取"最新 1000 条
已读"。NNW 读过一批**较新**的文章后：

- 这 1000 条已读里混着很多**早已读过**的旧条目，把窗口占满
- 你要同步的那条（若已读量大）可能**落在 1000 之外**
- 于是它既不在 `remoteRead`（被截断），也可能不在 `remoteUnread`（它已读）
  → **③ 不会插入它，④ 也无从翻它，两个集合都漏掉它**

### 为什么"反向可以"

Read You → NNW 方向走的是 `edit-tag` 写入，是我们处理正确、且**不经过
1000 条窗口**的路径（`edit-tag` 按 id 列表逐个写）。所以反向正常。

### 为什么 per-feed 同步也不受影响

`syncFeed()`（:600-640）用的是**自洽**的推导：

```kotlin
val remoteReadIds = remoteAllIds - remoteUnreadIds
```

不依赖 `it=`，也不受"已读集合被 1000 条截断"影响（两个集合同一窗口内相减，
差集内部一致）。所以**单源同步能正确传播已读，全局同步不能** —— 与你观察到的
"有时对、有时不对"一致。

---

## 三、这是客户端 bug，但有一个服务端缓解点

**根因在 Read You**：它的全局 sync 用"远程已读集合"与"本地未读快照"求交，
且把已读集合限制在最新 1000 条。任何服务端（包括 FreshRSS、miniflux）只要
已读量超过 `n`，都会触发同样的漏同步。

**但我们的响应里有一处可以直接缓解**：`continuation` 恒为 `null`。

```python
return {
    "itemRefs": [...],
    "continuation": None,  # ← 永远没有下一页
}
```

Read You 的 `fetchItemIdsAndContinue` 是**标准的翻页循环**：

```kotlin
while (result != null && result.continuation != null) {
    result = getItemIdsFunc(result.continuation)
    ids.addAll(...)
}
```

**它准备好了翻页，只是我们从不给 `continuation`。** 如果我们按 `c` 正确实现
游标（返回 `continuation` 直到取完），Read You 会把**全部**已读 id 拉完，
上面那个"1000 条之外的条目被漏掉"就**不会发生**。

miniflux 正是这么做的：`continuation` 是"数字 offset 编码成 JSON 字符串"，
不是不透明 token（其 README 明确说明）。

### 相关：miniflux README 的这一句

> if `n` is omitted, or is above 10000 or non-positive, 10000 items are returned
> at most — clients must follow `continuation` to retrieve the remaining items

即**翻页是协议的一部分**，不是可选优化。我们不返回 `continuation`，
等于声明"永远只有一页"，这在条目多时是**错误的答案**（与 `xt` 截断同类：
返回超集/子集而非报错，静默）。

---

## 四、修复（已实施）

采用方案 A：`stream/items/ids` 现在返回真正的 `continuation` 游标。

### 实现

`src/app/reader_routes.py`：

```python
offset = _offset(c)
limit = max(1, min(n, 10000))
ids = visible_ids_for_stream(..., limit=limit + 1, offset=offset)
#                          ^^^^^^^^^^^ 多取一条，用它的存在判断"还有更多"

has_more = len(ids) > limit and _more_than(c)
page = ids[:limit]
return {
    "itemRefs": [...],
    "continuation": str(offset + len(page)) if has_more else None,
}
```

要点：

- **多取一条**判断有无下一页，而不是再跑一次 COUNT。COUNT 必须重复应用
  全部过滤条件，而"同一条规则写两遍"就是规则漂移的来源。
- **游标是数字 offset**，与 miniflux 同形（`len(itemRefs) + offset`）；
  其 README 明确说这不是不透明 token。
- **深度上限必须终止，而不是夹紧**。原来的 `_offset` 把超限游标夹到
  `_MAX_OFFSET`；若照此继续返回游标，客户端会**永远请求同一个 offset**
  （`while continuation != null` 死循环）。新增 `_more_than(c)`：
  游标已达上限时回答"没有更多"，让遍历结束。
- **解析不了的游标回到开头**，不报错：游标由我们生成、客户端只是回传，
  非法值要么是我们的 bug 要么是手写请求，两者都不会因为 500 变好。

### 验证（A 档，实测）

**1. 小规模契约**（保留为常驻测试，20 个场景）：
- 分页取全：`page_size ∈ {1,2,3,4}` 都能走完全部条目，且无重复
- 最后一页不带游标（防无限翻页）
- 过滤流翻页：`it=read` 每页都过滤，游标仍终止
- 深度上限终止 / 非法游标回退

**2. 真实规模回归**（1200 条，保留为常驻测试）：

```
total walked: 1203 in 2 pages        ← 修复前只有 1 页、1000 条
marked oldest read: 200
read set walked: contains target? True
toBeReadLocal = remoteRead ∩ localUnread = 2 -> [两个被标记的条目]
```

即：**一个位于"最新 1000 条"之外的条目，现在能正常传播到客户端。**
这正是用户报告的场景。

**3. 修复前对照**：同一探针在修复前，`read set` 被截断在 1000 条，
目标条目不在其中，`toBeReadLocal` 漏掉它。

### 一个诚实的限制

此修复让**今后**不再漏，但**不会自动补回**客户端本地已经漏掉的已读状态。
Read You 若已经同步过并标记为未读，需要它重新拉全（例如清数据重同步），
或等这些条目因其他原因重新进入同步窗口。

---

## 五、教训

- **"反向可以"是决定性线索**：它把范围从"状态没存对"缩小到"某一条读取路径"，
  并排除了服务端存储问题。方向性症状要先当约束用，不要急着全链路排查。
- **同一客户端的不同代码路径可以有不同结论**：Read You 的 `syncFeed` 正确、
  `sync` 错误。只看一处会得出相反结论。
- **返回"没有下一页"是一个肯定性断言**，和返回错误集合一样可以是错的。
- **"客户端 bug"不等于"服务端无责"**：如果协议能力本身没实现，
  客户端依赖它就是一个坐在那里等着被触发的缺陷。判断"是谁的错"和
  判断"该不该修"是两个问题。
- **上限夹紧（clamp）在分页里等于无限循环**：任何"超出范围就取边界值"的
  保护，在"客户端按游标循环"的语境下都必须改成"到边界就终止"。
