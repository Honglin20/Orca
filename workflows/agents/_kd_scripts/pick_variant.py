"""pick_variant.py —— KD-NAS 确定性变体选择器。

按文件名排序遍历 ``$ORCA_KB_DIR/families/receiver/*.py``（**非递归**，排除 ``_*.py`` 共享模块），
取第一个**未 done** 的变体，写 SelectionSpec。全 done → ``ALL_DONE``。无变体 →
``NO_VARIANTS``（exit 3）。

done 谓词见 ``kd_common.is_variant_done``（跨 run 复用：sha256 / provider_id / ckpt / target 校验）。

CLI::
    python3 pick_variant.py --ledger <ledger.jsonl> --target_latency_us <f> \
        --latency_provider <path::func> [--receiver_dir <dir>] [--force_rerun] [--out <spec.json>]

stdout::
    VARIANT_SPEC: <abs path>          # 找到下一未 done 变体
    VARIANT_ID: <id>
    ALL_DONE: true                    # 全部变体已 done
    NO_VARIANTS: true                 # receiver 目录无变体 .py（exit 3）

fail loud：
    - 变体无 callable build_model / 无 DUMMY_INPUT.shape → 非零退出（禁硬编码 shape 回退）。
    - KNOBS 非法（step>=0 / leverage∉{high,medium,low}）→ 非零。
    - ledger 坏行 → raise（经 kd_common.read_ledger）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import traceback
from typing import Any

# 同目录共享 helper（脚本被 `python3 <abs>` 调用时，本文件目录在 sys.path[0]）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from kd_common import (  # noqa: E402
    RANK,
    is_variant_done,
    provider_id,
    read_ledger,
    sha256_file,
)


def _list_variants(receiver_dir: str) -> list[str]:
    """非递归 glob *.py，排除 _*.py 共享模块；按文件名排序。"""
    if not os.path.isdir(receiver_dir):
        raise FileNotFoundError(f"receiver 目录不存在: {receiver_dir}")
    names = sorted(
        n for n in os.listdir(receiver_dir)
        if n.endswith(".py") and not n.startswith("_")
    )
    return [os.path.join(receiver_dir, n) for n in names]


def _load_variant(path: str) -> Any:
    """import 变体 .py（其 ``from _model8_blocks import`` 需要 receiver_dir 在 sys.path）。"""
    receiver_dir = os.path.dirname(path)
    if receiver_dir not in sys.path:
        sys.path.insert(0, receiver_dir)
    variant_id = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(variant_id, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {path} 构造 import spec")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[variant_id] = mod
    spec.loader.exec_module(mod)
    return mod


def _validate_variant(mod: Any, path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验契约：build_model callable + DUMMY_INPUT.shape + KNOBS。

    返回 (dummy_input, knobs)。knobs={} 表示不可调（latency 超阈即 FAIL_latency）。
    """
    if not hasattr(mod, "build_model") or not callable(mod.build_model):
        raise AttributeError(f"{path} 无 callable build_model（契约必备）")
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        raise ValueError(
            f"{path} DUMMY_INPUT 缺 shape（list）——禁硬编码 shape 回退（用户须声明真实 I/O 维度）"
        )
    knobs = getattr(mod, "KNOBS", None)
    if knobs is None:
        return di, {}
    if not isinstance(knobs, dict) or not knobs:
        raise ValueError(f"{path} KNOBS 必须是非空 dict（得到 {type(knobs).__name__}）")
    for k, kn in knobs.items():
        if not isinstance(kn, dict):
            raise ValueError(f"{path} KNOBS[{k!r}] 不是 dict")
        for field in ("default", "min", "step", "leverage"):
            if field not in kn:
                raise ValueError(f"{path} KNOBS[{k!r}] 缺字段 {field!r}")
        if not isinstance(kn["step"], (int, float)) or kn["step"] >= 0:
            raise ValueError(f"{path} KNOBS[{k!r}].step 必须 <0（缩容方向；得到 {kn['step']!r}）")
        if kn["leverage"] not in RANK:
            raise ValueError(
                f"{path} KNOBS[{k!r}].leverage={kn['leverage']!r} 非法；须 ∈ {sorted(RANK)}"
            )
        if not isinstance(kn["default"], (int, float)) or not isinstance(kn["min"], (int, float)):
            raise ValueError(f"{path} KNOBS[{k!r}] default/min 须为数值")
    return di, knobs


def pick_variant(
    receiver_dir: str,
    ledger_path: str,
    target_latency_us: float,
    latency_provider: str,
    force_rerun: bool,
) -> dict[str, Any] | None:
    """返回下一未 done 变体的 SelectionSpec，或 None（全 done）。无变体 → raise（exit 3 由 caller）。"""
    variants = _list_variants(receiver_dir)
    if not variants:
        # 无变体是配置错误，raise（caller 退 exit 3）。
        raise _NoVariants(receiver_dir)

    rows = [] if force_rerun else read_ledger(ledger_path)
    cur_provider_id = provider_id(latency_provider)

    for path in variants:
        variant_id = os.path.splitext(os.path.basename(path))[0]
        mod = _load_variant(path)
        dummy_input, knobs = _validate_variant(mod, path)
        vsha = sha256_file(path)

        if not force_rerun:
            rows_for_v = [r for r in rows if r.get("variant_id") == variant_id]
            if is_variant_done(rows_for_v, target_latency_us, cur_provider_id, vsha):
                continue  # 已 done → 跳过

        return {
            "variant_id": variant_id,
            "variant_path": os.path.abspath(path),
            "variant_sha256": vsha,
            "build_fn": getattr(mod, "BUILD_FN", "build_model"),
            "dummy_input": dummy_input,
            "knobs": knobs,
            "tunable": bool(knobs),
        }
    return None  # 全 done


class _NoVariants(Exception):
    """receiver 目录无变体 .py。专用信号 → exit 3。"""

    def __init__(self, receiver_dir: str):
        super().__init__(f"NO_VARIANTS: {receiver_dir} 无变体 .py")
        self.receiver_dir = receiver_dir


def _main() -> int:
    p = argparse.ArgumentParser(description="KD-NAS 确定性变体选择器（contract §4）")
    p.add_argument("--receiver_dir", default="",
                   help="receiver KB 目录；默认 $ORCA_KB_DIR/families/receiver")
    p.add_argument("--ledger", required=True, help="ledger.jsonl 路径（可能不存在）")
    p.add_argument("--target_latency_us", required=True, type=float, help="当前 latency 目标")
    p.add_argument("--latency_provider", required=True,
                   help="用户 latency 脚本 path::func（算 provider_id）")
    p.add_argument("--force_rerun", action="store_true", help="忽略 ledger，全量重扫（仅 variants）")
    p.add_argument("--out", default="", help="SelectionSpec json 输出路径")
    args = p.parse_args()

    receiver_dir = args.receiver_dir or os.path.join(
        os.environ.get("ORCA_KB_DIR", ""), "families", "receiver"
    )

    try:
        spec = pick_variant(
            receiver_dir=receiver_dir,
            ledger_path=args.ledger,
            target_latency_us=args.target_latency_us,
            latency_provider=args.latency_provider,
            force_rerun=args.force_rerun,
        )
    except _NoVariants as e:
        print("NO_VARIANTS: true")
        print(f"# {e}", file=sys.stderr)
        return 3  # 配置错误专用码
    except Exception as e:
        print(f"[pick_variant] FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    if spec is None:
        print("ALL_DONE: true")
        return 0

    out_path = args.out
    if out_path:
        out_path = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False, indent=2)
    print(f"VARIANT_SPEC: {out_path or '(stdout only)'}")
    print(f"VARIANT_ID: {spec['variant_id']}")
    print(f"# tunable={spec['tunable']} knobs={list(spec['knobs'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
