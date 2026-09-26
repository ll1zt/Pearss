# 0032: 半开请求头超时——在应用层实施，并记录部署层的实际拓扑

Status: accepted
Date: 2026-09-26
Context: issue #9。一个只发了半截请求头就挂住的连接（slowloris 形态）实测保持
**>120 秒**不被关闭。issue 要求的第一项验收是**先判定哪一层在保持连接**——
因为如果是 Tailscale Funnel 的 Go 边缘，改 nginx 配置就是无效修复。

## 背景：issue 的拓扑前提在本机不成立

issue 假设路径是 `Funnel -> nginx -> Pearss`。2026-09-26 在部署主机
（`pearss-host`）实测：

```bash
tailscale funnel status   # 只打印 "tailnet only"，无 "Funnel on"
ss -ltn | grep -E ':(8000|10000)'   # 只有 8000
ps aux | grep -c '[n]ginx'          # 0
curl -s -o /dev/null -w '%{http_code}' https://pearss-host...:10000/...  # 连接被拒
curl -s -o /dev/null -w '%{http_code}' https://pearss-host...:8443/...   # 401
```

与宿主配置一致（`host-config` 的 `hosts/<host>/pearss.nix`）：该主机
`funnelEnabled = false`，且 `services.nginx.enable` 被 `lib.mkForce` 强制关闭。
nginx 在本模块里的**唯一职责**是给 funnel 做 path 过滤；funnel 关掉后它没有消费者。

实际路径是 **`tailscale serve :8443 -> 127.0.0.1:8000`**，中间没有 nginx。

## 决定

### 1. 超时在应用层实施（uvicorn 协议类），不依赖部署层的 nginx

**理由不是「顺手」，而是 nginx 在本环境不存在**：给一个未运行的 nginx 加
`client_header_timeout` 是无效修复，这正是 issue 要求先判定层的原因。

uvicorn **没有**对应的配置项：`Config` 只有 `timeout_keep_alive`、
`timeout_graceful_shutdown`、`timeout_notify`、`timeout_worker_healthcheck`，
没有「请求头收到一半就停了」这一项。因此用 uvicorn 文档化的 `--http` 接缝安装
自定义协议类（`app.http_timeout:HeaderTimeoutProtocol`），继承 uvicorn 自己的 h11 实现，
只加一个「请求头进度」截止时间。这样请求解析、keep-alive、pipeline、WebSocket 升级、
400 响应等全部保持 uvicorn 上游行为，本模块的唯一主题就是那条截止时间。

**为什么不是 ASGI 中间件**：中间件在 request 对象存在之后才运行，而未完成的请求头
永远不会产生 request 对象——字节还在 h11 手里。同时持有 transport 与「尚无完整请求」
状态的只有协议层。

### 2. 语义：针对「无进展」，不是「总时长」

- 截止时间在 `connection_made` 就装上（**不是**等到第一个字节）。纯连接不发任何字节是
  最廉价的一种占用，若在首个字节才装表就完全漏掉它（注入验证：改成懒惰装表 →
  `test_a_connection_that_sends_nothing_is_closed` 变红）。
- **每个收到的字节重置窗口**，直到请求完成。慢速但在推进的客户端是合法流量，
  按总时长限制会误杀（注入验证：去掉重置 → 慢速推进测试变红）。
- **判定必须每个字节重新从 h11 推导，不能闩锁**。这是最微妙的一半，本 PR 在这里
  走错了两次，两次都只有真实 socket + 注入验证才暴露：

  - 第一次把 `IDLE` 当作「还没有请求」：h11 对**新建连接**与**已派发的请求**都报
    `IDLE`，于是截止时间到期后看到 `IDLE` 就拒绝关闭——攻击从未被拦。
  - 第二次改成「这条连接曾经完成过请求」**闩锁**，修好了 keep-alive 误杀，却开了
    一个**新洞**：客户端发一个完整请求、再发一个**不完整**的第二个请求，h11 又回到
    `IDLE`，闩锁拒绝装表 → 连接可永久占用（实测：1 秒截止时间下 4 秒后仍开着）。
    keep-alive 上的 slowloris 就是同一个攻击多送一个免费请求。

  正确的信号是 `their_state is DONE`（有完整请求在手 → 解除，空闲是合法的）与
  「其他一律装表」（解析器正在半途，覆盖新建连接与复用连接的后半段）。
- 到期直接关闭 transport。不写 408：客户端从未问完，而给一个不读的对端写响应本身
  也是资源开销。

### 3. 默认开启，且无法被配置关掉

默认 15 秒，上限 300 秒。所有不可用取值（缺失、非数字、0、负数、NaN、inf）**回落到默认**。
不提供「关闭」：一个必须被配置才存在的防护，等于在所有没读 release note 的部署上关闭。
「坏值把安全边界静默关掉」正是本决策要防的失败。

### 4. 部署层的两条路径分别记录，不混淆

- **tailnet（当前路径）**：应用层超时生效。但它**不覆盖 tailscale serve 这一段**——
  tailscale 与本后端之间的连接可能在应用关闭前一直挂着。因此本项在 tunnel 形态下
  仍保留为**已知限制**，直到有独立的边缘超时。写进 `tunnel-drill.md` §7，不假装已解决。
- **funnel（`funnelEnabled = true` 时）**：nginx 在 127.0.0.1:10000，需在
  `nix/tunnel.nix` 的 location 内**显式**设 `client_header_timeout`。issue 记录的实测
  与 nginx 默认值（60s）不符，说明「默认值」在本环境不可依赖。

## 已否决

- **只在 nginx 加配置**：本机 nginx 未运行（决策 1），且 issue 要求先判定层。
- **ASGI 中间件**：看不到未完成的请求头（见决策 1）。
- **等第一个字节再装表**：漏掉最廉价的无字节占用（注入验证过）。
- **按总时长限制请求头**：误杀慢速但合法的客户端（注入验证过）。
- **到期回 408**：对不读的对端写响应不划算，且客户端本就没问完。
- **默认关闭 / 可配置关闭**：见决策 3。
- **写成一个独立 ASGI 服务器 / 反向代理**：为一条超时引入新组件，与本仓库
  「不为一个需求引入一整套供应链」的既有判断（ADR 0028 对抽取器的同类决定）相反。

## 影响

- 新增 `src/app/http_timeout.py`；无 migration、无 API/MCP 面变化。
- 两个入口都安装它：`nix/package.nix`（部署实际启动）与 `justfile`（开发者 `just run`）。
  只装一处的修复会让人对着**另一个** socket 测试。
- 新环境变量 `PEARSS_HTTP_HEADER_TIMEOUT_SECONDS`，已写入 `.env.example` 与
  `env-reference.md`；在 env allowlist 守卫中声明为 **host-side**（父进程的协议类读取，
  沙箱子进程没有 socket 可超时，也不该看到它）。
- `[tool.mutmut] also_copy` 增加 `nix/`：新增的结构守卫读 `nix/package.nix`，
  否则单测套件会在 mutmut 树里于 stats 阶段直接死掉——门禁**什么都不报**而不是报成功。
  这是 `test_mutmut_copy_tree.py` 第四次抓到同一类问题（前三次：`justfile`、
  `.env.example`、`.forgejo`）。

## 验收

`tests/integration/test_http_header_timeout.py`（AC-25..AC-28，登记见该文件头注释）、
`tests/unit/test_structure_guards.py`（两个守卫：入口点安装 + 变量可发现）、
`tests/unit/test_env_allowlist_coverage.py`（host-side 声明）。

注入式验证（均在提交后的树上，逐个杀掉对应测试）：

| 注入 | 变红 |
|---|---|
| 完全不装截止时间（改动前的状态）| 3 条（半开/无字节/配置生效）|
| 请求头字节不重置窗口 | 慢速推进不被误杀 |
| 改为首个字节才装表 | 无字节连接被关闭 |
| 请求完成后不解除 | keep-alive 复用 |
| 闩锁「曾经完成过请求」（第二次实现引入的洞）| 复用连接上的半截第二个请求 |
| 去掉上限钳制 | 超大取值被钳制 |

`src/app/http_timeout.py` 达到 **100% 语句与分支覆盖**。

端到端验证：用**打包形态的启动参数**跑真实应用
（`uvicorn app.main:app --http app.http_timeout:HeaderTimeoutProtocol`），
半开连接 2 秒内被关闭（`PEARSS_HTTP_HEADER_TIMEOUT_SECONDS=2`），其后 `/healthz` 仍返回 200。
