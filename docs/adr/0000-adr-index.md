# 架构决策记录（ADR）索引

一决策一文件。本目录记录那些**难逆转、影响多个组件、或日后会被反复追问「当初为什么这么定」**的决策。

## 何时写 ADR

写：

- 数据模型、API/MCP 契约、hookspec、错误分类法的变更（替换成本最高的一层）
- 引入或替换外部依赖、运行环境、持久化方案
- 破坏式变更及其迁移路径
- 在多个可行方案间做过明确取舍的地方

不写：

- 可逆且局部的实现细节（换解析器、改函数签名）——用 commit message 说明即可
- 纯格式化、依赖小版本升级——交给 lock 文件与 CI

## 文件约定

命名 `NNNN-kebab-case-title.md`，四位数字零填充、按创建顺序递增：

```text
docs/adr/
  0000-adr-index.md          # 本文件
  0001-四位数从 0001 起的第一个决策
```

每个文件是一份自足记录，包含：

| 字段 | 内容 |
|---|---|
| `标题` | 决策的祈使句摘要，与文件名一致 |
| `状态` | `proposed` / `accepted` / `superseded by NNNN` / `deprecated` |
| `日期` | 决策被接受的日期（YYYY-MM-DD） |
| `背景` | 约束与问题；为什么现在必须做决定 |
| `决策` | 选了什么 |
| `理由` | 为什么选它；被否掉的备选方案各自差在哪 |
| `后果` | 正面与负面；后续必须遵守的约束 |
| `可逆性` | 难逆 / 可逆，以及逆转需要付出什么 |

## 纪律

- **不改写已接受的 ADR**：历史决策就是历史。要改就写新 ADR，并在旧文件头部把状态改为 `superseded by NNNN`
- 新 ADR 的「理由」必须引用它 supersede 的那份，说清环境发生了什么变化
- `architecture-spec.md` §8「关键设计决策记录」是本目录的**索引**，新增 ADR 时同步补一行
- AGENTS.md 要求：破坏 API、MCP 工具或数据语义的改动必须附 ADR 和迁移路径

## 索引

| 编号 | 标题 | 状态 | 日期 |
|---|---|---|---|
| 0000 | 本索引与 ADR 约定 | accepted | 2026-09-02 |
| [0001](0001-flake-inputs-use-git-https.md) | Flake 输入使用 git+https 而非 github | accepted | 2026-09-02 |
| [0002](0002-no-devenv-use-mkshell.md) | 开发环境用 pkgs.mkShell，不引入 devenv | accepted | 2026-09-02 |
| [0003](0003-rss-cdata-passthrough-and-lxml.md) | RSS 正文以 CDATA 原样透传，并用 lxml 生成 | **部分 superseded**（净化取向被 0007 反转） | 2026-09-03 |
| [0004](0004-reader-api-promoted-to-p1.md) | Reader API（C4）从 P3 提前到 P1 | accepted | 2026-09-03 |
| [0005](0005-reader-api-credentials-and-id-mapping.md) | Reader API 的凭据与 item ID 映射 | **部分 superseded**（ID 部分被 0006 取代） | 2026-09-03 |
| [0006](0006-reader-api-ids-must-be-decimal.md) | Reader API 的 item ID 必须是十进制整数 | accepted | 2026-09-04 |
| [0007](0007-sanitize-feed-html-at-store-time.md) | 入库前用 nh3 净化正文 HTML | accepted | 2026-09-04 |
| [0008](0008-source-canonical-url-not-null.md) | source.canonical_url 必须 NOT NULL | accepted | 2026-09-04 |
| [0009](0009-derived-schedule-without-apscheduler.md) | 调度时间从策略与历史推导，不引入 APScheduler | accepted | 2026-09-04 |
| [0010](0010-mcp-tools-via-core-api.md) | MCP 工具面经 Core API 调用，用官方 mcp SDK | accepted | 2026-09-04 |
| [0011](0011-telegram-collector-decisions.md) | Telegram 采集器：t.me 原生 URL、CSS 取图、可选回溯 | accepted | 2026-09-05 |
| [0012](0012-authenticated-collectors.md) | 登录型采集器：允许使用用户自供凭据 | accepted（**修订 collector-guide 第 14 行**） | 2026-09-05 |
| [0013](0013-get-item-body-tool.md) | 允许 get_item_body 工具，以截断为条件 | accepted（**修订 AGENTS.md 第 99 行**） | 2026-09-05 |
| [0014](0014-zhihu-collector-fetch-hook.md) | 知乎采集器与注册表的可选 fetch 钩子 | accepted | 2026-09-06 |
| [0015](0015-zsxq-collector-scope-query-param.md) | 知识星球采集器：scope 即 URL 查询参数 | accepted | 2026-09-06 |
