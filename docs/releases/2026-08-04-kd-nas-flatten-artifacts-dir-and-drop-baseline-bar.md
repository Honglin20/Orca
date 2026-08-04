# 2026-08-04 — kd-nas flatten 产物落项目 artifacts 根 + 删除 baseline latency bar

## 背景

用户发现两处 drift：

1. **flatten 产物落 per-run 目录**：`model-flatten` 把 `<base>_flat.py` 写进引擎注入的
   `$ORCA_ARTIFACTS_DIR`（= `runs/<run_id>/artifacts/`，per-run），而下游 `setup` 早在 commit
   `046d768`（串行 rework）就改用**跨 run 持久**的 `kd_artifacts_dir` = `${PROJECT_ROOT}/artifacts/kd-nas/`，
   且 setup 给 baseline 建了 `models/baseline/` 子目录却空置——flatten 没往里写。
   **根因**：`flatten`（commit `8c24db1`，串行三件套 1/3，最早写）用 P9 时代旧约定 `$ORCA_ARTIFACTS_DIR`；
   setup 后换约定但没回头改 flatten → baseline 契约漂在 per-run，与 ledger/champions（跨 run）不同根。

2. **baseline_latency_bar 冗余**：flatten 末尾推单柱 baseline latency bar（只有 baseline 一根柱，
   信息量≈0），而紧接的 `setup` 节点推 `baseline_seed_table`（含 latency + accuracy + met_* + round + id），
   信息是 bar 的超集 → bar 完全冗余。

## 改动

### 1. flatten 产物落项目 artifacts 根（与 setup 合流）

`model-flatten/agent.md` step3 + `SKILL.md` 路径约定 + `kd-nas.yaml` `flat_artifacts_dir` description：
`<output_dir>` 从 `$ORCA_ARTIFACTS_DIR` 改为 `${PROJECT_ROOT}/artifacts/kd-nas/models/baseline/`——
与 setup `kd_artifacts_dir`（`${PROJECT_ROOT}/artifacts/kd-nas/`）同根，baseline 契约随项目跨 run 持久，
不再散落 per-run `runs/<run_id>/`。

- **合流保证**：flatten 是入口节点（先于 setup 执行，拿不到 `setup.output`），故**自算**
  `${PROJECT_ROOT}`（去 ` (low-confidence...)` 后缀的干净路径，与 setup `split(' (low-confidence')`
  同款去后缀逻辑），两边算出同一根——不需等 setup。
- **无 fallback**：PROJECT_ROOT 由 step2 推断（找不到 .git/pyproject.toml/train.py 时取 baseline_model_path
  的 dirname），总非空 → OUTPUT_DIR 总非空。低置信（low-confidence）时 PROJECT_ROOT = dirname，
  OUTPUT_DIR = `dirname/artifacts/kd-nas/models/baseline/`（可能与 setup 重算根不合流，但 `baseline_contract_path`
  绝对路径仍供 setup 读取，功能不阻断）。
- `flat_artifacts_dir` 无下游消费者（下游只用 `baseline_contract_path`），改值零功能影响。

### 2. 删除 baseline latency bar（flatten 不推图）

`viz_kd_stage.py`：删 `_push_baseline_latency_bar` + `_STAGES["baseline"]` + `render_stage` baseline
分支 + docstring baseline 描述。`--stage baseline` 现被 argparse choices 直接拒（fail loud）。

`model-flatten/agent.md`：删「末尾 web 推送 viz_kd_stage --stage baseline」bash 块；`viz_status`
固定 `{"env_status": "skipped", "charts": {}}`。flatten 因此不再依赖 `viz_kd_stage.py` /
`$ORCA_ARTIFACTS_DIR` env_anchor。

- **保留 viz_status 字段**（非删）：kd-nas.yaml 所有节点统一 viz_status schema 是既有设计纪律；
  flatten 填 `env_status: skipped`（enum 合法值）诚实表达「本节点跳过 web 推送」，非 sidecar 失败。
- baseline 信息由 setup `baseline_seed_table` 承载（latency + accuracy + met_*）。

### 3. 文档同步

- `kd-nas.yaml` flatten 节点注释 + `flat_artifacts_dir` description。
- `CONTRACTS.md` §0 目录树 viz_kd_stage 注释 + §3.1 CLI `--stage` choices 删 baseline。
- `kd-nas-serial-iteration-rework.md` SPEC §6.1 / §8 表格 / §14 验收：flatten 不推图（baseline
  信息由 setup seed table 承载）。

### 4. 测试（`test_viz_kd_stage_metrics_tail.py`）

- 删 `test_baseline_stage_pushes_latency_bar` / `test_baseline_stage_missing_latency_skip`，替换为
  `test_baseline_stage_removed_no_chart`（守护：baseline stage 已移除 → 走 unknown-stage 分支，
  `baseline_latency_bar` 不在 charts）。
- `test_render_chart_exception_does_not_block` 从 baseline stage 改用 teacher stage（保异常容错覆盖）。
- `test_viz_kd_stage_main_emits_json_even_on_bad_args` 从 `--stage baseline` 改 `--stage baseline_seed`
  （baseline 已不在 choices，argparse 会拒）。

## 设计决策

- **为何 flatten 用 PROJECT_ROOT 而非 $ORCA_ARTIFACTS_DIR**：setup 的 ledger/champions/ckpts 都在
  `${PROJECT_ROOT}/artifacts/kd-nas/` 跨 run 持久（baseline 模型不变时跨 run 复用）。baseline 契约
  若落 per-run，每次重跑都重新展平 + 与 ledger 不同根。合流后 baseline 契约也跨 run 持久，单一真相源。
- **为何不删 viz_status 字段**：所有节点统一 viz_status schema 的纪律价值 > 一个 skipped 字段的简洁性。
  删字段会连锁影响 output_schema + 前端 + 测试断言，且 skipped 已是合法诚实值。

## 验证

- 3 核心测试文件（`test_viz_kd_stage_metrics_tail` + `test_model_flatten` + `test_kd_redesign`）：
  **92 passed, 1 skipped**。
- 全套 `tests/workflows/`：**418 passed, 3 skipped, 5 failed**——5 个失败全为 HEAD 预存
  （2026-08-04 dead-code-cleanup release §6.1 记录：finalize_kd baseline fallback ×2 + teacher_setup
  latency source ×2 + kd_setup path fields ×1），与本次改动零交集，**0 新失败**。

## Review 闭环（code-reviewer R1）

code-reviewer 审完这批改动，唯一 🟡 建议修复 **R1**：flatten step3 的 PROJECT_ROOT 去后缀原是
prose（"去掉 `(low-confidence...)` 后缀"），而 setup 端是确定性 python 片段（`split(' (low-confidence')[0]`
+ `os.path.abspath`）——Rule 5（deterministic 用代码不用模型）+ DRY 双重隐患（两处公式若一处改，另一处不会同步）。

**闭环**：flatten step3 落定为与 `kd-setup/agent.md` step1 **逐字对齐**的 bash+python 片段
（`split(' (low-confidence')` + `os.path.abspath` + `os.path.join(proot,'artifacts','kd-nas','models','baseline')`），
两边算同一根由代码保证（非 prose）。同步去掉 low-confidence 时 fallback `llm_artifacts/`（统一用 PROJECT_ROOT
公式，确定性优先）。`SKILL.md` 路径约定同步。

**测试升级（R2）**：`test_flatten_agent_md_output_dir_co_rooted_with_setup` 加断言 step3 含
`split(' (low-confidence')` + `os.path.abspath`（确定性代码守护，非 prose 字符串匹配）；docstring 诚实标注
"守契约 prose + 确定性代码两层；runtime 同根性靠两边片段逐字对齐"。

**R3（setup fallback marker 2 vs flatten 3）未动**——该 fallback 在当前 flatten→setup 流程是事实死代码
（flatten 总填非空 project_root，setup 的 walk-up fallback 永不触发），保留作 defensive。

## 未 commit

按惯例未 commit / 未 push。Commit SHA 待补。
