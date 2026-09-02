# 0001 · Flake 输入使用 git+https 而非 github

- 状态：accepted
- 日期：2026-09-02

## 背景

本项目按 §10.4 把 `nix flake check` 作为 CI 第一道闸门，本地与 CI 跑同一条命令。首个 flake 按 devenv 官方模板的惯用写法，把输入声明为 `github:`：

```nix
inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
inputs.devenv.url  = "github:cachix/devenv";
inputs.systems.url = "github:nix-systems/default";
```

在开发环境首次 `nix flake check` 时，三个输入全部以同一方式失败：

```
error: unable to download 'https://api.github.com/repos/.../commits/HEAD': HTTP error 403
{"message":"API rate limit exceeded for <IP>. ..."}
```

原因是 `github:` 这类 "shorthand" 输入在解析 ref（如 `HEAD`、`nixos-unstable`）时要先调 `api.github.com`。该接口对**未认证**请求按来源 IP 限流，额度远低于 git 协议端点；同一出口 IP 上多个人/CI 共享额度时，几小时内就会被打满，且失败发生在"更新 lock 文件"阶段，任何本地缓存都救不回来。

这不是偶发网络抖动：git 协议端点（`git+https://github.com/...`）在同一环境下稳定可用。

## 决策

> 注：本文的 devenv 部分已被 ADR 0002 取代（devenv 已移除）。`git+https` 决策仍然有效并继续适用。

所有 GitHub 托管的 flake 输入统一声明为 `git+https`，并带 `?shallow=1`：

```nix
nixpkgs.url = "git+https://github.com/NixOS/nixpkgs?ref=nixos-unstable&shallow=1";
devenv.url  = "git+https://github.com/cachix/devenv?shallow=1";
systems.url = "git+https://github.com/nix-systems/default?shallow=1";
```

（本 ADR 记录时开发环境还用 devenv，故文中出现 `inputs.devenv` 与 devenv 的 binary cache 配置。ADR 0002 之后 devenv 已移除，这两处不再适用；**`git+https` 这一决策依然有效**，nixpkgs 与 systems 两个输入仍然按此声明。）

## 理由

- **git+https 不走 api.github.com**：ref 解析在 git 协议内完成，不受 REST 限流配额约束
- **`shallow=1`**：只取需要的那一个 commit，不下载 nixpkgs 完整历史（否则首次 fetch 要多收几百 MB）
- **比"配 GITHUB_TOKEN"更好**：后者要求每个开发者和每台 CI 机器都配置凭据才能构建，把"clone 即用"这条 §10.4 的核心承诺变成需要前置配置；限流额度也还是会被共享消耗
- **比 `flake.lock` 里手改 url 更好**：lock 是从 `flake.nix` 生成的，改 lock 会在下次 `nix flake update` 被覆盖，等于埋雷

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| 保持 `github:` + 配置 token / `netrc` | 破坏 clone 即用；CI 需额外 secret；额度仍共享 |
| 用 `git+file://` 本地 nixpkgs 副本 | 环境不再由仓库定义，直接违背 F1/F2 |
| 只改 lock 文件里的 url | 会被 `nix flake update` 覆盖，问题复发且难诊断 |

## 后果

正面：

- 无凭据环境下 flake 输入获取稳定，CI 与本地行为一致
- 首次 fetch 因 `shallow=1` 显著变小

负面与必须遵守的约束：

- **新增输入时必须沿用 `git+https`**。若有人加回 `github:`，会在限流时以 403 形式失败，且报错指向 api.github.com 而非仓库本身——看到这个错误先检查输入 url
- `shallow=1` 的 lock 条目不含完整历史：需要 `git log` 追溯某个 nixpkgs 版本时要额外 fetch
- shorthand 语法（`github:owner/repo`）与 `git+https` 生成的 `locked` 字段不同，切换时 lock 会整体重写一次
- devenv 自身的 `inputs.nixpkgs` 仍用 `follows` 指向我们的 nixpkgs，避免闭包里出现第二份 nixpkgs

## 可逆性

可逆，成本低：改回 `github:` 只需编辑 `flake.nix` 的 url 并 `nix flake update`。但只有在"不再有无凭据构建需求"或"GitHub 放开未认证 API 限额"时才应考虑。
