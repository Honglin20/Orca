#!/usr/bin/env python3
"""check_choice_contract.py —— PSU choice 契约确定性 gate（choice-only + 反向维度）。

对 ``supernet.py`` 的 ``SearchSpace``（零参构造）断言三组契约：

  1. 反向维度 gate：按 search-record schema 生成器同款反射逻辑扫描公有
     list/tuple 属性，发现的搜索维度必须**唯一为 choice 容器
     ``branch_choices``**。任何其他维度属性即 FAIL——含单值元组（平铺单值
     元组会被反射误报为 type=list 假维度，钉死维度必须标量）。
  2. 分支集契约：``branch_choices`` 含 "original"（必含冻结分支）、无重复、
     ≥2 分支（choice-only 搜索空间至少要有一个真实选择）。
  3. pin 校验（``.baseline.json`` 存在时）：``depth`` 与 ``internal_dims``
     各键必须 == SearchSpace 同名标量属性——原层实测值是钉死值的唯一来源。

deterministic：无 LLM / 网络 / 时钟 / 随机。fail loud：逐条列出违规。
退出码：0 = 契约满足；1 = 任一断言失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import types
from pathlib import Path
from typing import Any

BASELINE_NAME = ".baseline.json"


def _fail(msg: str) -> None:
    print(f"[check_choice_contract] FAIL {msg}", file=sys.stderr)


def _reflect_dimensions(ss: Any) -> dict[str, dict[str, Any]]:
    """按 generate_schema.py 同款反射逻辑收集「搜索维度」属性。

    与下游 schema 生成器逐字对齐：公有（非 ``_`` 前缀）list/tuple 属性，
    嵌套容器 → list_of_lists，纯标量容器 → list。这里只收集不裁决。
    """
    dims: dict[str, dict[str, Any]] = {}
    for attr in dir(ss):
        if attr.startswith("_"):
            continue
        val = getattr(ss, attr)
        if isinstance(val, (list, tuple)) and len(val) > 0:
            if all(isinstance(v, (list, tuple)) for v in val):
                dims[attr] = {"type": "list_of_lists", "values": [list(v) for v in val]}
            elif all(isinstance(v, (int, float, str)) for v in val):
                dims[attr] = {"type": "list", "values": list(val)}
    return dims


def run_check(artifacts_dir: Path, supernet_name: str) -> tuple[bool, list[str]]:
    """执行契约检查。返回 (passed, 失败清单)。"""
    failures: list[str] = []
    supernet_path = artifacts_dir / supernet_name
    if not supernet_path.is_file():
        failures.append(f"前置缺失: {supernet_name} 不存在于 {artifacts_dir}")
        return False, failures

    # 零参构造 + 模块级零副作用（__name__ 置非 __main__ 跳过 demo 块）。以真实
    # ModuleType 注册进 sys.modules 再 exec（py3.14 dataclass 的 postponed
    # annotation 解析查 sys.modules[cls.__module__]，裸 dict 会 AttributeError）。
    sys.path.insert(0, str(supernet_path.parent))
    module = types.ModuleType("supernet_choice_checked")
    module.__dict__["__file__"] = str(supernet_path)
    sys.modules["supernet_choice_checked"] = module
    src = supernet_path.read_text(encoding="utf-8")
    exec(compile(src, str(supernet_path), "exec"), module.__dict__)  # noqa: S102 -- 与 check_expand.sh 同款 exec 探测
    ns = module.__dict__
    ss_cls = ns.get("SearchSpace")
    if ss_cls is None:
        failures.append(f"{supernet_name} 未暴露 SearchSpace")
        return False, failures
    try:
        ss = ss_cls()
    except Exception as exc:  # noqa: BLE001 -- 构造失败本身就是契约违规
        failures.append(
            f"SearchSpace() 零参构造失败（契约：三处 exec 消费方依赖零参构造）: "
            f"{exc.__class__.__name__}: {exc}"
        )
        return False, failures

    # 1. 反向维度 gate。
    dims = _reflect_dimensions(ss)
    for attr, spec in sorted(dims.items()):
        if attr != "branch_choices":
            failures.append(
                f"反向维度 gate: SearchSpace.{attr}={spec['values']!r} 是公有容器属性，"
                f"会被 schema 反射误报为搜索维度（{spec['type']}）。唯一合法的公有容器是 "
                f"choice 容器 branch_choices；钉死维度一律标量（单值元组同样违规）"
            )
    if "branch_choices" not in dims:
        failures.append(
            "choice 容器缺失: SearchSpace 须暴露公有 branch_choices（唯一搜索维）"
        )
        return False, failures

    # 2. 分支集契约。
    branches = list(dims["branch_choices"]["values"])
    if "original" not in branches:
        failures.append('分支集契约: branch_choices 缺 "original"（必含冻结继承分支）')
    if len(set(branches)) != len(branches):
        failures.append(f"分支集契约: branch_choices 有重复项 {branches!r}")
    if len(branches) < 2:
        failures.append(
            f"分支集契约: branch_choices 仅 {len(branches)} 个分支，choice-only 搜索空间"
            f"至少需要 original + 1 个变体分支"
        )
    depth = getattr(ss, "depth", None)
    if not isinstance(depth, int) or depth < 1:
        failures.append(f"分支集契约: SearchSpace.depth={depth!r}，须为 ≥1 的 int（原层数钉死值）")

    # 3. .baseline.json pin 校验。
    baseline_path = artifacts_dir / BASELINE_NAME
    if baseline_path.is_file():
        try:
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(f"pin 校验: {BASELINE_NAME} 不可解析: {exc}")
            baseline = None
        if isinstance(baseline, dict):
            base_depth = baseline.get("depth")
            if base_depth is not None and base_depth != depth:
                failures.append(
                    f"pin 校验: SearchSpace.depth={depth!r} != baseline {base_depth!r}"
                    f"（depth 钉死 = 原层数实测值）"
                )
            internal = baseline.get("internal_dims")
            if isinstance(internal, dict):
                for key, want in sorted(internal.items()):
                    got = getattr(ss, key, None)
                    if got != want:
                        failures.append(
                            f"pin 校验: SearchSpace.{key}={got!r} != baseline 实测值 {want!r}"
                            f"（钉死维度必须等于原层实测值）"
                        )
    else:
        print(f"[check_choice_contract] WARN {BASELINE_NAME} 缺失，跳过 pin 校验",
              file=sys.stderr)

    return not failures, failures


def main() -> int:
    parser = argparse.ArgumentParser(description="PSU choice 契约 gate（choice-only + 反向维度）")
    parser.add_argument("--artifacts-dir", default=None, help="$ORCA_ARTIFACTS_DIR（默认 cwd）")
    parser.add_argument("--supernet", default="supernet.py", help="supernet 文件名（相对 artifacts-dir）")
    args = parser.parse_args()

    artifacts_dir = Path(args.artifacts_dir or os.environ.get(
        "ORCA_ARTIFACTS_DIR", ".")).resolve()

    try:
        passed, failures = run_check(artifacts_dir, args.supernet)
    except Exception as exc:  # noqa: BLE001 -- gate 自身异常 fail loud
        print(f"[check_choice_contract] FATAL 执行异常: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1

    for f in failures:
        _fail(f)
    print(f"[check_choice_contract] branch/维度契约 failures={len(failures)} passed={passed}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
