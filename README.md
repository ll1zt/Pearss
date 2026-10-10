# Pearss

单用户优先、Agent-First 的 RSS+ 系统。采集 RSS/Atom 与网页内容，规范化为统一的
`ItemDraft → Item` 领域模型后存储，并通过 Reader API（FreshRSS / Google Reader
兼容）向 NetNewsWire 提供信息流；AI agent 在用户指令下经 MCP 操作控制平面，管理
采集、调度、诊断与配置。四层结构 `core → adapters → plugins → app`，领域规则与
I/O 分离，写操作幂等，审计覆盖全部 API 调用方。

**已归档（archived）。** 本项目已停止维护，代码按「原样」公开，供参考，不提供支持
或后续更新。架构说明、ADR、运行手册等文档未随此归档版发布。

Licensed under [Apache-2.0](LICENSE). Third-party attribution in [NOTICE](NOTICE).
