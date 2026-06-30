# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 5-M（schema 单轨化迁移）已完成。

- **状态**：✅ 已完成（353 测试全绿：323 基线 + 30 净增，零回归；零 after 字段残留；3 examples 全过）
- **release note**：[`docs/releases/2026-06-30-phase5-migration.md`](../releases/2026-06-30-phase5-migration.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 5-R：run/ 编排层（Orchestrator + Router）。参考 [`docs/specs/phase-5-run.md`](../specs/phase-5-run.md) §3 §4 + [`docs/plans/2026-06-30-phase5-run.md`](../plans/2026-06-30-phase5-run.md)。
核心：`orchestrator = make_orchestrator(wf)` → 单指针推进 / parallel 组并行 / foreach 分批 / 路由 first-match-wins / 循环控制；
orchestrator 拿 executor 产出的 `AsyncIterator[Event]` 逐个 `bus.emit(..., session_id=...)` + 写 tape（**phase 4 executor 不写 tape，归此层**）；
retry / interrupt / checkpoint_resume 在此层或后续。
phase 4 留给 phase 5 的契约：`make_executor(node) -> Executor`、`async executor.exec(node, ctx) -> AsyncIterator[Event]`、`RunContext(inputs, outputs, run_id)`（node 间累加 outputs 构造新实例）；
Event.seq=0 占位需 orchestrator 在 `tape.append` 重分配。

**phase 5-M 提供给 5-R 的契约**（schema 已变，编排按此实现）：
- `Node` 只有 `routes`（无 after）；`Workflow.parallel: list[ParallelGroup]`。
- `Router.resolve(routes, output, ctx) -> str`：first-match-wins，无兜底且全不匹配 → NoRouteMatchError。
- parallel 组：`asyncio.gather(branches)`，已执行 branch 跳过（幂等），failure_mode 三态。
- entry 必须是 node；遇 parallel 组则并行执行其 branches 后按组 routes 推进。

## 阶段 2 遗留给 run/ 的运行时校验（勿忘）

compile/ 只做**静态/浅**校验；以下归 run/（运行时才知道上下文）：
- `.output.field` 字段级存在性/类型（compile 只查 node 名）
- foreach `source` 的字段是否为数组、元素格式是否符合 body 期望
- **「无 route 命中」死锁检测**：所有 when 不匹配且无兜底 route → NoRouteMatchError → workflow_failed
  （静态只校验兜底位置 ⑪，不判运行时是否命中）
- 路由条件（Jinja2 `when`）求值、模板 render
