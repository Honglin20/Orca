# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**阶段 1：schema/ 数据层**

- **状态**：🟡 待开始（计划中）
- **SPEC**：[`docs/specs/phase-1-schema.md`](../specs/phase-1-schema.md)
- **TASK1 完整描述**：SPEC §10

## 必读文件（开工前读完）

1. [`/CLAUDE.md`](../../CLAUDE.md) —— 协作规则
2. [`docs/TASK.md`](../TASK.md) —— 全局架构决策
3. [`docs/specs/phase-1-schema.md`](../specs/phase-1-schema.md) —— 本阶段契约

## 待办

- [ ] 写实施计划 `docs/plans/2026-06-29-phase1-schema.md`（等监工确认）
- [ ] 实现 `orca/schema/`（workflow.py / event.py / state.py / __init__.py）
- [ ] 写测试 `tests/schema/`
- [ ] 写 examples（nas.yaml / parallel_research.yaml / batch_assess.yaml）
- [ ] pyproject.toml（uv + hatchling + pydantic>=2.0）
- [ ] 自我 review（分发 review agent）
- [ ] 写 release note `docs/releases/2026-06-29-phase1-schema.md`
- [ ] 更新 CHANGELOG + 清空本文件

## 完成标志

见 SPEC §7 验收标准（全部勾选）。
