# 2026-07-22 P5：quant 四 workflow 正确性修复（删造假 + device + bit-curve bake 改动生效）

> 来源计划：[`docs/plans/2026-07-21-workflow-redesign.md`](../plans/2026-07-21-workflow-redesign.md) §Phase 1（P5）。
> 输入原则：[`docs/specs/workflow-input-design-principle.md`](../specs/workflow-input-design-principle.md)。
> 范围：4 个 quant workflow（ptq-sweep / sensitivity / qat / bit-curve）+ 各自 agent.md / scripts/run_*.py + 新共享 `_quant_scripts/_device.py`。

---

## 背景：P5 修什么

审计 P5 之前的 quant 四 workflow 有三大类「契约级硬伤」：

1. **造假数据（最高优先）**：agent.md 模板里直白指示子 agent 「找不到 calib loader 就 `torch.randn` 造假」+「eval 找不到就复用 calib/train」+「eval_fn 空就静默退 teacher-student mse」——所有这些路径都让 metric 失去业务含义、让 best 候选选错、让用户拿到看起来正常但实际是假的交付物。
2. **device 完全没落地**：四个 yaml 没有 `target_hardware` input；脚本里没有 `model.to(device)`、没有 `--device`、NPU 完全无路径——quant 量化的 GPU kernel 跑不到 GPU 上。
3. **bit-curve bake 改动不生效**：`run_bit_curve.py::_bake_selected` bake 完直接报 search 内部 `final.score` 当 best_metric，没 reload + 重 eval——用户拿到的 `best_mixed_model.pt` 实际精度可能与报告 metric 漂移（state_dict 序列化丢 observer buffer 等），无人对账。

外加：`output_dir` 默认全 4 workflow 都用 `llm_artifacts/<model>/`，同模型串跑互覆；qat 示例数字自相矛盾（recovery 符号错）；sensitivity 缺 `--env_file`（推图静默失败）。

---

## 改动（按硬约束「单一真相源」组织）

### 1. 新增共享模块 `workflows/agents/_quant_scripts/_device.py`

四个 quant 脚本经 `sys.path.insert(0, "<...>/_quant_scripts")` + `from _device import ...` 共用，避免 4 份复制（CLAUDE.md Rule 6 DRY）。

- **`resolve_device(device_arg, local_rank=0)`**：inline 自 `nas-agent/nas_agent/train/distributed.py:214`（不引跨包依赖——nas-agent 不在 orca pyproject）。`auto` / 显式 `cuda`/`cuda:N`/`npu`/`cpu` / 非法值 fail loud（不静默退 cpu）。区分 torch.device 语义（本模块处理）与 onnxruntime provider 语义（struct/kd 自管）。
- **`is_npu_available()`**：`lru_cache` + `find_spec("torch_npu")` + `torch.npu.is_available()`，与 nas 同实现。
- **`set_seed(seed)`**：固定 `random` / `numpy`（若装）/ `torch`（含 cuda/npu `manual_seed_all`，老 torch_npu 缺 API 走 `AttributeError` 兜底 + WARN，其余 raise 不静默——code-reviewer 🟡）。
- **`move_batch_to_device(batch, device)`**：递归处理 Tensor / dict / tuple / list / scalar，幂等（`.to(device)` on-device no-op）。
- **`wrap_forward_with_device(raw_forward_fn, device)`**：把 adapter 的 `forward_fn(module, batch) -> Tensor` 包一层，先搬 batch 再调 raw——adapter 不感知 device，device 作为 cross-cutting concern 由脚本统一处理（DIP）。raw=None 直返 None（caller fail loud）。
- **`add_device_seed_args(parser)`**：argparse helper 加 `--device`/`--seed`（DRY：消除 P5 引入的 4 份 argparse 块）。
- **`resolve_device_and_seed(device_arg, seed_arg, *, log_prefix)`**：合并 try/except + `set_seed` + stderr log，返 `(device, seed)`，失败 exit 2。

### 2. 四 yaml 加 input + 改 output_dir 默认 + 删造假描述

每个 yaml（`quant-ptq-sweep.yaml` / `quant-sensitivity.yaml` / `quant-qat.yaml` / `quant-bit-curve.yaml`）：

- 加 **`target_hardware`**（Tier A `[ask]`，默认空→脚本自动探测；cuda/npu/cpu）。
- 加 **`seed`**（Tier C `[default]="0"`，复现性底座）。
- `output_dir` 默认改为 `llm_artifacts/<model_name>/<wf-name>/`（ptq-sweep/、sensitivity/、qat/、bit-curve/）——同模型串跑四个 workflow 产物不再互覆。
- `calib_data_ref` / `eval_data_ref` / `train_data_ref` description 改为 Tier B `[infer]` 契约：明确「读用户代码找 loader，找不到 fail loud，绝不造假 torch.randn」。
- `eval_data_ref` 特别强调「绝不复用 calib/train 当 eval」（plan §P5：禁掉的造假口径）。
- `eval_fn_ref` 改为「未提供→stderr WARN：用 teacher-student mse，精度仅自洽性参考」（teacher-student mse 是 SDK 合法默认，有诊断价值，非造假——与 eval_loader 不同）。

### 3. 四 agent.md 删造假 + 加 Tier B 契约 + 加 device 约定 + 改 Jinja bash 调用

每个 agent.md（`ptq-sweeper` / `sensitivity-analyzer` / `qat-trainer` / `bit-curve-searcher`）：

- 删除所有 `torch.randn` / 「兜底假随机」/「少量假随机」/「复用 calib 当 eval」/「复用 train 当 eval」**指示性段落**（保留否定句「绝不 torch.randn 造假」作为契约钉死）。
- `get_calib_loader() -> DataLoader` 改 **Tier B 获取三步**：①读用户代码（`grep -rn "def load_calib\|DataLoader" project_root`）找 loader 的 dotted-path → import；②找不到 → **fail loud**（adapter raise 或不实现该函数，脚本 exit 2）；③绝不造假。
- `get_eval_loader() -> DataLoader` 改 **必实现**：读代码找 eval loader → import；找不到 fail loud（绝不复用 calib/train——会让 best 候选选错）。
- `load_model()` 加 **不在此处 `.to(device)`** 约定——脚本顶层 `resolve_device` 后统一搬（device 由 `--device` 传入，单一真相源）。
- `forward_fn` 注明「脚本会包装一层把 batch 搬到 device，adapter 不需要懂 device」。
- `eval_fn_ref` 空 → 不生成 `get_eval_fn`，脚本 stderr WARN「用 teacher-student mse，精度仅自洽性参考」并继续。
- bash 调用块全部加 `--device "{{ inputs.target_hardware }}" --seed "{{ inputs.seed }}"`（Jinja 与新 input 同步，避免 StrictUndefined 崩）。
- **sensitivity-agent.md 补 `source orca_env.sh` + `--env_file`**（对齐 PTQ 已踩过的坑，否则 opencode bash 拆调用丢 `ORCA_CHART_SOCK` → 推图静默失败）。
- **qat-trainer.md 修示例数字**：原 `before=0.000732, after=0.002745, recovery=+0.002012`（fake-quant mse 反而比 QAT 后低，错）→ 改 `before=0.002745, after=0.000732, recovery=-0.002013`（mse 口径下 fake-quant 升高、QAT 回落、recovery 为负=改善）。

### 4. 四脚本加 device + 删造假 + WARN/Fail loud 区分

每个 `run_*.py`：

- 加 `from _device import add_device_seed_args, resolve_device_and_seed, wrap_forward_with_device`。
- `add_device_seed_args(ap)` 加 `--device` / `--seed` 参数。
- `resolve_device_and_seed(args.device, args.seed, log_prefix=...)` 解析 device + set_seed（共享 helper，4 脚本零复制）。
- `fp_model.to(device)` 在 `adapter.load_model()` 之后、SDK 调用之前。
- `forward_fn = wrap_forward_with_device(adapter.forward_fn, device)` —— batch 搬 device 由 wrapper 自动做。
- **`_resolve_eval` 的 teacher-student mse 退回打 WARN**（不静默）：明确告知「该指标仅自洽性参考，不代表业务精度」。
- **`eval_loader` 缺失 fail loud**（exit 2 + stderr 明确报缺什么；code-reviewer 🟡 + plan §1-c + user brief「复用 calib 当 eval」禁掉口径）：原「WARN 复用 calib_loader/train_loader」改为 fail loud——eval 用错分布会让 best_metric 选错候选 / Pareto 前沿选错位宽，是比 teacher-student mse 更严重的口径污染。
- **run_qat.py `train_loader` 缺失 fail loud**（原「复用 calib 做最小 smoke」是数据泄漏 + 烧算力跑无意义短训）。
- run_sensitivity.py 补 `--env_file` + `_load_env_file`（对齐 PTQ/bit-curve/qat 三脚本）。

### 5. bit-curve `_bake_selected` 改动生效（核心修复，plan §P5 N7）

`run_bit_curve.py::_bake_selected` 签名从 `-> str` 改为 `-> tuple[str, float | None]`，新增 `eval_fn` / `metric_kind` 参数：

1. bake 主体不变（`quantize_model(qconfig_dict=...)` + `torch.save(state_dict)`）。
2. **新增 reload + 重 eval**：再 deepcopy fp_model → quantize_model 出同拓扑空壳 → `load_state_dict(torch.load(baked_path), strict=True)`（code-reviewer 🔴：原 `strict=False` 会掩盖 state_dict 键失配，改为 strict=True 让键失配 fail loud——丢 observer state 是 bake 真坏了的信号）→ `eval_fn(reload_q_model)` → `reeval_metric`。
3. 任何步骤失败 → 返回 `(path, None)`（不阻断 bake 本身；spec-review N7：曲线产出不受 bake 影响）。

新增 `_compute_bake_metric_relative_diff(reev, final)` 纯函数（无 torch 依赖，便于单测）：`|reev - final| / max(|final|, 1e-12)`，None 或类型错返 None。

新增 `_check_bake_metric_consistency(reev, final, metric_kind)` 副作用层：
- `rel = _compute_bake_metric_relative_diff(...)`；None → stderr WARN「对账跳过」。
- `rel > _BAKE_METRIC_REL_TOL (1e-4)` → stderr 「FAIL LOUD」+ `sys.exit(3)`。
- 在容差内 → stderr log rel_diff。

main 调用点改动（plan §P5 + code-reviewer 🔴 持久化顺序）：
- **持久化顺序**：bake 完成立即 `_dump_json(summary 含 baked_model_path + reeval_metric)` **再** 跑对账——保证对账 fail loud `exit(3)` 时磁盘 `best_mixed_model.pt` 与 `bit_curve_summary.json` 一致，不留「summary=None 但 .pt 已落盘」孤儿态。
- **best_metric 取值优先级**：bake 成功且 reeval 可用 → 取 baked 实测值（非 search `final.score`）；否则取 search `final.score`（bake=false / bake 重 eval 失败）。

### 6. 图表用 P1 轴标签（plan §P5 §7）

- **bit-curve pareto 标题修正**：「Bit-Width vs Accuracy Pareto Frontier」→「Bit-Width vs `{metric_kind}` Pareto Frontier」（原写死 Accuracy 但 y 常是 mse，名实不符）。加 `x_label="avg bit-width (lower is better)"`、`y_label="{metric_kind} (direction: {pareto_y_direction})"`、`caption` 解释 mse 低=好 vs accuracy 高=好。
- **qat recovery bar 加 caption/y_label**：`y_label="after − before ({metric_kind})"`、`caption="recovery = after − before；mse 口径下负值=改善（QAT 把 metric 降下来了）。"`——避免读图者误以为正=好。
- **qat 收敛 line 加 x_label/y_label**：`x_label="QAT training step"`、`y_label="{metric_kind} (lower is better)"`（mse 口径）或 `{metric_kind}`（其它）。

---

## 硬约束遵守

| 约束 | 遵循 | 证据 |
|---|---|---|
| 单一真相源（device 解析） | ✅ | `_device.py` + 4 脚本 `from _device import`；无跨包依赖 |
| yaml input ↔ agent.md Jinja 同步 | ✅ | target_hardware/seed 在 4 yaml + 4 agent.md 都对齐；bash 调用块都加 `--device`/`--seed` |
| `tars validate` 0 error | ✅ | 4 yaml 全过 |
| 不碰 struct/kd/NAS/P4 文件 | ✅ | 改动严格限 quant 范围 |
| argparse required=True 不破坏 | ✅ | 既有 required 参数全保留（P9 slim） |
| 原子写 report | ✅ | `_dump_report`/`_dump_json` 保 tmp + `os.replace` 不变 |
| WARN 走 stderr 不静默 | ✅ | 全部 WARN 用 `sys.stderr.write` |
| Tier B fail loud 不造假 | ✅ | calib/eval/train loader 缺失 exit 2 + stderr 明确；脚本 grep 0 个 `torch.randn` |

---

## code-reviewer 闭环

两轮 review（impl review + test coverage review 并行），找到 5 🔴 + 6 🟡 + 8 🟢，全闭环：

### 🔴 MUST FIX（全修）
1. `_bake_selected` reload 用 `strict=False` 掩盖键失配 → 改 `strict=True`。
2. `_check_bake_metric_consistency` exit(3) 留孤儿 .pt + 过时 summary → bake 完成立即 dump summary 再对账。
3. `_check_bake_metric_consistency` 零测试覆盖 → 抽 `_compute_bake_metric_relative_diff` 纯函数 + 补 13 个测试（5 个 pure math + 5 个 side-effect + 3 个边界）。
4. `_BAKE_METIC_ABS_FLOOR` 拼写错 → `_BAKE_METRIC_ABS_FLOOR`（抽 helper 时一并修）。
5. QAT eval_loader 缺失应 fail loud 而非 WARN（plan §1-c）→ 改 fail loud；PTQ/bit-curve eval_loader 同步改 fail loud（user brief「复用 calib 当 eval」禁掉口径）。

### 🟡 SHOULD FIX（按范围闭环）
- **eval_loader fail loud**（plan §1-c + §P5）→ 已改（见上 🔴5）。
- **`set_seed` NPU `except Exception` 过宽** → 缩窄到 `except AttributeError` + WARN。
- **P5 引入的 4 份 argparse 块重复** → 抽 `add_device_seed_args` + `resolve_device_and_seed` 共享 helper。
- **`_add_quant_scripts_to_path` 死代码** → 删除。
- **`resolve_device(None)` 测试缺** → 补 `test_none_arg_is_auto`。
- **`wrap_forward_with_device` module 透传断言缺** → 测试加 `assert seen_modules == ["fake_module"]`。

### 🟢 NIT（按范围分批）
- **`_free_q_model` 的 `del q_model` 是 no-op** → 不改（idiomatic，与原 nas-agent 同风格）。
- **`reload_q_model.to(next(...).device)` 是 no-op** → 删（reload skeleton 已在 device）。
- **既有 7 类 helper 函数跨 4 脚本复制（_load_adapter / _load_env_file / _resolve_eval / _dump_json / _free_model / _BITWIDTH_PRESETS / _TRUE_TOKENS）** → **登记给 P9**（task brief「不要 slim 现有 input，P9 统一按 input 原则收口」——这些 helper 的抽取与 input slimming 同期做最自然）。
- **ptq/qat/bit-curve 三脚本的 `--project_root` / `--calib_data_ref` / `--eval_data_ref` / `--eval_fn_ref` required=True 死参数** → **登记给 P9**（同上：scripts 不单测，参数精简与 yaml input slim 同期）。
- **qat yaml 的 `lr` / `total_steps` 标 `[default]`** 但 input-principle SPEC §5 标 `[infer]` → P9 对齐。

---

## 测试

- **新增 `tests/workflows/test_quant_device.py`（37 测试）**：
  - `resolve_device` 6 测试：cpu / cuda:N / cuda+local_rank / None=auto / auto 探测 / 非法 tpu fail loud。
  - `is_npu_available` 2 测试。
  - `set_seed` 5 测试：torch / random / numpy 复现 / 异 seed 发散 / `set_seed(0)` 默认。
  - `move_batch_to_device` 7 测试：Tensor / dict / tuple / list / nested / scalar / idempotent。
  - `wrap_forward_with_device` 3 测试：None / 包装后 batch + module 透传 / dict batch。
  - `_compute_bake_metric_relative_diff` 8 测试：both None / 单边 None / 在 tol 内 / 超 tol / final=0 触发 abs_floor / 类型错 / 负 metric。
  - `_check_bake_metric_consistency` side effects 5 测试：在 tol 内 / 超 tol exit 3 / 双 None 跳过 WARN / 单 None 跳过 / final=0 触发 fail loud。
- **py_compile 验证**：5 个 Python 文件（`_device.py` + 4 脚本）全过。
- **import 验证**：4 脚本经 importlib 加载 OK（ts_quant 缺包时不 raise，仅 `_TS_QUANT_OK=False`）。
- **`tars validate`**：4 yaml 全过 0 error。
- **既有测试回归**：`tests/workflows/` + `tests/chart/` 110 passed。

未做 E2E 真机测试（plan §6：批 3 后统一 headless TARS harness 验证），本次仅 unit + py_compile + import 校验——按既定惯例（viz 大修 release note 同款：用 py_compile / mock / 真实账本捕获，不补脚本主体单测）。

---

## 范围外（P9 / 批 4）

- **input 精简**（`mode`/`bit_widths`/`recipes`/`scheme`/`bake`/`lr` 等）按 input 原则 Tier 化——task brief「不要 slim，本任务只加 target_hardware/seed + 修正确性」。
- **既有 7 类 helper 跨 4 脚本复制抽取到 `_common.py`**——与 P9 input slim 同期最自然。
- **死 required=True 参数 `--project_root`/`--calib_data_ref`/`--eval_data_ref`/`--eval_fn_ref`** 清理——同 P9。
- **真机 E2E**（headless TARS-SKILL harness）——plan §6 批 3 之后统一建。

---

## 验收

- 4 个 quant workflow 缺校准/训练/评估数据时**不再造假**：脚本 grep 0 个 `torch.randn`；agent.md 模板里 0 个「兜底假随机 / 复用 calib 当 eval / 复用 train 当 eval」指示段；改为 Tier B 三步契约 + fail loud（stderr 明确报缺什么 + exit 2）。
- `--device cuda` 真在 GPU 跑：`fp_model.to(device)` + `wrap_forward_with_device`（batch 搬 device 由 wrapper 自动做，幂等）。
- `--device npu` 有路径：`resolve_device` 经 `is_npu_available()`（`find_spec("torch_npu")` + `torch.npu.is_available()`）支持 NPU；`set_seed` 含 `torch.npu.manual_seed_all`。
- **bit-curve bake 对账生效**：`_bake_selected` 返 `(path, reeval_metric)`；`_check_bake_metric_consistency` 超 tol（相对 1e-4）fail loud exit 3；持久化顺序保证 exit(3) 时磁盘一致。
- `tars validate` 0 error（4 yaml）。
- 37 新测试全过；110 既有测试无回归。
- code-reviewer 两轮闭环（impl + coverage 并行）。
