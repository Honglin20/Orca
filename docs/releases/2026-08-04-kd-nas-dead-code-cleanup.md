# 2026-08-04 — kd-nas 死代码清理 + review 修复

**Commits**：`323a0a4`（§1）+ `61a6e45`（§2）+ `3bcc1d2`（§3+§4）
**SPEC**：`docs/specs/2026-08-04-kd-nas-dead-code-cleanup.md`

## 概览

按 SPEC §5 执行顺序，分 3 commits 完成全 4 节：§1（review 修复）→ §2（Phase 1 纯删）→
§3+§4（Phase 2 迁移-再删 + 文档清扫）。reachability 已由前序 obsolete audit agent 验证为 0
active yaml `agent:` 引用；本次 belt-and-suspenders 在删除时再次 grep 复核。

## §1 review 修复（commit `323a0a4`）

四项小修：

1. **Y1 — `_LEDGER_STATUS` 对齐合约**（`kd_reducer.py:95`）：扩为
   `{SUCCESS, FAIL_latency, FAIL_train, FAIL_build, FAIL_accuracy, FAIL_export}`，与
   `kd_common.ALL_TERMINAL_STATUSES` / `viz_kd_stage._push_fail_status_bar` /
   `finalize_kd.known_statuses` 四处对齐（消除延时炸弹：FAIL_accuracy/export candidate 原本
   会被 `_validate_candidate` 拒绝 exit 2，断下游 measure/export 失败时的 ledger append）。
   加 reducer parametrize 测「FAIL_accuracy/FAIL_export 候选合法 append」锁合约。
2. **Y3 — `_parse_accuracy` 优先级**（`teacher_setup.py`）：TEACHER_ACCURACY 优先于
   STUDENT_ACCURACY（用户自写 eval_command 可能复用 train_pipeline eval stdout 但额外标注
   teacher 真值，TEACHER_ 是显式真值）。加测试守护 stdout 同时含两者时取 TEACHER_。
3. **Y5 — hue/met_* 字面统一**（`viz_kd_stage` + `finalize_kd`）：student 行 met_*
   全用 `str(bool(...)).lower()`（"true"/"false" 不再 Python 原生 "True"/"False"）；
   baseline/teacher 行填 "ref"（不再空串 / "—"，避免前端 hue 渲染成第三类目）。
4. **release note 补全**：为 `16bd8b5` 写
   `docs/releases/2026-08-03-kd-nas-us-kd-mandatory-teacher-eval-master-table.md` +
   CHANGELOG 顶部加索引。

## §2 Phase 1 纯删（commit `61a6e45`）

5 组脚本/目录 + ~60 测试 + 11 个 @pytest.mark.skip obsolete：

- `_kd_scripts/gate_all.py` + `agents/kd-gate/`（整目录）+ `_kd_scripts/distill_dispatch.py`
- `_kd_scripts/train_pool.py` + `agents/kd-train/`（整目录）
- `_kd_scripts/viz_kd.py`（pareto/sentinel/direction 不变量在 d9fcd9c 已 port 到 viz_kd_stage；
  覆盖守护在 `test_viz_kd_stage_metrics_tail.py:318/367/381/415`，无 invariant 漏迁）
- `agents/kd-select/`（整目录，含 `scripts/select_and_report.py`）+ `scripts/e2e_kd_nas_select.sh`
- `_kd_scripts/_deprecated/`（整目录：`train_adapter_template.py` + `README.md`）

修改：
- `distill/agent.md:38`：FAIL_train provenance 注释原引「v1 train_pool.py:172-173」改为
  「decide reducer / viz_kd_stage / finalize_kd 字面一致」。
- `test_kd_redesign.py::test_kd_agent_md_has_strong_execution_directive`：循环从
  (kd-setup, kd-gate, kd-train) 收窄到 (kd-setup,) —— kd-gate / kd-train 已删。

## §3 Phase 2 迁移-再删（commit `3bcc1d2`）

迁移纪律：先迁不变量 → 改 import → 跑测试确认覆盖 → 再删源（防漏迁断下游）。

| 删 | 迁移什么 → 去哪 |
|---|---|
| `pick_variant.py` | `_validate_variant` → `kd_common.validate_variant` |
| `measure_student.py` | `_parse_accuracy` + `_compute_met_accuracy_absolute` → `kd_common.{parse_accuracy, compute_met_accuracy_absolute}` |
| `setup_helpers.py` | 无（YAGNI：活跃路径无 tree-walk） |
| `teacher_model.py` | 无（活跃 teacher 来自 teacher-gen wrapper） |

teacher_model fixture 替换：`test_kd_train_script.py::TEACHER_MODEL` 改指 receiver KB
`spt_alt.py`（同 contract shape + feature_hook_names；非 teacher_model.py 也能验证
train_pipeline state_dict load 回路）。`test_struct_kd_p7.py::TestTeacherSetupLatencySource`
同款替换 + `_minimal_teacher_ckpt` 加 receiver dir sys.path setup（spt_alt 依赖 _model8_blocks）。
删 2 个 teacher topology 测（teacher_model.py 删后无意义）。

## §4 文档清扫（commit `3bcc1d2`）

- `kd_common.py` docstring：消费者列表更新（measure_student / viz_kd / kd-select →
  viz_kd_stage / kd_common.compute_met_accuracy_absolute / finalize_kd）；
  gate_all/train_pool 注释改为「crash-safety 契约级逻辑，活跃 gen_student/distill 经
  ledger_reducer append」。
- `examples/kd-nas-demo/README.md`：消费者表述更新；B6 measure_student 示例段加「已删」banner；
  B4 train_adapter_template 标已删。
- `CONTRACTS.md`：v5 banner 重写（删旧 DEPRECATED 标注，标 2026-08-04 死代码清理完成）；
  §0 目录布局删 9 个脚本/目录条目；§3.1 measure_student entry 改「已删，不变量 port 到 kd_common」；
  §3.2 整段 DEPRECATED 删除；§1 teacher reference 改 teacher-gen 产物；§5 铁律三处消费者更新。
- `kd-train-script/SKILL.md` + `references/.../train_pipeline_script_generation.md`：
  teacher_model_path 示例改「teacher-gen wrapper 或任意 KD variant .py」。
- `model-flatten/SKILL.md:189`：pick_variant/gate_all import 改 kd_common.validate_variant +
  tune_latency（标已 port）。

## 验证

`pytest tests/workflows/` → 5 预存失败（HEAD 已有，未触动）/ **0 新红** / 总数 -148 死代码。
预存失败：
- `test_finalize_kd.py::test_main_baseline_fallback_writes_report_no_eval`
- `test_finalize_kd.py::test_main_real_champion_runs_eval_onnx_latency`
- `test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_from_param_skips_provider`
- `test_struct_kd_p7.py::TestTeacherSetupLatencySource::test_latency_fallback_to_provider_when_param_absent`
- `test_struct_kd_p7.py::test_kd_setup_node_exposes_path_fields`

## 偏差

无（按 SPEC §5 逐条实现）。SPEC §3 表中「删 topology 测 `test_struct_kd_p7.py:40,62`」
实际指 `test_kd_redesign.py:40,62`（行号定位的 test_teacher_ten_blocks_alternating +
test_student_feature_hooks_match_teacher_length），按语义删除。
