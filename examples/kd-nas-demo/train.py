"""train.py —— kd-nas-demo 的用户任务 loss + dataloader（KD 蒸馏消费）。

kd-nas workflow 的 setup 节点（kd-setup agent step 6）会从用户 train.py grep loss/dataloader，
抽出 ``compute_loss`` 与 ``build_dataloader``，写入 setup output 的 ``user_train_import`` /
``user_loss_fn``，再经 train_pool 注入 ``train_adapter_template.py``。

本文件让 demo **自包含 + 确定**（不依赖 kd-setup agent 的 ask-user 哨兵决策）：
  - ``compute_loss(s_out, y)``：MSE（demo 随机数据，不求收敛，只撑训练流水）；
  - ``build_dataloader()``：返回**可重复迭代**的随机 (x, y) batch 生成器（shape 对齐 DUMMY_INPUT）。

train_adapter 的加载逻辑（``train_adapter_template._load_user_loss``）：
  - ``USER_LOSS_FN = compute_loss``（由 kd-setup 写入 user_loss_fn）；
  - ``build_dataloader = getattr(module, "build_dataloader", ...)``。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

# demo I/O（与 DUMMY_INPUT 一致）。
_BATCH_SIZE = 4
_N_BATCHES = 8
_SHAPE = (1, 4, 48, 64, 1)


def compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """用户任务 loss（MSE）。s_out / y 同形 [B, 4, 48, 64, 1]。"""
    return F.mse_loss(s_out, y)


class _RandomDataLoader:
    """可重复迭代的随机 (x, y) batch 生成器（每 epoch 重新生成；demo 随机数据不求收敛）。

    返回 re-iterable 对象（每次 ``iter()`` 重新 yield），故 train_adapter 多 epoch 训练
    每 epoch 都能拿到 ``n_batches`` 个 batch（不像一次性 generator 跑完即空）。
    """

    def __init__(self, batch_size: int = _BATCH_SIZE, n_batches: int = _N_BATCHES,
                 shape: tuple = _SHAPE):
        self.batch_size = batch_size
        self.n_batches = n_batches
        self.shape = shape

    def __iter__(self):
        inner = tuple(self.shape[1:])
        for _ in range(self.n_batches):
            x = torch.randn(self.batch_size, *inner)
            y = torch.randn(self.batch_size, *inner)
            yield x, y

    def __len__(self) -> int:
        return self.n_batches


def build_dataloader(batch_size: int = _BATCH_SIZE, n_batches: int = _N_BATCHES):
    """构造随机 dataloader（train_adapter 先试 ``build_dataloader()``，故默认参数必备）。"""
    return _RandomDataLoader(batch_size=batch_size, n_batches=n_batches)


# 显式导出（kd-setup agent grep 时可识别）。
_LOSS_FN_NAME = "compute_loss"
_DATALOADER_FN_NAME = "build_dataloader"
