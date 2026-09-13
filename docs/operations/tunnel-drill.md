# 隧道复测清单（部署后执行）

> 目的：验证"配置正确性"——这是安全 posture 里唯一无法靠单元测试证明的部分。
> 每条都给出可执行命令与预期结果；**预期值是判据，不是描述**。
>
> 前置：`services.pearss` + `nix/tunnel.nix` 已 switch 生效。
> 在 pearss-host 上执行，标注 `[公网]` 的从外部网络执行。

## 0. ACL 前置检查

Funnel 需要在 tailnet ACL 里显式授权该节点：

```jsonc
// 访问控制 → nodeAttrs
{ "target": ["pearss-host"], "attr": ["funnel"] }
```

```bash
tailscale funnel status        # 未授权时报 "not allowed to use Funnel"
```

预期：`https://pearss.example.ts.net:10000 (Funnel on)`，或未授权时的明确报错。

## 1. 两条入口的路由边界

```bash
# tailnet 侧：整应用可达
curl -s -o /dev/null -w "serve healthz: %{http_code}\n" \
  https://pearss.example.ts.net:8443/healthz          # 期望 200
curl -s -o /dev/null -w "serve /v1:     %{http_code}\n" \
  https://pearss.example.ts.net:8443/v1/sources       # 期望 401（无 token）

# 公网侧：只有 Reader API
curl -s -o /dev/null -w "funnel reader: %{http_code}\n" \
  https://pearss.example.ts.net:10000/api/greader.php/reader/api/0/token
                                                             # 期望 200
for p in /v1/sources /mcp/mcp /healthz /docs /views/health; do
  printf "funnel %-14s %s\n" "$p" \
    "$(curl -s -o /dev/null -w '%{http_code}' \
        https://pearss.example.ts.net:10000$p)"
done
```

预期：上表**五项全为 404**。任何一项不是 404 都意味着公网多暴露了一个面——
`/v1` 或 `/mcp` 泄漏是安全事件，`/docs` 泄漏是 schema 泄漏（`PEARSS_DOCS_ENABLED=0`
本应也让它在 tailnet 侧消失）。

## 2. XFF 传递（限速按真实客户端地址计数的前提）

这是本次复测的核心。应用从 `X-Forwarded-For` 取客户端地址；如果 nginx 没设该头，
所有公网请求都会呈现为 `127.0.0.1`，限速就会把所有人的失败算在同一个桶里。

```bash
# 从公网发起，看审计行里的 actor/来源地址
curl -s -o /dev/null https://pearss.example.ts.net:10000/api/greader.php/reader/api/0/token

# 立刻查最近的认证失败审计（或用一次错密码触发一条 401）
sqlite3 /var/lib/pearss/pearss.db \
  "select occurred_at, actor, operation, target from audit_log
    where operation like '%greader%' or target like '%greader%'
    order by occurred_at desc limit 5;"
```

**判据**：审计/日志中的客户端地址是**你公网的真实出口 IP**，不是 `127.0.0.1`。
若是 `127.0.0.1`，说明 `proxy_set_header X-Forwarded-For` 未生效——
检查 `services.nginx.virtualHosts."pearss-funnel".locations."/api/greader.php".extraConfig`。

## 3. 限速按真实地址计数

```bash
# 连续发 N 次错误凭据，观察是否在预算内被阻断
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST https://pearss.example.ts.net:10000/api/greader.php/accounts/ClientLogin \
    -d "Email=reader&Passwd=wrong-$i")
  [ "$i" = 1 ] || [ "$i" = 50 ] || [ "$i" = 51 ] || [ "$i" = 60 ] && echo "attempt $i -> $code"
done
```

**判据**：默认预算 50 次/5 分钟。第 50 次前后开始出现 **429**，且阻断持续 15 分钟
（`PEARSS_AUTH_RATELIMIT_BLOCK_SECONDS`）。修复前的症状是 60 连发无阻尼——
这与 ADR 0016 审计时实测到的一致。

阻断后验证它按地址而非全局计数：从**另一个网络**（如手机热点）访问同一端点，
应仍能正常响应（那份预算尚未用尽）。

## 4. 公网请求不泄漏 token

```bash
# 公网请求一个不存在的路径，确认 404 响应体不含任何服务端信息
curl -s https://pearss.example.ts.net:10000/v1/sources | head -3
# 期望：nginx 的 404 页，无 JSON、无版本号、无 "pearss"

# 确认响应头不泄露
curl -sI https://pearss.example.ts.net:10000/api/greader.php/reader/api/0/token | head -8
# 期望：无 Server 版本细节，有 nosniff
```

## 5. Reader API 端到端（真实客户端语义）

```bash
# 登录取 token
TOKEN=$(curl -s -X POST \
  https://pearss.example.ts.net:10000/api/greader.php/accounts/ClientLogin \
  -d "Email=${PEARSS_READER_USERNAME}&Passwd=${PEARSS_READER_PASSWORD}" \
  | grep '^Auth=' | cut -d= -f2)

# 订阅列表（GET 只认 header）
curl -s -H "Authorization: GoogleLogin auth=$TOKEN" \
  "https://pearss.example.ts.net:10000/api/greader.php/reader/api/0/subscription/list?output=json" | head -c 300

# 认证不对称是硬约束：GET 用 query 传 token 必须被拒
curl -s -o /dev/null -w "GET with ?T=: %{http_code}\n" \
  "https://pearss.example.ts.net:10000/api/greader.php/reader/api/0/subscription/list?T=$TOKEN"
# 期望 401
```

**判据**：ClientLogin 返回 `Auth=`；subscription list 返回 JSON；
`?T=` 形式返回 **401**（GET 只认 `Authorization` 头，POST 只认表单 `T`）。

## 6. 沙箱出网仍走宿主代理

隧道环境下沙箱集体经宿主代理出网（ADR 0017），SSRF 守卫覆盖沙箱 egress：

```bash
journalctl -u pearss -n 100 | grep -ciE "egress_denied|zlib|AttributeError"
```
**判据**：`egress_denied` 为 0（除非真订阅了私网源）；`zlib`/`AttributeError` 为 0
（这两个是 gzip 双重解压与 handle_request 的历史回归信号）。

## 7. 记录

把 1–6 的实测结果写回本文件末尾，附日期与 curl 的实际输出。
**未实测的项目标注"未验证"，不要凭推断勾选**——ADR 0019 的教训：
静态判断协议符合度是不可靠的，隧道配置同理。
