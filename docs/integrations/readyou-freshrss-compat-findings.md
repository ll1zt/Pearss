# Read You × Pearss FreshRSS 兼容性核查

- 日期：2026-09-14
- 分支：`fix/readyou-freshrss-compat`
- **状态：9 项全部处置完毕**（#1–#4 已修；#5 决定不实现；#6/#7 记录为有意缺口；
  #8/#9 随 #1/#2 消除）。决策落在 ADR 0024，行为测试在
  `tests/integration/test_readyou_compat.py`。本文保留**核查过程与判定依据**，
  它是"为什么这么修"的叙述记录，与 ADR 的可执行结论互补。
- 对照对象：
  - 客户端：`Ashinch/ReadYou` @ `d2b979c`
    - `infrastructure/rss/provider/greader/GoogleReaderAPI.kt`
    - `infrastructure/rss/provider/greader/GoogleReaderDTO.kt`
    - `domain/service/GoogleReaderRssService.kt`
  - 服务端：`src/app/reader_routes.py`、`src/adapters/reader_api.py`

核查方式：读对端源码取出**它实际发出的请求**，再对一个已迁移的测试实例逐条回放
（临时探针，已删除），以响应为准，不靠文档或推断。这与 ADR 0005/0006 两轮教训
一致——对外契约以对端实现为准。

结论与修复状态见下表，其余各节保留判定依据。**实测数据均为 A 档证据**
（受控回放 + 可复现），标「未确证」处只有机制推演。

| # | 问题 | 状态 | 处置 |
|---|---|---|---|
| 1 | `it=` include tag 被忽略 | ✅ 已修 | 下推 SQL，与 `xt` 对称 |
| 2 | `subscription/quickadd` 404 | ✅ 已实现 | 接收已解析的 feed URL |
| 3 | `rename-tag` 404 | ✅ 已实现 | 批量改 `source.folder` |
| 4 | `disable-tag` 404 | ✅ 已实现 | 组内源回 `default` |
| 5 | `ac=unsubscribe` 被拒 | ✅ 已实现（2026-09-16 修订） | 原「决定不实现」被推翻——拒绝把删除意图困进重试循环；现走 ADR 0021 真删，见 ADR 0024 修订节 |
| 6 | `t=` 重命名静默 OK | ✅ 已实现（2026-09-16 修订） | 原「会被覆盖」论断有误：`set_source_title_if_absent` 条件写入本就防覆盖 |
| 7 | 空分组不落库 | 📄 记录为缺口 | folder 是派生值，无实体可存 |
| 8 | `xt` 在 `LIMIT` 之后过滤 | ✅ 已修 | 与 #1 同一下推 |
| 9 | 缺失端点放大成无限重试 | ✅ 随 #2–#4 消除 | — |


## 我们对照的到底是哪份契约？

**是 Google Reader API，但必须带上 FreshRSS 的差异。** 这不是一个可选的措辞问题，
搞错会修错地方。

Read You 内部有 6 种账号类型（`AccountType.kt`），其中：

```kotlin
// RssService.kt:39-40
AccountType.GoogleReader.id -> googleReaderRssService
AccountType.FreshRSS.id     -> googleReaderRssService   // ← 同一个实现
```

两者**共用同一个 `GoogleReaderRssService`**，全部请求都由 `GoogleReaderAPI.kt`
发出。FreshRSS 类型与 GoogleReader 类型的差别，在客户端整个代码库里只有**一处**：

```kotlin
// GoogleReaderRssService.kt:284
val isFreshRss = account.type.id == FreshRSS.id
// GoogleReaderAPI.kt:305 -- useIt 决定用 `s=` 还是 `it=`
if (useIt) add("s" to Stream.AllItems.tag)
add(Pair(if (useIt) "it" else "s", Stream.Read.tag))
```

所以准确的表述是：

> Read You 说的是 **Google Reader 协议**，只是对 FreshRSS 后端打了一个
> `it=` 的补丁（因为 FreshRSS 不把 `user/-/state/com.google/read` 当作
> 可查询的 `s=` 流，而是把它当 include tag）。

**我们实现的是 Google Reader 的子集**（ADR 0004 明确写了 scope「derived from
the maintained reference implementation documented in miniflux's
internal/googlereader README, not a full Google Reader reissue」）。
按这个口径，"全面支持"= **把 Read You 实际会发的 Google Reader 请求全部覆盖**，
其中包含 FreshRSS 那个 `it=` 补丁。

需要澄清一个常见误解：**FreshRSS 官方 API 是另一套东西**（`/api/fever.php` 与
`/api/greader.php` 下的 FreshRSS 自有扩展），Read You 并没有用它的自有扩展，
走的是标准 greader 路径。所以"支持 FreshRSS"在这里 == "支持 greader +
`it=` 语义"，不需要去实现 FreshRSS 的私有接口。

## 结论摘要

Read You 与 Pearss 的问题分三类：**完全缺失的端点**（功能直接不可用）、
**语义被忽略的参数**（能连上但状态同步是错的，且会自激振荡）、
**约定差异**（能跑但有偏差）。
PR #1302 之后的 Read You 对 greader 的用法已经比上次（ADR 0006）更广，
它不再只用 NNW 那 8 个端点。

| # | 问题 | 严重度 | 现象 |
|---|---|---|---|
| 1 | `/reader/api/0/stream/items/ids` 忽略 `it=`（include tag） | **高** | 「已读」同步失效，**并引起未读数震荡（见 #8）** |
| 2 | `/reader/api/0/subscription/quickadd` 不存在（404） | **高** | 无法在 App 内添加订阅，且触发无限重试 |
| 3 | `/reader/api/0/rename-tag` 不存在（404） | 中 | 重命名分组失败 |
| 4 | `/reader/api/0/disable-tag` 不存在（404） | 中 | 删除分组失败 |
| 5 | `subscription/edit` 只认 `ac=edit`，拒绝 `unsubscribe` | 中 | 无法在 App 内退订 |
| 6 | `ac=edit` 带 `t=`（重命名 feed）时静默 OK 但不生效 | 低 | 重命名订阅源无效 |
| 7 | `addGroup`：`ac=edit` 只有 `a=` 没有 `s=` 时是 no-op | 低 | 新建空分组不落库 |
| 8 | `xt=` 在 `n=` 截断**之后**才在 Python 里过滤 | **高** | 未读列表被截断，未读数偏低且随刷新跳变 |
| 9 | `subscription/quickadd` 之外的 405/404 让 sync 整体 `retry()` | 中 | 失败被放大成无限重试 |

---

## 1（高）`it=` include tag 被忽略 —— 已读状态同步错误

### 对端行为

`GoogleReaderAPI.getReadItemIds`（`GoogleReaderAPI.kt:295-312`）：

```kotlin
params = mutableListOf<Pair<String,String>>().apply {
    if (useIt) add("s" to Stream.AllItems.tag)          // s=reading-list
    add(Pair(if (useIt) "it" else "s", Stream.Read.tag)) // it=read  ← FreshRSS 走这条
    add(Pair("ot", since.toString()))
    ...
}
```

`GoogleReaderRssService.sync` 里：

```kotlin
val isFreshRss = account.type.id == FreshRSS.id
val remoteReadIds = async {
    fetchItemIdsAndContinue {
        googleReaderAPI.getReadItemIds(since = lastMonthAt, continuationId = it, useIt = isFreshRss)
    }.map { it.shortId }.toSet()
}
```

也就是说 **FreshRSS 账号类型下，Read You 拉「已读列表」用的是
`s=reading-list` + `it=user/-/state/com.google/read`**，而不是
`s=user/-/state/com.google/read`。

### Pearss 行为

`reader_routes.py:367-405` 只接受 `s / n / xt / c`。`it` 未被声明，FastAPI
直接忽略。`s=reading-list` 走 `visible_ids_for_stream` 的 reading-list 分支
（`repository/reader.py:52`）——**返回全部可见条目**。

### 实测

```
baseline all: 3
unread (s= + xt=read): 2   -> 正确
read   (s= + it=read): 3   -> 应为 1，实际 3   ← BUG
```

### 影响链：这是一个自激振荡，不是"暂时无害"

Read You 的同步用三个远程集合做集合运算（`GoogleReaderRssService.kt:270-390`）：

| 集合 | 查询 | 结果 |
|---|---|---|
| `remoteUnreadIds` | `s=reading-list&xt=read` | ✅ 正确 |
| `remoteStarredIds` | `s=starred` | ✅ 正确 |
| `remoteReadIds` | `s=reading-list&it=read` | ❌ **等于全集** |

两个在用的消费点：

```kotlin
// :349-355  本机未读 -> 已读
val toBeReadLocal = remoteReadIds.await().intersect(localUnreadIds)
// 因为 remoteReadIds == localItemIds（全集），这个交集
// 恒等于 localUnreadIds —— 于是"把所有本机未读标记为已读"

// :361-368  本机已读 -> 未读（反向，基于正确的 unread 集合）
val toBeUnreadLocal = localReadIds.intersect(remoteUnreadIds.await())
```

两条 `launch` 是**并行**的，谁后提交谁赢。服务端状态不变的情况下，
客户端每轮同步都会先把全部未读读成已读、再被翻回未读，**未读数在 0 和 N 之间
反复跳**。这正是用户报告的"每次刷新未读数在两种情况间变来变去"。

实测（临时探针，已删除；3 条条目全部未读）：

```
total: 3
round1 remote_unread=3  remote_read=3 (应为 0)
  -> client marks 3 items READ locally (of 3 unread)
round2 remote_unread=3; client flips 3 back to UNREAD
```

### 修复方向

在 `stream_items_ids` 增加 `it: list[str]`，语义为「只要带这些 tag 的条目」。
与 `xt`（排除）对称，与 `s` 取交集。`it` 与 `xt` 同时出现时（Read You 不这么发）
按 Google Reader 原语义：先按 `it` 取交集，再按 `xt` 排除。

**这一条同时修掉 #8**（见下），因为两者都在同一个端点、同一段排序/过滤逻辑里。

---

## 8（高）`xt=` 在 `n=` 截断之后才过滤 —— 未读数偏低且随刷新跳变

### 现状

`reader_routes.py:386-399`：

```python
ids = visible_ids_for_stream(
    session,
    stream_id=s,
    tenant_id=TENANT,
    limit=max(1, min(n, 10000)),
    offset=_offset(c),
)
#      ^^^ SQL 里 LIMIT n，只按 published_at 排序，不看已读状态
if not ids:
    return {"itemRefs": []}
exclude_read = any(reader_api.is_state_stream(t, state="read") for t in xt)
if exclude_read:
    states = read_states_for(session, ids, tenant_id=TENANT)
    ids = [i for i in ids if not (states.get(i) and states[i].is_read)]
#      ^^^ 已读过滤在 Python 里，n 已经生效
```

`visible_ids_for_stream` 的 docstring 说「Pending items are excluded here, in SQL,
for the same reason the RSS path excludes them」——**同样的理由对 `xt` 也成立，
但 `xt` 没走 SQL**。

### 后果（实测）

8 条条目，标记最新 2 条已读：

```
n=3    xt=read -> 1 个 id   （真实未读是 6）
n=1000 xt=read -> 6 个 id   （正确）
```

Read You 用 `n=1000`：

- 未读 ≤ 1000 时正确 —— 所以**这不是每天都能看到**
- 未读 > 1000 时，取 1000 条最新、剔除其中已读，返回**少于 1000**，
  客户端把剩下那些算作"不存在的条目"，**未读数偏低**
- 更糟的是与已读率耦合：最新 1000 条里已读越多，返回越少。
  用户一边读、一边刷新，**返回集合随已读分布变化 → 未读数跳变**

### 与 #1 的区别

| | 触发条件 | 表现 |
|---|---|---|
| #1 `it=` 忽略 | 每次同步，立刻 | `remoteReadIds` = 全集 → 未读震荡（0 与 N） |
| #8 `xt` 后置过滤 | 未读 > `n=1000` | 返回少于 `n` → 未读偏低、随已读分布漂移 |

两者都会让未读数看起来"跑来跑去"，但**成因不同、修法不同**，必须分别验证。
#8 的修法是把已读过滤下推到 SQL（`NOT IN (select item_id from read_state
where is_read)`），让 `LIMIT` 作用于**已过滤后**的结果集。

---

## 9（中）缺失端点导致整个 sync 失败并被放大成无限重试

`GoogleReaderRssService.sync` 的整个函数体在一个 `try/catch` 里，catch 末尾：

```kotlin
} catch (e: Exception) {
    Timber.tag("RLog").e(e, "On sync exception: ${e.message}")
    syncLogger.log(e)
    ListenableWorker.Result.retry()      // ← 无限重试
}
```

且 `subscribe()` / `addGroup()` / `deleteGroup()` 里的
`requireNotNull(feedId)`、`requireNotNull(...)` 会直接抛异常。

这意味着 #2（quickadd 404）、#3、#4 不只是"某个按钮不好用"：
`quickAdd.streamId` 为 null → `requireNotNull` 抛 → **整个账号同步失败并重试**，
用户看到的是"一直转圈/一直不同步"，而不是"添加订阅失败"。
这解释了为什么这些问题会表现为"整体不可用"而不是局部功能缺失。

### 修复优先级含义

#2 必须最先修（它污染整个 sync），其次是 #1/#8（状态正确性），
再次 #3/#4。

### 修复方向

在 `stream_items_ids` 增加 `it: list[str]`，语义为「只要带这些 tag 的条目」。
与 `xt`（排除）对称；与 `s` 取交集。同时支持 `t=`（要求全部 tag 都有，
Google Reader 原语义）暂不需要——Read You 只发一个 `it`。

---

## 2（高）`subscription/quickadd` 缺失

Read You 添加订阅的主路径（`GoogleReaderRssService.kt:133-167`）：

```kotlin
val quickAdd = getGoogleReaderAPI().subscriptionQuickAdd(feedLink)
val feedId = quickAdd.streamId?.ofFeedStreamIdToId()
...
subscriptionEdit(destFeedId = feedId, destCategoryId = groupId.dollarLast(), destFeedName = feedTitle)
```

`subscriptionQuickAdd`（`GoogleReaderAPI.kt:395-400`）打
`POST reader/api/0/subscription/quickadd?quickadd=<url>`，
**期望响应**（DTO 注释）：

```json
{"numResults":1,"query":"...","streamId":"feed/10","streamName":"blog"}
```

实测：`404 {"error":{"code":"not_found",...}}`。

`requireNotNull(feedId)` 会抛异常 → 整个 sync 的 try/catch 把结果置为
`ListenableWorker.Result.retry()` → **无限重试**。

### 修复方向（已实现，含一处修正）

新增 `POST /reader/api/0/subscription/quickadd`：
- 解析 `quickadd`（query 或 form，Read You 两处都发）
- 返回 `{numResults, query, streamId: "feed/<id>", streamName}`
- 幂等：复用 `create_source` 的"同 URL 返回既有行"

> **修正**：本节初稿写「复用现有源发现逻辑（A5）」。实现前读
> `RssHelper.searchFeed` 确认这**不成立**——Read You 在客户端完成发现
> （抓取、解析、跟随 alternate link），发来的是**已解析的 feed URL**。
> 所以这里不做发现，**A5 继续暂缓**（ADR 0010 有效）。详见文末「一处判定修正」。

注意：这与 ADR 0004 的「只读+已读回写」范围外，是新增写能力；决策记录在
**ADR 0024**，并遵守其中与 ADR 0021 及审计/幂等相关的约束。

---

## 3 / 4（中）`rename-tag`、`disable-tag` 缺失

```kotlin
suspend fun disableTag(categoryId: String) = retryablePostRequest<String>(
    query = "reader/api/0/disable-tag",
    form = listOf(Pair("s", categoryId.ofCategoryIdToStreamId())))
suspend fun renameTag(categoryId, renameToName) = retryablePostRequest<String>(
    query = "reader/api/0/rename-tag",
    form = listOf(Pair("s", ...), Pair("dest", renameToName.ofCategoryIdToStreamId())))
```

调用点：`renameGroup`（`:181`）、`deleteGroup`（`:209`）。

实测两者均 404。后果：重命名/删除分组在 App 内失败。`deleteGroup` 会先
`feedDao...deleteFeed` 逐个退订（`:210-213`）→ 那些也会因 `ac=unsubscribe`
被拒（见 #5）而失败，然后才轮到 disable-tag 404。

### 修复方向

- `rename-tag`：把 folder 从旧名改到新名（`UPDATE source SET folder=...`），
  复用 `core/folders.normalize_folder`。`dest` 是 `user/-/label/<name>` 形态。
- `disable-tag`：文件夹在 Pearss 不是独立实体（由 source.folder 派生），
  删除分组 = 把该 folder 下所有 source 移回 `default`。
- 两者都要新 ADR（同 #2）。

---

## 5（中）`subscription/edit` 不接受 `ac=unsubscribe`

`deleteFeed`（`:218-222`）：

```kotlin
subscriptionEdit(action = "unsubscribe", destFeedId = feed.id.dollarLast())
```

Pearss `reader_routes.py:708-712` 直接 400：

```python
if ac != "edit":
    raise HTTPException(
        400, f"unsupported subscription/edit action {ac!r}; only 'edit'"
    )
```

`mcp-tool-guide.md:149` 明确写了「**不支持 subscribe/unsubscribe**——增删订阅走
`/v1`」。这是**有意决策**，不是疏漏。

### 但要注意

对 ADR 0021「删除即删除」的语义，`unsubscribe` 若实现，必须是**真删**还是
**停用**？Read You 期望它消失。这需要用户决策：
- 选项 A：维持现状（App 内不能退订，用 MCP/`/v1`），在文档写明这是
  Read You 的功能缺口
- 选项 B：实现 `ac=unsubscribe` → 映射到 `disabled`（保留历史，可恢复）
- 选项 C：实现为真删（与 ADR 0021 一致，但客户端一次误操作即不可逆）

**建议 A**：与既有 ADR 决策一致，且删除是破坏性动作，不适合暴露给一个
每轮同步都会尝试调用的客户端路径。

---

## 6（低）`t=` 重命名静默 OK 但不生效

`renameFeed`（`:203-207`）发 `subscriptionEdit(destFeedId=..., destFeedName=...)`。
Pearss `reader_routes.py:731-733`：无 `a`/`r` 时直接返回 `OK`。

`reader_routes.py:697-702` 的 docstring 已解释这是有意的（标题属于 origin，
下次采集会覆盖）。**判断仍然成立**，但 Read You 会在 UI 上显示新名字、重启后
变回去。属于「诚实的缺口」级别，建议文档记录而非改代码。

---

## 7（低）`addGroup` 的空分组不落库

`addGroup`（`:169-177`）在没有 destFeedId 时发 `ac=edit&a=user/-/label/NewGroup`。
Pearss `reader_routes.py:739-741`：`feed_ids` 为空 → 直接 `OK`，不落库。

因为 Pearss 的 folder 由 source 派生（`tag_list` 亦然），**空分组无处可存**。
这是数据模型差异，非 bug。UI 上会出现「新建了分组但刷新后消失」。

建议：文档记录。若确需支持，需要一个 folder 实体（新 ADR + migration），
成本高、收益低。

---

## 附：核查通过、无需改动的部分

| 项 | 状态 |
|---|---|
| `ClientLogin`（form 字段 + `output=json` 两种形态） | ✅ 均 200 |
| `GET /token`（含 `?output=json`） | ✅ 200，纯文本 token |
| `user-info` | ✅ 200 |
| `subscription/list`（`categories[0].id/label`、`url`、`iconUrl`） | ✅ 结构匹配 |
| `tag/list` | ✅ 返回 starred + folders |
| `contents` 响应的长式 id **零填充 16 位** | ✅ `isLongId` 通过（ADR 0006 修订已生效） |
| `contents` 的 `categories` 含 reading-list/read/starred | ✅ |
| `edit-tag`（header + T 双发、裸十进制 id） | ✅ 200 OK |
| `mark-all-as-read` | ✅ 200 OK |
| `xt=` 排除已读 | ✅ 正确 |
| `continuation` 字段存在（Read You 循环终止依赖它） | ✅ 返回 null |
| feed stream `s=feed/<id>` | ✅ 正确 |
| label stream `s=user/-/label/<name>` | ✅ 正确 |
| 401 时 `X-Reader-Google-Bad-Token` | ✅ 已实现，Read You 会 clearAuthData 重认证 |

---

## 全量支持范围

用户要求"全面支持"。按上面「我们对照的是哪份契约」一节，范围 =
**Read You 实际会发出的全部 greader 请求**。下表是它在 `GoogleReaderAPI.kt`
里定义的所有调用，与**修复前**的现状一一对应（保留为基线快照，
当前状态看文首的结论表）：

| Read You 调用 | 端点 | 我们 | 待办 |
|---|---|---|---|
| `reAuthenticate` | `POST accounts/ClientLogin` | ✅ | — |
| `reAuthenticate` | `GET reader/api/0/token` | ✅ | — |
| `getUserInfo` | `GET reader/api/0/user-info` | ✅ | — |
| `getSubscriptionList` | `GET subscription/list` | ✅ | — |
| `getReadItemIds` | `GET stream/items/ids`（`s`+**`it`**+`ot`） | ⚠️ 忽略 `it`/`ot` | **#1** |
| `getItemIdsForFeed` | `GET stream/items/ids`（`s=feed/x`+`xt`） | ✅ | — |
| `getItemIdsForCategory` | `GET stream/items/ids`（`s=label`） | ✅ | — |
| `getUnreadItemIds` | `GET stream/items/ids`（`xt`） | ⚠️ `n` 后置过滤 | **#8** |
| `getStarredItemIds` | `GET stream/items/ids`（`s=starred`） | ✅ | — |
| `getItemsContents` | `POST stream/items/contents` | ✅ | — |
| `subscriptionQuickAdd` | `POST subscription/quickadd` | ❌ 404 | **#2** |
| `editTag` | `POST edit-tag` | ✅ | — |
| `disableTag` | `POST disable-tag` | ❌ 404 | **#4** |
| `renameTag` | `POST rename-tag` | ❌ 404 | **#3** |
| `subscriptionEdit(ac=edit)` | `POST subscription/edit` | ⚠️ 部分 | #6 #7 |
| `subscriptionEdit(ac=unsubscribe)` | `POST subscription/edit` | ❌ 400（有意） | **#5** |
| `markAllAsRead` | `POST mark-all-as-read` | ✅ | — |
| （未用）`tag/list` | `GET tag/list` | ✅ | — |

结论：**18 个调用点里 11 个已经正确，7 个有问题**。其中
「能连上、列表正常、正文正常」这部分是对的，问题集中在
**状态同步正确性**（#1 #8）和**订阅管理写路径**（#2–#5）。

## 实际执行结果

按优先级全部完成，三次提交：

| 提交 | 内容 |
|---|---|
| `9ca5cb9` | #1 + #8：`it=` 支持与 `xt` 下推 SQL（含夹具移入 conftest） |
| `c641a5a` | #2 + #3 + #4：三个订阅管理写端点 |
| 本次 | ADR 0024 + 文档同步（本文件、netnewswire、mcp-tool-guide、README） |

行为测试：`tests/integration/test_readyou_compat.py`（13 个场景），
全部回放 Read You 的真实请求形状，先失败后实现。

### 一处判定修正（记录，因为它是本轮最有价值的发现）

计划阶段我判断 quickadd「需要源发现能力，正好顺带解决 A5」。**这个判断是错的**，
实现前读 `RssHelper.searchFeed` 才确认：Read You **在客户端完成发现** ——
抓地址、解析、跟随 `<link rel=alternate>`，然后把**已解析的 feed URL** 发来。

所以 quickadd 不需要服务端发现，**A5 的暂缓决定完全不受影响**（ADR 0010 继续有效）。
若按原判断去实现服务端发现，就会在不知情的情况下复活一个被用户明确暂缓的功能，
并写一个客户端根本不会调用的代码路径。这正是"读对端源码而不是推测"的价值。

### #5 的处置

采用原建议的 A 方案（不实现 `ac=unsubscribe`），理由已写入 ADR 0024 决策 5 与
`subscription_edit` 的 docstring。核心三点叠加：**不可逆**（ADR 0021 物理删除）、
**用户不可见**（发生在同步过程中）、**失败即重试**（不是对话框）。

### 已知缺口（有意保留）

三项记录在 ADR 0024 与 mcp-tool-guide，不改代码：

1. ~~App 内不能退订（#5）~~ 已实现（2026-09-16）
2. ~~`t=` 重命名订阅源不生效（#6）~~ 已实现（2026-09-16）
3. 新建空分组刷新后消失（#7）—— folder 是派生值，无实体可存（维持）

