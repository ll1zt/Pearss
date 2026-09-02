# 0002 · 开发环境用 pkgs.mkShell，不引入 devenv

- 状态：accepted
- 日期：2026-09-02

## 背景

§10.2 原本指定 `flake.nix + devenv.nix + .envrc` 的形态，首个 flake 也照此实现（`devenv.lib.mkShell`，两个 profile：default 与 crawler）。`nix flake check` 通过，但 §10.4 规定的 CI 唯一入口跑不起来：

```
$ nix develop --command just ci
error: Failed assertions:
- devenv was not able to determine the current directory.
```

根因在 devenv 2.2.1 的 `src/modules/flake-compat.nix`：

```nix
devenv.root = lib.mkDefault (builtins.getEnv "PWD");
...
assertions = [{ assertion = config.devenv.root != ""; message = "devenv was not able to determine the current directory."; }];
```

`builtins.getEnv` 在**纯求值**下返回空字符串。devenv 官方模板用 `use flake . --no-pure-eval` 绕开（`.envrc` 里放行），但 §10.4 的命令是 `nix develop --command just ci`，不带 `--impure`；`nix flake check` 同样纯求值。也就是说，devenv 需要一个我们承诺不提供的标志。

实测：加 `--impure` 可以通过（`PWD_IN_SHELL` 正确，工具链齐全），但那要把文档里的命令一起改掉。

## 决策

移除 devenv，用 nixpkgs 原生 `pkgs.mkShell` 定义 `devShells.default` 与 `devShells.crawler`。同时删除 `devenv.nix`，`.envrc` 改为不带 `--impure` 的 `use flake .`。

环境变量（`UV_PYTHON`、`UV_CACHE_DIR`、`PEARSS_ENV`、`PYTHONDONTWRITEBYTECODE`、`PYTHONUNBUFFERED`）直接写在 `mkShell` 的 `env` 里，`enterShell` 的内容变成 `shellHook`。

## 理由

- **§10.4 的命令原样可用**：`nix flake check` 与 `nix develop --command just ci` 都不需要 `--impure`。这是 F1「新机器 clone 后按 README/AGENTS.md 可进入相同环境并通过 CI」的核心承诺
- **求值保持纯**：`--impure` 允许求值读取任意环境变量，恰恰削弱 §10.5 追求的可复现性。为迁就一个框架而给整条 CI 打开这个口子不划算
- **P1 用不到 devenv 的能力**：没有 services（SQLite 是文件、APScheduler 进程内）、没有 processes、没有 tasks。devenv 在 P1 只提供了一个 shell
- **输入更少**：`flake.lock` 从 4 个输入（nixpkgs / devenv / systems / flake-utils）降到 2 个，闭包更小，供应链面更小

被否决的备选：

| 方案 | 否决理由 |
|---|---|
| 保留 devenv，命令改 `nix develop --impure` | 命令与 §10.4、README、AGENTS.md 不一致，需同步改三处文档；给整条 CI 开了 `--impure` |
| 用 `devenv-root` 输入（devenv 官方机制） | 该输入需指向随仓库移动的本地路径文件（`file+file://` 绝对路径无法进版本控制）；实测 devenv CLI 的 `init` 在 flake 模式下也不会生成它 |
| 保留 devenv，把 devenv 的能力降级为只用 shell | 为一个 shell 拖进整个 devenv 求值闭包与 2 个 flake 输入，收益为零 |

## 后果

正面：

- CI 与本地跑的是同一条、且是文档里那条命令
- 求值纯、输入少、闭包小

负面与必须遵守的约束：

- **失去 devenv 的声明式 `services.*`**。P3 要 Postgres / Redis / MinIO 时，需手写进程管理或用 `pkgs.mkShell` + 启动脚本，并在 `devenv.nix` 的位置留说明。这条代价应在 P3 重新评估：届时若服务数量证明值得，可以带 ADR 0003 引回 devenv 或改用 `process-compose-flake`
- `git-hooks`（devenv 里叫 `pre-commit`）不再由 devenv 管理。`justfile` 是钩子的唯一入口
- 环境变量在 `flake.nix` 内定义后，改动 shell 需要重新 `nix develop`（direnv 会因 `watch_file flake.nix` 自动重载）
- 本文档与 §10.2 的「`flake.nix + devenv.nix + .envrc`」表述已不一致，需同步修订 §10.2 与 §10.1 的工具职责表

## 可逆性

可逆，成本中等：恢复 `devenv.nix`、把 `mkShell` 换回 `devenv.lib.mkShell`、`.envrc` 加回 `--no-pure-eval` 即可。触发条件应为「P3 需要多个后台服务，且手写进程管理的成本超过 `--impure` 的代价」——届时以新 ADR 记录，不修改本文件。
