# 2026-08-12 — Puzzle U4 cleanup：删 deprecated inputs + expand_model.py + DRY 注释清理

## 上下文

Phase U3（commit `6987418`）把 puzzle.yaml 的 5 个 inputs 标 `[DEPRECATED U3]` 但保留以不破坏既有路径，并把测量 / block_map 识别的重心从 v1 `expand_model.py`（类名 regex）迁到 U2a 的 LLM 自适应 pz_expand + 预写 `measure_baseline.py`。本 cleanup phase（任务 9）按 SPEC v2 §13（不做清单）/ §14（输入契约）把这 5 个 deprecated inputs 与 v1 `expand_model.py` 整文件退役，对齐下游 agent.md 的 manifest 桥接，清掉跨文件 DRY 注释残留。

SPEC v2 §14 明确 `eval_kind` 保留 `[ask]`（D6：用户必给，enum 三选一），不在删除清单。

## 改动点

### 1. puzzle.yaml 删 5 个 deprecated inputs

删：`pretrained_ckpt` / `build_fn` / `eval_fn` / `train_loader_fn` / `accuracy_tolerance`。

- 新链路（puzzle-universal）全部从 `manifest.yaml` 桥接，不消费这 5 个 inputs；
- `accuracy_tolerance` 被 U3 D5 baseline-dependent 容差取代（`baseline_acc≥0.5` 绝对 0.5；`<0.5` 相对 10%）；
- `eval_kind` 保留 `[ask]`、`required: true`、`enum: [classification, embedding, regression]`（SPEC §14 / D6）；
- 同步 `terminate_gate_failed.reason` 文案（runtime 用户可见）+ pz_report 节点 header 注释，改为 D5 baseline-dependent 表述，删 stale `accuracy_tolerance` 引用；
- `input_invariants` 唯一规则（`latency_unit∈{us,s} ⇒ latency_script_path 非空`）不涉及被删字段，零改动。

### 2. 下游 agent.md manifest 桥接补全（Task 4）

3 个 agent.md 把 CLI args 从 `{{ inputs.X }}` 模板改为 manifest 桥接占位符：

- `pz_build_library/agent.md`：`--build_fn` → `<manifest.yaml 的 model.build_entry>`
- `pz_score/agent.md`：`--build_fn` / `--eval_fn` → `<manifest.yaml 的 model.build_entry>` / `<manifest.yaml 的 training_and_evaluation.evaluation_entry>`
- `pz_report/agent.md`：`--build_fn` / `--eval_fn` 同上；删 `--accuracy_tolerance "{{ inputs.accuracy_tolerance }}"`（D5 自动判）；frontmatter description + 脚本契约段更新为 D5 baseline-dependent 表述

`pz_retrain/agent.md` U3 已完成 manifest 桥接（E19 manifest 桥接段，5 个 args），本 phase 仅核对无回归。`pz_expand/agent.md` 把 `build_fn` / `eval_fn` / `pretrained_ckpt` 作为 LLM 从源码「发现」的概念（写进 manifest），非 inputs 引用——保持不变。

### 3. 删 `expand_model.py`（Task 2）

整文件删除。理由：

- 类名 regex slot 识别（`_ATT_NAME_PATTERNS` / `_FFN_NAME_PATTERNS` / `_find_layer_containers`）已被 U2a LLM 自适应 + 4 道 smoke 取代；
- `measure_baseline.py`（U2a 新增）独立完成「基线测量」职责，不 import expand_model 的任何函数；
- 全仓 grep `from expand_model` / `import expand_model` 零生产命中，唯一功能性消费者是 v1 测试 `test_puzzle_scripts_smoke.py`（已迁移到 measure_baseline 路径）；
- 内部 `measure_baseline_latency` / `measure_baseline_acc` 是 orphan 重复（puzzle_common 已有 `measure_whole_model_latency`，measure_baseline.py 跑 eval-stability 自带 acc 测量），删除无 DRY 损失。

### 4. DRY 注释清理（Task 3）

- `puzzle_common.py::_extract_state_dict` 注释原指「与 expand_model.py 的 `# 2a)` 段逻辑一致」→ 改为「father 权重加载在 measure_baseline / bld / score / build_selected / gkd / gate 共用此 helper（DRY）」，删 dead 文件引用。
- `puzzle_blocks.py::ACTIVATION_CLASS_TO_NAME` 注释原指「expand_model 的 activation 推断消费」→ 改为「puzzle activation 命名的 DRY 单一真相源；test_puzzle_catalog 验证其与 `_ACTIVATION_MAP` 对偶」。映射本身保留（公开 API + 测试覆盖 + 未来 LLM 自适应可能消费）。

`measure_baseline.py` / `gate_report.py` 经 U3 已统一走 `puzzle_common.measure_whole_model_latency`，本 phase 无重复 measure_latency 删除动作（U3 已完成）。

### 5. 测试迁移（test_puzzle_scripts_smoke.py）

- 新增 helper `_bootstrap_measure_baseline(tmp_path, output_dir, num_blocks=2)`：合成 flat + father ckpt + search_space.yaml（模拟 pz_expand LLM 产物）→ 跑真实 `measure_baseline.py`。是 LLM 产物的 faithful deterministic proxy。
- `_search_space_payload(num_blocks)`：合成 search_space dict（in_dim/out_dim 留 -1 待 trace）。
- `test_puzzle_full_chain_cpu`：第 1 步从 `expand_model.py` 改为 helper bootstrap；下游 bld/score/latency/mip/build/gkd/gate 跑真实预写脚本不变；gate 步骤删 `--accuracy_tolerance "0.5"` 实参（D5 无条件执行，argparse 兼容入参不被 main body 读）。
- `test_score_runs_and_identity_passthrough_score_is_zero`：bootstrap 步骤同上迁移。
- **删除** `test_expand_no_slot_exit_2` + `_NO_SLOT_MODEL_PY` fixture：测的是已删的 expand_model.py 的 fail-loud 路径；新链路下「empty-slots 输入校验」由 `test_puzzle_measure_baseline.py::test_measure_baseline_empty_slots_exit_2` 覆盖，「slot 识别算法拒绝非 transformer」的 intent 随架构迁到 LLM pz_expand + evaluator 审查层（recall 由 `test_puzzle_evaluator_recall.py` 测，env-gated）。
- `_TINY_MODEL_PY.evaluate` 加 `torch.manual_seed(0)`：对齐 `test_puzzle_measure_baseline.py::_TINY_FLAT_PY.evaluate` 的 sibling pattern，让合成 fixture 通过 measure_baseline 的 eval-stability smoke（两次 evaluate 必须返相同 acc）。

## Rule 7 surface（冲突声明）

- **`gate_report.py` 的 `--accuracy_tolerance` CLI arg 保留作 backward-compat**（argparse 消费但 main body 不读，D5 `_acc_pass` 无条件执行）。code-reviewer 标为可接受。保留是为下一个 deprecation cycle 给潜在外部 launcher / 用户脚本一个 grace period；现状等于 dead 但不破坏。可在后续 cleanup 删。
- **决策 ID `D5` / `E14` / `E19` / `E22` / `E23` 在 agent.md 残留**：U3 既存 pattern，非本 PR 引入；CURRENT.md 任务列表明确把洁净审查闭环分配给 Phase U5，不在本 cleanup scope。
- **测试 fixture DRY（`_search_space_payload` / `_TINY_MODEL_PY`）**：code-reviewer (test) 建议抽到 `tests/conftest.py`。**不抽**——只有 2 处引用（CLAUDE.md DRY 阈值「3+ 处」未触发）；两 fixture 存在有意差异（`_TINY_MODEL_PY` 含 `build_calib_loader` + `num_classes` 形参，是 `_TINY_FLAT_PY` 的超集）；跨文件测试自包含利于阅读。

## 验收

- `tars validate workflows/puzzle.yaml` 0/0（含 `_check_prompt_dev_residue` lint，零 warning）；
- `pytest tests/ -k puzzle` → **97 passed + 14 skipped**（baseline 98 → -1 删的冗余 `test_expand_no_slot_exit_2`，14 skip 是 LLM-backend-gated evaluator recall，env 无 key + deepseek 余额 0）；
- grep `\{\{\s*inputs\.(build_fn|eval_fn|pretrained_ckpt|train_loader_fn|accuracy_tolerance)\}\}` 全仓：唯一命中是 `bit-curve-searcher/agent.md`（属独立 workflow `quant-bit-curve.yaml`，自己的 input，不在 scope）；
- grep `expand_model`：仅历史文档（`docs/releases/` / `docs/specs/` / `docs/status/` / `CHANGELOG.md`）+ 测试中 3 处解释性「已退役」注释，零生产代码、零 agent.md 命中；
- grep `slot_type`：仅 `kind 替代 v1 slot_type` 类历史说明（puzzle_common / mip_select / pz_expand references/workflow-checklists/puzzle.yaml.md），零业务残留。

## code-reviewer 闭环

两轮并行 dispatch（implementation review + test coverage review）：

- **implementation review**：0 Blocker / 1 Major / 4 Minor。
  - Major（puzzle.yaml terminate_gate_failed.reason stale `accuracy_tolerance`）+ Minor（pz_report header 注释同 stale）→ **本 PR 修**（D5 baseline-dependent 文案）。
  - Minor（gate_report `--accuracy_tolerance` CLI backward-compat）→ Rule 7 surface 保留。
  - Minor（决策 ID D5/E14/E19/E22/E23 跨 PR 残留）→ U5 scope。
- **test coverage review**：0 Blocker / 0 Major / 4 Minor。
  - Minor（注释精度：澄清 deleted test 的 intent 拆分）+ Minor（注释精度：澄清 `--accuracy_tolerance` 是 dead arg 而非行为变更）→ **本 PR 修**。
  - Minor × 2（测试 fixture DRY 抽 conftest）→ Rule 7 surface 不抽。

## Commit SHA

`eee7125`

## 相关文件（绝对路径）

- `D:\Projects\Orca\workflows\puzzle.yaml`
- `D:\Projects\Orca\workflows\agents\pz_build_library\agent.md`
- `D:\Projects\Orca\workflows\agents\pz_score\agent.md`
- `D:\Projects\Orca\workflows\agents\pz_report\agent.md`
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\expand_model.py`（已删）
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\puzzle_common.py`（注释）
- `D:\Projects\Orca\workflows\agents\_puzzle_scripts\puzzle_blocks.py`（注释）
- `D:\Projects\Orca\tests\test_puzzle_scripts_smoke.py`（测试迁移）
