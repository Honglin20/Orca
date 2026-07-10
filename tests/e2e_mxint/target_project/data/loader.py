"""Data loader for sklearn digits (8x8=64 features, 10 classes, 1797 samples).

bitx adapter 的 ``get_data()`` 直接调 ``get_data()`` 拿 (calib_data, eval_loader)。
"""
from __future__ import annotations

import torch
from sklearn.datasets import load_digits
from torch.utils.data import DataLoader, TensorDataset


def _load_split():
    """Load sklearn digits, split 80/20 (deterministic), normalize to [0, 1]."""
    digits = load_digits()
    x = torch.as_tensor(digits.data, dtype=torch.float32) / 16.0  # 1797 x 64
    y = torch.as_tensor(digits.target, dtype=torch.long)
    # 固定 split（无 random_state 干扰，bitx run 间可复现）
    n_train = int(len(x) * 0.8)  # 1437 train / 360 eval
    return x[:n_train], y[:n_train], x[n_train:], y[n_train:]


def get_data():
    """Return (calib_data, eval_loader) for bitx adapter.

    calib_data: List[Tensor] —— 前 32 个训练样本（bitx 量化 calibration）
    eval_loader: DataLoader —— 360 个评估样本（bitx 量化前后精度计算）
    """
    x_train, y_train, x_eval, y_eval = _load_split()
    # calib: list of (1, 1, 8, 8) tensors for bitx observers
    calib_data = [x_train[i].reshape(1, 1, 8, 8) for i in range(32)]
    eval_ds = TensorDataset(
        x_eval.reshape(-1, 1, 8, 8),  # NCHW for nn.Flatten
        y_eval,
    )
    eval_loader = DataLoader(eval_ds, batch_size=64, shuffle=False)
    return calib_data, eval_loader
