# 2026-07-31 — KD-NAS flatten `__main__` 升级为「正确性 + latency」统一契约

## 背景

KD-NAS workflow 的 baseline latency 原由 `kd-setup/agent.md` step2 调 `tune_latency.py`
（`--max_measurements 1`）测量。用户要求把 latency 测试**下沉到 flatten 产出的契约文件
`<base>_flat.py` 的 `__main__`**：统一「跑 `__main__` = 正确性 + latency」，让 baseline 时延
在 flatten 阶段就测了（用 `inputs.latency_provider` 实测），下游 setup 不用重复测。后续
teacher-gen 产出的 teacher 文件照同样 `__main__` 结构，也自带 latency。

## 改动清单

### 1. 新增 `workflows/agents/model-flatten/scripts/measure_latency.py`

flatten 自包含的 latency 测量 helper（与 `validate_contract.py` 同目录、同款 standalone 原则——
**不 import `_kd_scripts` / `_struct_scripts`**，ONNX 导出 inline 实现）：

- `measure_contract_latency(contract_path, latency_provider, device, seed, opset, repeats, onnx_out)`
  → `{latency_ms_median, latency_ms_std, source, confidence, onnx_path}`
- latency_provider 非空 → 加载 `path::func` → `measure(onnx[, device])` repeats 次取 median+std
  （`source=provider` / `confidence=high`）
- latency_provider 空 → ONNXRT-CPU fallback + stderr WARN（`source=cpu-fallback` / `confidence=low`）
- 绝不伪造（CONTRACTS §6）：测不出 → raise + exit 2（不编造数值）
- CLI emit `LATENCY_MS:` / `LATENCY_STD:` / `LATENCY_SOURCE:` / `LATENCY_CONFIDENCE:` / `ONNX:`

### 2. `<base>_flat.py` 的 `__main__` 升级

- SKILL.md Step 3 `__main__` 模板：先跑 correctness（build + forward + shape），再调
  `measure_contract_latency` 测 latency（经 `$ORCA_AGENT_RESOURCES` env 定位 helper）。
- helper 未找到（非 orca 编排上下文）→ `LATENCY_SKIPPED`（不伪造，明确告知如何启用）。
- `--latency_provider` 默认值 = flatten 时渲染后的 `{{ inputs.latency_provider }}` 实际路径串
  （非 Jinja 模板；空 input → `""` → helper fallback）。CLI 可覆盖。
- **契约 standalone 不变**：latency 逻辑全在 `if __name__ == "__main__":` 内，顶层 contract
  符号（`DUMMY_INPUT` / `BUILD_FN` / `KNOBS` / `build_model`）只依赖 torch + 3rd-party。
  `validate_contract.py` import 时 `__main__` 不执行（有测试守门）。

### 3. flatten agent.md / SKILL.md / kd-nas.yaml

- flatten agent.md：新增 `latency_provider` input + output JSON 加 `baseline_latency_ms`；
  bash 块在 `validate_contract.py` PASS 后跑 `python3 "$CONTRACT" --latency_provider ...`，
  解析 `LATENCY_MS:` → `baseline_latency_ms`（拿不到 → exit 2 fail loud）。
- SKILL.md Step 6b flatten-verifier 加第三维校验「Latency `__main__` wiring」：
  input 给了 latency_provider 但 `__main__` default 空（→ fallback）→ [BLOCKER]
  （违反「latency 必用用户脚本」铁律，CONTRACTS §6 / BLK-3）。
- kd-nas.yaml：flatten output_schema 加 `baseline_latency_ms`（number, required）；
  setup output 的 `baseline_latency_ms` description 改为「从 flatten.output 透传」。
- CONTRACTS.md §0 目录布局加 `measure_latency.py`；§4 节点 I/O 表更新 flatten / setup 行。

### 4. kd-setup step2 简化

- 契约 assert（`build_model` + `DUMMY_INPUT`）保留作 fail-loud 复核。
- **删 `tune_latency.py` 测量调用**；`BASELINE_LATENCY_MS` 改读
  `{{ flatten.output.baseline_latency_ms }}` + python float 校验。

## 边界（未动）

- gate 的 `tune_latency`（student 变体卡门调参搜索）不动——职责不同。
- `teacher_setup.py` 不动（还做 teacher_cache/ONNX/meta；teacher-gen 那轮再理）。
- KB 手写 student 变体（`spt_*.py`）不改——仍走 gate tune_latency。
- `kd-train-script/` 不碰。

## 设计决策（说明理由）

- **helper 位置 = `model-flatten/scripts/`**（非 `_kd_scripts/`）：与 `validate_contract.py`
  同级，保持 flatten standalone（不引跨包依赖）；ONNX 导出 inline。
- **`<base>_flat.py` 的 `__main__` 调 helper 用 env-import**（`$ORCA_AGENT_RESOURCES`）：
  参考 `train_adapter_template.py` 同款模式；不新增 env var；helper 未找到 → `LATENCY_SKIPPED`
  （不伪造）。契约顶层 import 不受影响（latency 逻辑全在 `__main__` 内）。
- **measure_latency.py 自包含**（内联 `_resolve_device` / `_ort_providers` / `_load_measure` /
  ONNX 导出）：与 `validate_contract.py` 同款 standalone 原则；Rule 6 的 DRY 例外是 standalone
  契约的刻意代价（docstring 标注）。

## 验证

- `tests/workflows/test_model_flatten.py`：52 测试（原 26 + 新增 22 覆盖 measure_latency CLI /
  函数级 / flat `__main__` 端到端 / doc-contract / standalone 守门），全绿。
- `tests/workflows/test_kd_redesign.py`：flatten output_schema 测试更新含 `baseline_latency_ms`。
- `tests/workflows/` 全套：358 passed（无回归）。
- WSL Ubuntu + venv（torch 2.13.0+cpu / onnxruntime 1.27.0 / numpy 2.4.4）实测：
  measure_latency 三路径（CPU fallback / demo provider / fail loud）均符合预期。

## Review 闭环

两路 code-reviewer 审查（代码质量 + 测试覆盖）。代码质量审查无 [BLOCKER] / [MAJOR]。
测试覆盖审查抓到 3 个 [MAJOR]，**全部修复**：

1. 模块级 `pytest.importorskip` 误伤全部 validate_contract 测试 → 改 `@needs_ort` skipif
   装饰器逐测试 gate（validate_contract 测试只依赖 torch，不受 ort 缺失影响）。
2. `accepts_device=False` 分支（provider 无 `device` 形参）零覆盖 → 加
   `test_measure_latency_provider_without_device_kwarg`。
3. 「绝不伪造」intent 4 条 failure path 仅 1 条断言 → 其余 3 条各加
   `assert "LATENCY_MS:" not in r.stdout`。

[MINOR] 修复：`_load_measure` AttributeError 分支、空 KNOBS 容错分支、`--seed/--opset`
非默认值、`LATENCY_STD/ONNX` stdout 行、step2 dead OR 分支、measure_latency standalone
守门测试、CLI 覆盖空默认值路径、verifier「rendered 值」断言。

## Open Questions

1. **torch.onnx.export DeprecationWarning**（legacy TorchScript exporter，torch 2.9+ 新 exporter
   成默认）：本 helper 用 `dynamo=False` 保持 legacy 行为，与现有 `_struct_scripts/export_onnx.py`
   一致。torch 真移除 legacy exporter 时，全仓 ONNX 导出脚本（export_onnx / tune_latency /
   teacher_setup / measure_latency）需统一迁移到新 exporter——本轮不处理。
2. **teacher-gen（下一轮）**：teacher 文件应照同样 `__main__` = correctness + latency 结构。
   本轮不动 `teacher_setup.py`（其 latency 产出暂保留），下一轮 teacher-gen 统一。
3. **`_materialize_dummy` dtype 缺失静默回退 float32**：与 `validate_contract` 的「dtype 显式
   声明」严格要求略有出入；保留为 standalone 容错（validate_contract 是 dtype 契约的 gate，
   measure_latency 是只读消费者）。code-reviewer 建议保持现状。
4. **`_load_measure` 模块名生成**用 basename（`os.path.basename(path).replace(".", "_")`）vs
   `tune_latency.py` 的 md5 hash——单次加载无实际碰撞风险；非必须统一。

## 未 commit

按任务要求未 commit / 未 push。Commit SHA 待补。
