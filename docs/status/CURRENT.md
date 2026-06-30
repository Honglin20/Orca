# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 5-R（run/ 编排层）已完成。

- **状态**：✅ 已完成（439 测试全绿：353 基线 + 86 净增，零回归；5 条铁律全过；9 demo 端到端全跑通）
- **release note**：[`docs/releases/2026-06-30-phase5-run.md`](../releases/2026-06-30-phase5-run.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 6 CLI（typer/click 命令绑定：`orca run` / `orca graph` / `orca validate`）。
参考 [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md)（三壳共同契约）。

phase 5-R 提供给 phase 6 的契约：
- `run_workflow(wf, inputs, task, max_iter, tape_path, run_id) -> RunState`（`from orca.run import run_workflow`）
- `python -m orca.run <yaml> [task] [-i k=v]... [--max-iter N]` 最小入口已就位（phase 6 包装成 typer 子命令）
- 事件流经 EventBus（subscribe → WS 推 / GET /api/state 读 tape，phase 7）
- Tape 落 `./runs/<run_id>.jsonl`，`replay_state(tape)` 重建 RunState

## phase 5-R 遗留（非阻断，后续可优化）

- `fail_fast` 在 gather 语义下与 `all_or_nothing` 等价（真正的「不等其余」需 `asyncio.wait(FIRST_EXCEPTION)`）
- `run_workflow` 返回值未带 tape_path（调用方需猜 `./runs/<run_id>.jsonl`，phase 6 CLI 完善时补）
- foreach `_eval_source_array` 仅支持 JSON 兼容的字符串化数组（Python repr 风格如 `[True, None]` 不支持）
