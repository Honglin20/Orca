# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 4（exec/ 执行内核）已完成。

- **状态**：✅ 已完成（322 测试全绿：schema 50 + compile 53 + events 45 + profiles 32 + compile-profiles 8... + exec 8 + exec/claude 17... 详见 release note，零回归）
- **release note**：[`docs/releases/2026-06-30-phase4-exec.md`](../releases/2026-06-30-phase4-exec.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 5：run/（编排层）。参考 [`docs/TASK.md`](../TASK.md) §3 / §6 + [`docs/PLAN.md`](../PLAN.md)。
核心：`orchestrator = make_orchestrator(wf)` → 拓扑 / 并行 / foreach 分批 / 路由 first-match-wins / 循环控制；
orchestrator 拿 executor 产出的 `AsyncIterator[Event]` 逐个 `bus.emit(..., session_id=...)` + 写 tape（**phase 4 executor 不写 tape，归此层**）；
retry / interrupt / checkpoint_resume 在此层或后续。
phase 4 留给 phase 5 的契约：`make_executor(node) -> Executor`、`async executor.exec(node, ctx) -> AsyncIterator[Event]`、`RunContext(inputs, outputs, run_id)`（node 间累加 outputs 构造新实例）；
Event.seq=0 占位需 orchestrator 在 `tape.append` 重分配。

## 阶段 2 遗留给 run/ 的运行时校验（勿忘）

compile/ 只做**静态/浅**校验；以下归 run/（运行时才知道上下文）：
- `.output.field` 字段级存在性/类型（compile 只查 node 名）
- foreach `source` 的字段是否为数组、元素格式是否符合 body 期望
- **「无 route 命中」死锁检测**：SPEC §1 的「routes 全条件无兜底」warning 未做静态实现
  （会对枚举穷尽型 router 如 nas.reviewer 误报），改由 run/ 在运行时精确判「无 route 命中」
- 路由条件（Jinja2 `when`）求值、模板 render
