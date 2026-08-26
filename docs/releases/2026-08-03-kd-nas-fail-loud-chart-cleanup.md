# Release Note — kd-nas fail-loud 加固 + 终态图补全 + CONTRACTS 串行化

**日期**：2026-08-03
**Commit**：`d9fcd9c`
**SPEC**：`docs/specs/2026-08-03-kd-nas-fail-loud-chart-cleanup.md`（v2.1，spec-reviewed，conditional-pass conditions met）

## 做了什么

按 SPEC §5 执行顺序（§1 → §3+§4 → §2）完成 4 节收尾改动：

### §1 P0 — 特征蒸馏项 fail-loud
- `workflows/agents/_kd_scripts/kd/compose.py`：`KDComposite.__call__` 加守卫——
  `kd_losses` 含 ofd/fitnets/rkd 且运行时 `s_feats`/`t_feats` 空 → raise `ValueError`。
  旧 `_compute_term` 静默 `return None` + 主调 `continue` 让配置声称 ofd 在跑、实际只跑 mse，
  违反 Rule 12。mse-only / ema-only 路径不受影响（mse 不依赖 feats）。
- `workflows/agents/distill/agent.md`：默认 KD_CONFIG 改 AST 条件化（`ast.FunctionDef,
  ast.AsyncFunctionDef`），按 student 是否暴露 `feature_hook_names()` 决定启 mse+ofd 还是 mse-only。
- `workflows/agents/gen-student/agent.md`：step 5 的 `grep '^def feature_hook_names'`
  改 AST 判定（**F2 dormant bug**：缩进 class method 永远漏判 → ofd 永远被剥离）。

### §3 — 终态帕累托前沿 + FAIL 分布
- `workflows/agents/_kd_scripts/viz_kd_stage.py`：新增 `_push_pareto_front` + `_push_fail_status_bar`
  （**port viz_kd 语义，非 import**——为 §2 followup 删 viz_kd 铺路）。方向门 + display 取负变换 +
  sentinel 过滤经 `kd_common.accuracy_direction` / `is_measured_row` 单一真相源。接入 `--stage final`
  在 `all_models_table` 之后。
- `finalize_kd._write_report`：新增 `## Search Outcome` 段（纯文本 status 计数；图表是唯一真相源，
  不重复实现 pareto）。

### §4 — finalize 一致性
- baseline 行 `b_met` 由 `"true"/""` 改 `"true"/"false"`（与 student 行字面一致）。
- student 行 `met_lat`/`met_acc` 统一 `str(bool(...)).lower()`（旧 Python 原生 `True/False`）。
- 两段 latency 读取统一双 fallback `latency_us_median → latency_us`。

### §2 — CONTRACTS 对齐 + 4 脚本 DEPRECATED 头
- `workflows/agents/_kd_scripts/CONTRACTS.md` 重写为串行 v5：§0 DAG 改活跃串行版
  （`flatten → setup → gen_teacher → gen_train_script → train_script_verify → train_teacher →
  (gen_student → distill → decide)* → finalize`）；§3 拆 3.1 活跃 / 3.2 DEPRECATED。
- `gate_all.py` / `train_pool.py` / `viz_kd.py` / `kd-select/scripts/select_and_report.py`
  模块 docstring 加 `# ⚠️ DEPRECATED` 头（零逻辑变化；物理删除 + 80+ 测试外科迁/删 defer followup SPEC）。

## 测试

新增/扩展测试（覆盖 SPEC §1.3 / §3.4 / §4）：
- `test_compose_feature_term_without_hooks_fails_loud`：4 分支（feature+no-feats raise /
  mse-only ok / ema-only ok / prepare 后仍 raise）。
- `test_distill_gen_student_ast_hook_detection_handles_indented_class_method`：
  F2 回归守护（缩进 class method 必须被 AST 识别；旧 `^def` grep 漏判）。
- `test_final_stage_pareto_front_and_fail_status_bar_pushed`：5 不变量
  （① min-kind y 取负 ② unknown WARN-skip ③ sentinel 剔除 ④ FAIL_accuracy+kind 计入
  ⑤ db-kind -1.0 不误剔）+ fail_status_bar 6 status 计数。
- `test_pareto_front_unknown_kind_warn_skip` / `test_pareto_front_min_kind_negates_y_for_max_direction`
  / `test_fail_status_bar_empty_ledger_skip`：单不变量守护。
- `test_write_report_has_search_outcome_section` / `test_write_report_student_bool_render_lowercase_and_latency_dual_fallback`：
  finalize §3.3 + §4 一致性。

## 偏离 SPEC

- `compose.py` `__init__` 的「空 kd_losses ∧ ema off → raise」是 SPEC 之外的接口加强
  （**非本次新增**，HEAD 已存在）。本次 `__call__` 守卫与其方向一致（fail-loud），互不冲突。
  Test `test_compose_rejects_empty_kd_losses_fail_loud` 锁定该行为。
- code-reviewer 🟢 建议 `_FEATURE_TERMS` 从 `VALID_KD_LOSSES` 派生（DRY）、`fail_status_bar`
  过滤零计数行——已并入。

## 验证

- 全套 kd-nas 测试：**309 passed, 14 skipped, 5 failed**（5 个预存失败 HEAD 已存在，SPEC §6.1
  明确不计：`test_finalize_kd.py::test_main_baseline_fallback_writes_report_no_eval` /
  `test_finalize_kd.py::test_main_real_champion_runs_eval_onnx_latency` /
  `test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_from_param_skips_provider` /
  `test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_fallback_to_provider_when_param_absent` /
  `test_struct_kd_p7.py::test_kd_setup_node_exposes_path_fields`）。
- AST 缩进 method 验证（SPEC §6.4）：手工脚本确认 AST 返回 True、旧 `grep '^def'` 返回 0（漏判）。
- code-reviewer 一轮闭环：0 FATAL / 0 MAJOR / 2 🟡 建议（AST 回归测试 + release note 标注
  `__init__` 守卫属 SPEC 外加强——均已并入）/ 3 🟢 可选优化（2 个已并入）。

## Commit SHA

- `d9fcd9c` — fix(kd-nas): fail-loud 守卫 + pareto/fail 图补全 + CONTRACTS 串行化（14 文件）
