#!/usr/bin/env python3
"""check_equivalence.py —— PSU 等价 gate（确定性，fail loud）。

参照物钉死 = ``load_pretrained.py`` 构建的预训练原模型（prepared_model 同款 +
pretrained_ckpt 权重）；受检物 = ``supernet.py`` 全 original 路径。三段判定：

  A 物化键契约：全 original ``get_active_subnet()`` 的 state_dict 键集合 ==
    原模型键集合，且逐张量值相等——未匹配键 / 值错配 fail loud 列清单
    （一条断言同时锁权重继承完整性与 choice 容器物化键规范）。
  B forward 等价：同输入（``build_probe_inputs()``，原层签名含 mask 时必须含
    带 mask 用例）逐输出张量 allclose(atol=1e-5, rtol=1e-4)；eval mode + CPU +
    dtype/device 归一后比对。
  C freeze 分组：original 分支 + 非 slot 固定模块 requires_grad=False、变体分支
    requires_grad=True（eval forward 不读 requires_grad——freeze 漏配只能在显式
    断言里暴露，不能依赖 forward 等价兜底）。

无论 pass/fail 都落盘 ``.equivalence.json``（reporter 消费 pass 布尔与失败清单）。
deterministic：无 LLM / 网络 / 时钟 / 随机（随机源只来自被检模型自身的构造，
比对本身零随机）。

退出码：0 = 全部通过；1 = 任一断言失败或前置文件缺失（失败原因已写入 JSON）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import types
from inspect import signature
from pathlib import Path
from typing import Any

import torch
import torch.nn as nn

ATOL_DEFAULT = 1e-5
RTOL_DEFAULT = 1e-4
MARKER_NAME = ".equivalence.json"

# 原层 forward 传 mask 时常见的 kwarg 名（与变体库 _MASK_KEYS 同口径）。
_MASK_KEYS = ("attn_mask", "src_mask", "attention_mask", "mask", "key_padding_mask")

# 失败清单打印上限（避免大模型键位错配刷屏；完整计数始终给出）。
_MAX_LISTED = 20


def _fail(msg: str) -> None:
    print(f"[check_equivalence] FAIL {msg}", file=sys.stderr)


def _exec_module(path: Path, mod_name: str) -> dict[str, Any]:
    """exec 一个 python 文件并返回其模块命名空间（不触发 __main__ 块）。

    两点必须：
      * 文件所在目录挂 sys.path 前部（load_pretrained.py sibling-import prepared_model）；
      * 以真实 ModuleType 注册进 sys.modules **再** exec——py3.14 dataclass 解析
        postponed annotation 时查 ``sys.modules[cls.__module__]``，裸 dict 命名空间
        会 AttributeError（fail loud 变成 gate 自身崩溃）。
    """
    sys.path.insert(0, str(path.parent))
    module = types.ModuleType(mod_name)
    module.__dict__["__file__"] = str(path)
    sys.modules[mod_name] = module
    src = path.read_text(encoding="utf-8")
    exec(compile(src, str(path), "exec"), module.__dict__)  # noqa: S102 -- 与 check_expand.sh 同款 exec 探测
    return module.__dict__


def _compare_outputs(ref: Any, test: Any, atol: float, rtol: float, path: str,
                     failures: list[str]) -> None:
    """递归比对两条 forward 输出：tensor / tuple / list / dict / 标量。

    浮点张量归一 float 后 allclose；int 张量 torch.equal。
    """
    if isinstance(ref, torch.Tensor) or isinstance(test, torch.Tensor):
        if not (isinstance(ref, torch.Tensor) and isinstance(test, torch.Tensor)):
            failures.append(f"{path}: 一侧为 tensor 另一侧不是 ({type(ref)} vs {type(test)})")
            return
        a, b = ref.cpu(), test.cpu()
        if a.shape != b.shape:
            failures.append(f"{path}: shape {tuple(a.shape)} != {tuple(b.shape)}")
            return
        if a.is_floating_point() or b.is_floating_point():
            a, b = a.float(), b.float()
            if not torch.allclose(a, b, atol=atol, rtol=rtol):
                diff = (a - b).abs().max().item()
                failures.append(
                    f"{path}: allclose 超差 (max|diff|={diff:.3e}, atol={atol}, rtol={rtol})"
                )
        else:
            if not torch.equal(a, b):
                failures.append(f"{path}: 整型张量不相等")
        return
    if isinstance(ref, (tuple, list)) and isinstance(test, (tuple, list)):
        if len(ref) != len(test):
            failures.append(f"{path}: 容器长度 {len(ref)} != {len(test)}")
            return
        for i, (ra, ta) in enumerate(zip(ref, test)):
            _compare_outputs(ra, ta, atol, rtol, f"{path}[{i}]", failures)
        return
    if isinstance(ref, dict) and isinstance(test, dict):
        if set(ref.keys()) != set(test.keys()):
            failures.append(
                f"{path}: dict 键不一致 only_ref={sorted(set(ref) - set(test))} "
                f"only_test={sorted(set(test) - set(ref))}"
            )
            return
        for k in sorted(ref, key=str):
            _compare_outputs(ref[k], test[k], atol, rtol, f"{path}.{k}", failures)
        return
    if isinstance(ref, (int, float)) and isinstance(test, (int, float)):
        if not (abs(ref - test) <= atol + rtol * abs(ref)):  # allclose 同式
            failures.append(f"{path}: 标量 {ref} != {test}")
        return
    if type(ref) is not type(test) or ref != test:
        failures.append(f"{path}: {ref!r} != {test!r}")


def _check_key_contract(model: nn.Module, subnet: nn.Module, failures: list[str]) -> dict[str, int]:
    """物化键契约：键集合相等 + 逐张量值相等（torch.equal——继承即拷贝，须逐位一致）。"""
    sd_ref = model.state_dict()
    sd_sub = subnet.state_dict()
    keys_ref, keys_sub = set(sd_ref), set(sd_sub)
    missing = sorted(keys_ref - keys_sub, key=str)
    extra = sorted(keys_sub - keys_ref, key=str)
    if missing:
        preview = ", ".join(missing[:_MAX_LISTED])
        failures.append(
            f"物化键契约: {len(missing)} 个原模型键未出现在物化子网（前 {_MAX_LISTED}: {preview}）"
        )
    if extra:
        preview = ", ".join(extra[:_MAX_LISTED])
        failures.append(
            f"物化键契约: {len(extra)} 个物化子网键不在原模型（前 {_MAX_LISTED}: {preview}）"
        )
    value_bad: list[str] = []
    for k in sorted(keys_ref & keys_sub, key=str):
        a, b = sd_ref[k].cpu(), sd_sub[k].cpu()
        if a.dtype != b.dtype:
            value_bad.append(f"{k} (dtype {a.dtype} != {b.dtype})")
        elif not torch.equal(a, b):
            value_bad.append(k)
    if value_bad:
        preview = ", ".join(value_bad[:_MAX_LISTED])
        failures.append(
            f"物化键契约: {len(value_bad)} 个键值不等于父权重（前 {_MAX_LISTED}: {preview}）"
        )
    return {"n_keys_ref": len(keys_ref), "n_missing": len(missing),
            "n_extra": len(extra), "n_value_mismatch": len(value_bad)}


def _check_probe_mask_case(model: nn.Module, cases: list[dict[str, Any]],
                           failures: list[str]) -> list[str]:
    """原层 forward 签名含 mask 参数时，probe 用例必须含带 mask 用例。

    否则变体库的 _MASK_KEYS 适配 bug 不可见（mask 用例是等价 gate 的显式要求）。
    """
    params = signature(model.forward).parameters
    mask_params = [k for k in _MASK_KEYS if k in params]
    if not mask_params:
        return []
    has_mask_case = any(
        any(case.get(k) is not None for k in mask_params) for case in cases
    )
    if not has_mask_case:
        failures.append(
            f"probe 用例缺带 mask 用例：原模型 forward 含 mask 参数 {mask_params}，"
            f"但 build_probe_inputs() 的 {len(cases)} 条用例全部未传非 None mask"
        )
    return mask_params


def _freeze_group_failures(supernet: nn.Module, failures: list[str]) -> dict[str, int]:
    """freeze 分组断言：按 ChoiceLayer.branches 归组逐参数判 requires_grad。

    归组口径（与 spec 的分支适配契约一致）：
      * ``layers.<i>.branches.<name>`` 下的参数 → name=="original" ? original : variant
      * 其余（stem / head / stage 过渡等非 slot 固定模块）→ fixed
    """
    stats = {"n_original": 0, "n_variant": 0, "n_fixed": 0}
    grouped: dict[int, tuple[str, str]] = {}  # id(param) -> (组名, 展示名)

    layers = getattr(supernet, "layers", None)
    if not isinstance(layers, nn.ModuleList):
        failures.append(
            "freeze 分组: supernet.layers 不是 nn.ModuleList（ChoiceLayer 容器契约被破坏）"
        )
        return stats
    for i, layer in enumerate(layers):
        branches = getattr(layer, "branches", None)
        if not isinstance(branches, nn.ModuleDict):
            failures.append(
                f"freeze 分组: layers[{i}] 无 branches ModuleDict（ChoiceLayer 容器契约被破坏）"
            )
            continue
        for bname, branch in branches.items():
            group = "original" if bname == "original" else "variant"
            for pname, p in branch.named_parameters(recurse=True):
                grouped[id(p)] = (group, f"layers.{i}.branches.{bname}.{pname}")

    for pname, p in supernet.named_parameters(recurse=True):
        group, display = grouped.get(id(p), ("fixed", pname))
        stats[f"n_{group}"] = stats.get(f"n_{group}", 0) + 1
        want = group == "variant"  # 只训变体分支参数
        if p.requires_grad != want:
            failures.append(
                f"freeze 分组: {display} requires_grad={p.requires_grad}，"
                f"应为 {want}（original 分支与非 slot 固定模块冻结，变体分支可训）"
            )
    return stats


def run_gate(artifacts_dir: Path, supernet_name: str, load_pretrained_name: str,
             atol: float, rtol: float) -> tuple[bool, dict[str, Any]]:
    """执行完整 gate。返回 (passed, 结果 dict)。任何前置缺失也产出 failed 记录。"""
    result: dict[str, Any] = {
        "passed": False,
        "atol": atol,
        "rtol": rtol,
        "supernet": supernet_name,
        "load_pretrained": load_pretrained_name,
        "checks": {},
        "failures": [],
        "stats": {},
    }
    failures: list[str] = result["failures"]

    supernet_path = artifacts_dir / supernet_name
    lp_path = artifacts_dir / load_pretrained_name
    for p in (supernet_path, lp_path):
        if not p.is_file():
            failures.append(f"前置缺失: {p.name} 不存在于 {artifacts_dir}")
            return False, result

    # ── 参照物：预训练原模型 ────────────────────────────────────────────────
    lp_ns = _exec_module(lp_path, "load_pretrained_checked")
    build_model = lp_ns.get("build_pretrained_model")
    build_inputs = lp_ns.get("build_probe_inputs")
    if not callable(build_model) or not callable(build_inputs):
        failures.append(
            "load_pretrained.py 契约缺失: 须暴露 build_pretrained_model() 与 "
            "build_probe_inputs()（flatten 期生成契约）"
        )
        return False, result
    model = build_model()
    if not isinstance(model, nn.Module):
        failures.append(f"build_pretrained_model() 返回 {type(model)}，须为 nn.Module")
        return False, result
    model.to("cpu").eval()  # CPU 确定性优先 + eval mode

    cases = build_inputs()
    if not isinstance(cases, list) or not cases:
        failures.append("build_probe_inputs() 须返回非空 list[dict]（forward kwargs 用例）")
        return False, result

    mask_params = _check_probe_mask_case(model, cases, failures)
    mask_case_failed = any("probe 用例缺带 mask" in f for f in failures)
    result["checks"]["probe_mask_case"] = "failed" if mask_case_failed else "ok"

    # ── 受检物：supernet 全 original 路径 ──────────────────────────────────
    ns = _exec_module(supernet_path, "supernet_checked")
    ss_cls = ns.get("SearchSpace")
    build_supernet = ns.get("build_supernet")
    if ss_cls is None or not callable(build_supernet):
        failures.append(
            "supernet.py 契约缺失: 须暴露 SearchSpace 与 build_supernet()（零参友好构造入口）"
        )
        return False, result
    ss = ss_cls()  # 零参构造硬约束（三处 exec 消费方同款）
    all_original = getattr(ss, "all_original", None)
    if not callable(all_original):
        failures.append("SearchSpace 缺 all_original()（默认 config = 全 original 路径契约）")
        return False, result

    supernet = build_supernet(pretrained_state=model.state_dict())
    if not isinstance(supernet, nn.Module):
        failures.append(f"build_supernet() 返回 {type(supernet)}，须为 nn.Module")
        return False, result
    supernet.to("cpu").eval()
    supernet.set_sample_config(all_original())

    # ── A 物化键契约 ────────────────────────────────────────────────────────
    subnet = supernet.get_active_subnet()
    key_stats = _check_key_contract(model, subnet, failures)
    result["stats"].update(key_stats)
    result["checks"]["materialized_key_contract"] = (
        "ok" if key_stats["n_missing"] == 0 and key_stats["n_extra"] == 0
        and key_stats["n_value_mismatch"] == 0 else "failed"
    )

    # ── B forward 等价（逐用例逐输出张量）────────────────────────────────────
    fwd_failures: list[str] = []
    for idx, case in enumerate(cases):
        with torch.no_grad():
            out_ref = model(**case)
            out_sup = supernet(**case)
        _compare_outputs(out_ref, out_sup, atol, rtol, f"case{idx}", fwd_failures)
    failures.extend(fwd_failures)
    result["stats"]["n_probe_cases"] = len(cases)
    result["checks"]["forward_equivalence"] = "ok" if not fwd_failures else "failed"

    # ── C freeze 分组 ───────────────────────────────────────────────────────
    freeze_failures: list[str] = []
    freeze_stats = _freeze_group_failures(supernet, freeze_failures)
    failures.extend(freeze_failures)
    result["stats"].update(freeze_stats)
    result["checks"]["freeze_groups"] = "ok" if not freeze_failures else "failed"

    result["passed"] = not failures
    return result["passed"], result


def main() -> int:
    parser = argparse.ArgumentParser(description="PSU 等价 gate（全 original 路径 ≡ 预训练原模型）")
    parser.add_argument("--artifacts-dir", default=None, help="$ORCA_ARTIFACTS_DIR（默认 cwd）")
    parser.add_argument("--supernet", default="supernet.py", help="supernet 文件名（相对 artifacts-dir）")
    parser.add_argument("--load-pretrained", default="load_pretrained.py",
                        help="load_pretrained 文件名（相对 artifacts-dir）")
    parser.add_argument("--atol", type=float, default=ATOL_DEFAULT)
    parser.add_argument("--rtol", type=float, default=RTOL_DEFAULT)
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir or os.environ.get(
        "ORCA_ARTIFACTS_DIR", ".")).resolve()
    marker = artifacts_dir / MARKER_NAME

    passed = False
    try:
        passed, result = run_gate(artifacts_dir, args.supernet, args.load_pretrained,
                                  args.atol, args.rtol)
    except Exception as exc:  # noqa: BLE001 -- gate 自身异常也必须落盘 + fail loud
        result = {
            "passed": False, "atol": args.atol, "rtol": args.rtol,
            "supernet": args.supernet, "load_pretrained": args.load_pretrained,
            "checks": {}, "stats": {},
            "failures": [f"gate 执行异常: {exc.__class__.__name__}: {exc}"],
        }
        print(f"[check_equivalence] FATAL gate 执行异常: {exc}", file=sys.stderr)
        traceback.print_exc()

    # 无论 pass/fail 都落盘（reporter 消费）。
    try:
        marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[check_equivalence] WARN .equivalence.json 落盘失败（不改变判定）: {exc}",
              file=sys.stderr)

    for f in result["failures"]:
        _fail(f)
    checks = result.get("checks", {})
    print(f"[check_equivalence] key_contract={checks.get('materialized_key_contract', '-')} "
          f"forward={checks.get('forward_equivalence', '-')} "
          f"freeze={checks.get('freeze_groups', '-')} "
          f"marker={MARKER_NAME} passed={result['passed']}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
