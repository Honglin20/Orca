# mnist_kd —— 一个 PyTorch MNIST 分类项目

这是一个**普通的 PyTorch MNIST 手写数字分类项目**：LeNet 风格两层 CNN，
真用 torchvision MNIST、真 cross-entropy 训练、真在 test set 上算 accuracy（>99%）。
CPU 即可训练，数据自动下载到 `./data/`。

> 项目本身的形态（暴露 `build_model` / `DUMMY_INPUT` / `KNOBS` / `feature_hook_names` 的模型入口 +
> 暴露 `compute_loss` / `build_dataloader` 的训练脚本 + ONNX latency 探针）刚好能作为 kd-nas
> 结构蒸馏 workflow 的真实输入项目。详见文末「作为 kd-nas 输入」。

---

## 目录

```
mnist_kd/
├── model.py              # MnistCnn + build_model + DUMMY_INPUT/KNOBS/feature_hook_names
├── train.py              # compute_loss/build_dataloader/build_optimizer + 训练入口
├── eval.py               # test set top-1 accuracy
├── latency_provider.py   # ONNX 单次推理 latency（onnxruntime 实测，us）
├── requirements.txt
├── data/                 # torchvision 自动下载 MNIST（gitignore）
└── README.md
```

## 安装

```bash
pip install -r requirements.txt
```

CPU 版 torch 即可（MNIST 很小，一个 epoch 几秒到几十秒视机器而定）。

## 使用

### Smoke：模型前向校验

```bash
python -c "from model import build_model, DUMMY_INPUT; import torch; \
m = build_model(); print(m(torch.randn(*DUMMY_INPUT['shape'])).shape)"
# 期望输出：torch.Size([1, 10])
```

### 训练

```bash
python train.py --epochs 10          # 默认 batch_size=128, lr=1e-3, device=cpu
```

每个 epoch 打印 `loss` 和 `test_acc`，训练结束保存 checkpoint 到 `mnist_cnn.pt`。
典型 10 epoch 后 test accuracy ≈ 0.99。

### 评估

```bash
python eval.py --ckpt mnist_cnn.pt   # 打印 ACCURACY / ACCURACY_KIND
```

### Latency 探针

需要先导出 ONNX（任意标准导出方式），再调 latency_provider：

```bash
python -c "import torch; from model import build_model, DUMMY_INPUT; \
m = build_model().eval(); x = torch.randn(*DUMMY_INPUT['shape']); \
torch.onnx.export(m, x, 'mnist.onnx', input_names=['input'], output_names=['logits'], \
dynamic_axes={'input': {0: 'B'}, 'logits': {0: 'B'}}, opset_version=17)"

python latency_provider.py --onnx mnist.onnx    # 打印 LATENCY_US
```

## 模型结构与可调旋钮

`MnistCnn` 是 LeNet 风格：

```
input  [B, 1, 28, 28]
  conv1: Conv2d(1,  C1, 3, pad=1) -> BN -> ReLU -> MaxPool(2)   # -> [B, C1, 14, 14]
  conv2: Conv2d(C1, C2, 3, pad=1) -> BN -> ReLU -> MaxPool(2)   # -> [B, C2, 7, 7]
  flatten                                                        # -> [B, 7*7*C2]
  fc1: Linear(7*7*C2, H) -> ReLU -> Dropout
  fc2: Linear(H, 10)                                             # logits
```

`KNOBS`（`model.py` 顶部声明）：

| 旋钮 | default | min | step | leverage |
|---|---|---|---|---|
| `conv1_channels` | 16 | 4 | -4 | medium |
| `conv2_channels` | 32 | 4 | -8 | high |
| `fc_hidden`      | 64 | 16 | -16 | medium |

`step<0` 表示「向下搜索更小的变体」，`leverage` 表示该旋钮对 latency 的影响量级。

`feature_hook_names()` 返回 `["conv1", "conv2"]`，即两个卷积分支的输出——可用于 feature-level
知识蒸馏的对齐目标。

---

## 作为 kd-nas 输入

本项目可作为 kd-nas 结构蒸馏 workflow 的真实输入项目，建议的 workflow inputs：

| input | 值 / 来源 |
|---|---|
| `model_entry` | `<abs>/examples/mnist_kd/model.py::build_model` |
| `latency_provider` | `<abs>/examples/mnist_kd/latency_provider.py::measure` |
| `accuracy_baseline` | 0.90（建议；10 epoch baseline 通常 ≈ 0.99，0.90 是宽松门） |
| `accuracy_baseline_kind` | `acc`（top-1 accuracy，越大越好） |
| `target_latency_us` | 先实测 baseline ONNX latency，再按压缩目标定（如 baseline × 0.7） |

`accuracy_baseline_kind=acc` 是「accuracy 越大越好」方向，kd-nas 据此判定 student 是否达标。
