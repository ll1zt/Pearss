# NetNewsWire 集成说明

本项目面向 NetNewsWire（NNW）只有一条集成路径：**FreshRSS/Google Reader 兼容
API**（原定 P3，已提前，见 ADR 0004）。无鉴权的标准 RSS 输出已在公网化时移除
（ADR 0016）——内容阅读、feed 更新与已读/星标的多端同步全部经 Reader API 完成。

## 集成方式

| 阶段 | 集成方式 | 支持能力 | 不支持能力 |
|---|---|---|---|
| P1（原 P3） | FreshRSS 账号方式，Reader API 兼容端点 | 内容阅读、feed 更新、已读/星标多端同步 | 不承诺 FreshRSS 的每一个非 NNW 专有行为 |

NNW 以「FreshRSS」账号类型连接 `/api/greader.php`。Reader API 端点由
`adapters/reader_api.py` 实现，兼容格式不得渗入 core 领域模型。

## 接入步骤

1. `.env` 配置 `PEARSS_READER_USERNAME` / `PEARSS_READER_PASSWORD` 并
   `just service-restart`
2. NNW → 添加账户 → 类型选 **FreshRSS**
3. 服务器地址填 `http://<host>:8000/api/greader.php`，填入用户名与密码

## 发布闸门：不可妥协约束

NNW 会缓存已获取条目。因而：

```text
Item pending -> 不经任何输出路径可见
Item ready   -> 可经 Reader API / body 端点输出
首次被输出   -> Item published，主体内容与 GUID 冻结
```

如果一条 feed 配置了“翻译后发布”等处理器链，原 Item 必须保持 `pending`，直到
全部必需产物成功。**禁止先输出原文，再原地更新为译文。** 后者即使服务器数据
改了，NNW 也可能继续显示第一次缓存的内容。

处理超时默认 `hold`；仅在用户明确决定并经审计后，使用 `publish_original` 或
`force_publish`。

## 富媒体（P3）

TTS 音频以 enclosure 形式输出到条目：

```xml
<enclosure url="https://example.invalid/media/abc.mp3"
           type="audio/mpeg"
           length="123456" />
```

音频文件必须有稳定 URL、正确 MIME type 和 length。NNW 不应被视作完整播客
播放器；若音频消费是主要需求，应同时用播客客户端做集成测试。

## Reader API / FreshRSS 账号同步

> 原定 P3，已提前到 P1（ADR 0004）：标准 RSS 没有已读状态的回写通道，
> 缺了它多设备场景不可用。

提供 Google Reader API 兼容入口：

```text
/api/greader.php
```

目标是使 NNW 可以通过“FreshRSS”账号类型登录并同步内容与状态。最低兼容范围
从 NNW 实际调用的 endpoint 出发，以受控集成测试逐个确认，而不是盲目复刻整个
FreshRSS API。

### 已实现端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/accounts/ClientLogin` | POST | 凭据换 token（`Email` / `Passwd` 表单字段） |
| `/reader/api/0/token` | GET | 返回同一 token |
| `/reader/api/0/user-info` | GET | 账号校验 |
| `/reader/api/0/subscription/list` | GET | 订阅列表 |
| `/reader/api/0/tag/list` | GET | 星标状态 |
| `/reader/api/0/stream/items/ids` | GET | 条目 ID 流，支持 `xt` 排除已读 |
| `/reader/api/0/stream/items/contents` | POST | 条目正文 |
| `/reader/api/0/edit-tag` | POST | 已读 / 星标回写 |
| `/reader/api/0/mark-all-as-read` | POST | 批量标记已读 |

### 认证（关键约束）

```text
GET  -> Authorization: GoogleLogin auth=<token>
POST -> 表单字段 T=<token>
```

两者**不可互换**：GET 不接受 query 中的 token，POST 不接受 header。scheme 必须
严格为 `GoogleLogin`，字段名必须小写 `auth`。代码与测试见
`src/app/reader_routes.py`，依据记录在 ADR 0005。

凭据由环境变量注入（`PEARSS_READER_USERNAME` / `PEARSS_READER_PASSWORD`），库内
只存 scrypt 哈希。**token 由密码哈希派生、永不过期（ADR 0016，风险已知并接
受）；改密码会使所有已登录客户端的 token 失效，需重新登录**——这也是怀疑
泄露时的处置手段。

item ID 对外是**十进制数字**（内部 id 前 15 位十六进制，60-bit），不是内部那个
32 字符的 128-bit 值。原因很实际：NNW 对 `stream/items/ids` 的返回值做
`Int(id)!`，无法解析的值会被**静默丢弃**——表现为订阅源能看到、文章一条没有。
各端点的具体格式差异见 ADR 0006。

### 状态写回

- 已读、未读、星标写入 `ReadState`，不写入 Item 主体内容
- 同步事件必须幂等：重复请求不产生矛盾状态
- 服务端接收时间写入 `updated_at`
- Item 删除/清理前必须有 ReadState 保留或级联策略
- Reader API 写入仍记录审计事实与 correlation ID

## 限速与公网

公网部署时 Reader API 受认证失败限速保护（ADR 0016）：同一地址在 5 分钟内
产生 50 次 401/403 会被 429 阻断 15 分钟。客户端反复用过期 token 轮询会撞上
限速——处置是更新凭据并等待阻断过期，而不是提高限速阈值。

## 兼容测试清单

建立一个真实或受控的 NNW 测试账户，验收至少包括：

1. 能添加 FreshRSS 类型账户并通过认证
2. 能列出订阅/分类与未读条目
3. 能同步新发布条目，GUID 不重复
4. 在设备 A 标记已读后，设备 B 最终同步到已读
5. 星标状态可同步
6. 断网后重试或重复同步请求不造成重复条目/状态错误
7. pending 条目在处理完成前对 Reader API 同样不可见

## 故障排查

| 现象 | 先检查 | 修复原则 |
|---|---|---|
| 新条目不出现 | Item publish_status、处理链配置 | 未发布则排查 processing；已发布再检查 GUID/客户端刷新 |
| 原文而非译文出现 | 发布闸门事件、首次输出时间 | 不改已发布 Item；修正未来条目的链配置 |
| 同一条目重复 | GUID、canonical URL、content_hash、排序 | 修复生成规则；不得删除已发布 GUID 后复用 |
| NNW 无法登录 Reader 账户 | 认证失败是否触发 429 限速、凭据是否过期 | 更新凭据等待限速窗口过去；根据 NNW 实际调用补 adapter |
| 已读不同步 | ReadState、同步事件审计、时间戳 | 重放幂等事件并检查状态写回 |

## 相关文档

- `architecture-spec.md`：C1–C5 需求与阶段（C1 已由 ADR 0016 移除）
- `docs/domain/state-machines.md`：发布与 ReadState 不变量
- `docs/api/style-guide.md`：`/v1` REST 与 `/api/greader.php` 的隔离边界
- `docs/operations/runbook.md`：限速、发布和同步事故处置
