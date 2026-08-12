"""measure_baseline.py —— Puzzle U2a：确定性测量 + 4 道 fidelity smoke（SPEC v2 §9）。

职责（确定性，fail loud）——pz_expand 的「测量」部分（判断由 LLM 做）：
  1. 读 LLM 产的 ``search_space.yaml`` → slot 声明（path/kind/证据）。
  2. 4 道 smoke（§9.2）：
     - **strict-load**（E5/BLK-1）：father_ckpt load_state_dict missing/unexpected 必须双零。
     - **forward-determinism**（E25）：同输入 forward 两次 ``torch.equal``。
     - **eval-stability**（E25）：eval_fn 跑两次 acc 一致。
     - **per-slot identity allclose**（E5）：hook 每个 slot，forward 两次逐元素 allclose。
  3. trace 每个 slot 的 in/out 末维 → 回填 search_space.yaml + 落 block_map.json。
  4. 测 baseline acc + latency → baseline_metrics.json。

empty slots（E22）→ exit 2（terminate_unsupported，确定性 post-check）。
任一 smoke 失败 → exit 2 + stderr 写明哪道 smoke（fail loud，不静默吞）。

stdout 关键行：
    BASELINE_ACC: <value>
    BASELINE_LATENCY: <value>
    BLOCK_MAP: <path>
    SEARCH_SPACE: <path>
    RESULT_JSON: {...}
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

from puzzle_common import (
    _extract_state_dict,
    get_module_dummy_input,
    load_flat_model,
    resolve_eval_fn,
)
from search_space_io import (
    load_search_space_yaml,
    save_search_space_yaml,
    to_block_map,
)

# per-slot identity allclose 容差（§9.2 / §16.4）
_ALLCLOSE_ATOL = 1e-5
# eval-stability acc 容差（两次 eval 完全相等几乎不可能，留极小 float 漂移窗）
_EVAL_STABILITY_ATOL = 1e-9


# ── smoke 1: strict-load（E5/BLK-1）───────────────────────────────────────────

def strict_load_father(
    model: nn.Module, father_ckpt: Path
) -> dict[str, torch.Tensor]:
    """load father ckpt → load_state_dict(strict=False) → missing/unexpected 双零。

    SPEC §9.2.1：father 是全链 teacher，missing 污染 BLD/score/gkd——零容忍。
    比 puzzle_common.load_father_model 的 20% 阈值更严（本节点是 fidelity 关卡）。
    """
    if not father_ckpt.is_file():
        raise FileNotFoundError(
            f"father_ckpt 不存在：{father_ckpt}（pz_expand 契约必给预训练父权重）"
        )
    ckpt = torch.load(father_ckpt, map_location="cpu", weights_only=False)
    state = _extract_state_dict(ckpt)
    if not isinstance(state, dict):
        raise TypeError(
            f"father_ckpt 解出的 state_dict 非 dict: {type(state).__name__}（{father_ckpt}）"
        )
    missing, unexpected = model.load_state_dict(state, strict=False)
    if missing:
        raise RuntimeError(
            f"strict-load smoke 失败：missing {len(missing)} keys（须双零）。"
            f"前 8: {missing[:8]}。检查 flat.py 的 state_dict schema 是否与 ckpt 对齐"
            f"（reparenting / 前缀 / 模块结构）——确定性 hint：diff missing keys 与 flat 的"
            f" state_dict 找 exact prefix mismatch。"
        )
    if unexpected:
        raise RuntimeError(
            f"strict-load smoke 失败：unexpected {len(unexpected)} keys（须双零）。"
            f"前 8: {unexpected[:8]}"
        )
    return state


# ── smoke 2 + 4: forward-determinism + per-slot identity allclose ─────────────

def _hook_slot_outputs(
    model: nn.Module, paths: list[str], dummy_input: torch.Tensor, device: torch.device
) -> dict[str, torch.Tensor]:
    """forward 一次，hook 抓每个 slot path 的 output tensor（detach）。"""
    model.eval().to(device)
    captured: dict[str, torch.Tensor] = {}
    handles: list[Any] = []

    def make_hook(path: str):
        def hook(_mod: nn.Module, _inputs: tuple, output: Any):
            if path in captured:
                return
            out_t = output[0] if isinstance(output, (tuple, list)) else output
            captured[path] = out_t.detach()

        return hook

    try:
        for path in paths:
            try:
                mod = model.get_submodule(path)
            except AttributeError as e:
                raise AttributeError(
                    f"slot path {path!r} 定位失败（get_submodule）：{e}——检查 search_space path"
                ) from e
            handles.append(mod.register_forward_hook(make_hook(path)))
        with torch.no_grad():
            model(dummy_input.to(device))
    finally:
        for h in handles:
            h.remove()
    missing_paths = [p for p in paths if p not in captured]
    if missing_paths:
        raise RuntimeError(
            f"hook 未捕获 slot output：{missing_paths[:3]}（共 {len(missing_paths)} 个）"
        )
    return captured


def forward_determinism_and_identity_allclose(
    model: nn.Module,
    slot_paths: list[str],
    dummy_input: torch.Tensor,
    device: torch.device,
) -> None:
    """smoke 2 + smoke 4 合并跑（两次 forward 复用）。

    - smoke 2（forward-determinism）：whole-output torch.equal（捕获未固定 RNG）。
    - smoke 4（per-slot identity allclose）：每个 slot 两次 forward output 逐元素 allclose
      （§16.4 真实机制——验证 father-loaded 模块在 zero intervention 下输出稳定可复现，
      即 identity 候选的行为承诺）。atol = _ALLCLOSE_ATOL。
    """
    model.eval().to(device)
    with torch.no_grad():
        out1 = model(dummy_input.to(device))
        out2 = model(dummy_input.to(device))
    # smoke 2：whole-output（tuple/tensor 都支持）
    if isinstance(out1, (tuple, list)):
        if len(out1) != len(out2):
            raise RuntimeError(
                f"forward-determinism smoke 失败：两次 forward 输出 arity 不一致"
                f"（{len(out1)} vs {len(out2)}）"
            )
        for i, (a, b) in enumerate(zip(out1, out2)):
            if not torch.equal(a, b):
                raise RuntimeError(
                    f"forward-determinism smoke 失败：第 {i} 个输出 torch.equal=False"
                    f"（forward 内含未固定 RNG / 无序算子）"
                )
    else:
        if not torch.equal(out1, out2):
            raise RuntimeError(
                "forward-determinism smoke 失败：torch.equal=False"
                "（forward 内含未固定 RNG / 无序算子）"
            )

    # smoke 4：per-slot identity allclose（hook 两次 forward 的 slot outputs）。
    # 语义澄清：本 smoke 验证 father-loaded 模块在 zero-intervention 下逐 slot 输出可复现
    # ——这是 §16.4 identity 契约的前置必要条件（father 自身可复现）。完整的 §16.4 AC
    # （identity-passthrough student vs father 的跨模型 allclose）在下游 build_selected 节点，
    # 需 selected_arch；本节点无 selected_arch，只验证前置条件。
    if not slot_paths:
        return  # 无 slot 时跳过（但调用方应在有 slot 时才到这）
    cap1 = _hook_slot_outputs(model, slot_paths, dummy_input, device)
    cap2 = _hook_slot_outputs(model, slot_paths, dummy_input, device)
    for path in slot_paths:
        a, b = cap1[path], cap2[path]
        if not isinstance(a, torch.Tensor) or not isinstance(b, torch.Tensor):
            raise RuntimeError(
                f"per-slot identity allclose smoke 失败：slot {path!r} output 非 tensor"
                f"（{type(a).__name__}）——puzzle 契约 slot 须输出 tensor"
            )
        if not torch.allclose(a, b, atol=_ALLCLOSE_ATOL):
            max_diff = (a - b).abs().max().item()
            raise RuntimeError(
                f"per-slot identity allclose smoke 失败：slot {path!r} 两次 forward"
                f" max|Δ|={max_diff:.2e} > atol={_ALLCLOSE_ATOL:.0e}（father-loaded 模块"
                f" 在 zero intervention 下逐 slot 输出不可复现——这是 identity 契约的"
                f" 前置条件；检查 flat 是否漏 register_buffer / 含未固定 RNG）"
            )


# ── smoke 3: eval-stability（E25）─────────────────────────────────────────────

def eval_stability(
    model: nn.Module, eval_fn_name: str, flat_model_path: Path
) -> float:
    """eval_fn 跑两次 → acc 一致（捕获 train-mode 泄漏 / 未 seed workers）。返回 acc。"""
    fn = resolve_eval_fn(eval_fn_name, flat_model_path)
    acc1 = fn(model)
    acc2 = fn(model)
    for acc in (acc1, acc2):
        # bool 是 int 子类——显式排除（eval_fn 误返 True/False 会静默通过类型检查）
        if isinstance(acc, bool) or not isinstance(acc, (int, float)):
            raise TypeError(
                f"eval_fn {eval_fn_name!r} 返回非数值：{type(acc).__name__}"
            )
    if abs(float(acc1) - float(acc2)) > _EVAL_STABILITY_ATOL:
        raise RuntimeError(
            f"eval-stability smoke 失败：两次 eval acc 不一致（{acc1} vs {acc2}，"
            f"Δ > {_EVAL_STABILITY_ATOL:.0e}）——eval_fn 内 train-mode 泄漏 / 未 seed workers / RNG"
        )
    return float(acc1)


# ── trace slot I/O shape（回填 search_space）───────────────────────────────────

def trace_slot_shapes(
    model: nn.Module, slot_paths: list[str], dummy_input: torch.Tensor, device: torch.device
) -> dict[str, tuple[int, int]]:
    """hook 每个 slot 抓 in/out 末维 → ``{path: (in_dim, out_dim)}``。"""
    model.eval().to(device)
    captured: dict[str, tuple[int, int]] = {}
    handles: list[Any] = []

    def make_hook(path: str):
        def hook(_mod: nn.Module, inputs: tuple, output: Any):
            if path in captured:
                return
            in_t = inputs[0] if isinstance(inputs, tuple) and inputs else inputs
            if isinstance(in_t, (list, tuple)):
                in_t = in_t[0]
            out_t = output[0] if isinstance(output, (tuple, list)) else output
            # fail loud：in_dim/out_dim 是 slot 必填（下游 factory 依赖）——非 tensor 或零维
            # 不能静默写 -1 污染 search_space。
            if not isinstance(in_t, torch.Tensor) or in_t.dim() < 1:
                raise RuntimeError(
                    f"trace slot {path!r} 的输入非 ≥1D tensor（{type(in_t).__name__}）——"
                    f"puzzle 契约 slot 输入须 tensor，无法 trace in_dim"
                )
            if not isinstance(out_t, torch.Tensor) or out_t.dim() < 1:
                raise RuntimeError(
                    f"trace slot {path!r} 的输出非 ≥1D tensor（{type(out_t).__name__}）——"
                    f"puzzle 契约 slot 须输出 tensor，无法 trace out_dim"
                )
            captured[path] = (int(in_t.shape[-1]), int(out_t.shape[-1]))

        return hook

    try:
        for path in slot_paths:
            try:
                mod = model.get_submodule(path)
            except AttributeError as e:
                raise AttributeError(
                    f"trace slot {path!r} 定位失败：{e}"
                ) from e
            handles.append(mod.register_forward_hook(make_hook(path)))
        with torch.no_grad():
            model(dummy_input.to(device))
    finally:
        for h in handles:
            h.remove()
    missing = [p for p in slot_paths if p not in captured]
    if missing:
        raise RuntimeError(
            f"trace 未捕获 slot shape：{missing[:3]}（共 {len(missing)} 个）"
        )
    return captured


# ── latency ────────────────────────────────────────────────────────────────────

def measure_latency(
    model: nn.Module,
    dummy_input: torch.Tensor,
    device: torch.device,
    latency_unit: str,
    latency_script_path: str,
) -> float:
    """默认 ``measure_module_latency``（PyTorch ms）；``latency_script_path`` 提供则包装。"""
    if latency_script_path:
        from puzzle_common import load_external_callable
        fn = load_external_callable(latency_script_path)
        return float(fn(model, dummy_input))
    from nas_agent.latency import measure_module_latency
    return float(measure_module_latency(model, dummy_input, device, repetitions=100, warmup=30))


# ── main ──────────────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Puzzle U2a measure_baseline：4 道 smoke + baseline 测量 + shape trace"
    )
    p.add_argument("--flat_path", required=True, help="LLM 产的 flat model .py 绝对路径")
    p.add_argument("--build_fn", required=True, help="flat 内 build 函数名")
    p.add_argument("--build_cfg", default="", help="build_fn 的 JSON kwargs（空则零参）")
    p.add_argument("--father_ckpt", required=True, help="预训练父权重 .pt（state_dict）绝对路径")
    p.add_argument("--eval_fn", required=True, help="评估函数名（或 path::func）")
    p.add_argument(
        "--eval_kind", required=True,
        choices=["classification", "embedding", "regression"],
        help="评估范式（sanity check 用）",
    )
    p.add_argument("--search_space_path", required=True, help="LLM 产的 search_space.yaml 绝对路径")
    p.add_argument("--latency_unit", default="ms", choices=["ms", "us", "s"])
    p.add_argument("--latency_script_path", default="", help="外部 latency 脚本 path::func")
    p.add_argument("--output_dir", required=True, help="产物输出目录绝对路径")
    p.add_argument("--seed", type=int, default=0, help="复现性种子")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    torch.manual_seed(args.seed)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    flat_path = Path(args.flat_path).resolve()
    father_ckpt = Path(args.father_ckpt).resolve()

    try:
        # 1) 读 search_space.yaml（LLM 产物）
        slot_dicts, candidates = load_search_space_yaml(args.search_space_path)

        # E22：empty slots → terminate_unsupported（确定性 post-check）
        if not slot_dicts:
            result = {
                "output_dir": str(output_dir),
                "model_type": "No supported match",
                "model_type_supported": False,
                "flat_model_path": str(flat_path),
                "block_map_path": "",
                "search_space_path": str(Path(args.search_space_path).resolve()),
                "baseline_metrics_path": "",
                "baseline_acc": 0.0,
                "baseline_latency": 0.0,
                "latency_unit": args.latency_unit,
                "fidelity_passed": False,
                "smokes_passed": [],
                "error": "empty search_space.slots (terminate_unsupported, E22)",
                "generated_artifacts": [str(flat_path)],
            }
            print(f"BASELINE_ACC: 0")
            print(f"BASELINE_LATENCY: 0")
            print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
            return 2

        # 2) 加载 flat model
        model = load_flat_model(flat_path, args.build_fn, args.build_cfg)
        if not isinstance(model, nn.Module):
            raise TypeError(
                f"{args.build_fn} 返回非 nn.Module：{type(model).__name__}"
            )

        # 3) smoke 1: strict-load father（零 missing/unexpected）
        strict_load_father(model, father_ckpt)
        # 保存统一 father_state_dict 供下游 bld/score/build/gkd 复用同一份预训练权重。
        # 用 model.state_dict() 而非入参 ckpt——strict-load 已保证零 missing/unexpected，
        # 故 model.state_dict() 与 father ckpt 全覆盖且 schema = flat 模型 schema（下游复用所需）。
        father_state_path = output_dir / "father_state_dict.pt"
        torch.save(model.state_dict(), father_state_path)
        smokes_passed = ["strict-load"]

        # dummy input（DUMMY_INPUT 声明真实 I/O 维度）
        dummy_meta = get_module_dummy_input(flat_path)
        shape = list(dummy_meta["shape"])
        dtype = getattr(torch, str(dummy_meta.get("dtype", "float32")))
        dummy_input = torch.randn(*shape, dtype=dtype)
        device = torch.device("cpu")
        model.eval().to(device)

        # 4) smoke 2 + smoke 4（两次 forward 复用）
        slot_paths = [str(d["path"]) for d in slot_dicts]
        forward_determinism_and_identity_allclose(model, slot_paths, dummy_input, device)
        smokes_passed.append("forward-determinism")
        smokes_passed.append("per-slot-identity-allclose")

        # 5) smoke 3: eval-stability + 取 acc
        baseline_acc = eval_stability(model, args.eval_fn, flat_path)
        smokes_passed.append("eval-stability")

        # 6) trace slot I/O shape → 回填 slot_dicts
        shapes = trace_slot_shapes(model, slot_paths, dummy_input, device)
        for d in slot_dicts:
            in_dim, out_dim = shapes[str(d["path"])]
            d["in_dim"] = in_dim
            d["out_dim"] = out_dim

        # 7) latency
        baseline_latency = measure_latency(
            model, dummy_input, device, args.latency_unit, args.latency_script_path
        )

        # 8) 写产物：block_map.json + baseline_metrics.json + 更新 search_space.yaml
        block_map = to_block_map(slot_dicts)
        block_map_path = output_dir / "block_map.json"
        block_map.to_json(block_map_path)

        search_space_out = output_dir / "search_space.yaml"
        save_search_space_yaml(search_space_out, slot_dicts, candidates)

        baseline_metrics = {
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_unit": args.latency_unit,
            "eval_kind": args.eval_kind,
            "eval_fn": args.eval_fn,
            "seed": args.seed,
            "smokes_passed": smokes_passed,
        }
        baseline_metrics_path = output_dir / "baseline_metrics.json"
        with open(baseline_metrics_path, "w", encoding="utf-8") as f:
            json.dump(baseline_metrics, f, ensure_ascii=False, indent=2)

        # 9) stdout
        generated = [
            str(flat_path),
            str(father_state_path),
            str(block_map_path),
            str(search_space_out),
            str(baseline_metrics_path),
        ]
        result = {
            "output_dir": str(output_dir),
            "model_type": _infer_model_type(block_map),
            "model_type_supported": True,
            "flat_model_path": str(flat_path),
            "block_map_path": str(block_map_path),
            "search_space_path": str(search_space_out),
            "baseline_metrics_path": str(baseline_metrics_path),
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_unit": args.latency_unit,
            "fidelity_passed": True,
            "smokes_passed": smokes_passed,
            "error": "",
            "generated_artifacts": generated,
        }
        print(f"BLOCK_MAP: {block_map_path}")
        print(f"SEARCH_SPACE: {search_space_out}")
        print(f"BASELINE_ACC: {baseline_acc}")
        print(f"BASELINE_LATENCY: {baseline_latency}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: measure_baseline 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _infer_model_type(block_map) -> str:
    """粗标签（agent 可改写）。"""
    n_att = sum(1 for s in block_map.slots if s.kind == "attention")
    n_ffn = sum(1 for s in block_map.slots if s.kind == "ffn")
    if n_att >= 2 and n_ffn >= 2:
        return "isotropic_transformer"
    if n_att >= 1:
        return "hierarchical_transformer"
    return "unknown_transformer"


if __name__ == "__main__":
    sys.exit(main())
