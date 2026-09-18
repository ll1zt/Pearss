# 0027 · SSRF 守卫的 tailnet 豁免与拒绝原因拆分

- 状态：accepted
- 日期：2026-09-19
- 实现：`src/adapters/http.py`（`_TAILSCALE_NETWORKS`、`_refusal_reason`）、
  `tests/unit/test_http.py`（tailnet 放行 / 非_tailnet ULA 仍拒 / 原因拆分）
- 关联：ADR 0016（公网暴露加固，私有地址拒绝的来源）、ADR 0020（NixOS 部署与隧道）、
  ADR 0023（沙箱出口信任模型）
- 背景：issue #1 —— `8502cc5..72f2e0a` 把 `check_destination()` 接到沙箱子进程
  出口代理后，部署里以 tailnet 域名配置的内部服务（CookieCloud、translate
  gateway）被整体拒绝（54/54 源失败），被迫全局关闭 SSRF 守卫规避。

## 裁定 1：Tailscale 两个网段豁免私有地址拒绝

`100.64.0.0/10`（CGNAT）与 `fd7a:115c:a1e0::/48`（Tailscale ULA）在本部署里是
**运行者自己的网络**：实例本身 tailnet 托管（ADR 0020），内部服务是 tailnet
服务，采集器必须能到达它们。守卫的意图是阻止被泄漏的令牌探测 RFC1918、
链路本地、元数据端点等「非运行者控制面」的内网；tailnet 不在其列——那条路径
由运行者的 ACL 把守（ADR 0016 的暴露面分析本就以 tailnet 为边界）。

豁免**只覆盖这两个网段**：其余 ULA（fc00::/7 的非 Tailscale 分配）、RFC1918、
loopback、link-local、保留与多播地址维持拒绝。`PEARSS_ALLOW_PRIVATE_FETCH`
保持原语义，作为对全部私有地址的整体开关。

已知权衡：被泄漏的写令牌可以探测 tailnet 上的其他主机。接受它，因为
（a）持有写令牌的调用方经 Core API 本就能造成更大的影响面；
（b）拒绝 tailnet 等于让本部署的核心依赖（凭据同步、翻译）无法工作，
「功能不可用」不是安全目标的正确兑现方式。

## 裁定 2：拒绝 detail 必须区分原因并携带目标

私有地址与解析失败是两种故障，运维动作不同（查解析目标 vs 查 DNS）。
统一为：

- `private-address fetch refused for <redacted-url>`
- `unresolvable-host fetch refused for <redacted-url>`

此前两者共用一句 `private-address fetch refused`，且沙箱出口路径的 detail
不带 URL——issue #1 记录的真实排障过程先被引向 DNS/知乎封禁，方向全错。

## 后果

- 部署方在升级后可移除 `PEARSS_ALLOW_PRIVATE_FETCH=1` 的临时规避，恢复守卫。
- ADR 0016 的私有地址定义按本 ADR 收窄（tailnet 两网段除外），其余条款不变。
- `any()` 语义保留：混合应答中任一私有地址即拒——这是对 DNS rebinding 的
  抵御，tailnet 豁免已让内部服务的双栈应答自然通过，无需放宽。
