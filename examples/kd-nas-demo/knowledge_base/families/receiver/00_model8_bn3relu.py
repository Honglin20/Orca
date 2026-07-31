"""00_model8_bn3relu.py —— kd-nas-demo KB 主变体（原始 model8 + BN + 3层 + ReLU）。

**KB 第一个变体**（文件名字典序最小：digit ``0`` (0x30) < letter ``d`` (0x64)，故 ``00_*``
排在所有 ``demo_tiny_*`` 前；见 ``pick_variant._list_variants`` 的 ``sorted()`` 排序，
排除 ``_*.py`` 共享模块）。用户真实场景的最激进轻量化 student：基于原始 ``SignalProcessingTransformer``
架构（3-tap Conv1d QKV/proj + attention + FFN），三条轻量化路径**全开**：
  1. **NORM 用 BatchNorm1d**（替代 LayerNorm，user 验证过时延能达标）——BN 跑通道维
     （embed_dim），作用于 reshape 后的 ``[B*num_syms, embed_dim, num_subs]``；
  2. **缩到 3 层**（原始 baseline 4 层 / teacher 10 层；3 层大概率时延达标）；
  3. **GELU → ReLU**（FFN 激活）。

3 层有精度损失 → 正是 KD 弥补的场景（teacher 10 层 t1/t2 交替作软标签源）。

架构来源：``workflows/agents/_kd_scripts/teacher_model.py``（``SignalProcessingTransformer``
等类）逐字复刻到同目录 ``_model8_student_blocks``，仅多 ``norm_type`` / ``act_type`` 两开关。

I/O 契约同所有变体（见 ``workflows/agents/_kd_scripts/CONTRACTS.md`` §1）：
  - 输入 ``[B, 4, 48, 64, 1]``，输出同形；内部 alpha 功率归一。
  - ``build_model(**cfg)`` 零参用 KNOBS.default；cfg 覆盖 num_blocks / embed_dim。
  - ``feature_hook_names()`` 恒 2 个（与 teacher 等长，KD OFD/FitNets 要求）。
"""

from __future__ import annotations

import torch.nn as nn

from _model8_student_blocks import SignalProcessingTransformer  # 同目录共享积木（receiver_dir 由 pick_variant/validate_contract 注入 sys.path）

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}
BUILD_FN = "build_model"

# 可调旋钮：latency 超阈时按 leverage 高→低、step 缩容，刚跨 target 即停。
# min=2：保 feature_hook_names 两 hook 落 distinct block（main.0 / main.1），
# 避免 num_blocks=1 时 hook 重复致 KD OFD 特征对齐退化（与 demo KB 约定一致）。
KNOBS = {
    "num_blocks": {"default": 3, "min": 2, "step": -1, "leverage": "high"},
    "embed_dim": {"default": 16, "min": 8, "step": -4, "leverage": "medium"},
}

# 固定结构参数（非旋钮；与 DUMMY_INPUT 一致）。
_IN_CHANNELS = 4
_NUM_SYMBOLS = 64
_NUM_SUBCARRIERS = 48
# 本变体结构身份（与同族兄弟变体区分；内部 block 全 t1 = 原始 baseline 模式）。
_NORM_TYPE = "bn"
_ACT_TYPE = "relu"


def build_model(**cfg) -> nn.Module:
    """实例化主变体（model8 + BN + 3层 + ReLU）。

    cfg 取 num_blocks / embed_dim（缺省用 KNOBS.default）。block_mtypes 全 t1（原始 baseline 模式）。
    """
    num_blocks = int(cfg.get("num_blocks", KNOBS["num_blocks"]["default"]))
    embed_dim = int(cfg.get("embed_dim", KNOBS["embed_dim"]["default"]))
    block_mtypes = ["t1"] * num_blocks
    return SignalProcessingTransformer(
        block_mtypes=block_mtypes,
        in_channels=_IN_CHANNELS,
        embed_dim=embed_dim,
        num_symbols=_NUM_SYMBOLS,
        num_subcarriers=_NUM_SUBCARRIERS,
        norm_type=_NORM_TYPE,
        act_type=_ACT_TYPE,
    )
