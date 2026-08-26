# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## prof-opt v4 重构 —— **已完成**（2026-08-26；mfu 模式 E2E 待用户真机自跑）

**终态**：v4 全部交付（含 D-V4-20 profiling 子代理化），13 commits `86ccf99..9ae6438`；134 单测绿 + tars validate 0 warning + 18 份 prompt 文件逐一独立 reviewer 全 CLEAN（`verify/cleanliness/`）+ 本地 placeholder 模式 E2E 2 轮真执行（mnist success / gate 零解堵自然过）。详见 CHANGELOG [2026-08-26] + `docs/releases/2026-08-26-prof-opt-v4-refactor.md`。

**待用户（真机 mfu 模式 E2E）**：① 用真实脚本替换 `workflows/agents/_po_scripts/mfu_benchmark.py` 内容（文件名不变，跑 `tars install` 同步部署件）② inputs 传 `npu_chip=6613|1951`（+可选 npu_precision/npu_core_num）③ 首跑若 `mfu_adapter.py` exit 2 = 真实产物字段与文档契约的出入点，stderr 指名缺什么（改适配层或修真脚本输出二选一）④ 断言集见 SPEC §7 + release note §四。

### 关键事实（后续别再踩）
- 变体注入唯一可行形态 = sitecustomize + meta path finder；循环 = DAG 回边 + po_gate 脚本轮数硬帽；in-session script 节点 spawn env 已接 project-scoped artifacts 派生（cad9ef9）
- 工作区跨 run 复用前提 = **零晋升史**（promoted 后 shadow 前进而 BASELINE.lock 锚原始基线 → 必须 fresh_start）
- tars skill 有逐字传递铁律（派发禁转述、--output 禁加叙述）——驱动层 schema 违约的防线
- E2E 后端例外：prof-opt 用 claude 后端 + tars skill（WSL）；并行子代理 ≤3-4 防 429

---

## 并行：create-workflow skill v2 —— 实现中（另一 session）

**状态**：SPEC 闭环（附 A）→ 分批实现中。必读：`docs/specs/create-workflow-skill-v2-spec.md`。进度见该任务自身记录；本任务产物（orca/skills/create-workflow/*、orca/iface/cli/install_cmds.py、tests/iface/ 等）**勿动**。

---

## 工作区遗留（非 prof-opt 任务）
- puzzle-universal 前任务 WIP（冻结）/ 2026-08-17 调研报告 / .e2e_po、.e2e_spe2e E2E scratch；详见 git status
