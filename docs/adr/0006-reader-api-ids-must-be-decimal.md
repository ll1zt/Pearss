# 0006 · Reader API 的 item ID 必须是十进制整数（supersede ADR 0005 的 ID 部分）

- 状态：accepted
- 日期：2026-09-04
- 部分取代：ADR 0005（仅其「item ID 映射」一节；凭据部分仍然有效）

## 背景

ADR 0005 决定「对外发送完整的 32 字符（128-bit）item id，不截断」，理由是「NNW 实测不校验位数，它把 ID 当不透明字符串处理」。

**这个前提是错的。** 接入 NNW 时的实际表现是：订阅源 `planet-python` 正常显示，但**没有任何文章**，刷新也无内容。

服务器日志给出了决定性证据：NNW 反复请求 `stream/items/ids` 并拿到 200，却**从未请求过 `stream/items/contents`**。它收到了 id，然后全部丢弃。

读 NNW 源码（`Account/Sources/Account/ReaderAPI/ReaderAPICaller.swift`）确认：

```swift
// stream/items/ids 响应
let itemIDs = entriesItemRefs.compactMap { $0.itemId }

// stream/items/contents 请求
let idValue = Int(articleID)!                                  // ← 强制解包
return "i=tag:google.com,2005:reader/item/\(idValue ... radix:16)"

// edit-tag 请求
guard let intValue = Int(idValue) else { return nil }
let idHexString = String(format: "%.16llx", intValue)          // ← 零填充 16 位

// contents 响应
guard let idNumber = Int(idPart, radix: 16) else { ... }        // ← 按 16 进制读回
```

`Int(articleID)` 只接受**十进制**，且 Swift 的 `Int` 是 **64 位有符号**。我们的 32 字符十六进制 id 无法解析，于是每一条都被丢弃。

## 决策

Reader API 对外暴露**收窄后的数字 id**：内部 id 的前 15 个十六进制字符，即 **60-bit** 值。

```python
READER_ID_HEX_DIGITS = 15
reader_id(item_id)   = int(item_id[:15], 16)     # 60 bit，稳落在 Int64 内
reader_id_hex(ext)   = format(ext, "015x")       # 反查：还原为内部 id 的前缀
```

各端点按协议要求分别输出：

| 位置 | 格式 |
|---|---|
| `stream/items/ids` 响应 | **十进制字符串** |
| `stream/items/contents` 响应 | `tag:google.com,2005:reader/item/<hex>`（不填充） |
| `edit-tag` 请求 | 同上但零填充到 16 位 |
| `contents` 请求 | 同上但不填充 |

反查用前缀匹配（`substr(id,1,15) = :prefix`），因为截断是可逆前缀操作。

## 理由

- **协议要求**：Google Reader 生态的 id 一直是 64-bit 数字（历史上是 16 位十六进制）。客户端按此实现，不是可以协商的细节
- **60 bit 而非 64 bit**：留出余量，确保任何值都 < 2^63，不会让 Swift 的 `Int()` 溢出
- **确定性**：不引入自增列或新表，重启与重建库后 id 稳定
- **碰撞风险可接受**：60 bit 的生日界约在 2^30（约 10 亿）条目才到 50% 概率。单用户 RSS 系统离这个量级很远

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| 发完整 32 字符（ADR 0005 原方案） | **实测失败**：NNW 丢弃全部条目，且静默无报错 |
| 新增自增整数列 | 需要 migration 与额外状态；确定性前缀已够用 |
| 用 SQLite rowid | 引擎特定，P3 迁 Postgres 会破坏稳定性 |

## 一个实现陷阱

**前缀决定进制，必须先判断再解析。** 十进制 `12345` 的十六进制是 `3039`——每个字符都是数字。若在剥掉 `tag:.../item/` 前缀之后再用 `isdigit()` 判断，会把它当十进制读，**静默解析成错误的条目**。正确做法是：带长格式前缀的一律按 16 进制，裸值才按十进制。

## 后果

- NNW 能正常显示文章与已读/星标同步（已用逐行复刻其 Swift 解析逻辑的脚本验证）
- 内部 id 仍是 128-bit，只有 Reader API 边界做收窄映射
- **`/v1` REST 与 RSS 输出不受影响**，它们继续用完整 id 与 guid
- 若将来条目数逼近十亿量级，需重新评估位宽——届时应以新 ADR 处理，因为客户端已存的 id 无法召回

## 可逆性

**不可逆**（就对外 id 而言）。客户端本地已存的数字 id 无法召回；改变映射规则会让所有历史已读状态指向错误条目。这正是 ADR 0005 当初担心截断的点——但代价不是理论上的碰撞风险，而是**功能完全不工作**。

## 教训

ADR 0005 的错误在于：用一个「我觉得客户端会怎么处理」的假设，替代了「去读客户端源码」。**涉及对外契约时，应以对端实现为准，而不是以规范文档或直觉为准。**
