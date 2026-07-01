# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 11 第三波 P2.1 Semantic Output Validator 实现完成 —— 全绿（0 回归）。**

wave 3 第二项：LLM 二次语义校验。`orca/exec/validator.py` spawn 第二个 claude -p 判断 agent output
是否满足 criteria（非 shape/type），失败时 issues 作 guidance 反馈重 spawn。Rule 7 裁定：
`validate_output` 不持 bus（化解铁律 2），三类 validator_* 事件由 orchestrator loop 统一 emit；
validator 与 retry 独立预算（SPEC §11.6 deviation）。30 新测试断言 INTENT（含 fail-safe 5 路径 +
dirty-issues 归一化 + 多失败 guidance 累积 + validator/retry 双预算独立）。

- **最新 release note**：[`2026-07-02-phase11-validator.md`](../releases/2026-07-02-phase11-validator.md)
- **SPEC**：[`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.6 / §11.6

## 待办

1. **wave 3 余项**：Dialog(P2.2，agent 跑完后多轮对话)。
2. **人工 E2E（待真 TTY + ANTHROPIC_API_KEY）**：`orca run examples/with_validator.yaml`（真 claude
   validator 判断 model_class 标识符合规性；自动化证明已在 `tests/run/test_validator_orchestrator.py`）。
3. **后续 wave**：daemon(P3.2) → Skip(P4)。

## 必读文件

1. [`docs/specs/phase-11-cli-enrichment.md`](../specs/phase-11-cli-enrichment.md) §9.6 / §11.6
2. [`docs/releases/2026-07-02-phase11-validator.md`](../releases/2026-07-02-phase11-validator.md)
3. [`orca/exec/validator.py`](../../orca/exec/validator.py)（validate_output + 事件归属裁定 Rule 7）
   + [`orca/run/orchestrator.py`](../../orca/run/orchestrator.py)（`_dispatch_with_validator` loop）

## 裁定的决策（不再讨论）

1. 保持 `claude -p` CLI 子进程路线（SPEC §1.1）；D1 wave 顺序；D2 descope attach；D3 Budget 不做；D4 ask_user 确定性 tool-params 路由。
2. CLI 单壳中断不经 await-future（SPEC §11.1）；多壳路径保留给 P3。
3. Tape 是唯一 checkpoint：不另起状态序列化系统（反 Conductor）；`replay_state` 复用即 checkpoint。
4. parallel 组中间崩溃不支持 resume（SPEC §7 risk，歧义状态，exit 1）。
5. ask_user 路由参名 `orca_run_id`/`orca_node`（非 `_orca_*`，FastMCP 拒下划线前缀，SPEC §11.2）；
   register 时机前移到 spawn 前 + 按 run 批清（SPEC §11.4）。
6. **WaitExecutor 依赖 `WaitHandleRegistry` Protocol 而非直接持 `EventBus`**（SPEC §11.5，铁律 2 张力化解）。
7. **`validate_output` 不持 bus、不 emit**（SPEC §11.6，铁律 2 张力化解，Rule 7 选 B）；三类
   validator_* 事件由 orchestrator loop 统一 emit；validator 与 retry 独立预算（不共享 max_attempts）。
