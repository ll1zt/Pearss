# 0005 · Reader API 的凭据与 item ID 映射

- 状态：**部分 superseded**（本文「item ID 映射」一节已被 [ADR 0006](0006-reader-api-ids-must-be-decimal.md) 取代；凭据部分仍有效）
- 日期：2026-09-03
- 修订：2026-09-04（标注被取代部分，不重写原文）
- 依赖：ADR 0004（C4 提前到 P1）

## 背景

实现 Reader API 需要定两件**实际上不可逆**的事：

1. 凭据从哪来——NNW 的「FreshRSS 账号」界面要求填用户名 + 密码，并自行调用 `POST /accounts/ClientLogin`
2. item ID 怎么映射——客户端会把 ID 存下来，回写已读状态时再发回来。映射规则一旦上线，客户端本地已存的 ID 无法更改

查证依据（miniflux `internal/googlereader/README.md`，一份持续维护的 Google Reader 兼容实现文档）：

- `GET` 只从 header `Authorization: GoogleLogin auth=<token>` 取 token
- `POST` 只从表单的 `T=<token>` 取，且不接受 header
- auth scheme 必须严格是 `GoogleLogin`，字段名必须小写 `auth`
- `edit-tag` 与 `stream/items/contents` 的 `i` 参数支持 16 字符十六进制（64-bit）；`stream/items/ids` 返回十进制 ID 字符串

## 决策

### 1. 凭据由环境变量注入，密码以 scrypt 哈希存储

```
.env（gitignore）
  PEARSS_READER_USERNAME=reader
  PEARSS_READER_PASSWORD=...

库内只存 scrypt 哈希，绝不存明文
token = HMAC-SHA256(key = username + scrypt_hash, msg = "")
```

**用 stdlib `hashlib.scrypt`，不引入 bcrypt / argon2-cffi**：本项目已有一个 C 扩展依赖（lxml，见 ADR 0003），单用户场景下没有理由再背第二个。scrypt 内存硬、抗 GPU，stdlib 自带，无供应链成本。

代价：token 由哈希派生，**改密码会让已登录客户端掉线**，需重新登录。单用户场景可接受。

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| HTTP Basic 直通 | NNW 的 FreshRSS 账号类型只会调 ClientLogin，走 Basic 登录不上 |
| 预生成固定 token | NNW 账号界面没有「直接贴 token」的字段，无法配置 |
| 密码明文存库 | 违反 §10.5 与 AGENTS.md 的密钥规则 |

### 2. item ID 双方都发 32 字符，不截断

> ⚠️ **本节已被 ADR 0006 取代。** 下面的「NNW 实测不校验位数」是错误假设：
> 接入 NNW 时它丢弃了全部条目（订阅可见但无文章）。真相是 NNW 对
> `stream/items/ids` 的返回值做 `Int(articleID)!`，只接受能塞进 64-bit 的十进制数。
> 现改为对外暴露内部 id 前 15 位十六进制（60-bit）的十进制形式。
> 保留本节原文是为了记下这个判断是怎么错的。

我们的 `item.id` 是 SHA-256 前 32 字符（128-bit），比 Reader 约定的 16 字符（64-bit）长一倍。**对外完整发送，不截断**：

```
GET  stream/items/ids      -> {"id": "1a7f...9b01"}   (32 字符)
POST edit-tag              -> i=1a7f...9b01           (32 字符)
```

理由：

- **零碰撞**。截断到 64-bit 会引入不必要的碰撞面；128-bit 下不可能撞
- **不动已冻结的 id 派生**。GUID 与 id 首次发布即冻结，为适配某个客户端去改派生规则是本末倒置
- **NNW 实测不校验位数**，它把 ID 当不透明字符串处理

被否决的备选：截断为 16 字符。虽贴合约定，但带来碰撞风险，且使对外 ID 与内部 id 不再一一对应，排查问题时要额外维护映射层。

## 后果

必须遵守的约束：

- **改密码 = 所有客户端重新登录**，因为 token 由哈希派生。这要写进 runbook
- `i` 参数解析按 32 字符十六进制处理，不要套用 Reader 文档里的 16 字符假设
- 若将来真有客户端严格校验 16 字符，应新增 ADR 决策，而不是就地截断——那会让已存 ID 失效

## 可逆性

- 凭据机制：可逆，改密码即可（代价是客户端需重新登录）
- ID 长度：**不可逆**。客户端已存的 ID 无法召回，截断会让所有历史已读状态指向错误条目
