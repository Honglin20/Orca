"""_common.py —— 四量化脚本（bit-curve / ptq-sweep / qat / sensitivity）共享的纯 helper。

P2-5 下沉：原四脚本各自维护 ``_load_env_file`` / ``_load_adapter`` / ``_resolve_eval`` /
``_dump_json`` / ``_free_model`` / ``_is_better`` / ``_BITWIDTH_PRESETS`` 七份近似副本，违反 DRY。
本模块抽出共享部分，脚本侧只保留**自己独有的契约**（log_prefix / module_name 等）。

设计原则：
- **纯函数 / 显式参数**：不读全局状态；``log_prefix`` 由 caller 传（每脚本 stderr 前缀不同）。
- **不改行为**：下沉前后字节等价（除 log_prefix 外）——git diff 应只见 import 替换 + 函数删除。
- **边界**：``_resolve_eval`` 三脚本（bit-curve/ptq-sweep/qat）契约相同，并入；sensitivity 契约
  不同（无 default teacher-student 路径）不强制并入。

铁律：失败路径显式 fail loud（exit 2 = 环境错，exit 3 = 业务错，由 caller 决定）。
"""

from __future__ import annotations

import gc
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

# bit_width 预设 → QConfig 构造字段（W1/W2/W3 三脚本原各自维护的等价表）。
# 复刻自 run_ptq_sweep._BITWIDTH_PRESETS（bit-curve / qat 的同名字段语义一致；三表合一）。
BITWIDTH_PRESETS: dict[str, dict[str, Any]] = {
    "w4a4-mx": {
        "method": "mx",
        "w_elem_format": "fp4_e2m1",
        "a_elem_format": "fp4_e2m1",
        "block_size": 16,
    },
    "w4a8-mx": {
        "method": "mx",
        "w_n_bits": 4,
        "a_n_bits": 8,
        "w_elem_format": "fp4_e2m1",
        "a_elem_format": "fp8_e4m3",
        "block_size": 16,
    },
    "w8a8-mx": {
        "method": "mx",
        "w_elem_format": "fp8_e4m3",
        "a_elem_format": "fp8_e4m3",
        "block_size": 16,
    },
    "w8a8-int": {
        "method": "int",
        "n_bits": 8,
    },
    # w4a16 真正语义：weight-only INT4 + 激活保持 FP16（a_quant_enabled=False）。
    # W1 旧版写 `a_elem_format="fp16"` 但 method=int 不消费该字段，实际退化成 w4a4；
    # 这里修正为 a_quant_enabled=False 让激活 bypass fake-quant，名副其实。
    "w4a16": {
        "method": "int",
        "n_bits": 4,
        "w_n_bits": 4,
        "a_quant_enabled": False,
    },
}


def load_env_file(path: str, log_prefix: str = "[quant] ") -> None:
    """自加载 ``orca_env.sh``（``export K=V`` 行）到 ``os.environ``。

    兜底场景：opencode bash 工具不跨调用保 env，若子代理把 ``source orca_env.sh`` 和
    ``python3`` 拆成两次调用，脚本运行的 shell 就没有 ``ORCA_CHART_SOCK`` →
    ``render_chart`` raise 被静默吞 → 图不推。已存在的 env 不覆盖（显式 env 优先）。

    ``log_prefix`` = 调用脚本的 stderr 前缀（如 ``[run_qat] ``），便于日志归因。
    """
    if not path:
        return
    p = Path(path).expanduser()
    if not p.is_file():
        sys.stderr.write(f"{log_prefix}--env_file 不存在: {p}（跳过自加载）\n")
        return
    cnt = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        os.environ.setdefault(k, v)
        cnt += 1
    sys.stderr.write(f"{log_prefix}自加载 {cnt} 个 env from {p}\n")


def load_adapter(path: str, module_name: str, log_prefix: str = "[quant] "):
    """按文件路径动态 import adapter 模块。

    ``module_name`` = 每脚本独立的逻辑名（如 ``ts_quant_qat_adapter``），避免多脚本
    互相覆盖 ``sys.modules`` 里同名 adapter。
    """
    p = Path(path).resolve()
    if not p.is_file():
        sys.stderr.write(f"{log_prefix}adapter 不存在: {p}\n")
        sys.exit(2)
    spec = importlib.util.spec_from_file_location(module_name, p)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def dump_json(obj: dict[str, Any], path: Path) -> None:
    """原子落盘 JSON（写 tmp → os.replace，中断不留半个文件）。

    ``default=str``：兜底不可直接序列化的对象（如 metric_spec），与原四脚本一致。
    """
    payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def free_model(q_model) -> None:
    """显式释放量化模型，触发 Python GC + CUDA cache 回收。

    容错：``del q_model`` / ``torch.cuda.empty_cache`` 任一异常都不抛（释放路径不能
    阻断主流程；最坏情况是显存稍晚归还）。
    """
    if q_model is None:
        return
    try:
        del q_model
    except Exception:
        pass
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def is_better(new_metric: float, cur_metric: float, higher_is_better: bool) -> bool:
    """方向感知的 metric 比较。``higher_is_better=True`` → ``new > cur``；否则 ``new < cur``。

    纯函数（便于单测，Rule 9）：钉死 best 选择的方向语义。
    """
    if higher_is_better:
        return new_metric > cur_metric
    return new_metric < cur_metric


def resolve_eval(
    adapter,
    fp_model,
    eval_loader,
    forward_fn,
    *,
    log_prefix: str = "[quant] ",
) -> tuple[Callable, str, bool]:
    """返回 ``(eval_fn, metric_kind, higher_is_better)``。bit-curve / ptq-sweep / qat 三脚本共享。

    契约（与原三脚本字节等价）：
    - ``adapter.get_eval_fn()`` 存在且返回非 None → 业务路径，需 ``adapter.get_metric_spec()``
      返回 ``{primary_metric: str, higher_is_better: bool}``（缺字段 fail loud exit 2）；
    - 否则 → 默认 teacher-student mse（lower is better）；``forward_fn`` 必填，否则 exit 2。
      并 stderr WARN 不静默（精度仅自洽性参考，不代表业务精度）。

    注：``sensitivity-analyzer`` 的 eval 契约不同（无 teacher-student 默认路径），不并入本函数。
    """
    get_eval_fn = getattr(adapter, "get_eval_fn", None)
    business_fn = get_eval_fn() if callable(get_eval_fn) else None
    if business_fn is not None:
        get_metric_spec = getattr(adapter, "get_metric_spec", None)
        if not callable(get_metric_spec):
            sys.stderr.write(
                f"{log_prefix}业务 eval_fn 路径需要 adapter.get_metric_spec() "
                "返回 {primary_metric: str, higher_is_better: bool}\n"
            )
            sys.exit(2)
        spec = get_metric_spec() or {}
        metric_kind = spec.get("primary_metric")
        if not metric_kind:
            sys.stderr.write(
                f"{log_prefix}get_metric_spec() 缺 primary_metric: {spec}\n"
            )
            sys.exit(2)
        return business_fn, str(metric_kind), bool(spec.get("higher_is_better", False))

    if forward_fn is None:
        sys.stderr.write(
            f"{log_prefix}默认 teacher-student eval 路径需要 adapter.forward_fn "
            "（按模型 forward 解包 batch）—— 异构 batch 会让 SDK fallback 误算\n"
        )
        sys.exit(2)
    # WARN 不静默（plan §P5）：未提供业务 eval_fn → 退 teacher-student mse，精度仅自洽性参考。
    sys.stderr.write(
        f"{log_prefix}WARN: 未提供业务 eval_fn（eval_fn_ref 空）→ 退 teacher-student mse。"
        "该指标仅自洽性参考（量化模型 vs FP teacher 的 mse），不代表业务精度。\n"
    )
    # 局部 import：仅 teacher-student 路径需要（业务路径已 early return），避免 sensitivity
    # 等不依赖 ts_quant.eval 的脚本被迫加载该模块。
    from ts_quant.eval import build_teacher_student_eval_fn

    eval_fn = build_teacher_student_eval_fn(
        teacher_model=fp_model,
        dataloader=eval_loader,
        forward_fn=forward_fn,
    )
    return eval_fn, "mse", False
