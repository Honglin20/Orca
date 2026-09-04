# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前任务：prof-opt architecture-first propose（2026-09-04 待提交）

实现、独立审查与契约校验已完成；按用户要求未做 E2E。因本轮未要求 commit/push，暂不写 CHANGELOG 的 commit SHA，提交后归档本任务。

## 已完成

- 三架构候选并行生成，selector 融合为唯一架构后进入 implementer/assessor/MFU。
- latency 严格优于 incumbent 即训练；accuracy success 后晋升，origin target 冻结。
- active path 移除 op-delta 硬门；补 Ascend reference、Web 文档清单和 lineage。
- 两轮独立只读 review 的 high/medium findings 全部关闭。
- 验证：98 + 17 + 7 tests passed；diff/compile/bash/tars validate 通过。

## 必读文件

- `docs/specs/prof-opt-architecture-first-design-draft.md`
- `docs/plans/2026-09-03-prof-opt-architecture-first.md`
- `workflows/prof-opt/agents/po_propose/agent.md`
- `workflows/prof-opt/agents/po_probe/agent.md`
- `workflows/prof-opt/workflow.yaml`
- `docs/releases/2026-09-04-prof-opt-architecture-first.md`

## 真机清单（归用户）

- v7 真机 E2E（in-session + claude 后端 + tars skill）：内网评测工具真实行为、
  watchdog SIGTERM/早停/等锚、agent 选卡真机分配
- 旧 v6 工作区复跑需 `fresh_start=true`（BASELINE.lock v2）

## 遗留 backlog（非阻塞）

- 仓库预存 422 测试失败（旧扁平 workflows/*.yaml 布局引用 + playwright 环境），
  与 prof-opt 无关，需另立任务收敛
- playwright 9d 补用例（环境修复后）

## 工作区遗留（非任务，不动）

- `.e2e_po/`、`.e2e_spe2e/`、`.e2e_perfver/`、`.e2e_scratch/` scratch（untracked，不提交）
- `tests/e2e_phase14/` tape 产物（测试运行产物，不提交）
