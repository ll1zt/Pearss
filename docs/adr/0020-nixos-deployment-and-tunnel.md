# 0020 · 部署形态：NixOS package + module，隧道双入口

- 状态：accepted
- 日期：2026-09-13
- 关联：ADR 0016（公网暴露）、ADR 0017（P2 插件冻结）、ADR 0009（推导式调度）
- 部署目标：一台 aarch64 Android 平板上的 Droidspaces NixOS 容器（示例主机名 `pearss-host`）

## 背景

服务此前只有开发机上的 systemd **用户**服务（`deploy/pearss.service`），
跑在 `nix develop` 里、指向 `%h/dev/Pearss`。要接 Tailscale Funnel 做隧道复测，
需要先有一个真正的部署形态。

目标主机的实测约束（全部来自对谈记录与实测，非推断）：

- **aarch64**，且无官方 binary cache 覆盖全部 Python 依赖（lxml/nh3/cryptography
  需 aarch64 wheel 或本地构建）
- **Droidspaces 容器**：共享 Android GKI 内核；`/dev` 白名单；cgroup 仅委派 memory
- **nix sandbox 不可用**：内核 `mount_too_revealing()` 拒绝在 userns 内挂 `/proc`。
  这是上游 Linux 5.12+ 的通用机制与 Droidspaces 的 mount-hardening 相冲突，
  非 Android 特有，且容器内无法绕过
- **入站包按监听 socket 的 UID 过滤**：`(uid % 100000) < 10000` 无条件放行，
  ≥ 10000 需在 netd 的 `uid_owner_map` 白名单内，否则内核层静默丢包

## 决定

| 决策点 | 选择 | 说明 |
|---|---|---|
| 打包 | `packages.default`：从 `uv.lock` 建 venv，**不重复声明依赖** | 保持 section 10.1 的职责线；uv 仍是 Python 依赖图的唯一权威 |
| 可复现性 | `uv venv --relocatable` + `--no-install-project` + `dontFixup` | 三者缺一则两次构建不一致（实测：43 → 8 → 0 个差异文件） |
| FOD vs 普通 derivation | **普通 derivation** | venv 必然引用 store（`.venv/bin/python` 符号链接 + `pyvenv.cfg` 的 `home=`），而 FOD 禁止 store 引用——这是结构性限制，不是配置问题 |
| 服务用户 | **固定 UID 993**，不用 `DynamicUser` | 见下方 UID 约束；DynamicUser 分配高位 UID，服务会监听正常但不可达 |
| 加固 | `ProtectSystem=strict` 等实测通过的集合 | **不含 `IPAddressDeny`**：容器内核拒绝向 unit cgroup 附着 BPF 程序 |
| 密钥 | `environmentFile`（sops 或 0400 文件） | 绝不进 Nix store（store 全局可读） |
| 公网入口 | **nginx 做路径过滤**，而非应用内判断 | 见下方 |

### UID 约束（本部署最关键的一条）

Android netd 的 cgroup ingress eBPF 程序按**监听 socket 的 UID** 过滤入站包。
判据（AOSP `netd.c` / `netd.h`）：

```c
is_system_uid(uid) = ((uid % 100000) < 10000)   // 早返回，不查 map
```

失败的形态极具误导性：服务启动、日志显示正在监听、容器内 `curl` 正常，
但**从 tailnet 不可达**——看起来像防火墙或隧道问题。实测对照：

| 监听进程 uid | 宿主可达 |
|---|---|
| 1000（现有服务） | ✅ |
| 65532 / 30001+ | ❌ Timeout |

因此 module 固定 `uid = 993` 并附完整理由，`DynamicUser` 不可用。

### 为什么路径过滤在 nginx 而不在应用内

Funnel 不支持按路径过滤：`--set-path` 是**给底层服务加前缀**（往请求上贴路径），
不是限制哪些路径到达后端；且 Funnel 的目标是**整个服务**。经 `tailscale funnel
--help` 核实，无路径过滤能力。

选择在 nginx 层过滤而非应用内加"请求是否来自公网"的判断，理由：

- **应用保持单一信任模型**：到达应用的请求都已通过其入口的规则。"这条来自
  funnel 吗"是第二个、更弱的信任边界
- **可测试**：nginx 配置可以 curl 验证；应用内的来源判断需要测试伪造该区分，
  且错了不会有测试发现
- 与 ADR 0016 原有设计一致（当时是 Cloudflare Tunnel 按路径路由）

## 已实测验证（非推断）

全部在目标主机上执行：

| 项 | 结果 |
|---|---|
| `nix build .#packages.aarch64-linux.default` | 成功；aarch64 wheel 全部解析 |
| 两次构建逐字节比对 | 一致（0 差异文件） |
| 服务在完整加固集下启动 | 是；`/healthz` 200 |
| `ExecStartPre` migration 在同一加固下 | exit 0 |
| 4 个沙箱采集子进程 | 全部启动 |
| `/v1/sources` 无 token / 有 token | 401 / 200 |
| module 求值 | UID 993、迁移在启动前、`EnvironmentFile` 指向密钥文件 |
| tunnel module 求值 | nginx 监听 127.0.0.1:10000；reader 路径代理到 :8000；其余 404 |

## 过程中发现并修复的真实缺口

**沙箱子进程的 env allowlist 缺 `PYTHONPATH`。** 开发机上被可编辑安装掩盖
（`plugins` 通过 venv 的 `.pth` 可见），打包部署（`--no-install-project`）下
子进程在 import 阶段即 `ModuleNotFoundError`，所有沙箱采集器报崩溃。
已修复并加回归测试。

**沙箱子进程的 DNS 未 hermetic（11 个集成测试长期失败）。** `conftest.py` 的
`resolve_host` monkeypatch 到不了独立解释器，子进程真实解析 `t.me` 得到私网地址，
被 SSRF 守卫正确拒绝。新增 `PEARSS_TEST_DNS` 跨进程传递 hermetic 答案——
**不**复用 `PEARSS_ALLOW_PRIVATE_FETCH`，那会关掉守卫而非固定其输入。
这两个缺口的共同教训：**开发形态的便利会掩盖部署形态的必需项**。

## 后果

- `host-config` 侧只需引入本 flake 的 `nixosModules.default` + `nix/tunnel.nix`，
  配置片段与步骤见 `nix/DEPLOY.md`
- 隧道复测清单见 `docs/operations/tunnel-drill.md`（含 XFF、限速按真实地址、
  公网仅 Reader API 的判据）
- **无沙箱构建**：本机构建的产物不具备"可复现构建"的保证。对从 lockfile
  安装的单用户服务可接受；`nix/package.nix` 头部与本节均记录，不隐去
- 若未来迁到 x86 真机，`packages.default` 与 module 可直接复用（UID 约束与
  sandbox 约束随之消失——那是部署环境的限制，不是配置的）
