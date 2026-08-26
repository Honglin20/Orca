---
name: teacher-gen
description: Derive a teacher structure file from a flatten-produced baseline contract by pure parameter tuning (depth ×3 / width ×2). LLM identifies depth/width axes (judgment); scripts do hard validation (deterministic). Teacher file is a wrapper that delegates to baseline.build_model — no architecture change.
---

# Teacher-Gen Skill

Use this skill to derive a **teacher** structure file from a baseline KD-NAS variant contract
(produced by `model-flatten`). The teacher is **pure parameter tuning** of the baseline:
the depth axis is scaled ×3 and the width axis ×2, **with no architecture change** and **no
block-type change**. The teacher file is a **wrapper** that delegates `build_model(**cfg)` to
the baseline's `build_model` with the scaled default cfg.

This skill derives the teacher file from the baseline KD-NAS variant contract
produced by `model-flatten`. The teacher file's `__main__` block mirrors the
flatten template: correctness + latency via `measure_contract_latency`. Teacher
latency is measured here in teacher-gen, not deferred to setup.

Skill resource paths:

- `<skill_dir>`: The directory containing this `SKILL.md`. All `scripts/` paths are relative
  to `<skill_dir>`. Resolved by the agent entry (agent.md) to `$ORCA_AGENT_RESOURCES`.
- `<skill_dir>/scripts/measure_latency.py`: latency helper (byte-aligned copy of
  `model-flatten/scripts/measure_latency.py`; kept independent so `$ORCA_AGENT_RESOURCES`
  resolution works for either agent).
- `<skill_dir>/scripts/validate_teacher.py`: teacher-specific hard validation
  (DUMMY_INPUT verbatim match + depth/width ×3/×2 + capacity > baseline).

## Lazy Loading

Do **not** read all project files upfront. Only read the materials a specific step requires
**when you begin that step**. The baseline contract file is the only mandatory read.

## Working Directory and Path Conventions

- `<output_dir>`: Use engine-injected `$ORCA_ARTIFACTS_DIR` if non-empty (run scope,
  authoritative). Otherwise default to `llm_artifacts/<base_name>_teacher/` under cwd.
  **Run `cd <output_dir>` once before executing any command**; the working directory persists.
- `<user_project_root>`: Inferred once in preparation (see agent.md), used to cross-reference
  `model-flatten/scripts/validate_contract.py` (reused, not copied).
- `<baseline_contract_path>`: The flatten output contract `.py` (absolute path).
- Use `pathlib.Path` for path construction; never string concatenation.

## Workflow

Follow these 4 steps in order. Use the `todowrite` tool to track progress.

### Step 1: Read Baseline Contract

Load `<baseline_contract_path>` as a Python module (via `importlib.util.spec_from_file_location`
in a scratch shell, or read it as text). Extract and record:

- `DUMMY_INPUT` (the dict — needed for verbatim copy into teacher file).
- `KNOBS` (the full dict — needed for default-scaling in teacher file).
- `BUILD_FN` (must be `"build_model"`; flatten guarantees this).
- `build_model` signature (must accept `**cfg` and pass knob kwargs to the model constructor).
- Module-level imports the baseline depends on (for wrapper path setup — see Step 3).

If any of these are missing or malformed → **fail loud**: emit a clear stderr message naming
the missing field and stop. Do not proceed to Step 2.

Do **not** read training code, data loaders, or downstream nodes — out of scope.

### Step 2: Identify Depth and Width Axes (LLM Judgment)

This step is the **only LLM-judgment-heavy part**; the rest is mechanical derivation.

For each knob name in baseline `KNOBS`, classify by **semantic name pattern**:

| Axis | Name pattern (case-insensitive substring) | Examples |
|---|---|---|
| **Depth** | `block`, `layer`, `stage`, `depth`, `num_layers`, `num_blocks`, `num_stages`, `layers` | `num_blocks`, `num_layers`, `depth`, `encoder_layers` |
| **Width** | `channel`, `embed_dim`, `hidden`, `width`, `feature`, `channels`, `embed`, `dim`, `features` | `embed_dim`, `channels`, `hidden_dim`, `width` |
| **Neither** | (anything else) | `num_heads`, `expansion`, `kernel_size`, `dropout` |

Decision rules (deterministic given the patterns, but the LLM applies them):

1. **Depth axis**: pick the knob whose name matches a depth-axis pattern. If multiple match,
   pick the one with `leverage == "high"` (block/layer count dominates latency). If still
   tied, pick the first in baseline KNOBS order.
2. **Width axis**: pick the knob whose name matches a width-axis pattern. If multiple match,
   pick `leverage == "medium"` first (embed_dim/channel), then `leverage == "high"`.
3. **Neither axis present**: if no knob matches depth pattern → `DEPTH_AXIS = ""` (rare; emit
   a low-confidence note in the final JSON). Same for width.
4. **Conflict**: a knob name cannot be both axes (the patterns are disjoint in practice; if a
   name somehow matches both, classify by the first pattern hit in the depth-then-width order).

Record the chosen axis names — they become `DEPTH_AXIS` / `WIDTH_AXIS` constants in the teacher
file, and the script `validate_teacher.py` enforces the ×3/×2 math based on them.

**Non-rules** (do not invent): do not invent axes the model doesn't have. Do not pick a knob
just because it has high leverage — name pattern decides axis membership; leverage only
disambiguates within an axis.

### Step 3: Write the Teacher File (Wrapper + Scaled Cfg + `__main__` Latency)

#### 3a. File name and location

- `<base_name>`: take baseline file stem, strip `_flat` suffix if present, append `_teacher`.
  E.g., `<base_name>_flat.py` → `<base_name>_teacher.py`; `baseline_model.py` →
  `baseline_model_teacher.py`.
- Write to `<output_dir>/<base_name>.py`.

#### 3b. File structure (use this template verbatim — only the `<FILL>` placeholders change)

```python
"""<base_name>.py —— 由 teacher-gen 从 <baseline_file_basename> 纯调参派生（深度轴 ×3 / 宽度轴 ×2）。

**纯调参派生**：teacher = baseline 的 ``build_model`` 调大 cfg，**不改架构、不改 block 类型**。
本文件是 wrapper：``build_model(**cfg)`` 通过 ``importlib.util.spec_from_file_location`` 按绝对
路径加载 baseline 模块，再委托其 ``build_model``，传调大的 default cfg。

I/O 契约（与 baseline 完全一致——KD 要求 teacher/student 同 I/O shape，硬约束）：
  - ``DUMMY_INPUT`` = baseline.DUMMY_INPUT（逐字复制，硬编码字面量；不用引用以免 mutable 共享）
  - ``BUILD_FN = "build_model"``
  - ``KNOBS`` = baseline.KNOBS 同 schema，default 调大（深度轴 ×3 / 宽度轴 ×2 / 其余不变）

派生轴（teacher-gen LLM 识别，可审计，``validate_teacher.py`` 强制 ×N 数学）：
  - ``DEPTH_AXIS = "<depth knob name>"``  （深度方向，default ×3）
  - ``WIDTH_AXIS = "<width knob name>"``  （宽度方向，default ×2）

``__main__`` 逐字照 ``model-flatten/SKILL.md`` Step 3 模板（正确性 + latency，调 measure_latency
helper via ``$ORCA_AGENT_RESOURCES``）——teacher 自带 latency 测试，teacher latency 在 teacher-gen
阶段测掉，不留给 setup。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

# baseline 契约绝对路径（teacher-gen 时渲染；wrapper 据此加载 baseline 模块）。
_BASELINE_CONTRACT_PATH = "<FILL: baseline_contract_path 绝对路径>"


def _load_baseline_module() -> Any:
    """按绝对路径加载 baseline 契约模块（spec_from_file_location，不污染 sys.modules）。

    baseline 自身的 sibling import（如 ``<baseline_sibling_module>``）由 baseline 顶层
    代码自管（其顶层会 ``sys.path.insert`` 自己的 sibling 目录）。
    """
    p = os.path.abspath(_BASELINE_CONTRACT_PATH)
    if not os.path.isfile(p):
        raise FileNotFoundError(f"baseline 契约不存在：{p}")
    parent = os.path.dirname(p)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec = importlib.util.spec_from_file_location(
        f"_teacher_baseline_{os.path.basename(p).replace('.', '_')}", p
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 baseline {p} 创建 module spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_baseline = _load_baseline_module()
_baseline_build_model = _baseline.build_model


# ---------------------------------------------------------------------------
# I/O 契约（与 baseline 逐字一致；KD 硬约束——teacher/student 必须同 I/O shape）。
# 硬编码字面量（不引用 baseline.DUMMY_INPUT 对象——避免 mutable 共享，且便于读源码审计）。
# ---------------------------------------------------------------------------
DUMMY_INPUT = <FILL: 逐字复制 baseline.DUMMY_INPUT，例如 {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}>
BUILD_FN = "build_model"

# 派生轴声明（teacher-gen LLM 识别；validate_teacher.py 据此强制 ×3/×2 数学）。
DEPTH_AXIS = "<FILL: 深度轴 knob 名>"
WIDTH_AXIS = "<FILL: 宽度轴 knob 名>"

# ---------------------------------------------------------------------------
# KNOBS：与 baseline 同 schema，default 调大（深度轴 ×3 / 宽度轴 ×2 / 其余逐字不变）。
# min / step / leverage 始终继承 baseline（轴只动 default；走同一套 validate_contract）。
# ---------------------------------------------------------------------------
KNOBS = {
    <FILL: 每个 knob 与 baseline 同 schema，default 按轴规则调大>
    # 例如 baseline num_blocks default=4, min=1, step=-1, leverage="high"
    #   → teacher DEPTH_AXIS default=12 (= 4*3), min/step/leverage 逐字继承
    # 例如 baseline embed_dim default=12, min=4, step=-2, leverage="medium"
    #   → teacher WIDTH_AXIS default=24 (= 12*2), min/step/leverage 逐字继承
    # 例如 baseline num_heads default=4 (非轴) → teacher 逐字复制
}


def build_model(**cfg) -> "nn.Module":
    """Teacher = baseline 调大 cfg（深度轴 ×3 / 宽度轴 ×2），纯调参派生（wrapper）。

    委托给 ``baseline.build_model``，**不改架构、不改 block 类型**。
    零参用 ``KNOBS`` defaults（已调大）；cfg 覆盖旋钮。
    """
    return _baseline_build_model(**cfg)


if __name__ == "__main__":
    # 逐字照 model-flatten/SKILL.md Step 3 __main__ 模板（正确性 + latency 统一契约）。
    import argparse

    import torch

    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _defaults = {k: v["default"] for k, v in KNOBS.items()}
    _model = build_model(**_defaults).to(_device).eval()
    _shape = list(DUMMY_INPUT["shape"])
    _dtype = getattr(torch, DUMMY_INPUT.get("dtype", "float32"))
    _dummy = torch.randn(*_shape, dtype=_dtype, device=_device)
    with torch.no_grad():
        _out = _model(_dummy)
    print(f"CORRECTNESS: OK | input={_shape} output={list(_out.shape)}")

    # latency 测量（默认 cfg）：跑 __main__ = 正确性 + latency（统一契约）
    # --latency_provider 默认值 = teacher-gen 时用户给的 inputs.latency_provider（可 CLI 覆盖）
    _ap = argparse.ArgumentParser(add_help=False)
    _ap.add_argument(
        "--latency_provider",
        default="<FILL: 渲染后的 inputs.latency_provider，如 /abs/path.py::measure；空串 → helper fallback>",
    )
    _ap.add_argument("--device", default="auto")
    _ap.add_argument("--seed", type=int, default=0)
    _ap.add_argument("--repeats", type=int, default=3)
    _ap.add_argument("--opset", type=int, default=17)
    _args, _ = _ap.parse_known_args()

    _resources = os.environ.get("ORCA_AGENT_RESOURCES", "")
    _helper = (
        os.path.join(_resources, "scripts", "measure_latency.py")
        if _resources
        else ""
    )
    if _helper and os.path.isfile(_helper):
        sys.path.insert(0, os.path.dirname(_helper))
        from measure_latency import measure_contract_latency  # noqa: E402

        _r = measure_contract_latency(
            contract_path=__file__,
            latency_provider=_args.latency_provider,
            device=_args.device,
            seed=_args.seed,
            opset=_args.opset,
            repeats=_args.repeats,
        )
        print(f"LATENCY_US: {_r['latency_us_median']:.6f}")
        print(f"LATENCY_STD: {_r['latency_us_std']:.6f}")
        print(f"LATENCY_SOURCE: {_r['source']}")
        print(f"LATENCY_CONFIDENCE: {_r['confidence']}")
    else:
        # helper 未找到（非 orca 编排上下文）：不伪造，明确告知。
        print(
            "LATENCY_SKIPPED: measure_latency helper 未找到"
            "（set ORCA_AGENT_RESOURCES 指向 teacher-gen skill 目录可启用 latency 测量）"
        )
```

#### 3c. Fill the placeholders (deterministic given Step 2)

Substitute each `<FILL: ...>` with the literal value:

- `_BASELINE_CONTRACT_PATH`: the absolute path string of the baseline `.py`.
- `DUMMY_INPUT`: copy the baseline's dict **verbatim** (same shape list, same dtype string).
- `DEPTH_AXIS` / `WIDTH_AXIS`: the names chosen in Step 2.
- `KNOBS`: every baseline knob reproduced with same `min` / `step` / `leverage`; `default`
  scaled as follows:
  - `name == DEPTH_AXIS` → `default = int(round(baseline_default * 3))` (e.g., 4 → 12).
  - `name == WIDTH_AXIS` → `default = int(round(baseline_default * 2))` (e.g., 12 → 24).
  - else → `default = baseline_default` (unchanged).
- `--latency_provider` default: the **rendered** value of `{{ inputs.latency_provider }}`
  (e.g., `/abs/latency.py::measure`), NOT a Jinja template string. Empty input → `""` default
  → helper falls back to ONNXRT-CPU + WARN (kd-nas workflow makes it required).

#### 3d. Self-review before saving

Re-read the rendered teacher file and verify (a) `_BASELINE_CONTRACT_PATH` is absolute and
points to an existing file; (b) `DUMMY_INPUT` matches baseline byte-for-byte; (c) `KNOBS`
keys are identical to baseline, with only axis defaults scaled; (d) `build_model` body is
exactly `return _baseline_build_model(**cfg)` — no architecture code, no inlined block classes,
no new imports beyond `importlib.util` / `os` / `sys` / `typing` / `torch` (torch is imported
inside `__main__`); (e) no hardcoded specific architecture name (use placeholder form, never
concrete model names) — the docstring uses generic terms (深度轴 / 宽度轴 / baseline).

### Step 4: Hard Validation + `teacher-gen-verifier` Iteration

Two complementary layers (same split as flatten): deterministic script + LLM verifier.

#### 4a. 执行：Script Hard Validation (deterministic, fail loud)

Run **two** scripts in sequence (both must exit 0):

```bash
# 1. model-flatten 的 validate_contract.py（teacher 是 KD 变体契约，同门规；复用不复制）
python3 "<user_project_root>/workflows/agents/model-flatten/scripts/validate_contract.py" \
  --contract "<output_dir>/<base_name>.py" \
  --device "auto" --seed 0

# 2. teacher-gen 的 validate_teacher.py（teacher 专属：DUMMY_INPUT 一致 + 深度/宽度 ×3/×2 + 容量上升）
python3 "$ORCA_AGENT_RESOURCES/scripts/validate_teacher.py" \
  --baseline "<baseline_contract_path>" \
  --teacher "<output_dir>/<base_name>.py" \
  --device "auto" --seed 0
```

Script (1) checks the contract format (import OK, BUILD_FN, KNOBS schema, forward shape ==
DUMMY_INPUT.shape, `build_model(**mins)` still forwards). Script (2) checks the teacher-specific
derivation fidelity (DUMMY_INPUT verbatim match, axis math ×3/×2, other knobs unchanged,
param count strictly up).

If either exits non-zero → read `FAIL_REASON:`, fix the teacher file, re-run. **Do not proceed
to 4b until both scripts PASS** — the LLM verifier cannot catch import / shape / math errors.

#### 4b. `teacher-gen-verifier` Subagent Iteration (LLM judgment, scaffold)

Invoke the `teacher-gen-verifier` subagent with the prompt framework below. The verifier
checks what scripts cannot: **axis identification correctness** (semantic — did the LLM pick
the right knob as depth/width?), **wrapper purity** (is teacher really a delegate, not an
architecture copy?), and **`__main__` latency wiring** (is `--latency_provider` default the
rendered value, not an empty string or Jinja template?).

**`teacher-gen-verifier` invocation prompt (scaffold —— adapt per baseline):**

```
You are the teacher-gen-verifier subagent. Review the derived teacher file for KD-NAS.

Inputs:
- Baseline contract: <baseline_contract_path>
- Teacher candidate: <output_dir>/<base_name>.py
- Validator status: validate_contract.py PASS + validate_teacher.py PASS (already enforced;
  do not re-run).

Check three dimensions and report issues with severity tags:

1. Axis identification (severity [BLOCKER] if violated):
   - DEPTH_AXIS is the knob whose name semantically denotes depth/block/layer count (matches
     one of: block, layer, stage, depth, num_layers, num_blocks, num_stages, layers). If the
     chosen name doesn't match any depth-axis pattern → [BLOCKER] (axis mislabeled).
   - WIDTH_AXIS is the knob whose name denotes width/channel/embed dim (matches one of:
     channel, embed_dim, hidden, width, feature, channels, embed, dim, features). Mismatch →
     [BLOCKER].
   - If baseline has an obvious depth/width knob that was skipped (DEPTH_AXIS="" or
     WIDTH_AXIS="" while a matching knob exists) → [BLOCKER] (axis missed).
   - If a knob matches both patterns (rare), depth-then-width order should apply; flag [MINOR]
     if you disagree with the disambiguation.

2. Wrapper purity (severity [BLOCKER] if violated):
   - teacher.build_model body is exactly `return _baseline_build_model(**cfg)` (or equivalent
     thin delegate). Any inlined block class / forward logic / new architecture code →
     [BLOCKER] (architecture change, not pure parametric derivation).
   - teacher file imports only stdlib (importlib.util, os, sys, typing) at top-level; torch
     imported inside __main__. Any local-project import beyond baseline module loading →
     [BLOCKER] (breaks standalone).
   - Docstring uses generic terms (深度轴 / 宽度轴 / baseline) — any hardcoded specific
     architecture name (e.g. `<SpecificModelName>`) → [MAJOR] (over-fitted to one model
     family; teacher-gen must be model-agnostic).
   - KNOBS min / step / leverage for axis knobs are inherited verbatim from baseline (only
     default scaled). Any extra field change → [MAJOR].

3. Latency `__main__` wiring (severity [BLOCKER] if violated):
   - The `__main__` block measures latency via `measure_contract_latency` (not just
     correctness). Missing latency step → [BLOCKER] ("跑 __main__ = 正确性 + latency" unified
     contract).
   - **When `{{ inputs.latency_provider }}` is non-empty**: the `--latency_provider` default
     in `__main__` MUST be the rendered value (e.g., `/abs/path.py::measure`), NOT empty.
     Giving a latency_provider in the input but leaving the default empty (→ ONNXRT-CPU
     fallback) violates the "latency 必用用户脚本" 铁律 → [BLOCKER].
   - The default is a **rendered path string**, not a Jinja template (`{{ ... }}` literally
     in the .py → [BLOCKER]).

Output format:
- If all checks pass: emit exactly `PASS` (or `LGTM`) and nothing else.
- Otherwise: one issue per line, format:
  `[<SEVERITY>] <symptom> | reason: <reason> | fix: <guidance>`

Prioritize [BLOCKER] > [MAJOR] > [MINOR] in your review.
```

**Iteration loop:**

1. Spawn `teacher-gen-verifier` with the prompt above (use the `task` tool / subagent
   invocation mechanism).
2. If output is `PASS` / `LGTM`: Step 4 complete; proceed to agent.md output JSON.
3. If output contains issues: apply targeted fixes (highest severity first), re-run both
   scripts in 4a to confirm still PASS, then re-spawn verifier.
4. **Cap iterations at 3** —— if still not PASS after 3 rounds, emit the teacher file as-is
   + add the verifier's most recent `[BLOCKER]` / `[MAJOR]` issue to the agent.md final JSON
   `depth_axis` field as a suffix ` (low-confidence: <one-line issue>)`. Do not hide the
   unresolved issue. Still must have both scripts PASS to return JSON.

## Validation Summary

- Step 1-3 each complete only when the teacher file's `__main__` block runs successfully (no
  import / shape / dtype / device / runtime errors). The `__main__` block must emit
  `CORRECTNESS: OK` AND `LATENCY_US: <number>` (the unified "run `__main__` = correctness +
  latency" contract; `LATENCY_SKIPPED` only when run outside the orca context without
  `ORCA_AGENT_RESOURCES`).
- Step 4a (`validate_contract.py` + `validate_teacher.py`) is the deterministic gate for
  contract format + derivation fidelity math; 4b is judgment-driven (axis identification +
  wrapper purity + latency `__main__` wiring).
- The final artifact is `<output_dir>/<base_name>.py` —— agent.md reads its path into the
  `teacher_model_path` output field and its `LATENCY_US:` into `teacher_latency_us`.

## Guidelines

- Preserve all generated artifacts unless the user explicitly asks for cleanup.
- Keep the teacher file free of `ModuleNotFoundError` for local project code (the wrapper
  loads baseline by absolute path; baseline's own siblings are handled by baseline's top-level
  code).
- Variable / function / class / string-literal / comment names in English (the depth/width axis
  comments may use 中文 for parity with flatten's comments).
- Conservative axis identification when knob names are ambiguous —— Step 4b verifier will
  catch mislabels.
