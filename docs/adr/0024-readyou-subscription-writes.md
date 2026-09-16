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
- 依据：`docs/integrations/readyou-freshrss-compat-findings.md`
  （对 `Ashinch/ReadYou@d2b979c` 的逐调用点对照与实测记录）

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
