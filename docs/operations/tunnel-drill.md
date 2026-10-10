# 隧道复测清单（部署后执行）

> 目的：验证"配置正确性"——这是安全 posture 里唯一无法靠单元测试证明的部分。
> 每条都给出可执行命令与预期结果；**预期值是判据，不是描述**。
>
> 前置：`services.pearss` + `nix/tunnel.nix` 已 switch 生效。
> 在部署主机上执行（下文 `pearss-host` / `pearss.example.ts.net` 均为占位符，
> 替换为你自己的主机名与 tailnet 域名），标注 `[公网]` 的从外部网络执行。

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
for p in /v1/sources /mcp /healthz /docs /views/health; do
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

阻断期间 liveness 不应被连带（issue #6）：被阻断的来源访问 `/healthz` 应拿到
**应用的真实应答（200/503）而非 429**——监控用它区分「应用挂了」与「我的来源
被封了」。修复前的症状是阻断期 `/healthz` 也返回 429，liveness 语义被顶替
15 分钟。

## 4. 公网请求不泄漏 token

```bash
# 公网请求一个不存在的路径，确认 404 响应体不含任何服务端信息
curl -s https://pearss.example.ts.net:10000/v1/sources | head -3
# 期望：nginx 的 404 页，无 JSON、无版本号、无 "pearss"

# 前缀内的精确路径（issue #7 的空隙）：nginx 按前缀转发 /api/greader.php，
# 所以精确路径 /api/greader.php 本身也会落到应用，由 MCPPathGuard 兜底 404。
curl -s https://pearss.example.ts.net:10000/api/greader.php | head -3
# 期望：应用 404 JSON 可以接受，但响应体（整包，含 suggestion）不得包含
#       "mcp" 或任何端点名/环境变量名——否则等于向公网广播 MCP 端点存在。

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

## 7. 半开请求头超时（慢速资源占用）

**这一节回答的问题**：一个只发了半截请求头就挂住的连接，多久会被服务端关掉？

**先确认本机是哪条路径**——这决定了改哪一层。2026-09-26 实测本机（`funnelEnabled = false`，
见 `hosts/<host>/pearss.nix`）的实际拓扑：

```bash
tailscale funnel status        # 本机实测：只打印 "tailnet only"，没有 "Funnel on"
ss -ltn | grep -E ':(8000|10000)'   # 只有 8000；没有 10000
ps aux | grep -c '[n]ginx'          # 0：本机 nginx 未启用
```

结论：**本机没有 nginx、也没有 funnel**。路径是
`tailscale serve :8443 -> 127.0.0.1:8000`。因此 issue #9 设想的「Funnel → nginx → Pearss」
在本机不成立——那个形态只在 `funnelEnabled = true` 时存在（那时 nginx 才监听 127.0.0.1:10000）。
判据：**先看 `funnelEnabled`**，再决定测哪条路径。

```bash
# 应用层（uvicorn）直连：这是所有路径的最后一段
check_half_open_header() {
  local host="$1" port="$2" wait="${3:-15}" rc=0
  exec 3<>"/dev/tcp/$host/$port" || return 2
  printf 'GET /p HTTP/1.1\r\nHost: x\r\nX-S: ' >&3 || rc=3
  [ "$rc" = 0 ] && { timeout "$wait" cat <&3 >/dev/null || rc=1; }
  exec 3<&- 2>/dev/null
  return "$rc"
}
check_half_open_header 127.0.0.1 8000 15 && echo "closed in time" || echo "STILL OPEN (no header timeout)"
# 期望（修复后）：15 秒内被关闭
# 实测（2026-09-26，未修复）：15s / 120s 均仍为 STILL OPEN

# 经 tailscale serve 的 TLS 一段（python3 只有标准库依赖）
python3 - <<'PYEOF'
import socket, ssl, sys, time
host, port, wait = "pearss.example.ts.net", 8443, 60.0
ctx = ssl.create_default_context()
with socket.create_connection((host, port), timeout=8) as raw:
    with ctx.wrap_socket(raw, server_hostname=host) as s:
        s.sendall(b"GET /p HTTP/1.1\r\nHost: x\r\nX-S: ")
        s.settimeout(wait)
        start = time.monotonic()
        try:
            s.recv(64); print("closed in time")
        except socket.timeout:
            print("STILL OPEN after %.0fs" % wait)
PYEOF
# 实测（2026-09-26，未修复）：60s 仍为 STILL OPEN
```

**判据**：修复后两段都应在 N 秒内关闭（N 由修复决定，当前定为 15 秒量级）。
未修复时两段都保持——即「无短超时」是**应用层与 tailscale 两段共有**的行为，
修一层不够。

**修复落点**：`.env` 的 `PEARSS_HTTP_HEADER_TIMEOUT_SECONDS`（默认 15）。
应用层超时消灭了直达 uvicorn 的半开连接；但**它不解决 tailscale serve 这一段**
（tailscale 与后端的连接可能在应用关闭前一直挂着），所以本项在 tunnel 形态下
仍应保留为已知限制，直到有独立的边缘超时。`funnelEnabled = true` 的部署
（nginx 在 10000）另需在 `nix/tunnel.nix` 的 location 内显式设
`client_header_timeout` —— nginx 的默认值在本环境不可依赖（issue #9 记录的实测
与默认值不符）。

## 8. 记录

把 1–7 的实测结果写回本文件末尾，附日期与 curl 的实际输出。
**未实测的项目标注"未验证"，不要凭推断勾选**——ADR 0019 的教训：
静态判断协议符合度是不可靠的，隧道配置同理。
