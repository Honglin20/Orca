"""model.py —— 一个 MNIST 手写数字分类 CNN。

典型的 LeNet 风格小 CNN：两层卷积 + 两层全连接，输入 28x28 灰度图，输出 10 类 logits。
通道数和隐层维度可配置，方便做结构搜索 / 蒸馏实验。

约定（被下游工具消费）：
    DUMMY_INPUT   —— 模型输入张量的 shape/dtype，描述单样本。
    BUILD_FN      —— 模型工厂函数名（build_model）。
    KNOBS         —— 可调结构旋钮及其取值范围（default/min/step/leverage）。
    build_model   —— 零参用 KNOBS 默认值；传 cfg 覆盖旋钮。
    feature_hook_names —— 中间特征层名，用于 feature-level 蒸馏对齐（可选）。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

# 单样本输入：1 通道 28x28 灰度图。
DUMMY_INPUT = {"shape": [1, 1, 28, 28], "dtype": "float32"}

# 模型工厂入口名（供字符串引用）。
BUILD_FN = "build_model"

# 结构旋钮。step<0 表示「向下搜索」（更小的变体），leverage 表示该旋钮对 latency 的影响量级。
KNOBS = {
    "conv1_channels": {"default": 16, "min": 4, "step": -4, "leverage": "medium"},
    "conv2_channels": {"default": 32, "min": 4, "step": -8, "leverage": "high"},
    "fc_hidden":      {"default": 64, "min": 16, "step": -16, "leverage": "medium"},
}


class MnistCnn(nn.Module):
    """两层卷积 + 两层全连接的 MNIST 分类器。

    卷积分支：Conv -> BN -> ReLU -> MaxPool（28→14→7）。
    全连接分支：Flatten -> Linear -> ReLU -> Dropout -> Linear(10)。
    """

    def __init__(
        self,
        conv1_channels: int = 16,
        conv2_channels: int = 32,
        fc_hidden: int = 64,
        dropout: float = 0.25,
        num_classes: int = 10,
    ):
        super().__init__()
        self.conv1 = nn.Sequential(
            nn.Conv2d(1, conv1_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(conv1_channels),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 28 -> 14
        )
        self.conv2 = nn.Sequential(
            nn.Conv2d(conv1_channels, conv2_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(conv2_channels),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 14 -> 7
        )
        self.flat_dim = 7 * 7 * conv2_channels
        self.fc1 = nn.Linear(self.flat_dim, fc_hidden)
        self.drop = nn.Dropout(dropout)
        self.fc2 = nn.Linear(fc_hidden, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        z1 = self.conv1(x)
        z2 = self.conv2(z1)
        h = z2.flatten(1)
        h = F.relu(self.fc1(h))
        h = self.drop(h)
        return self.fc2(h)

    def feature_hook_names(self) -> list[str]:
        """供 feature-level KD 对齐的中间特征层名（卷积分支输出）。"""
        return ["conv1", "conv2"]


def build_model(**cfg) -> nn.Module:
    """实例化 MNIST CNN。cfg 取 conv1_channels / conv2_channels / fc_hidden（缺省用 KNOBS 默认值）。"""
    conv1_channels = int(cfg.get("conv1_channels", KNOBS["conv1_channels"]["default"]))
    conv2_channels = int(cfg.get("conv2_channels", KNOBS["conv2_channels"]["default"]))
    fc_hidden = int(cfg.get("fc_hidden", KNOBS["fc_hidden"]["default"]))
    dropout = float(cfg.get("dropout", 0.25))
    return MnistCnn(
        conv1_channels=conv1_channels,
        conv2_channels=conv2_channels,
        fc_hidden=fc_hidden,
        dropout=dropout,
    )


if __name__ == "__main__":
    # smoke：前向 + 输出 shape 校验。
    m = build_model()
    m.eval()
    x = torch.randn(*DUMMY_INPUT["shape"])
    with torch.no_grad():
        y = m(x)
    assert y.shape == (1, 10), y.shape
    params = sum(p.numel() for p in m.parameters())
    print(f"OK MnistCnn: conv1={m.conv1[0].out_channels}, "
          f"conv2={m.conv2[0].out_channels}, fc={m.fc1.out_features}, "
          f"params={params}, out={tuple(y.shape)}")
