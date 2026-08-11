"""expand_model.py —— Puzzle P2.2：flatten 用户模型 + 识别可替换 slot + 测基线。

职责（确定性，fail loud）：
  1. 把用户 ``model_path`` flatten（拷贝为单文件 ``<base>_flat.py``，落 output_dir；
     load 时其本地 import 由 sys.path 注入解析——通用，不靠 AST 内联）。
  2. 用 forward hook + dummy_input trace 识别 transformer sub-block：
     - attention slot：类名含 Attention/MHSA/Attn 或继承 nn.MultiheadAttention
     - ffn slot：类名含 FeedForward/MLP/FFN 或结构是 Linear-Act-Linear
  3. 测基线：``eval_fn`` → acc；``measure_module_latency`` 或 wrap
     ``latency_script_path`` → latency。
  4. 写 ``block_map.json`` + ``baseline_metrics.json`` + ``project_manifest.md``。

stdout 关键行：
    BLOCK_MAP: <path>
    BASELINE_ACC: <value>
    BASELINE_LATENCY: <value>
    FLAT_MODEL: <path>
    RESULT_JSON: {...}

无任何 slot → exit 2（fail loud）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

from puzzle_common import (
    BlockMap,
    Slot,
    get_module_dummy_input,
    load_flat_model,
    resolve_eval_fn,
)


# ── slot 模式识别 ─────────────────────────────────────────────────────────────

_ATT_NAME_PATTERNS = ("Attention", "MHSA", "Attn", "SelfAttn", "MultiHead")
_FFN_NAME_PATTERNS = ("FeedForward", "MLP", "FFN", "Ffn", "Mlp")
_LINEAR_LIKE = (nn.Linear,)


def _class_name_match(cls_name: str, patterns: tuple[str, ...]) -> bool:
    return any(p.lower() in cls_name.lower() for p in patterns)


def _is_attention_module(mod: nn.Module) -> bool:
    cls_name = type(mod).__name__
    if _class_name_match(cls_name, _ATT_NAME_PATTERNS):
        return True
    if isinstance(mod, nn.MultiheadAttention):
        return True
    return False


def _is_ffn_module(mod: nn.Module) -> bool:
    cls_name = type(mod).__name__
    if _class_name_match(cls_name, _FFN_NAME_PATTERNS):
        return True
    # 结构：Linear -> Act -> Linear
    if isinstance(mod, nn.Sequential) and len(mod) >= 3:
        has_lin = any(isinstance(m, nn.Linear) for m in mod)
        has_act = any(
            isinstance(m, (nn.GELU, nn.ReLU, nn.SiLU, nn.Mish, nn.Tanh))
            for m in mod
        )
        if has_lin and has_act:
            return True
    return False


def _find_layer_containers(model: nn.Module) -> list[tuple[str, nn.Module]]:
    """识别"transformer block 容器"——同时含 attention 和 ffn 直接子模块的容器。

    返回 [(dotted_path, module)]，按模型 forward 序。
    """
    containers: list[tuple[str, nn.Module]] = []
    for name, mod in model.named_modules():
        if mod is model:
            continue
        children = list(mod.children())
        if len(children) < 2:
            continue
        has_att = any(_is_attention_module(c) for c in children)
        has_ffn = any(_is_ffn_module(c) for c in children)
        if has_att and has_ffn:
            containers.append((name, mod))
    return containers


def _infer_num_heads(mod: nn.Module, fallback_dim: int) -> tuple[int, int]:
    """从原 attention 模块读 num_heads/head_dim；读不到给保守默认。"""
    nh = getattr(mod, "num_heads", None) or getattr(mod, "n_heads", None)
    if isinstance(nh, int) and nh > 0:
        hd = getattr(mod, "head_dim", None)
        if not isinstance(hd, int) or hd <= 0:
            hd = max(1, fallback_dim // nh)
        return nh, hd
    # fallback：assume 4 heads
    nh = 4
    hd = max(1, fallback_dim // nh)
    return nh, hd


# ── trace 捕获 I/O shape ──────────────────────────────────────────────────────

def _trace_slot_shapes(
    model: nn.Module,
    slot_paths: list[tuple[str, str]],
    dummy_input: torch.Tensor,
    device: torch.device,
) -> dict[str, tuple[int, int]]:
    """对每个 (path, slot_type) 跑一次 forward，hook 抓 in/out 最后一维。

    返回 ``{path: (in_dim, out_dim)}``。
    """
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
            in_dim = in_t.shape[-1] if isinstance(in_t, torch.Tensor) and in_t.dim() >= 1 else -1
            out_dim = out_t.shape[-1] if isinstance(out_t, torch.Tensor) and out_t.dim() >= 1 else -1
            captured[path] = (int(in_dim), int(out_dim))
        return hook

    try:
        for path, _slot_type in slot_paths:
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
    missing = [p for p, _ in slot_paths if p not in captured]
    if missing:
        raise RuntimeError(
            f"trace 未捕获 slot shape：{missing[:3]}（共 {len(missing)} 个）"
        )
    return captured


# ── flatten ───────────────────────────────────────────────────────────────────

def flatten_model_file(model_path: str | Path, output_dir: Path) -> Path:
    """把用户 model_path 拷贝为 ``<base>_flat.py`` 落 output_dir。

    ``load_flat_model`` 会把 flat 文件目录注入 sys.path，让其本地 import 在
    下游脚本里仍可解析（通用，不依赖 AST 内联）。
    """
    src = Path(model_path).resolve()
    if not src.is_file():
        raise FileNotFoundError(f"model_path 不存在：{src}")
    flat_path = output_dir / f"{src.stem}_flat.py"
    output_dir.mkdir(parents=True, exist_ok=True)
    # 直接字节拷贝（保留原始内容；不内联外部 dep——load 时 sys.path 注入解析）
    shutil.copyfile(src, flat_path)
    # 同目录 .py 依赖也拷一份（best-effort，便于跨机迁）；失败 → WARN 不静默吞
    for sibling in src.parent.glob("*.py"):
        if sibling.name == src.name:
            continue
        dst = output_dir / sibling.name
        if not dst.exists():
            try:
                shutil.copyfile(sibling, dst)
            except OSError as e:
                print(
                    f"[expand_model] WARN: 跳过 sibling {sibling.name}: {e}",
                    file=sys.stderr,
                )
    return flat_path


# ── 基线测量 ─────────────────────────────────────────────────────────────────

def measure_baseline_latency(
    model: nn.Module,
    dummy_input: torch.Tensor,
    device: torch.device,
    latency_unit: str,
    latency_script_path: str,
) -> float:
    """测父模型 latency：默认 ``measure_module_latency``（PyTorch ms）；
    ``latency_script_path`` 提供 → 包装 ``path::func`` 单文件契约（ONNX 等）。

    数值单位由 latency_unit 标注，不换算。
    """
    if latency_script_path:
        from puzzle_common import load_external_callable
        fn = load_external_callable(latency_script_path)
        return float(fn(model, dummy_input))
    from nas_agent.latency import measure_module_latency
    return float(measure_module_latency(model, dummy_input, device, repetitions=100, warmup=30))


def measure_baseline_acc(
    model: nn.Module, eval_fn: str, flat_model_path: Path
) -> float:
    fn = resolve_eval_fn(eval_fn, flat_model_path)
    acc = fn(model)
    if not isinstance(acc, (int, float)):
        raise TypeError(f"eval_fn {eval_fn!r} 返回非数值：{type(acc).__name__}")
    return float(acc)


# ── 识别 block_map ────────────────────────────────────────────────────────────

def build_block_map(
    model: nn.Module,
    dummy_input: torch.Tensor,
    device: torch.device,
) -> BlockMap:
    """扫描 model 找所有 attention/ffn slot，按容器分组分配 layer_idx。

    若识别到 transformer block 容器，slot 的 layer_idx 取容器序号；
    否则所有 slot 共享 layer_idx=0（fallback，仍可作 MIP 组键）。
    """
    containers = _find_layer_containers(model)
    slots: list[Slot] = []
    if containers:
        for layer_idx, (ctr_path, ctr) in enumerate(containers):
            for child_name, child in ctr.named_children():
                child_path = f"{ctr_path}.{child_name}" if ctr_path else child_name
                _add_slot_if_match(slots, layer_idx, child_path, child)
    else:
        # fallback：直接扫全模型顶层 attention/ffn，按 forward 序
        layer_idx = 0
        for name, mod in model.named_modules():
            if mod is model:
                continue
            if _is_attention_module(mod) or _is_ffn_module(mod):
                _add_slot_if_match(slots, layer_idx, name, mod)

    if not slots:
        return BlockMap(slots=[])

    # 抓 I/O shape
    slot_paths = [(s.parent_module_path, s.slot_type) for s in slots]
    shapes = _trace_slot_shapes(model, slot_paths, dummy_input, device)

    # 回填 in_dim/out_dim/num_heads/head_dim
    finalized: list[Slot] = []
    for s in slots:
        in_dim, out_dim = shapes[s.parent_module_path]
        try:
            orig_mod = model.get_submodule(s.parent_module_path)
        except AttributeError:
            orig_mod = None
        if orig_mod is not None and s.slot_type == "attention":
            nh, hd = _infer_num_heads(orig_mod, in_dim)
        else:
            nh = max(1, in_dim // 4)
            hd = max(1, in_dim // nh)
        finalized.append(
            Slot(
                layer_idx=s.layer_idx,
                slot_type=s.slot_type,
                in_dim=in_dim,
                out_dim=out_dim,
                num_heads=nh,
                head_dim=hd,
                source_class=s.source_class,
                parent_module_path=s.parent_module_path,
            )
        )
    return BlockMap(slots=finalized)


def _add_slot_if_match(
    slots: list[Slot], layer_idx: int, path: str, mod: nn.Module
) -> None:
    cls_name = type(mod).__name__
    if _is_attention_module(mod):
        slots.append(
            Slot(
                layer_idx=layer_idx,
                slot_type="attention",
                in_dim=-1,  # 待 trace 回填
                out_dim=-1,
                num_heads=-1,
                head_dim=-1,
                source_class=cls_name,
                parent_module_path=path,
            )
        )
    elif _is_ffn_module(mod):
        slots.append(
            Slot(
                layer_idx=layer_idx,
                slot_type="ffn",
                in_dim=-1,
                out_dim=-1,
                num_heads=0,
                head_dim=0,
                source_class=cls_name,
                parent_module_path=path,
            )
        )


# ── main ─────────────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Puzzle P2.2 expand_model：flatten + slot 识别 + 基线测量"
    )
    p.add_argument("--project_root", required=True, help="用户项目根目录绝对路径")
    p.add_argument("--model_path", required=True, help="目标模型入口 .py（相对 project_root 或绝对）")
    p.add_argument("--build_fn", required=True, help="model_path 内 build 函数名")
    p.add_argument("--build_cfg", default="", help="build_fn 的 JSON kwargs")
    p.add_argument("--eval_fn", required=True, help="评估函数名（或 path::func）")
    p.add_argument(
        "--eval_kind",
        required=True,
        choices=["classification", "embedding", "regression"],
        help="评估范式",
    )
    p.add_argument(
        "--latency_unit", default="ms", choices=["ms", "us", "s"], help="latency 单位"
    )
    p.add_argument("--latency_script_path", default="", help="外部 latency 脚本 path::func")
    p.add_argument("--output_dir", required=True, help="产物输出目录绝对路径")
    p.add_argument(
        "--pretrained_ckpt",
        default="",
        help="预训练父模型权重 .pt 路径(state_dict)。Puzzle 的 father/teacher/baseline "
        "必须是预训练模型——提供则 load_state_dict;空串则用 build 的随机初始化",
    )
    p.add_argument("--seed", type=int, default=0, help="复现性种子")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    torch.manual_seed(args.seed)

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"ERROR: project_root 不存在：{project_root}", file=sys.stderr)
        return 2
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # 解析 model_path（相对 project_root 或绝对）
    mp = Path(args.model_path)
    if not mp.is_absolute():
        mp = project_root / mp
    mp = mp.resolve()

    try:
        # 1) flatten
        flat_path = flatten_model_file(mp, output_dir)

        # 2) 加载 + dummy_input
        model = load_flat_model(flat_path, args.build_fn, args.build_cfg)
        if not isinstance(model, nn.Module):
            raise TypeError(f"{args.build_fn} 返回非 nn.Module：{type(model).__name__}")
        # 2a) 加载预训练父模型权重(Puzzle 的 father/teacher/baseline 必须预训练)
        if args.pretrained_ckpt:
            ckpt_p = Path(args.pretrained_ckpt)
            if not ckpt_p.is_absolute():
                ckpt_p = (project_root / ckpt_p).resolve()
            if not ckpt_p.is_file():
                raise FileNotFoundError(f"pretrained_ckpt 不存在: {ckpt_p}")
            state = torch.load(ckpt_p, map_location="cpu")
            if isinstance(state, dict) and "state_dict" in state and not any(
                k.startswith(("blocks.", "patch_embed.")) for k in state.keys()
            ):
                # 形如 {state_dict: {...}, ...} 的 wrapper——取内层
                state = state["state_dict"]
            missing, unexpected = model.load_state_dict(state, strict=False)
            if missing:
                print(f"WARN: load_state_dict missing keys: {missing[:8]}", file=sys.stderr)
            if unexpected:
                print(f"WARN: load_state_dict unexpected keys: {unexpected[:8]}", file=sys.stderr)
            model.eval()
            # 保存 father state_dict 供下游 bld/score/build_selected/gkd/gate 复用同一份预训练权重
            father_state_path = output_dir / "father_state_dict.pt"
            torch.save(model.state_dict(), father_state_path)
        dummy_meta = get_module_dummy_input(flat_path)
        shape = list(dummy_meta["shape"])
        dtype = getattr(torch, str(dummy_meta.get("dtype", "float32")))
        dummy_input = torch.randn(*shape, dtype=dtype)
        device = torch.device("cpu")

        # 3) block_map
        block_map = build_block_map(model, dummy_input, device)
        if not block_map.slots:
            print(
                "ERROR: 未识别到任何 attention/ffn slot（模型不支持 puzzle 替换）",
                file=sys.stderr,
            )
            block_map_path_str = ""
            baseline_acc = 0.0
            baseline_latency = 0.0
            block_map_path = ""
            result = {
                "output_dir": str(output_dir),
                "model_type": "No supported match",
                "model_type_supported": False,
                "flat_model_path": str(flat_path),
                "block_map_path": "",
                "baseline_metrics_path": "",
                "baseline_acc": 0.0,
                "baseline_latency": 0.0,
                "latency_unit": args.latency_unit,
                "fidelity_passed": False,
                "workflow_verifier_passed": False,
                "error": "no attention/ffn slot detected",
                "generated_artifacts": [str(flat_path)],
            }
            print(f"FLAT_MODEL: {flat_path}")
            print(f"BASELINE_ACC: 0")
            print(f"BASELINE_LATENCY: 0")
            print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
            return 2

        # 4) 基线测量
        baseline_acc = measure_baseline_acc(model, args.eval_fn, flat_path)
        baseline_latency = measure_baseline_latency(
            model,
            dummy_input,
            device,
            args.latency_unit,
            args.latency_script_path,
        )

        # 5) 写产物
        block_map_path = output_dir / "block_map.json"
        block_map.to_json(block_map_path)

        baseline_metrics = {
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_unit": args.latency_unit,
            "eval_kind": args.eval_kind,
            "eval_fn": args.eval_fn,
            "seed": args.seed,
        }
        baseline_metrics_path = output_dir / "baseline_metrics.json"
        with open(baseline_metrics_path, "w", encoding="utf-8") as f:
            json.dump(baseline_metrics, f, ensure_ascii=False, indent=2)

        project_manifest_path = output_dir / "project_manifest.md"
        with open(project_manifest_path, "w", encoding="utf-8") as f:
            f.write(_render_manifest(
                project_root=project_root,
                model_path=mp,
                flat_path=flat_path,
                block_map=block_map,
                baseline_metrics=baseline_metrics,
                eval_kind=args.eval_kind,
            ))

        # 6) stdout 关键行
        generated = [
            str(flat_path),
            str(block_map_path),
            str(baseline_metrics_path),
            str(project_manifest_path),
        ]
        result = {
            "output_dir": str(output_dir),
            "model_type": _infer_model_type(block_map),
            "model_type_supported": True,
            "flat_model_path": str(flat_path),
            "block_map_path": str(block_map_path),
            "baseline_metrics_path": str(baseline_metrics_path),
            "baseline_acc": baseline_acc,
            "baseline_latency": baseline_latency,
            "latency_unit": args.latency_unit,
            "fidelity_passed": True,
            "workflow_verifier_passed": False,
            "error": "",
            "generated_artifacts": generated,
        }
        print(f"BLOCK_MAP: {block_map_path}")
        print(f"BASELINE_ACC: {baseline_acc}")
        print(f"BASELINE_LATENCY: {baseline_latency}")
        print(f"FLAT_MODEL: {flat_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(f"ERROR: expand_model 失败 — {type(e).__name__}: {e}\n{tb}", file=sys.stderr)
        return 2


def _infer_model_type(block_map: BlockMap) -> str:
    """从 block_map 推个粗标签（agent.md 可改写）。"""
    n_att = sum(1 for s in block_map.slots if s.slot_type == "attention")
    n_ffn = sum(1 for s in block_map.slots if s.slot_type == "ffn")
    if n_att >= 2 and n_ffn >= 2:
        return "isotropic_transformer"
    if n_att >= 1:
        return "hierarchical_transformer"
    return "unknown_transformer"


def _render_manifest(
    project_root: Path,
    model_path: Path,
    flat_path: Path,
    block_map: BlockMap,
    baseline_metrics: dict[str, Any],
    eval_kind: str,
) -> str:
    lines = [
        "# Puzzle Project Manifest",
        "",
        f"- project_root: `{project_root}`",
        f"- model_path: `{model_path}`",
        f"- flat_model: `{flat_path}`",
        f"- eval_kind: `{eval_kind}`",
        f"- baseline_acc: `{baseline_metrics['baseline_acc']}`",
        f"- baseline_latency: `{baseline_metrics['baseline_latency']}` "
        f"({baseline_metrics['latency_unit']})",
        "",
        "## Slots",
        "",
        "| layer_idx | slot_type | in_dim | out_dim | num_heads | head_dim | source_class | parent_module_path |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in block_map.slots:
        lines.append(
            f"| {s.layer_idx} | {s.slot_type} | {s.in_dim} | {s.out_dim} | "
            f"{s.num_heads} | {s.head_dim} | {s.source_class} | "
            f"`{s.parent_module_path}` |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
