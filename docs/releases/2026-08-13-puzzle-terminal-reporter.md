# Release: Puzzle 终端 reporter 化——删 7 个 terminate 节点，in-session 全兼容

**日期**：2026-08-13　**分支**：`puzzle-universal`　**commit**：（待回填）

## 动机

用户反馈：puzzle 只用 in-session 模式驱动，末尾的 terminate 节点全没用。核实属实——
`orca/run/step.py:876-888`（`_check_agent_node`）in-session 引擎**只支持 `kind: agent` 节点**，
路由到 `kind: terminate` 直接抛 `unsupported_node_kind`（irrecoverable 崩溃）。puzzle 是仓库里
唯一还挂 terminate 节点的 in-session workflow（nas-supernet-v2/v3 早已按此模式改造，见
`docs/specs/2026-08-11-nas-supernet-v2.md`：v2 用 reporter agent 节点，非引擎补 terminate）。

## 方案：对齐 ns3 reporter 模式（用户确认）

| 项 | 前 | 后 |
|---|---|---|
| 节点数 | 9 agent + 7 terminate = 16 | **9 agent，0 terminate** |
| 失败路由 | 各 terminate_*（in-session 崩 run） | 统一收敛到 `pz_report` |
| 终态判定 | terminate reason 静态文案 | `pz_report` 读磁盘 first-match 判终态 |
| outputs | 引用 pz_select/pz_materialize/pz_baseline（失败路径 StrictUndefined 崩） | 全读 `pz_report.output` |

**`pz_report` 改造为唯一终端 reporter**（对齐 ns3_report）：
- `scripts/emit_report.py`（新，确定性）：读 `$ORCA_ARTIFACTS_DIR` 磁盘状态 first-match 判终态
  → `.report.json` + 单行 JSON（status ∈ {success, failed} + stage + reason + gate 字段 +
  selected_arch/optimized_flat_path/output_dir/block_map/error/artifacts）。终态映射：
  gate_result.json pass/fail → success/report；final ckpt 缺 → retrain；optimized_flat 缺 →
  materialize；selected_arch 空 → select；scores 缺 → score；block_library 缺 → build_library；
  baseline_metrics latency_target_feasible=false → baseline（latency infeasible）；search_space
  缺失/空 slot → search_space（unsupported）；manifest/flat 缺 → ingest。
- `scripts/run.sh`（改）：成功路径（final ckpt + baseline_metrics + optimized_flat 齐）才跑
  gate_report.py（AC gate 不变）；失败路径跳过 gate 直接 emit_report.py。
- `scripts/check_report.sh`（新）：校验报告 JSON（status/stage enum + required 字段）。
- `agent.md`（重写）：产品说明书体；**零跨节点 output 引用**（失败路径上游可能未跑 → 只引用
  inputs + 磁盘，StrictUndefined 守门）。
- `final_status.json` 补写（U6 root cause J 契约）：成功路径 gate_report.py 已写不覆盖；失败
  路径 reporter 补写统一终态。

**yaml 改动**：pz_ingest / pz_search_space / pz_baseline / pz_select / pz_materialize /
pz_retrain 的失败分支路由 → `pz_report`（pz_baseline 双失败分支合并为单一 catch-all）；
pz_report 路由 → `$end` 无条件（gate pass/fail 都由 reporter 报告，不再路由判 pass）；
outputs 全读 pz_report.output（失败路径安全）。

**上游 agent.md 文案**：6 个节点 + pz_expand（遗留）的 terminate 引用改 pz_report；
workflow-checklist 同步。

## 验收

- `tars validate workflows/puzzle.yaml` 0 error / 0 warning；全 13 workflow 0 error。
- 路由完整性：9 节点全从 entry 可达，无 dangling target，无 terminate。
- pytest：test_puzzle_measure_baseline 31 passed（含路由断言重写：
  pz_baseline 失败分支 → pz_report、全 workflow 无 terminate、pz_report → $end 无条件 +
  output_schema 带 status/stage/reason）；test_puzzle_scripts_smoke + materialize + father_state +
  delta_review + catalog + check_ingest + check_search_space 82 passed / 3 skipped；
  tests/compile 215 passed（含 validator 120）；terminate 引擎测试 14 passed 无回归。
- emit_report.py 11 终态 synthetic 全对（success / gate_failed / retrain / materialize /
  select / score / build_library / latency_infeasible / unsupported_search_space / ingest /
  gate_crash）。
