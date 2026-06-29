# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 2（compile/ 解析与校验层）已完成。

- **状态**：✅ 已完成（commit `5b5ba06`，103 测试全绿：schema 50 + compile 53）
- **release note**：[`docs/releases/2026-06-30-phase2-compile.md`](../releases/2026-06-30-phase2-compile.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 3：events/（EventBus + tape 持久化）。参考 [`docs/TASK.md`](../TASK.md) §3 / §10 + [`docs/PLAN.md`](../PLAN.md)。
开工前先写对应阶段 SPEC（`docs/specs/phase-3-events.md`）再实现。

## 阶段 2 遗留给 run/ 的运行时校验（勿忘）

compile/ 只做**静态/浅**校验；以下归 run/（运行时才知道上下文）：
- `.output.field` 字段级存在性/类型（compile 只查 node 名）
- foreach `source` 的字段是否为数组、元素格式是否符合 body 期望
- **「无 route 命中」死锁检测**：SPEC §1 的「routes 全条件无兜底」warning 未做静态实现
  （会对枚举穷尽型 router 如 nas.reviewer 误报），改由 run/ 在运行时精确判「无 route 命中」
- 路由条件（Jinja2 `when`）求值、模板 render
