"""build_selected.py —— Puzzle 实例化异构架构（U6 适配器）。

读 selected_arch + block_library + flat/adapters，逐层把各 kind slot 换成选定
variant（载块库权重）；identity（passthrough）保留父块（SPEC v2 §3）。

U6 改造：
  - root cause A/K：allidentity allclose 比较走 ``adapters.forward_model``（不再
    ``model(dummy_input)`` / 取首 tensor）；father 也经 ``adapters.build_model()`` +
    ``adapters.load_pretrained()``。
  - 父权重注入路径走 ``build_student_from_arch(adapters=...)``。

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
    build_pretrained_model,
    build_student_from_arch,
    is_candidate_valid_for_slot,
    is_passthrough,
    load_puzzle_adapters,
)

# §16.4 全 identity 架构 allclose 容差
_ALL_IDENTITY_ALLCLOSE_ATOL = 1e-5


def _is_all_identity_arch(arch: dict) -> bool:
    """selected_arch 的每个 slot 都选了 identity（passthrough）。"""
    if not arch:
        return False
    for _, slot_dict in arch.items():
        for _, variant in slot_dict.items():
            if not is_passthrough(str(variant)):
                return False
    return True


def _verify_all_identity_allclose(
    student: torch.nn.Module,
    adapters,
    father_state_loaded: bool,
) -> None:
    """§16.4 完整 AC：全 identity selected_arch → student forward 必须与 father allclose。

    U6：father 也经 ``adapters.build_model()`` + ``adapters.load_pretrained()``，
    forward 走 ``adapters.forward_model``。``father_state_loaded=False`` 时（适配器
    ``load_pretrained.from_scratch=True``）跳过——非预训练路径不适用 allidentity AC。
    """
    if not father_state_loaded:
        return
    father = build_pretrained_model(adapters)
    father.eval()
    student.eval()
    device = torch.device("cpu")
    father.to(device); student.to(device)
    try:
        batch = next(iter(adapters.calib_iter(device=device)))
    except StopIteration as e:
        raise RuntimeError(
            "adapters.calib_iter() 返回空——allidentity allclose 无 batch"
        ) from e
    with torch.no_grad():
        father_out = adapters.forward_model(father, batch)
        student_out = adapters.forward_model(student, batch)
    # 取主 tensor（输出形态可能 tuple/list/tensor；adapters.kd_loss 同样假设）
    if isinstance(father_out, (tuple, list)):
        father_out = father_out[0]
    if isinstance(student_out, (tuple, list)):
        student_out = student_out[0]
    if not isinstance(father_out, torch.Tensor) or not isinstance(student_out, torch.Tensor):
        raise RuntimeError(
            "allidentity allclose 失败：father/student forward 非 tensor"
            "（adapters.forward_model 应暴露主 tensor）"
        )
    if not torch.allclose(student_out, father_out, atol=_ALL_IDENTITY_ALLCLOSE_ATOL):
        max_diff = (student_out - father_out).abs().max().item()
        raise RuntimeError(
            f"allidentity allclose 失败：全 identity 架构 student 应等价 father，"
            f"但 forward max|Δ|={max_diff:.2e} > atol={_ALL_IDENTITY_ALLCLOSE_ATOL:.0e}"
            f"（identity 零侵入承诺被破坏——检查 adapters.load_pretrained / build_model）"
        )


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Puzzle U6 实例化选定异构架构")
    parser.add_argument("--selected_arch", required=True, help="selected_arch.json 路径")
    parser.add_argument("--block_map", required=True)
    parser.add_argument("--flat_model", required=True, help="flat model .py（架构源）")
    parser.add_argument("--build_fn", required=True)
    parser.add_argument("--build_cfg", default="")
    parser.add_argument("--block_library", required=True)
    parser.add_argument(
        "--adapters", required=True,
        help="puzzle_adapters.py 路径（U6 §2.1：脚本唯一项目接口）",
    )
    parser.add_argument(
        "--manifest", default="",
        help="manifest.yaml 路径（metadata 用；脚本不解析）",
    )
    parser.add_argument("--output_dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    block_library_dir = Path(args.block_library).resolve()

    try:
        adapters = load_puzzle_adapters(args.adapters)
        with open(args.selected_arch, encoding="utf-8") as f:
            selection = json.load(f)
        arch = selection.get("selected_arch", selection)
        if not isinstance(arch, dict) or not arch:
            raise ValueError(f"selected_arch 无效或空：{selection!r}")

        block_map = BlockMap.from_json(args.block_map)
        device = torch.device("cpu")

        # student = build_student_from_arch（U6：经 adapters 注入 father 权重）
        model = build_student_from_arch(
            adapters=adapters,
            block_map=block_map,
            selected_arch=selection,
            block_library_dir=block_library_dir,
            device=device,
            flat_model_path=args.flat_model,
            build_fn=args.build_fn,
            build_cfg=args.build_cfg,
        )
        model.eval().to(device)

        # 校验：selected_arch 中所有 chosen 都被处理
        chosen_keys = {
            (int(L), k) for L, d in arch.items() for k in d
        }
        bm_keys = {(s.layer_idx, s.kind) for s in block_map.slots}
        unknown = chosen_keys - bm_keys
        if unknown:
            raise RuntimeError(
                f"selected_arch 中有未匹配 block_map slot 的项：{sorted(unknown)[:3]}"
            )

        # E6/E8：防御性 is_valid 校验
        for L, d in arch.items():
            for kind, variant in d.items():
                vname = str(variant)
                matched = [
                    s for s in block_map.slots
                    if s.layer_idx == int(L) and s.kind == kind
                ]
                if not matched:
                    continue
                slot = matched[0]
                if is_passthrough(vname):
                    continue
                if not is_candidate_valid_for_slot(vname, slot):
                    raise RuntimeError(
                        f"selected_arch L{L}_{kind}={vname} 对 slot 结构无效"
                        f"（ffn_struct={slot.ffn_struct!r}, mask_load_bearing="
                        f"{slot.mask_load_bearing}）——score.py 应已标 valid=False，"
                        f"MIP 不该选它；检查 mip_select 的 valid 过滤路径"
                    )

        # §16.4 完整 AC：全 identity 架构 → student forward 必须与 father allclose
        # （仅当 father 权重真实注入：adapters.load_pretrained.from_scratch=False）
        if _is_all_identity_arch(arch):
            # 重新做一次 load_pretrained 拿 _LoadResult 判 from_scratch
            probe_model = adapters.build_model()
            load_result = adapters.load_pretrained(probe_model)
            father_state_loaded = not bool(getattr(load_result, "from_scratch", True))
            _verify_all_identity_allclose(
                student=model,
                adapters=adapters,
                father_state_loaded=father_state_loaded,
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
