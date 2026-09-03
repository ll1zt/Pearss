# 0004 · Reader API（C4）从 P3 提前到 P1

- 状态：accepted
- 日期：2026-09-03

## 背景

`architecture-spec.md` 把 C4（Google Reader API 兼容端点）定在 **P3**：

- §5 分期路线表：C4 属「P3 完整形态」
- §6 需求矩阵：`| C4 Reader API | /api/greader.php | P3 |`
- §1.3 非目标：「P1/P2 阶段的已读状态同步（C4 属 P3）」
- `docs/domain/data-model.md`：`ReadState` 标注为 P3 表

但在 P1 的 RSS 输出可用后，缺已读同步意味着每换一个设备就要重新面对一堆未读，且无法在多端之间维持阅读进度。相比之下，P3 剩下的处理器插件（翻译/TTS）与 Ingest API 是增强而非日常刚需。

已知的替代方案是行不通的：标准 RSS 没有已读状态的回写通道。`netnewswire.md` 也写明 P1/P2 的 RSS 方式「不支持已读/星标的服务器多端同步」。所以这不是「先做后做」的排期问题，而是 P1 交付物在单设备之外不可用的缺口。

## 决策

C4 提前到 P1，本次交付完整能力（只读 + 已读回写）：

| 端点 | 方法 | 作用 |
|---|---|---|
| `/accounts/ClientLogin` | POST | 凭据换 token |
| `/reader/api/0/token` | GET | 返回同一 token |
| `/reader/api/0/user-info` | GET | 账号校验 |
| `/reader/api/0/subscription/list` | GET | 订阅列表 |
| `/reader/api/0/tag/list` | GET | 星标与标签 |
| `/reader/api/0/stream/items/ids` | GET | 条目 ID 流 |
| `/reader/api/0/stream/items/contents` | POST | 条目正文 |
| `/reader/api/0/edit-tag` | POST | 已读/星标回写 |

同时引入 `ReadState` 表（migration 0002），并写 ADR 0005 记录凭据与 ID 映射的取舍。

## 理由

- **已读同步是日常使用的刚需**，而 P3 的翻译/TTS 是增强
- **标准 RSS 无回写通道**，不存在「先凑合用着」的中间态
- 数据平面（采集 → normalize → 存储 → 出 feed）此时已跑通，Reader API 可复用它，无需等待

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| 维持 P3，先做处理器插件 | 处理器插件是增强；已读同步缺失使 P1 在多设备下不可用 |
| 用 FeedOut token 模拟已读 | RSS 无回写通道，客户端不会上报状态 |
| 只做只读、不做回写 | 只读仍无法在设备间维持进度，等于没解决 |

## 后果

正面：

- NNW 可用「FreshRSS 账号」类型登录，已读/星标多端同步
- P1 交付物在多设备场景下真正可用

必须遵守的约束：

- **兼容 API 的数据格式不得泄漏进 `core/`**（`docs/api/style-guide.md` §Reader API 边界）。Reader API 的 stream id、item id 形态全部留在 `adapters/reader_api/` 内转换
- **已读/星标写入仍走审计与幂等**，不因是兼容接口而绕过
- **`pending` 条目对 Reader API 同样不可见**（`netnewswire.md` 验收第 7 条）——发布闸门必须延伸到这条路径
- 本次改动需同步更新：§5 路线表、§6 需求矩阵、§1.3 非目标、`data-model.md` 的 `ReadState` 标注

## 可逆性

可逆，成本中等：端点集可停用，但 `ReadState` 作为事实源一旦有数据就不能删掉重来（清理 Item 前须先按保留策略处理，见 `data-model.md`）。若最终判断不需要，应停用端点而保留表与数据。
