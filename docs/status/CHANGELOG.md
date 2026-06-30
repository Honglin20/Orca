# CHANGELOG —— 任务索引

> 每个任务完成后，在**顶部**加一条索引（1-2 句话 + commit SHA + release note 链接）。
> 最近的在上面。**不积累、不延后**——完成即记。

---

## 模板

```
## [日期] 阶段名 —— 一句话描述
- commit: <SHA>
- 详情：[release note](releases/<date>-<name>.md)
```

---

<!-- 新条目加在这里（本行下方）-->

## [2026-07-01] 阶段 6 gates/ HMIL 层 —— HumanGate 统一原语（tool_permission + agent_ask 共模型）+ HumanGateHandler（request/resolve + _broadcaster 广播协程）+ PreToolUse hook HTTP 桥（stdlib only，安全优先 exit 2 语义）+ /gate & /gate/respond FastAPI 端点 + SessionContextRegistry（claude session_id → run_id/node 映射）+ ask_user；session_id 透传 event 顶层；36 单元 + 4 integration 测试，478 全绿（+36 净增，零回归）
- commit: `<待回填>`
- 详情：[release note](../releases/2026-06-30-phase6-gates.md)

## [2026-07-01] 阶段 5-R follow-up —— 集合 bug 修复（补 `tests/__init__.py` 让 `tests.run` 可绝对导入，三个 run 测试文件原本 collection 失败）+ code-review 修复（foreach `max_concurrent<1` 编译期 fail loud / `resolve_max_iter` 非法值 fail loud 不静默降级 / 补 parallel+foreach continue_on_error 部分失败聚合透传下游的端到端测试）；442 测试全绿（+7 净增），零回归
- commit: `7bf0f97`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)（§4.1 / §4.2）

## [2026-07-01] 阶段 5-R run/ 编排层 —— Orchestrator 单指针主循环（entry→…→$end）+ Router first-match-wins 纯函数 + ExecutorAdapter（executor AsyncIterator → bus.emit 拆四参桥接）+ parallel 组（asyncio.gather + 幂等 + failure_mode 三态）+ foreach（Semaphore + locals 注入 + 聚合）+ lifecycle（run_id / 生命周期事件 / max_iter）；扩展 RunContext 加 locals/task、ExecError 加 node 字段、validator 允许 inputs/parallel 组名作 Jinja2 root；9 demo 端到端（6 零 token + 3 agent）+ 439 测试全绿（353 基线 + 86 净增，零回归），5 条铁律全过
- commit: `6fa171b`
- 详情：[release note](../releases/2026-06-30-phase5-run.md)

## [2026-06-30] 阶段 5-M schema 单轨化迁移 —— 废除 `Node.after` 双轨制，统一为 routes 单指针 + `ParallelGroup` 显式并行（diamond）；validator 9 项重排（删 ③⑤ after 校验，加 ⑩ parallel 组结构 / ⑪ 兜底 route 位置 / ⑬ entry 非组）；3 examples + 9 fixtures + 3 测试文件全改 + 文档全覆盖；353 测试全绿（323 基线 + 30 净增，零回归），零 after 字段残留
- commit: `f0d7e99`
- 详情：[release note](../releases/2026-06-30-phase5-migration.md)

## [2026-06-30] 阶段 4 exec/ 执行内核 —— Executor 接口（AsyncIterator[Event]）+ ClaudeExecutor（claude -p 子进程 + 真 translator）+ ScriptExecutor / SetExecutor + CLIRunner（asyncio subprocess + stdin pump + 超时 SIGTERM→SIGKILL）+ Jinja2 渲染；3 条架构决策覆盖（translator 归 profiles / seq 占位 / result_extractor 拆半），322 测试全绿（196 基线 + 126 新增，零回归）
- commit: `c891f75`（feat(exec): phase 4 执行内核 — ClaudeExecutor + ScriptExecutor + SetExecutor + CLIRunner + translator 真实现）
- 详情：[release note](../releases/2026-06-30-phase4-exec.md)

## [2026-06-30] 阶段 3 events/ + profiles/ + capability 校验闭环 —— Tape 唯一真相源（append-only JSONL + Lock 覆盖 seq+write+flush + resume 清残行）+ EventBus（异步 fan-out + session_id 透传）+ 幂等 reducer + CliProfile/ProviderCapabilities 命令替换层 + compile `_check_profiles`（⑨），195 测试全绿（103 基线 + 92 新增，零回归）
- commit: `1b86019`（feat(events): phase 3 事件层 + profiles 命令替换层 + capability 校验闭环）
- 详情：[release note](../releases/2026-06-30-phase3-events-profiles.md)

## [2026-06-30] 阶段 2 compile/ 解析校验层 —— YAML→Workflow + 两层校验（结构 pydantic + 语义 8 项 + warnings），103 测试全绿
- commit: `5b5ba06`（feat(compile): phase 2 解析与校验层）
- 详情：[release note](../releases/2026-06-30-phase2-compile.md)

## [2026-06-29] 阶段 1 schema/ 数据层 —— 纯数据结构地基（workflow/event/state），50 测试全绿
- commit: `d69c47c`（实现）+ `6d7dfea`（二次 review 修复：SPEC 25→21 + 测试加固）
- 详情：[release note](../releases/2026-06-29-phase1-schema.md)

