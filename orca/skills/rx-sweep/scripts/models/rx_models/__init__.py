"""rx_models —— rx-sweep 多方案接收机模型包。

统一 I/O（全方案一致）：``[B, num_ports, num_subcarriers, num_symbols, 1]``。

设计（用户需求 2026-08-12）：
- **每方案一个文件**（干净），``@register`` 自注册到 ``MODEL_REGISTRY``。
- **统一 RxConfig** 管理维度（单一真相源，杜绝原 pure_cnn_model.py 的 64/32 漂移）。
- 每方案可单独 ``python -m rx_models.<name>`` smoke + ``python -m rx_models.export_onnx``
  导单文件 ONNX（昇腾友好）。
- forward I/O 恒为 ``[B,P,F,S,1]`` → **不动训练/数据代码**；特征变换在模型内部完成。

用法::

    from rx_models import get_model, list_models, RxConfig
    cfg = RxConfig(num_symbols=32)            # 对齐工程 beam_num
    model = get_model("pure_cnn", cfg)        # 任一已注册方案

方案清单（``list_models()`` 查）：
  - model8_trf     baseline（原 attention 主干）
  - pure_cnn       纯 CNN（DualAxisConvBlock）
  - cnn_trf_alt    CNN+TRF 交替堆叠
  - feat_complex   B1 复数卷积前端 + CNN 主干
  - feat_diff      B2 差分先验前端 + CNN 主干
  - feat_fft       B3 频域 FFT 前端 + CNN 主干（Vector 算子，验精度非降时延）
  - feat_adjbeam   B4 邻波束拼接前端 + Conv2d 主干
"""
from __future__ import annotations

from .config import RxConfig

MODEL_REGISTRY: dict[str, type] = {}


def register(name: str):
    """类装饰器：把方案模型类注册到 ``MODEL_REGISTRY``。重名 fail loud。"""
    def _decorator(cls):
        if name in MODEL_REGISTRY:
            raise ValueError(
                f"model name {name!r} 已注册（重复）→ {MODEL_REGISTRY[name]!r}"
            )
        MODEL_REGISTRY[name] = cls
        return cls
    return _decorator


_loaded = False


def _ensure_loaded() -> None:
    """惰性 import 所有方案文件以触发 ``@register``。

    不在包 import 顶层做，避免 ``import rx_models`` 时强依赖所有方案文件（某方案
    文件独立调试时，``import rx_models`` 仍可用 ``RxConfig``）。首次 ``get_model``
    / ``list_models`` 触发一次性加载。
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    # 触发各方案 @register（相对 import；缺文件会在 list_models 调用时 raise）
    from . import (  # noqa: F401
        model8_trf,
        pure_cnn,
        cnn_trf_alt,
        feat_complex,
        feat_diff,
        feat_fft,
        feat_adjbeam,
    )


def get_model(name: str, cfg: RxConfig):
    """按名实例化方案模型。未知名 fail loud。"""
    _ensure_loaded()
    if name not in MODEL_REGISTRY:
        raise ValueError(
            f"未知 model {name!r}，可用: {sorted(MODEL_REGISTRY)}"
        )
    return MODEL_REGISTRY[name](cfg)


def list_models() -> list[str]:
    """返回所有已注册方案名（触发加载）。"""
    _ensure_loaded()
    return sorted(MODEL_REGISTRY)


__all__ = [
    "RxConfig",
    "MODEL_REGISTRY",
    "register",
    "get_model",
    "list_models",
]
