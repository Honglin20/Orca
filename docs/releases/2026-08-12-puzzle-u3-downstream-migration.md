# Puzzle Phase U3 —— 下游脚本迁移 + 算法增强

> SPEC v2 `docs/specs/puzzle-universal-design-draft.md` §8（数据流 + "内核不动" 措辞修正）/§12（AC + fail-loud）/§15（迁移 checklist）/§17（E5/E6/E8/E11/E12/E14/E19/E24 闭环）。
>
> 分支：`puzzle-universal`。前置：U1（契约层 Slot.kind+catalog，`50c79c4`）+ U2a（pz_expand LLM 自适应 + measure_baseline + manifest/search_space，已提交）+ U2b（block-map/search-space evaluator，`a308784`）。

## 改动点

### 1. E14 BLD calib 改真实数据（隐性 BLOCKER，原 OOD bug）

- `puzzle_common.py`：新增 `build_real_calib_loader(loader_fn_str, device)`——调外部 loader_fn 抽首个 batch 真实数据物化到 `_TensorDataset`，支持 `capture_parent_activations` 重复 forward。空 loader / 非可迭代 / 非 tensor batch → raise（禁静默回退 randn）。
- `bld.py`：删 `build_calib_loader` import；增 required `--calib_loader_fn` CLI arg；改用 `build_real_calib_loader`。原 `torch.randn` OOD bug 根除（candidates 学 noise→teacher_on_noise 在真实数据上全错的问题修复）。
- 保留 `build_calib_loader`（randn）给 score/latency 等只需 I/O shape 的场景（latency 是 shape 级，score 距离排名相对稳定，不在 E14 scope）。
- `pz_build_library/agent.md`：增 E14 桥接段，agent 从 `manifest.data_and_environment.data_loader_entry` 读 → 填 bld.py `--calib_loader_fn`。

### 2. E6 + E8 is_valid 验证器（U1 声明 U3 消费）

- `puzzle_common.py`：新增 `is_valid_ffn_prune(slot)` + `is_candidate_valid_for_slot(name, slot, catalog)`。
  - E6：catalog 的 `requires_ffn_struct: [standard]` × slot 的 `ffn_struct` 交叉判定——bypass/GLU/dual FFN 拒 ffn_75/ffn_50/linear，候选自动收缩到 {identity, no_op}。
  - E8：`mask_load_bearing=True` slot 拒 `mask_aware=False` candidate（builtin 默认全 mask-blind，故只留 identity）。
  - 跨 kind 适用性：`slot.kind not in entry.kinds` → False（attention 候选 × ffn slot 等跨 kind 误配被拒）——把 `is_candidate_valid_for_slot` 立为 single source of truth。
  - passthrough（identity）永远 valid（SPEC §3 铁律）。
- `bld.py`：枚举候选时 `valid_variants = [v for v if is_candidate_valid_for_slot(v, slot)]`；valid 为空 → raise。
- `score.py`：`_score_variant` 头部加 `if not is_candidate_valid_for_slot(variant, slot): return 0.0, False`（invalid variant 标 valid=False 不打分）。
- `build_selected.py`：防御性关卡——遍历 selected_arch，每个非 identity variant 调 is_valid 校验；无效选 → raise（MIP 应已据 score.py 的 valid=False 过滤）。

### 3. §16.4 完整 AC：identity allclose 跨模型验证（U2a 延迟项）

- `build_selected.py`：新增 `_is_all_identity_arch(arch)` + `_verify_all_identity_allclose(student, flat, build_fn, cfg, father_state)`。
- 语义：**全 identity 架构**（所有 slot 选 identity）的 student 必须 `torch.allclose(student_out, father_out, atol=1e-5)`——验证 identity 零侵入承诺（SPEC §3 铁律的真实验证机制）。非全 identity 架构不要求（有意替换了块，输出会变）。
- father_state 缺 → 跳过（student 由 selected_state_dict 覆盖，不适用 allclose AC）。

### 4. E19 + E24 gkd_retrain argparse 迁移 + dict/list 输出 caveat

- `gkd_retrain.py`：新增 `_flatten_model_output(out)`——tensor 直通；tuple/list 取首 tensor；dict → raise（E24 caveat，flat.py 须按 pz_expand 契约加 output-flattening adapter）；空 tuple / 非 tensor 首 → raise。训练循环 teacher/student 输出统一走它。
- `pz_retrain/agent.md`：把 5 个 CLI args（`--build_fn`/`--eval_fn`/`--father_state`/`--train_loader_fn`/`--eval_kind`）的来源从 `{{ inputs.* }}` 改为 manifest-discovered（agent 读 manifest → 桥接 CLI args，脚本不解析 manifest——E9）。增 E19 manifest 桥接段 + E24 dict/list caveat 说明。
- `load_external_callable` 从函数内 lazy import 提到顶层（无循环依赖，统一 import 风格）。

### 5. D5 + E11 + E12 gate_report / mip_select（ACC 相对容差 + LAT 早警）

- **D5 ACC AC（baseline-dependent）**（`gate_report.py:_acc_threshold/_acc_pass`）：
  - `acc_base ≥ 0.5` → 绝对容差 0.5（threshold = acc_base − 0.5；高 baseline 保绝对）
  - `acc_base < 0.5` → 相对容差 10%（threshold = 0.9·acc_base；低 baseline 比例保护，近随机会 fail）
  - 高 baseline mnist 0.97 → final ≥ 0.47；低 baseline target 0.085 → final ≥ 0.0765（近随机 0.001 fail）。
- **E12 LAT 早警**（`mip_select.py`）：target_latency > baseline_latency/2 → 不跑 MIP，直接返回 `feasible=False, select_reason="target-too-aggressive", infeasible_reason=...`。早 fail 省 build_selected + gkd_retrain（最长的 GKD 分钟~小时级）。判断放 mip_select 而非 gate 的理由：(1) mip_select 已有 target+baseline 同框；(2) 省 GKD 是最大省时；(3) 具体 reason 让 pz_select agent 路由特定 terminate。
- `gate_report.py` 同时算绝对 + 相对 delta，gate_result 落 `acc_tolerance_kind` + `acc_threshold` 审计字段。

### 6. measure_latency DRY 提取（U2a 延迟项）

- `puzzle_common.py`：新增 `measure_whole_model_latency(model, dummy_input, device, latency_script_path)`——默认 `measure_module_latency`，提供 `latency_script_path` 则包装。
- `measure_baseline.py`：删本地 `measure_latency` 副本，改用共享 helper。
- `gate_report.py`：删本地 `_measure_latency`，改用共享 helper。

### 7. puzzle.yaml inputs 标 deprecated（任务 9 清理 phase 前的过渡）

- `pretrained_ckpt` / `build_fn` / `eval_fn` / `train_loader_fn` / `accuracy_tolerance`：description 标 `[advanced][DEPRECATED U3]`，default 空/0.5，required false。pz_expand U2a 起从源码发现并写 manifest.yaml；下游 agent 读 manifest 桥接（不再消费 inputs）。**保留**作 v1 expand_model.py 路径 + 测试兼容，U4 重跑后退役。

### 8. dispatch 查漏（U1 已做主要）

- `grep slot_type`：业务残留 = 0（仅保留 2 处历史注释「kind 替代 v1 slot_type」——SPEC 允许）。
- CatalogEntry/Slot 的 U3 TODO 注释全清。

## SPEC 冲突闭环（D5——Rule 7 surface conflicts）

SPEC §12.1 D5 内部矛盾：formula `δ = max(0.5, 0.1·acc_base)` 与示例（mnist 0.97 → final≥0.47；target 0.085 → final≥0.0765）+ "取更严者判 pass" 三者互斥——max 公式对所有 acc_base ≤ 1 都给 δ=0.5（→ mnist 0.485 / target 0.0425），与两例都不匹配。两例只能被 **baseline-dependent 规则**同时满足（高 baseline 绝对、低 baseline 相对），与用户显式 intent「低 baseline 比例保护，高 baseline 绝对容差」一致。

**Resolution**（gate_report.py module docstring + `_acc_threshold` docstring 注释矛盾）：按 baseline-dependent 规则实现，匹配示例 + 用户 intent。Boundary cliff（baseline=0.500 threshold=0.0 vs baseline=0.499 threshold=0.4491 的 0.449 跳跃）是 baseline-dependent 切换的固有特征——已 docstring 标注。SPEC formula 修订（消除 max 文字矛盾）留作后续。

## 验收

- `tars validate workflows/puzzle.yaml`：0/0（无 warning）
- `pytest tests/ -k puzzle`：**98 passed**, 14 skipped（skip 全是 deepseek 余额 0，与本次改动无关）
- 新测试 `tests/test_puzzle_u3_migration.py` 17 用例（E14 / E6 / E8 / §16.4 / D5 boundary / E12 / E24 / E6 bld 端到端 / build_selected 防御性）
- `grep slot_type`：仅 2 处历史注释
- `grep torch.randn` 在 BLD calib 路径：无残留（bld.py 不再用 `build_calib_loader`，仅用 `build_real_calib_loader`；剩余 `torch.randn` 命中均为 dummy input / latency floor 测量 / score+latency 的 shape-level 合成 calib，非 BLD teacher 信号）
- `code-reviewer` sub-review：1 🟡（is_candidate_valid_for_slot 补 cross-kind check）+ 1 🟡（D5 cliff docstring）+ 4 🟢（死代码 / 重复 mkdir / ternary 副作用 / lazy import）—— **全闭环**（见本 note 顶部改动点 2/5 + 提交记录）

## 偏差

- `score.py` 仍用 randn `build_calib_loader`：score 距离是相对排名（OOD 输入下仍可比较 variant 优劣），SPEC §11 未要求 score 真实数据。E14 scope 严格限 BLD（task description 措辞「BLD teacher 信号」）。
- `expand_model.py` v1 路径**未删**（task §"不动"——清理 phase 做）。
- `puzzle.yaml` 退役的 5 个 inputs **未删**（task：U3 先在 yaml 注释标 deprecated，U4 重跑后退役）。

## Commit SHAs

（commit 后回填）

## 不动（任务 9 清理 phase 做）

- 删 `expand_model.py` v1 路径
- 删 `puzzle.yaml` 的 build_fn/eval_fn/pretrained_ckpt input（U3 已注释标 deprecated）
