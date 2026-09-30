# 0043: 入库净化放行宿主受限的 `<video>`

Status: accepted
Date: 2026-09-29
Context: issue #91。X 载荷的 `video_info.variants` 带可直接播放的 mp4（HLS 主播放列表 +
三档 mp4，256k/832k/2176k，及 `duration_millis`），采集器此前只渲染「缩略图 + 外链」，
`video_info` 零引用。要把视频渲染成 `<video>`，净化器必须先放行该标签。

ADR 0007 明确禁止 `iframe`/`object`/`embed`（嵌入外来文档/插件），但它没有提到
`<video>`。放宽媒体标签白名单是**扩大攻击面**，所以本条只为一个可枚举的来源放行，
而不是为标签本身放行。

## 决策

### 1. 放行的是来源，不是标签

`<video>` 进入 `ALLOWED_TAGS`，但 `src` 经 `attribute_filter` 按**宿主**约束：
只接受 `https://video.twimg.com/` 下的 URL。与 `img` 的 `pearss-avatar` 同类思路
（ADR 0007 的豁免）：白名单看见属性值，过滤器决定它是否留下。

- 允许属性：`src`、`controls`、`poster`、`width`、`height`
- **不允许** `autoplay`（自动播放是骚扰，且绕过用户的流量偏好）、`loop`、`muted`：
  它们不在属性白名单里，所以被 nh3 直接丢弃
- `poster` 与 `img[src]` 同级（https，任意宿主）：它是一张图，不是新的取数通道

### 2. 非白名单来源的 video 整个移除

`attribute_filter` 只能剥属性、不能删元素；一个被剥掉 `src` 的 `<video>` 会留下
一个空元素（或显示其 fallback 内容）。净化器在 nh3 之前跑一趟**只做删除**的
前置 pass（`_drop_foreign_video`），把 `src` 缺失或宿主不在白名单的 `<video>`
连同其内容一起移除。**nh3 仍是安全边界**：前置 pass 只会删节点，不会引入节点；
它只在该 body 含 `<video` 子串时运行，普通正文不受影响。

### 3. 降级路径先于嵌入实现

采集器把**现有**「缩略图 + 外链」放进 `<video>` 的**内容**里：

```html
<video src="https://video.twimg.com/..." poster="https://pbs.twimg.com/..." controls>
  <a href="https://x.com/.../status/..."><img src="https://pbs.twimg.com/..." alt="▶ 视频"/></a>
</video>
```

- 净化器放行 `video` 时：浏览器播放视频，fallback 内容不显示
- 净化器**不放行** `video` 时（本 ADR 被否决/回退）：nh3 剥掉标签、**保留内容**，
  于是读者看到的正是原来的「缩略图 + 外链」——视频不会消失

这条是硬约束：嵌入实现依赖净化器改动，降级路径不依赖。

## 理由

- **为什么不是 `iframe`**：`iframe` 嵌入的是第三方文档，可执行脚本、可追踪；ADR 0007
  的禁令不变。`<video>` 是一段媒体，来源受限、无脚本、无自动播放。
- **为什么宿主白名单而不是 scheme**：`https` 只约束传输，不约束「谁」。放行一个可枚举
  的 CDN 宿主，等于放行一个已知用途的来源；其余宿主一律移除，避免把 `<video>` 变成
  任意远程取数/追踪通道。
- **为什么前置 pass 而不是放宽 nh3**：nh3 的能力边界是属性级；删元素必须发生在它之前，
  且这层删除是保守的（只删不加），安全边界仍是 nh3。

## 后果

- `src/adapters/sanitize.py`：`ALLOWED_TAGS` 加 `video`；`ALLOWED_ATTRIBUTES["video"]`；
  `attribute_filter` 增加 video `src` 宿主校验；新增 `_drop_foreign_video` 前置 pass。
- `src/plugins/collectors/twitter/collector.py`：`video_info.variants` 里选最高码率的
  `video/mp4`，渲染 `<video ...>`，fallback 为原「缩略图 + 外链」。
- `docs/guides/twitter-collector.md` 增「视频」一节；「明确不做」的 iframe 一行指向本条。
- **已入库的条目不会自动重净化**（ADR 0007 的既有后果）：本改动只影响此后入库的正文。
- 不新增依赖；不新增 migration；不改 API/MCP 面。

## 验收

见 `SCENARIOS.md` 的 AC-157..160 与 `tests/unit/test_sanitize.py`、
`tests/unit/test_properties_sanitize.py`、`src/plugins/collectors/twitter/test_collector.py`。
对抗性用例：非白名单宿主、`javascript:` URL、`onerror`、`autoplay` 全部不出现；
降级用例：`video` 不被放行时缩略图+外链仍在。
