"""build_selected.py —— Puzzle P2.7：实例化异构架构。

读 selected_arch + block_library + flat_model，逐层把各 kind slot 换成选定
variant（载块库权重）；identity（passthrough）保留父块（SPEC v2 §3）。

输出 ``selected_model.pt``（完整 state_dict，可独立 eval）。
stdout：``SELECTED_MODEL: <path>`` / ``RESULT_JSON: {...}``。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

import torch

from puzzle_common import (
    BlockMap,
    build_student_from_arch,
    get_module_dummy_input,
    is_candidate_valid_for_slot,
    is_passthrough,
    load_father_model,
)


# §16.4 全 identity 架构 allclose 容差（验证 identity 零侵入完整性）
_ALL_IDENTITY_ALLCLOSE_ATOL = 1e-5


def _is_all_identity_arch(arch: dict) -> bool:
    """selected_arch 的每个 slot 都选了 identity（passthrough）。

    语义：全 identity 架构 = 不替换任何 slot → student 必须等价于 father。
    任一 slot 选了非 identity → 有意替换了块，输出会变，allclose AC 不适用。
    """
    if not arch:
        return False
    for _, slot_dict in arch.items():
        for _, variant in slot_dict.items():
            if not is_passthrough(str(variant)):
                return False
    return True


def _verify_all_identity_allclose(
    student: torch.nn.Module,
    flat_model_path: str,
    build_fn: str,
    build_cfg: str,
    father_state_path: str,
) -> None:
    """§16.4 完整 AC：全 identity selected_arch → student forward 必须与 father-loaded
    全模型 forward ``torch.allclose``。

    语义：identity 候选承诺零侵入（SPEC §3 铁律）——若所有 slot 都选 identity，
    student 实际就是 father 本身（架构 + 权重经 build_student_from_arch 的 father
    注入路径）。本 check 是该承诺的跨模型真实验证（非 per-slot 近似）。

    非 father_state 路径 / 非 all-identity → 跳过（不适用，不强制 allclose）。
    """
    if not father_state_path:
        return  # father_state 缺 → student 是随机 init（路径 B 由 selected_state_dict 覆盖）；本 check 不适用
    father = load_father_model(flat_model_path, build_fn, build_cfg, father_state_path)
    father.eval()
    student.eval()
    dummy_meta = get_module_dummy_input(flat_model_path)
    shape = list(dummy_meta["shape"])
    dtype = getattr(torch, str(dummy_meta.get("dtype", "float32")))
    dummy_input = torch.randn(*shape, dtype=dtype)
    with torch.no_grad():
        father_out = father(dummy_input)
        student_out = student(dummy_input)
    # tuple/list 取首 tensor（与下游 gkd/gate 一致）
    if isinstance(father_out, (tuple, list)):
        father_out = father_out[0]
    if isinstance(student_out, (tuple, list)):
        student_out = student_out[0]
    if not isinstance(father_out, torch.Tensor) or not isinstance(student_out, torch.Tensor):
        raise RuntimeError(
            "allidentity allclose 失败：father/student forward 非 tensor"
            "（dict/list 输出需 flat.py 加 output-flattening adapter）"
        )
    if not torch.allclose(student_out, father_out, atol=_ALL_IDENTITY_ALLCLOSE_ATOL):
        max_diff = (student_out - father_out).abs().max().item()
        raise RuntimeError(
            f"allidentity allclose 失败：全 identity 架构 student 应等价 father，"
            f"但 forward max|Δ|={max_diff:.2e} > atol={_ALL_IDENTITY_ALLCLOSE_ATOL:.0e}"
            f"（identity 零侵入承诺被破坏——检查 build_student_from_arch 的 father 权重注入路径、"
            f"或 flat model schema 与 father_state_dict 是否对齐）"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puzzle P2.7 实例化选定异构架构")
    parser.add_argument("--selected_arch", required=True, help="selected_arch.json 路径")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--flat_model", required=True)
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_library", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument(
        "--father_state",
        default="",
        help="预训练父模型权重 .pt 路径（expand 保存的 father_state_dict.pt）。"
        "identity（passthrough）slot 保留 father 权重的来源——必须预训练;"
        "空串回退随机 init（仅 dry-run 兼容）",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = Path(args.block_library).resolve()

    try:
        with open(args.selected_arch, encoding="utf-8") as f:
            selection = json.load(f)
        arch = selection.get("selected_arch", selection)
        if not isinstance(arch, dict) or not arch:
            raise ValueError(f"selected_arch 无效或空：{selection!r}")

        block_map = BlockMap.from_json(args.block_map)
        device = torch.device("cpu")

        # 用共享 helper 重建异构 student（passthrough identity 保留父块）。
        # father_state 非空 → identity slot 保留预训练父权重（Puzzle 契约）。
        model = build_student_from_arch(
            flat_model_path=args.flat_model,
            build_fn=args.build_fn,
            build_cfg=args.build_cfg,
            block_map=block_map,
            selected_arch=selection,
            block_library_dir=block_library_dir,
            device=device,
            father_state_path=args.father_state,
        )
        model.eval().to(device)

        # 校验：selected_arch 中所有 chosen 都被处理（build_student_from_arch 会
        # 在 variant 不适用 / ckpt 缺时 raise，这里仅做 layer/kind 覆盖性检查）
        chosen_keys = {
            (int(L), k) for L, d in arch.items() for k in d
        }
        bm_keys = {(s.layer_idx, s.kind) for s in block_map.slots}
        unknown = chosen_keys - bm_keys
        if unknown:
            raise RuntimeError(
                f"selected_arch 中有未匹配 block_map slot 的项：{sorted(unknown)[:3]}"
            )

        # E6/E8：防御性 is_valid 校验——MIP 应已据 score.py 的 valid 字段过滤，但
        # build_selected 是 identity 完整性关卡，再核一遍 selected_arch 的每个非
        # identity variant 对 slot 是否结构有效。无效选 → raise（fail loud，不让
        # 结构破坏的架构进 gkd）。
        for L, d in arch.items():
            for kind, variant in d.items():
                vname = str(variant)
                # 找对应的 slot（layer_idx + kind 唯一定位）
                matched = [
                    s for s in block_map.slots
                    if s.layer_idx == int(L) and s.kind == kind
                ]
                if not matched:
                    continue  # 上方 unknown check 已拦
                slot = matched[0]
                if is_passthrough(vname):
                    continue  # identity 永远 valid
                if not is_candidate_valid_for_slot(vname, slot):
                    raise RuntimeError(
                        f"selected_arch L{L}_{kind}={vname} 对 slot 结构无效"
                        f"（ffn_struct={slot.ffn_struct!r}, mask_load_bearing="
                        f"{slot.mask_load_bearing}）——score.py 应已标 valid=False，"
                        f"MIP 不该选它；检查 mip_select 的 valid 过滤路径"
                    )

        # §16.4 完整 AC：全 identity 架构 → student forward 必须与 father allclose
        if _is_all_identity_arch(arch):
            _verify_all_identity_allclose(
                student=model,
                flat_model_path=args.flat_model,
                build_fn=args.build_fn,
                build_cfg=args.build_cfg,
                father_state_path=args.father_state,
            )

        replaced: list[str] = []
        for L, d in arch.items():
            for kind, v in d.items():
                replaced.append(f"L{L}_{kind}={v}")

        selected_model_path = output_dir / "selected_model.pt"
        torch.save(
            {
                "state_dict": {k: v.cpu() for k, v in model.state_dict().items()},
                "selected_arch": arch,
                "replaced": replaced,
            },
            selected_model_path,
        )

        result = {
            "status": "executed",
            "artifacts": [str(selected_model_path)],
            "assessment": f"实例化异构架构：{len(replaced)} 个 slot 赋值",
            "max_retries_hit": False,
            "healed_files": [],
            "fidelity_retriggered": False,
        }
        print(f"SELECTED_MODEL: {selected_model_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as e:
        tb = traceback.format_exc()
        print(
            f"ERROR: build_selected 失败 — {type(e).__name__}: {e}\n{tb}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
