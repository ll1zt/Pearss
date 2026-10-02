# ADR 0049：处理器子进程的 stderr 是日志通道——级别前缀、宿主重记、有界

- 状态：accepted
- 日期：2026-10-02
- issue：#229（本 ADR 是 #229 的交付物）
- 相关 / 修订：ADR 0018（P3 处理器链）、ADR 0047（行预算：同一子进程边界的另一条数据通道）、ADR 0048（模型健康：本条日志里的回落行就是它的观测面）、ADR 0029/0033（采集器子进程的 stderr 早已继承宿主流，处理器此前是例外）

**新增场景从 **AC-230** 起**（AC-230，见 `SCENARIOS.md`）。

## 背景

翻译处理器在每次模型回落时都会写一行

    translate model primary failed (network): ...; falling back to fallback

而近 24h 的 journal 里这行**是 0 行** —— 不是"日志太少"，是**没有人接收**：

- 宿主 spawn 子进程时 `stderr=subprocess.DEVNULL`（`app/processor_runtime.py`），stdout 被
  `contextlib.redirect_stdout` 抢去协议用途（"discard plugin prints"），于是处理器的
  `logging` 输出落在 `/dev/null`。
- 后果是诊断只能靠猜：#228 的整个"哪个模型坏了"问题，其**观测面**就是这个洞。

对照：采集器子进程的 stderr 是**继承**宿主流的（runbook 里那条 `child_error` 处置就是这么写的），
处理器是唯一的例外 —— 一个不对称，而不是一个决定。

## 决策

1. **stderr 是通道，不是洞**：子进程把 `logging` 配到 stderr（`basicConfig(stream=sys.stderr,
   level=INFO, format="%(levelname)s %(name)s %(message)s", force=True)`），写的是 Python
   logging 自己的词汇，**不是**我们另造的格式。
2. **宿主在子进程运行期间排空 stderr**，而不是等它退出：stderr 是管道，先写满 64KB 的子进程
   会**阻塞在写**上而永远不结束 —— 一个只会把"太吵"变成"卡死"的转发还不如不转发。stderr 因此
   和 stdin/stdout 一起进同一个 selector。
3. **按原级别重记，前缀是行的上下文**：每行以 `[plugin_id item_id]` 前缀记进宿主的 logger，级别
   取自行首的级别词。前缀是重点：没有它，处理器的 warning 与宿主自己的 warning 无法区分，
   `journalctl -u pearss | grep model` 什么也答不出。级别保真也是重点：警告被记成 info 就再也
   触发不了任何"对 warning 告警"的东西。
4. **无级别前缀的行按 warning 转发**（traceback、插件里的裸 `print` 到 stderr 都算观测，不是垃圾）。
5. **转发有界**：每行最多 1000 字符，每行最多 200 行，超出时记一条"child log truncated" —— 日志
   不是桶，一个在循环里打日志的处理器不能靠它把 journal 灌满；截断必须说出来，否则日志会
   静默地撒谎。

## 后果

- "哪个模型失败、原因、回落到谁"第一次可以从 journal 单独回答（runbook 的故障处置表已给出
  这条命令）。
- 处理器不需要知道这一切：它照常 `logging.getLogger(...).warning(...)`，插件面契约不变
  （ADR 0017 不被触碰）。**反面也是约束**：插件不应自己 `basicConfig`，那只会改变格式。
- 子进程的未捕获 traceback 现在也会到达 journal（以前随 stderr 一起消失）。
- 一次判定的代价：被 deadline **击杀**时，尚未读到的 stderr 尾部仍会丢（#230 的范围是让击杀也
  留下诊断）。

## 被否决

| 备选 | 否决理由 |
|---|---|
| 继续 DEVNULL，靠 `detail` 传递 | 那正是 #229 的现状：一条 detail 装不下"每个模型说了什么"，而且成功路径根本没有 detail |
| 让子进程把日志混进 stdout 协议流 | stdout 是单次 JSON 响应（`json.loads` 整段），流式日志会破坏它 —— 也不该让 logging 与协议抢同一个 fd |
| 等子进程退出后再读 stderr | 管道写满即阻塞：把"太吵"变成"卡死"，且卡的是子进程，宿主只能看着它超时 |
| 自造 `LEVEL\tmessage` 之类的格式 | 复用 `logging` 的格式器，就不会有两份格式定义要同步 |
| 全部按 INFO 重记（不做级别解析） | 对 warning 告警的通道会失明；级别是这行的语义之一 |
| 不设上限 | journal 不是桶：一行程序错误就能把它写满，而前缀还会让这些行看起来像宿主自己的 |

## 参考

- `src/app/processor_runtime.py`（`_ChildLog`、stderr 进 selector、子进程 `basicConfig`）
- `tests/integration/test_processor_logs.py`（真处理器 + 真子进程 + caplog；含灌满上界）
- `docs/operations/runbook.md`（故障处置表：怎么看、以及不要自己 `basicConfig`）
- ADR 0018（处理器链）、ADR 0047/0048（同一子进程边界的另两条通道）、ADR 0017（hookspec 冻结）
