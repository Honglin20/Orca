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
