# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 1（schema/ 数据层）已完成。

- **状态**：✅ 已完成（commit `d69c47c`，43 测试全绿）
- **release note**：[`docs/releases/2026-06-29-phase1-schema.md`](../releases/2026-06-29-phase1-schema.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 2：exec/ 执行内核（SPEC 待写）。参考 [`docs/TASK.md`](../TASK.md) §10「开发阶段」。
开工前先写对应阶段 SPEC（`docs/specs/phase-2-exec.md`）再实现。

## 阶段 1 遗留给 compile/ 的强制校验（勿忘）

schema 层刻意未做的结构校验，须在 compile/ 阶段补齐（SPEC §2.4）：
- 所有顶层 node `name` 非空 + 全局唯一（schema 层 name 可选，是为 foreach 无名 body 让路）
- `entry` 必须是某个 node 的 name
- `after` / `routes[].to` 引用合法（已定义 node 或 `"$end"`）
- `after` 静态边无环（routes 条件边允许回指）
