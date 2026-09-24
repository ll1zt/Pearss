# fulltext 处理器 — 验收场景（behavior-first-testing）

场景编号登记表。测试文件 `test_processor.py` 头部声明与该表机械对账；
实现前先跑红，红证据进 PR。

## AC-1 用户订阅的摘要源，新条目自动带全文入库
  Given 一个只在 feed 里输出摘要的源，处理链为 [{"plugin_id":"fulltext","kind":"fulltext"}]
  And 条目 URL 指向的原文页含完整文章正文
  When 新条目被采集并进入处理链
  Then enrichment 行成功，产物是从原文页抽取的正文（经 ADR 0007 消毒）
  And 发布闸门打开，条目正常进入 reader 视野

## AC-2 用户订阅的源站临时不可达时，摘要不丢
  Given 处理链含 fulltext 的源
  And 条目 URL 的原文页不可达（超时 / 5xx / 私网地址被 SSRF 守卫拒绝）
  When 处理器尝试抽取
  Then 抛出 ProcessorError（network/blocked），摘要条目仍保留，闸门由重试/失败语义接管

## AC-3 用户已经能读到全文的条目，不再重复抓原文页
  Given 条目正文可见文本长度已不低于阈值
  When 处理链含 fulltext
  Then 处理器不发起任何网络请求，产出空产物（成功），闸门照常打开

## AC-4 页面上没有可抽取的正文时，不假装成功
  Given 原文页返回 200 但不含可识别的文章正文（长度低于下限）
  When 处理器抽取
  Then 抛出 ProcessorError(parse)，条目保留摘要等待人工/重试

## AC-5 摘要里的「阅读全文」链接在 reader 里不再可用时，用户仍能看到标题
  Given 处理链含 fulltext 且条目无正文（title-only）
  When 处理器运行
  Then 不发网络请求，产出空产物（成功），与 title-only 翻译条目同一处理

## AC-6 私网/回环地址默认拒绝（SSRF 约束），测试用环境开关放行
  Given 条目 URL 解析到私网地址且未设置 PEARSS_ALLOW_PRIVATE_FETCH
  When 处理器尝试抓取
  Then 抛出 ProcessorError(blocked)，不发出任何请求

## AC-7 用户存量条目可通过既有 backfill 机制补全文
  Given 源启用 fulltext 链之前的已发布条目
  When 调用 backfill
  Then 为存量条目排队 fulltext 产物（复用现有机制，本插件不新增 API）
