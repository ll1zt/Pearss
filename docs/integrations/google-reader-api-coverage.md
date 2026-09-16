# Google Reader API 覆盖度对照

- 日期：2026-09-14
- 目的：回答「距离完整的 Google Reader API 实现还差什么」
- 参照实现：`miniflux/v2` 的 `internal/googlereader`（ADR 0005 引用的同一份参考）
- 我们：`src/app/reader_routes.py`（13 个端点）、`src/adapters/reader_api.py`

## 先说结论：端点齐了，差在参数与语义

miniflux 注册的端点（`handler.go:49-63`）与我们**完全一致，13 个**：

```
POST /accounts/ClientLogin
GET  /reader/api/0/token
POST /reader/api/0/edit-tag
POST /reader/api/0/rename-tag
POST /reader/api/0/disable-tag
GET  /reader/api/0/tag/list
GET  /reader/api/0/user-info
GET  /reader/api/0/subscription/list
POST /reader/api/0/subscription/edit
POST /reader/api/0/subscription/quickadd
GET  /reader/api/0/stream/items/ids
POST /reader/api/0/stream/items/contents
POST /reader/api/0/mark-all-as-read
```

所以差距**不在端点数量**，而在：
1. `stream/items/ids` 的**参数**少 3 个（`ot` / `nt` / `r`）
2. `subscription/edit` 的 `ac=subscribe` 未实现
3. 若干**响应格式**与错误处理的偏差

以下逐条给出「事实—我们—影响」，实测数据标 A 档。

---

## 一、`stream/items/ids` 缺少的参数

miniflux 从 `request_modifier.go` 读这些（`parameters.go` 是名字常量）：

| 参数 | 含义 | 我们 |
|---|---|---|
| `s` | 流 | ✅ |
| `n` | 上限（cap 10000） | ✅ |
| `c` | 续页 token（数字 offset） | ✅ |
| `xt` | 排除 target | ✅ |
| `it` | 包含 target | ✅（本轮新增） |
| **`ot`** | **只返回此 Unix 秒之后发布的** | ❌ 忽略 |
| **`nt`** | **只返回此 Unix 秒之前发布的** | ❌ 忽略 |
| **`r`** | **排序方向：`o`=升序，其他=降序** | ❌ 忽略（恒降序） |

### 实测（A 档，探针已删）

```
baseline: 3 条
ot=<远未来> -> 200 3 ids   （正确应为 0）
nt=1        -> 200 3 ids   （正确应为 0）
r=o         -> 200 3 ids，顺序与默认相同（正确应反转）
```

### 影响

- **`ot` 是静默的**：客户端以为限制了时间窗，实际拿到全部。Read You 的
  `getReadItemIds(since=一个月前)` **每次都带 `ot`**（`GoogleReaderAPI.kt:309`
  的 `add(Pair("ot", since.toString()))`），我们忽略它。
- **`r=o` 影响分页正确性**：客户端按升序翻页时，我们给降序，
  第 2 页会取到完全不同的集合。

### `ot` 的实际影响是"有界偏差"而非数据损坏（重要修正）

修好 `it=` 之后，`remoteReadIds` 已经是**正确的已读集合**，只是**时间窗
没有生效**——包含了比"最近一个月"更早的已读条目。而它的消费点是：

```kotlin
val toBeReadLocal = remoteReadIds.intersect(localUnreadIds)   // :350
```

与**本机未读**求交。比一个月更早的已读条目**不会是本机未读**，所以交集
结果**不受影响**。因此：

- 当前**不产生错误的已读状态**（对比 `it=` 那个 bug —— 那个直接把全集求交，
  导致全部未读被标已读）
- 实际代价是**每轮多传数据**：`n=1000` 上限内尽可能多地传已读 id，
  已读量大时挤占分页，使真正需要的"最近一个月"部分可能被 `n` 截断

严重度：**中**（性能与分页边界，非正确性）。这与我起初的"高"判断不同，
写在这里是因为**当初判"高"凭的是"参数被忽略"这个模式，而没有顺着消费点
验算**——同一模式在不同调用点后果可以差别很大。

`nt` 未确证有客户端使用；`r=o` 取决于客户端是否提供升序视图。

---

## 二、`subscription/edit` 的 `ac=subscribe` 未实现

miniflux 支持三个 action（`handler.go`）：

| action | `s` 的格式 | 说明 | 我们 |
|---|---|---|---|
| `ac=subscribe` | `feed/<绝对 URL>` | 新建订阅 | ❌ 400 |
| `ac=unsubscribe` | `feed/<数字 id>` | 退订 | ❌ 400（**有意**，ADR 0024） |
| `ac=edit` | `feed/<数字 id>` | 改分类/标题 | ⚠️ 部分 |

**注意 miniflux 自己记录的坑**：三种 action 的 `feed/<...>` **格式不一致**
（read 响应给数字 id，`subscribe` 期望 URL）。这不是我们引入的。

### 我们为什么"部分"支持 `ac=edit`

- ✅ `a=` 改分类
- ⚠️ `r=` 移除分类 → 回 `default`（miniflux 明确「removing a label is not
  implemented here」，我们比它多做了）
- ❌ `t=` 改标题 → 静默 OK 不生效（ADR 0024 记录为有意缺口）

### 影响

`ac=subscribe` 缺失**不影响 Read You** —— 它加订阅走 `quickadd`，已实现。
只影响**用 `ac=subscribe` 的客户端**（部分 greader 客户端这么加订阅）。
`unsubscribe` 的缺失是决定，不是缺口。

严重度：`subscribe` 中（无已知客户端在用，但协议完整性上是个洞）。

---

## 三、响应格式与错误处理的偏差

| 项 | miniflux | 我们 | 判读 |
|---|---|---|---|
| 未知 `/reader/api/0/*` 端点 | `[]` + **200** | `404` + JSON error | ⚠️ 见下 |
| API 认证失败 | 401 + `X-Reader-Google-Bad-Token` + 纯文本 | ✅ 一致 | ✅ |
| `ClientLogin` 失败 | 401 + JSON `{"error_message": ...}` | ✅ 一致 | ✅ |
| `quickadd` 找不到 feed | `{"numResults": 0}` + 200 | `400` + detail | ⚠️ 见下 |
| `rename-tag` 源 label 不存在 | `404` | `200 OK`（无操作） | 低 |
| `disable-tag` 删最后一个分类 | 失败 | 允许（回 default） | 低，模型差异 |
| `stream/items/contents` | 仅 POST | 仅 POST | ✅ |
| `tag/list` | 需 `output=json` | 不需要 | ✅ 更宽松 |

### 未知端点的 `[]` vs `404`（值得单独说）

miniflux 用 catch-all 返回 `[]` + 200。**这是"宽容"策略**：客户端探测自己不
确定的功能时不会炸。我们返回 404。

诚实地说：**我们的 404 更正确**（不存在的端点就该 404），但它是
**兼容性风险**——如果某个客户端靠"200 空数组"来判断功能是否存在，
它会看到 404 而报错。**未确证**是否有客户端这么做（我没有找到实例）。
Read You 与 NNW 都不用未知端点。

### `quickadd` 的 numResults=0

miniflux：URL 无法解析出 feed → `{"numResults": 0}` + 200。
我们：非法 URL → 400。**我们更严格，但语义不同**：客户端可能靠
`numResults == 0` 显示"没找到订阅源"而不是"请求出错"。**未确证**
Read You 是否分支处理（它的代码里 `numResults` 只在 DTO 注释出现，
`streamId` 才被 `requireNotNull`）。低风险。

---

## 四、明确不做 / 不需要做的

| 项 | 状态 | 理由 |
|---|---|---|
| `ac=unsubscribe` | 不实现 | ADR 0024 决策 5：不可逆 + 用户不可见 + 失败即重试 |
| `broadcast` / `broadcast-friends` / `like` 流 | 不记录 | 我们无此概念；miniflux 也是「recognized but ignored」 |
| `verified` 流 | 返回空 | 同上 |
| 多人 / `user/<id>/` 前缀 | 单用户 | 我们解析 `user/<anything>/...` 但不区分（单租户） |
| OPML 导入导出 | 不在 greader 协议内 | 属 A6 |

---

## 五、若要做到"完整覆盖"，工作量与优先级

按「是否有已知客户端在调用」排序：

| # | 缺口 | 依据 | 优先级 |
|---|---|---|---|
| 1 | `ot` 时间窗 | Read You 每次都传；但有界偏差，非损坏 | 中 |
| 2 | `r=o` 排序 | 分页正确性；未有客户端确认在用 | 中 |
| 3 | `ac=subscribe` | 协议完整性；无已知调用方 | 中 |
| 4 | 未知端点 `[]` vs 404 | 兼容性风险**未确证** | 低，需先找到实例 |
| 5 | `quickadd` numResults=0 | 语义细节**未确证** | 低 |
| 6 | `rename-tag` 不存在时 404 | 语义细节 | 低 |
| — | `nt` 时间窗 | 无已知调用方 | 低 |

**建议**：#1/#2/#6 与刚修的 `it`/`xt` 同在 `stream/items/ids` 一个端点、
同一个 SQL 下推位置，**要做就一起做**，边际成本低。但**当前没有一项是
"必须做"**——#1 的实际代价只是多传数据，其余缺调用方证据。
在补之前，建议先问"哪个客户端会因此出错"，而不是"协议上还差什么"。

### 与 ADR 0004 的范围声明关系

ADR 0004 / `reader_api.py` 开头的 scope 声明是「按 NetNewsWire 实际调用，
不是完整复刻」。ADR 0024 把它扩到「Read You 实际调用」。**"完整 Google
Reader API"从来不是目标**，本文件是覆盖度账本，不是待办清单。
若要改变目标，应新开 ADR 讨论，而不是逐个补端点。
