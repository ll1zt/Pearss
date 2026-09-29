# ADR 0040：embedding 运行时，以及它跨层的依赖放在哪一层

- 状态：accepted
- 日期：2026-09-29
- 相关：ADR 0035（检索能力，决策 5/6）、ADR 0019、issue #82、issue #120

## 背景

D10a 选定 `onnxruntime` 做本地推理。issue #120 记录了随后发现的可行性阻塞：
`uv pip install onnxruntime` 装得上但 import 失败，补 `LD_LIBRARY_PATH` 又让 numpy
缺另一个库，而 `nix shell nixpkgs#python3Packages.onnxruntime` 报
`ModuleNotFoundError`。当时的结论是「这套环境里装不起来」，本 ADR 记录**它被实测
推翻**之后确定的做法。

## 决策

### 1. Python 依赖归 uv，系统库归环境：`uv add onnxruntime` + 环境给 loader 路径

`onnxruntime` 通过 `uv add` 进入 `uv.lock`（连同它传递需要的 `numpy`——本项目
**不**在检索路径上用 numpy，向量点积仍是 `math.sumprod`）；共享库则由环境提供：

```nix
LD_LIBRARY_PATH = "${pkgs.stdenv.cc.cc.lib}/lib:${pkgs.zlib}/lib";
```

这正是 `flake.nix` 里那条分工的原文：**Nix owns the interpreter, system
libraries and CLI tooling; uv owns the Python dependency graph**。所以这个决定不是
例外，而是那句话的执行：`onnxruntime` 是 Python 依赖（进 `uv.lock`），
`libstdc++`/`libz` 是系统库（进环境）。

### 2. 两个入口都要有，但只有一个来源

- **devShell**：覆盖开发、CI，以及 `deploy/pearss.service`（它的 `ExecStart` 是
  经 `nix develop` 启动服务的，所以自动继承）；
- **打包启动器**（`nix/package.nix` 的 wrapper）：`nix/module.nix` 直接执行打包好的
  `pearss`，**不**经过 `nix develop`，所以那条路必须自己再导出一次。

两处都是**追加**而不是覆盖 `LD_LIBRARY_PATH`，以免部署方已有的设置被抹掉。

### 3. 由一条真跑推理的测试持有这个决定

`tests/integration/test_embedding_runtime.py` 用一个提交进仓库的极小 ONNX 图
（`src/plugins/processors/embed/fixtures/tiny-encoder.onnx`，8→384 的 MatMul，带
固定种子可复现）真跑一次推理。**探针不是回归测试**：它在某一台机器上讲过一次事实
就再也不讲了，而这条测试会因 loader 路径丢了、wheel 与解释器不再匹配、或
`flake.lock` 升级把它们拆开而失败。

## 被否决的选项

| 选项 | 为什么不做 |
|---|---|
| 把 `python313Packages.onnxruntime` 当 Python 依赖进 devShell（PYTHONPATH） | 能把真推理跑通（实测 1.27.1 可行），但运行时版本就活在 `flake.lock` 里、`uv.lock` 看不见它，与上面那条分工相反。作为**退路**保留 |
| 把 `nixpkgs#python3Packages.onnxruntime` 直接 `nix shell` | 失败原因是**选错 attr**：该 pin 里 `python3Packages` 指向 python3.14，而环境是 python3.13；`python313Packages.onnxruntime` 一直是有的 |
| 手写纯 numpy 前向（D10a 的选项 3） | 正确性要对标参考实现、速度要实测，是最大的一笔自担成本。留作再退一步的选项 |

## 后果与风险

- `flake.lock` 升级会**同时**改变系统库与解释器，而 `uv.lock` 钉住 wheel；两者
  不匹配时由上面那条测试失败告警，而不是在首次使用时静默出错；
- 环境侧固定为**两条**而不是开放枚举：再加一个原生消费者时才需要重新审视，这也是
  它没有退化成「把能找到的库全拼上」的原因；
- 打包启动器与 devShell 两处必须同步——少一处的症状是「本地能跑、部署起来
  `ImportError`」，与 issue #9 类事故同形，因此由测试覆盖而不是靠记忆。
