# Stage 3：统一 headless TARS-SKILL E2E（禁 CLI 驱动 workflow）

> 来源：[redesign plan §6](2026-07-21-workflow-redesign.md)。本文件是 Stage 3 实施计划，不重复 SPEC。

## 范围（受限于无真模型/数据集/GPU）

本环境**无真模型/数据集/GPU**，全量真 run 不可行（那是用户手动测的事）。Stage 3 落在三层可行验证：

1. **结构/契约验证**（8 workflow，静态 + 动态 bootstrap，不需真模型）
2. **DAG walk E2E**（单节点 quant 系走完到 done:true；多节点 bootstrap + 首跳，证明链不破）
3. **哨兵路径 E2E**（≥1 workflow，mock 子 agent：哨兵→恢复→真实 output→done:true）

## 禁用模式

- ❌ `orca run`（后端 CLI 自驱）
- ❌ 手搓 `orca next` 循环绕过 TARS（`e2e_check.sh` 式）
- ✅ 经 TARS skill 行为投影（`tars_loop.drive_workflow` / 新 `walk_dag`），调 `orca <wf> --inputs` + `orca next --run-id` —— 这是 TARS skill 内部调的命令，允许

## 复用 spike 基建

`tests/spike_ask_user/`：`SubagentBackend` ABC、`MockSubagentBackend`、`ClaudeCliBackend`、`tars_loop.drive_node/drive_workflow`、`orca_cli.bootstrap/next_step/stop`、`sentinel.*`。**零重造**。

## 交付物（`tests/e2e_redesign/`）

| 文件 | 职责 |
|---|---|
| `contract.py` | 纯 Python 静态契约：inputs 解析 / 无 `{{ inputs.X }}` 残留 / output_schema 链不破 / render_chart 标签 / 造假扫描 |
| `schema_faker.py` | 从 output_schema 合成最小合规 JSON（驱动 `orca next` 用） |
| `tars_harness.py` | headless TARS 投影：`bootstrap_run` + `walk_dag`（schema-aware mock 喂 next）+ `sentinel_e2e_run` |
| `test_workflow_contracts.py` | 8 workflow × 静态契约（parametrized） |
| `test_tars_harness_walk.py` | 单节点 quant×4 完整 walk + 多节点 bootstrap+首跳 |
| `test_sentinel_e2e.py` | ptq-sweeper 哨兵路径闭环（mock 子 agent） |

## 契约规则（chart 标签）

- **axis-bearing**（line/bar/scatter/heatmap/area）：`x_label` + `y_label` + `caption` 三者必显式传
- **table**：仅需 `caption`（无轴，x/y_label N/A）
- **scope**：仅 active-path 脚本（8 workflow 引用的 agent）；`nas-viz/` 死代码（CURRENT 已登记 debt）不在 gate 内

## 验收

- 8 workflow 静态契约全 pass（或 finding 已修 / 登记为设计边界）
- 单节点 quant×4 walk 到 done:true；多节点 bootstrap + 首跳成功
- ptq-sweeper 哨兵 E2E 闭环：sentinel_triggered=1、task_id 复用、哨兵不进 `--output`、done:true
- code-reviewer 两轮闭环（impl + coverage）
- CHANGELOG + CURRENT 标 Stage3 ✅
