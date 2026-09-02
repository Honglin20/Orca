# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前任务：无（prof-opt v7 已完成，2026-09-02 收尾归档）

上一任务：prof-opt v7 重设计——mfu 唯一链路 + 判断力归还 + 校验全覆盖。
见 CHANGELOG 索引 + `docs/releases/2026-09-02-prof-opt-v7.md`（SPEC：`docs/specs/prof-opt-v7-spec.md`）。

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
