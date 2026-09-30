# 0024 · Read You 的订阅管理写端点

- 状态：accepted
- 日期：2026-09-14
- 实现：`src/app/reader_routes.py`（`subscription/quickadd`、`rename-tag`、
  `disable-tag`）、`src/adapters/repository/reader.py`（`it=`/`xt=` 下推 SQL）、
  `tests/integration/test_readyou_compat.py`
- 修订：`docs/integrations/netnewswire.md`（端点表）、
  `docs/api/mcp-tool-guide.md`（Reader API 一节）、`README.md`（客户端能力表）
- 关联：ADR 0004（Reader API 提前到 P1，范围「只读 + 已读回写」）、
  ADR 0005 / 0006（凭据与 item id）、ADR 0010（A5 `discover_feed` 暂缓）、
  ADR 0021（删除就是删除）
- 依据：对 `Ashinch/ReadYou@d2b979c` 的逐调用点对照与实测回放（核查记录
  已随该轮文档清理移除；可执行结论在 `tests/integration/test_readyou_compat.py`）

## 背景

用户要求「全面支持」Read You 通过 FreshRSS 账号使用本服务。核查后发现
ADR 0004 定的范围（**只读 + 已读回写**）覆盖不了这个客户端：Read You 在
自己的 UI 里能**加订阅、建分组、改分组、删分组、退订**，其中后四项我们从
未实现，而它每轮同步都会调用。

关键事实（读对端源码得到，非推测）：

**1. 「FreshRSS 账号」就是 Google Reader 协议加一个补丁。**
Read You 的 6 种账号类型里，`GoogleReader` 与 `FreshRSS` **共用同一个
service 类**（`RssService.kt:39-40`），全部请求由同一个 `GoogleReaderAPI.kt`
发出。整个代码库里区分二者的只有一处：

```kotlin
// GoogleReaderRssService.kt:284
val isFreshRss = account.type.id == FreshRSS.id
// GoogleReaderAPI.kt:305 —— useIt 决定 s= 还是 it=
add(Pair(if (useIt) "it" else "s", Stream.Read.tag))
```

所以「支持 FreshRSS」= 「支持 greader + `it=` 语义」，**不需要实现 FreshRSS
的私有 API**（它也没有被使用）。

**2. 参数被静默忽略会放大成"整个账号同步失败"。**
`sync()` 的函数体整体在一个 try/catch 内，catch 末尾是
`ListenableWorker.Result.retry()`；`subscribe()` / `addGroup()` 等又用
`requireNotNull` 取响应字段。于是任何一个端点的缺失或语义错误，
表现都不是「某个按钮不好用」，而是**无限重试 / 未读数震荡**。

## 决策

### 1. 补齐 Read You 实际调用的写端点

| 端点 | 语义 |
|---|---|
| `POST subscription/quickadd` | 按 URL 订阅；返回 `streamId`/`streamName` |
| `POST rename-tag` | 重命名文件夹（`s`、`dest` 均为 `user/-/label/<name>`） |
| `POST disable-tag` | 删除文件夹：其中源回到 `default` |

三者都按写端点处理：**经审计中间件**（任何 POST 自动入 AuditLog）、
按 `docs/api/style-guide.md` 记 correlation id、日志里带 source 与结果。

### 2. `it=` 是 include 过滤器，与 `xt=` 一样必须在 SQL 里生效

`stream/items/ids` 增加 `it: list[str]`，与 `s` 取交集；`xt` 取差集。
两者都在 **SQL 层、`LIMIT n` 之前**应用。

理由不是"更干净"，而是**不这样就是错的**：
- 忽略 `it=` 会让 `s=reading-list&it=<read>` 返回全集。Read You 把该结果当作
  「已读集合」与本地未读求交，于是**每轮同步把全部未读标记为已读**，而另一条
  并行分支（基于正确的 `xt` 查询）又翻回来 —— 未读数在 0 与 N 之间震荡。
- `xt` 在 `LIMIT` **之后**过滤，会让「最新 1000 条未读」少返回若干条，差额被
  客户端当成"不存在"，且随已读比例漂移。

无法识别的 tag 仍然返回**空流**而非忽略："无法兑现的过滤器不得放宽成未过滤
列表"与既有的 `s=starred` 规则一致。

### 3. `quickadd` 不是发现端点，A5 继续暂缓

Read You 在**客户端**完成发现：先抓用户输入的地址、解析、跟随
`<link rel=alternate>`，然后把**已解析的 feed URL** 发来
（`RssHelper.searchFeed` → `discoverFeedLink` → `subscribe(feedLink = resolvedFeedLink)`）。

所以本端点接收的是可直接订阅的 URL，**不抓 HTML、不做站点发现**。
ADR 0010 记录的「A5 `discover_feed` 用户已决定不做」**不受影响、继续有效**。
两者共享的只是"不能返回空回复"这条纪律：客户端 `requireNotNull(streamId)`
会让空回复变成整个账号的重试循环，所以这里是**要么给出真实订阅与 id，要么报错**。

### 4. 文件夹是源上的值，重命名/删除都是批量改值

Pearss 没有 folder 实体，`source.folder` 是名字，文件夹集合由 `tag/list`
用 DISTINCT 派生。因此：

- **重命名** = 把该名字批量改成新名字（所有源一起改，否则一个分组会裂成两个）
- **删除** = 把该名字下的源移回 `default`；组"消失"是因为没有源再携带这个名字

**删除时组内还有源是允许的，这是刻意的**。Read You 的 `deleteGroup` 会先逐个
`deleteFeed` 再调 `disable-tag`；但它要退订的那些因 `ac=unsubscribe` 未实现而
失败（见下），若这里再以"非空"拒绝，用户就会**得到一个刚删掉却仍存在、且源仍
留在里面的分组**。两害相权，选择让源存活于默认文件夹。

### 5. `ac=unsubscribe` **不实现**（唯一有意留下的功能缺口）

Read You 的 `deleteFeed` 发 `subscription/edit?ac=unsubscribe`，我们返回 400。
这是决定，不是遗漏：

- 删除在 Pearss 是**物理删除且带走历史**（ADR 0021），**不可逆**
- 客户端在**用户没有看着的同步过程中**发这个请求
- 它的失败模式是**重试循环而非对话框** —— 用户不会看到"确认删除"

三者叠加意味着一次误操作会静默销毁已采集的历史。增删订阅走 `/v1` 或 MCP，
调用方在那里显式表达意图。这是 Read You 侧**唯一**已知不可用的功能，
`docs/api/mcp-tool-guide.md` 与 `README.md` 都写明。

`t=`（重命名订阅源）同样不生效但返回 OK：标题属于 origin（采集时发现、只记
一次），兑现了也会被下次抓取覆盖。回复对"本端点拥有的一切"是诚实的。

## 理由

- **对端实现优先于规范**：ADR 0005/0006 两轮教训都是"把一个客户端的容忍度
  当成协议契约"。本轮按 `GoogleReaderAPI.kt` 的**实际调用**驱动，而不是按
  Google Reader 文档推断
- **写端点必须与既有写路径同规**：审计、幂等、correlation id 不因是兼容接口
  而豁免（ADR 0004 已定此约束）
- **幂等由 source 身份承担**：`create_source` 已实现"同 URL 返回既有行"，
  quickadd 复用它，重发不会产生第二个订阅

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| 用回退逻辑让 quickadd 支持站点首页并做服务端发现 | 客户端已自己完成发现，服务端再做一遍是重复且更差的实现；且会静默复活被明确暂缓的 A5 |
| 把 `it=` 当成"等同于 `s=read`"就地特判 | 只修了一个调用点，`it` 的一般语义（任意 tag 的 include）仍然缺失，下次接入新客户端会复发 |
| `xt` 保持 Python 后置过滤（"反正 `n=1000` 够大"） | 未读超过 1000 就错，且错误随已读分布漂移；这正是用户报告的"数字跳来跳去" |
| 实现 `ac=unsubscribe` 映射到 `disabled` | 与客户端预期不符（它期望订阅消失），会把"删除"变成"隐藏"，用户看到的是删不掉 |
| 实现 `ac=unsubscribe` 为真删 | 见决策 5：不可逆 + 用户不可见 + 失败即重试，误操作代价过高 |
| `disable-tag` 在组非空时拒绝 | 会让用户留下一个已删除却仍存在的分组（见决策 4） |

## 后果

正面：

- Read You「FreshRSS 账号」类型可完整使用：登录、列表、加订阅、分组建/改/删、
  已读星标双向同步、未读数稳定
- 未读数震荡与 `n` 截断两个 bug 消除，对 NNW 同样生效（它也用 `xt`）

必须遵守的约束：

- **Reader API 的数据格式不得泄漏进 `core/`**（`docs/api/style-guide.md`）。
  `it`/`xt` 的解析留在 `adapters/reader_api.py`，SQL 谓词留在
  `adapters/repository/reader.py`，`core/` 只接受"状态名"
- 新增的写端点**不得绕过审计**；`quickadd` 创建源必须继续走
  `create_source`（幂等身份）而不是直接 INSERT
- **A5 `discover_feed` 的暂缓状态不变**；若将来要启用，应新开 ADR，
  而不是把它塞进 quickadd

已知缺口（有意保留，需在客户端文档可见）：

- `ac=unsubscribe`（Read You 内不能退订）
- `t=` 重命名订阅源不生效
- 空分组不落库（Read You 新建空分组会在刷新后消失）

## 可逆性

可逆，成本低：三个端点可停用（返回 404 即回到本轮之前的行为），`it=`/`xt=`
的 SQL 下推可退回 Python 过滤（但会重新引入震荡）。
**不可逆的只有已发生的写入**：quickadd 创建的 source、rename/disable 改过的
`source.folder` 都已是事实数据，需要按常规 source 管理处置。

## 修订（2026-09-16）：`stream/items/ids` 补上 `continuation`

本 ADR 决策 2 修掉了 `it=`/`xt` 的"静默给出错误集合"，但**同一个端点还有
第三个同类缺陷当时没被发现**：响应恒返回 `continuation: null`。

后果（用户报告）：**NNW 标记的已读传不到 Read You**（反向正常）。Read You
全局 sync 把远程已读集合与本地未读求交，而已读集合受 `n=1000` 约束；
没有游标就只取到最新 1000 条已读，超出窗口的条目两个分支都漏掉。
用户观察到的"方向性"正是线路差异 —— 反向走 `edit-tag`（不经过分页）。

修复：按 `limit + 1` 多取一条判断有无下一页，返回
`continuation = str(offset + len(page))`，取完为 `null`。
miniflux 同形（`len(itemRefs) + offset`）。另修正深度上限语义：
原 `_offset` 把超限游标**夹紧**到 `_MAX_OFFSET`，若继续返回游标会让客户端
永久循环；改为达到上限即报告结束。

本 ADR 的决策 1–5 不变；决策 2 的推理同样适用于本次（**参数/字段被忽略
→ 静默给出子集**）。回归测试见
`tests/integration/test_reader_api.py` 的 continuation 游标用例。

## 修订（2026-09-16）：`t=` 重命名生效；`ac=unsubscribe` 从拒绝改为删除

本节**修订决策 5 与决策 4 的两处论断**，原决策按 ADR 惯例保留原文不重写。

### `t=`：撤回「会被下次采集覆盖」的论断——它是错的

决策 4 与 `subscription_edit` 原文档字符串都以「标题属于 origin，兑现了也会被
下次抓取覆盖」为由不实现 `t=`。**核对代码发现这个前提不成立**：

`set_source_title_if_absent`（`scheduling.py:112`，2026-09-05 引入，**早于本
ADR 十一天**）是**条件写入**——只在 `title` 为空时落库，从不覆盖已有值。其
文档字符串原话即为防覆盖而写："an operator who renamed a source through the
API would otherwise have that choice overwritten by the next collection"。

也就是说，防覆盖保护在写本 ADR 时**已经存在**，而当时只查证了「title 从哪
来」（采集时记录），没有查证「会不会被覆盖」（不会）。这与 ADR 0005/0006 的
教训同构：**结论下在半个证据链上**。据此：

- `t=` 现已实现：把 `t` 写入 `source.title`，由既有的条件写入规则保护，
  重命名在下一次采集后保持。`renameFeed`（Read You）与 miniflux 的同名
  实现自此生效。
- 空 `t=`（trim 后）不改名，与「缺省即不动」的表单语义一致。
- 验收场景 J11（testing.md）钉住「改名生效 + 采集后不被改回」两点。

### `ac=unsubscribe`：从拒绝改为删除，走 ADR 0021 语义

决策 5 拒绝了 App 内退订，理由是三条叠加（不可逆、用户不可见、失败即重试）。
重新推演后，**第三条恰恰推翻了结论**：Read You 的 `deleteFeed` 发出
`unsubscribe` 后，400 与 404 一样进入 `sync` 的 catch → `Result.retry()`——
拒绝并没有让删除「安全」，而是让用户的删除意图**永远停在重试循环里**，
UI 上表现为删除永不成功。拒绝方案实际产出的是「看似更安全、实则更坏」：
用户在 App 里点了删除、确认了（App 侧有对话框），然后系统假装什么都没发生。

同时重新评估了前两条：不可逆与「用户不可见」针对的是**误触**；而 Read You
的删除有 UI 确认步骤，与 `DELETE /v1/sources` 的调用方承担同等的确认义务。
两条路径没有理由不同语义。据此：

- `ac=unsubscribe` 现已实现：复用 `delete_source`（与 `DELETE /v1/sources`
  同一入口），删除源、条目、正文、已读状态、处理器产物与抓取记录——完整
  ADR 0021 语义，不做软删除。
- **重放语义**：对已不存在的 id 再次 `unsubscribe` 返回 OK（「已删除」与
  「删除完成」对重试方是同一事实），与 `/v1` 端点确立的规则一致。
- 验收场景 J9 改写：删除后**两个客户端**的订阅列表都消失、源与条目行数
  为零、重放仍 OK。
- `ac=subscribe` 仍不实现：订阅走 `quickadd`（唯一会订阅的客户端也正是
  这么调用的）。

### 对决策记录方式的说明

本修订按「不重写已接受 ADR」的惯例追加。需要提示读者：**决策 5 与决策 4
中关于 `t=` 的段落在 2026-09-16 之后不再是现行决策**；现行状态以本节为准。
教训（第三次同构）：写「某端点做不到 X」之前，先确认支撑 X 的机制是否
已经存在——三回里有两回（`it=`、`t=`），答案是「已存在，只是没被接上」。

## 修订（2026-09-30）：#92 覆盖度未决项裁决（对账 ReadYou@d2b979c）

`docs/integrations/google-reader-api-coverage.md` 在文档清理轮删除时，留下 7 个
「未定」项，迁入 issue #92。本修订按 issue 的方法逐项裁决：**先回答「哪个客户端
会因此出错」**，再决定修或明确记 declined。对账对象是 `ReadYouApp/ReadYou@d2b979c`
——与本 ADR 初版钉住的同一 revision，因此结论可直接比对。

### 对账结论（读客户端源码得到，非推测）

| 参数 / 行为 | ReadYou@d2b979c 的实际调用 | 裁决 |
|---|---|---|
| `ot` | **每次全局同步都发**：`getReadItemIds(since = now − 1 月)`（`GoogleReaderRssService.kt:293`）；`fetchItemIdsAndContinue`（`:672`）循环到 `continuation == null`，**无页数上限** | **实现** |
| `nt` | 全库零引用 | 已明确不做 |
| `r`（排序 `o`） | `r` 只作为 edit-tag / subscription-edit 的**移除字段**出现（`GoogleReaderAPI.kt:414`、`:447`），从不是排序参数 | 已明确不做 |
| `ac=subscribe` | 从不发送；`subscribe()`（`:130`）走 `quickadd` | 已明确不做 |
| 未知端点 `[]` vs `404` | 从不探测未知端点；非 2xx 一律抛异常并重试，没有「200 空数组 = 不支持」分支 | 已明确不做（保留 404） |
| `quickadd` `numResults=0` | DTO 有该字段（`GoogleReaderDTO.kt:94`）但**从不读取**；只 `requireNotNull(streamId)`（`:141`） | 已明确不做（保留 400） |
| `rename-tag` 不存在 → `404` | `renameTag` 只发 `s`/`dest`，不依赖 404 | 已明确不做（保留 200 no-op） |

### `ot` 实现（本次唯一的代码变更）

忽略 `ot` 的代价比原账本记的更大：账本写于 `c` 游标修复**之前**，当时只取第一页，
所以记为「有界偏差（多传数据）」。现在游标会翻到穷尽，忽略 `ot` 意味着**每轮同步
翻完整个已读历史**；而当已读历史超过本服务单次游走上限（`_MAX_SERVED = 100_000`）
时会被**截断**，更早的远程已读条目就此不再传播。

实现与 miniflux 对齐（`internal/googlereader`，`handler.go:1047`、
`entry_query_builder.go:85`）：

- `ot` 是 Unix 秒；**仅在 > 0 时生效**（`0`/缺失/非数字 = 无下界，而不是「空集」）
- 比较是**严格大于**：`published_at > ot`
- 在 **SQL 层、`LIMIT` 之前**下推，与 `it=`/`xt=` 同一位置；NULL `published_at`
  无法满足 `>`，因此无日期条目离开有窗口的流（miniflux 同样如此）

### 修正「已知缺口」一节

初版「后果」下的「已知缺口」列表已过期，两个方向都错，以本修订为准：

- `ac=unsubscribe` 与 `t=` 重命名订阅源**均已实现**（见 2026-09-16 修订），
  从缺口列表移除
- 现行缺口（有意保留，各有可执行证据）：

| 缺口 | 理由 | 证据 |
|---|---|---|
| `nt` | 无调用方 | `test_reader_coverage_gaps.py::test_nt_is_ignored` |
| `r=o` 升序 | 无调用方；游标按降序设计 | `test_reader_coverage_gaps.py::test_r_o_does_not_change_the_order` |
| `ac=subscribe` | 订阅走 `quickadd`（决策 1/3） | `test_reader_coverage_gaps.py::test_ac_subscribe_is_refused_and_creates_nothing` |
| 未知端点返回 404 而非 `[]` | 无调用方探测；404 更正确 | `test_reader_coverage_gaps.py::test_an_unknown_reader_endpoint_is_404` |
| `quickadd` 非法 URL 返回 400 而非 `numResults=0` | 无调用方读 `numResults` | `test_reader_coverage_gaps.py::test_quickadd_with_an_invalid_url_is_400` |
| `rename-tag` 不存在时 200 no-op 而非 404 | 无调用方依赖 | `test_reader_coverage_gaps.py::test_rename_tag_on_a_missing_folder_is_ok` |
| 空分组不落库（刷新后消失） | 模型无 folder 实体，集合由源派生 | `test_reader_api.py::test_tag_list_reports_the_folders_that_exist` |

`ot` 的实现由 `test_reader_coverage_gaps.py::test_ot_windows_the_read_stream`
与 `::test_a_non_positive_or_unparseable_ot_disables_the_window` 钉住。

### 范围声明不变

「完整 Google Reader API 从来不是目标」不变；本修订只把未决项落到可判定的去向
（实现或 declined），没有扩大范围。若要改变目标本身，应新开 ADR。
