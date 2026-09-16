# Read You × Pearss FreshRSS 兼容性核查

- 日期：2026-09-14
- 分支：`fix/readyou-freshrss-compat`
- 对照对象：
  - 客户端：`Ashinch/ReadYou` @ `d2b979c`
    - `infrastructure/rss/provider/greader/GoogleReaderAPI.kt`
    - `infrastructure/rss/provider/greader/GoogleReaderDTO.kt`
    - `domain/service/GoogleReaderRssService.kt`
  - 服务端：`src/app/reader_routes.py`、`src/adapters/reader_api.py`

核查方式：读对端源码取出**它实际发出的请求**，再对一个已迁移的测试实例逐条回放
（临时探针，已删除），以响应为准，不靠文档或推断。这与 ADR 0005/0006 两轮教训
一致——对外契约以对端实现为准。

## 结论摘要

Read You 与 Pearss 的问题分三类：**完全缺失的端点**（功能直接不可用）、
**语义被忽略的参数**（能连上但状态同步是错的）、**约定差异**（能跑但有偏差）。
PR #1302 之后的 Read You 对 greader 的用法已经比上次（ADR 0006）更广，
它不再只用 NNW 那 8 个端点。

| # | 问题 | 严重度 | 现象 |
|---|---|---|---|
| 1 | `/reader/api/0/stream/items/ids` 忽略 `it=`（include tag） | **高** | 「已读」同步彻底失效，且是静默的 |
| 2 | `/reader/api/0/subscription/quickadd` 不存在（404） | **高** | 无法在 App 内添加订阅 |
| 3 | `/reader/api/0/rename-tag` 不存在（404） | 中 | 重命名分组失败 |
| 4 | `/reader/api/0/disable-tag` 不存在（404） | 中 | 删除分组失败 |
| 5 | `subscription/edit` 只认 `ac=edit`，拒绝 `unsubscribe` | 中 | 无法在 App 内退订 |
| 6 | `ac=edit` 带 `t=`（重命名 feed）时静默 OK 但不生效 | 低 | 重命名订阅源无效 |
| 7 | `addGroup`：`ac=edit` 只有 `a=` 没有 `s=` 时是 no-op | 低 | 新建空分组不落库 |

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

### 影响链（为什么判「高」但**不是**立即的数据损坏）

Read You 的同步用三个远程集合做集合运算（`GoogleReaderRssService.kt:270-390`）：

| 集合 | 查询 | 结果 |
|---|---|---|
| `remoteUnreadIds` | `s=reading-list&xt=read` | ✅ 正确 |
| `remoteStarredIds` | `s=starred` | ✅ 正确 |
| `remoteReadIds` | `s=reading-list&it=read` | ❌ **等于全集** |

两个在用的消费点：

- `toBeReadLocal = remoteReadIds ∩ localUnreadIds`（`:349-355`）
  → 本机未读 ⊂ 全集，**恒等于 `localUnreadIds`**，即把本机所有未读标记为已读。
  这与「服务端确实全读了」的期望不同，但**恰好**与 `remoteUnreadIds` 一致时无害；
  不一致时会把本机未读误标为已读（下一轮 unread 查询又会纠正回来）。
- `toBeUnreadLocal = localReadIds ∩ remoteUnreadIds`（`:361-368`）→ 与本次 bug 无关。

另外两处**依赖 `remoteReadIds` 的回写逻辑在当前 revision 被注释掉了**
（`:315-323` 的 `toBeStarredRemote`、`:379-387` 的 `toBeReadRemote`）。

所以当前版本的**可观测**后果是：

1. **语义被静默忽略**：客户端以为拿到的是「已读集合」，实际是全集。
2. **每轮同步多拉全库 id**（在 `n=1000` 上限内），条目多时浪费明显。
3. **`n=1000` 截断**：全库 > 1000 时 `remoteReadIds` 不完整，集合运算开始出错。
4. **潜在的数据损坏**：一旦上游取消注释那两段回写分支，错误的
   `toBeReadRemote = localReadIds ∩ remoteUnreadIds`（注意它用的是 unread）
   与 `remoteReadIds` 语义混用会立刻产生错误的回写。

判「高」的理由是**语义静默错误 + 位于活跃请求路径 + 有明确的升级为数据损坏的路径**，
而不是「现在就丢数据」。

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

### 修复方向

新增 `POST/GET /reader/api/0/subscription/quickadd`：
- 解析 `quickadd`（query 或 form，Read You 两处都发）
- 复用现有「源发现」逻辑（P3 `discover_feed` / RFC 探测，A5 待做）
- 返回 `{numResults, query, streamId: "feed/<id>", streamName}`
- 必须同时兼容 `output=json`（Read You 总是带）

注意：这与 ADR 0004 的「只读+已读回写」范围外，是新增写能力；**需要新 ADR 记录**，
且要遵守 ADR 0021 与审计/幂等约束。

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
    raise HTTPException(400, f"unsupported subscription/edit action {ac!r}; only 'edit'")
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

## 下一步

修复范围（#1–#4）触及**新增写端点**（quickadd/unsubscribe/rename-tag/disable-tag），
超出 ADR 0004 的只读+已读回写范围，**需要新 ADR**。建议：

1. 先修 #1（纯读语义补全，无需新 ADR，风险最低、影响最大）
2. #2–#4 合并为一个新 ADR「Read You 订阅管理的写端点」，明确每条的语义与
   与 ADR 0021 的关系
3. #5 维持现状 + 文档记录
4. #6/#7 文档记录
5. 全部按 behavior-first：先写 e2e（回放 Read You 的真实请求形状），再改实现
