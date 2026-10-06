# ADR 0060：CookieCloud 消费端契约——admin token、`/export` 与 `raw`

- 状态：accepted
- 日期：2026-10-06
- issue：#338、#341
- 场景：AC-322
- 相关 / 修订：**取代** [ADR 0012](0012-authenticated-collectors.md) 中「CookieCloud 的 token 比它取回的凭据更强力」一节对消费端凭据的表述（那里把 `COOKIE_CLOUD_SERVER_PASSWORD` 当作消费端凭据）；**取代** `docs/operations/env-reference.md`、`docs/operations/runbook.md`、`docs/guides/twitter-collector.md`、`README.md` 与 `src/plugins/collectors/twitter/README.md` 里把同步主口令写成消费端凭据的段落。实现见 `src/adapters/cookiecloud.py`

## 背景

ADR 0012 决定「登录型采集器可用用户自供凭据」，并让 CookieCloud 承担续期。它当时记录的消费端契约是旧服务端的：

```
GET /get/<uuid>?format=header&domain=x.com
Authorization: Bearer <COOKIE_CLOUD_SERVER_PASSWORD>
```

部署现在跑的是 CookieCloud 仓库的 **Go 服务端**（`cookiecloud:8088`，经 `tailscale serve` 暴露为 `https://cookiecloud.example.ts.net:8088`）。该服务端的 `/get` 是 v1 冻结端点：**只回密文**；被 v2 客户端写过的 uuid 连 legacy 记录都不存在（首次 v2 写入即删除整包记录），于是返回 404。旧契约不只是「换个地址」，它是**死的**。

新契约（`design/protocol.md` §11、`docs/consumer-migration.md`）：

```
GET /export/<uuid>?format=raw&domain=<domain>
Authorization: Bearer <ADMIN_TOKEN>
```

## 决定

1. **消费端凭据是 `ADMIN_TOKEN`，环境变量 `PEARSS_COOKIECLOUD_ADMIN_TOKEN`。** 同步主口令（`COOKIE_CLOUD_SERVER_PASSWORD`）只留在服务端内部用于解密，消费端不再持有。旧变量 `PEARSS_COOKIECLOUD_TOKEN` **删除**，不保留别名：两套来源就是两个事实源，而只设旧名的部署会静默发出一个服务端必拒的 token（正确的落点是「未配置 → 跳过」）。
2. **用 `format=raw`，不用 `format=header`。** 服务端对文本格式做转义（`;`→`\;`、`=`→`\=`）以防 cookie 注入下游，而 cookie 值常是 base64、以 `=` 结尾，所以那个格式给出的「请求头」是坏值。`raw` 返回扁平 JSON 数组，由消费端自己拼头——这才有可能按 `expirationDate` 丢弃过期项、按目标 scheme 丢弃 `secure` 项。
3. **`secure` 项按目标 scheme 取舍。** `fetch_cookie_header(..., secure_ok=...)` 是**必填**关键字参数：只有调用方（采集器）知道自己发的是 http 还是 https，给默认值等于允许 `secure` cookie 在明文里被静默发出。
4. **`domain=` 过滤是服务端的职责，消费端不重复实现。** 服务端用 label 后缀规则（`zhihu.com` 命中 `www.zhihu.com` 与 `zhuanlan.zhihu.com`，不命中 `evilzhihu.com`），消费端把答案原样拼成头。
5. **token 的权限大于它取回的凭据，部署必须按此收缩。** `ADMIN_TOKEN` 除了导出任意 uuid、任意域名，还能 `DELETE /records/<uuid>`。因此：**专用 uuid**（只放该采集器需要的站点）、token 只从环境读、绝不进日志/异常/URL。
6. **`raw` 不做转义，所以消费端负责不让它破坏头。** 名字或值里带 `;`、CR/LF 或控制字符的 cookie 被丢弃（RFC 6265 的 cookie-octet 不允许这些字符），否则一个 cookie 就能往头里注入另一个键值对。

## 已否决

- **保留 `COOKIE_CLOUD_SERVER_PASSWORD` 作消费端凭据。** 服务端已把「解密导出」与「admin 操作」分成两把钥匙；继续用主口令等于继续持有解密全部站点的能力，而且新版 `/export` 根本不认它。这不是不优选，是不可行。
- **继续用 `format=header` 并自己反转义。** 反转义是猜测：`\;` 与真正以 `\` 结尾的值不可区分。服务端把 `raw` 标为程序化消费端首选。
- **消费端再按 `domain=` 过滤一次（纵深防御）。** 看起来更安全，实际会引入第二个 label 规则：服务端的匹配是「cookie 自己的 domain **或** 它所在的分片 bucket」，而 `raw` 已把 bucket 摊平，重复实现会把服务端按 bucket 保留的 cookie 丢掉。判据见 `tests/unit/test_cookiecloud_contract.py` 的「适配器不重复过滤」用例，契约证据是 `tests/unit/fixtures/cookiecloud/` 里带与不带 `domain=` 的一对录制。
- **把目标 scheme 放进部署配置。** 目标站点是采集器的事实（和 `domain` 一样），不是部署选项；放进环境变量会让同一份部署无法同时服务 http 与 https 目标而不互相污染。

## 后果

- 运维迁移是**破坏性**的：sops 里的 `PEARSS_COOKIECLOUD_TOKEN` 必须换成 `PEARSS_COOKIECLOUD_ADMIN_TOKEN`，否则采集器静默走「未配置 → 跳过」。
- 明文 http 目标的告警保留（`_warn_if_plaintext`），但告警主体从「主口令」改为「admin token」，并写明它还能删除记录。
- 契约测试以**真实响应录制**为 fixture（`tests/unit/fixtures/cookiecloud/`），因为「fake 只有与真实系统对过契约才可信」；录制命令、时间与服务端版本记在 fixture 的 README 里。
- 该 token 的爆炸半径大于它服务的采集器，所以「专用 uuid」从建议升级为部署要求。

## 参考

`src/adapters/cookiecloud.py`、`design/protocol.md` §10/§11/§12、`docs/consumer-migration.md`、
`server/internal/export/render.go`、`server/internal/httpapi/export.go`。
