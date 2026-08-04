# mnist —— nas-supernet workflow 的 E2E 目标项目

一个**普通的 PyTorch MNIST 手写数字分类项目**，作为 `nas-supernet` workflow
（`workflows/nas-supernet.yaml`，在建）E2E 测试的**真实输入**：标准的 LeNet 风格两层 CNN，
torchvision MNIST、cross-entropy 训练、test set accuracy。CPU/单 GPU 可跑，数据自动下载到 `./data/`。

> 本 fixture 的设计意图就是「像一个普通用户项目」——**不暴露任何 kd-nas / nas-agent
> 专属契约**（无 KNOBS / feature_hook_names / DUMMY_INPUT / latency_provider）。这样
> `expand-to-supernet` 节点才能真实地「从用户模型里发现可搜索维度」（conv 通道宽度、FC 隐层宽度），
> E2E 才能反映真实用户场景。CNN 架构借鉴同仓 `examples/mnist_kd/model.py`（已验证可被张开），
> 但去掉了 kd-nas 特有的装饰。

---

## 目录

```
mnist/
├── model.py            # MnistCNN + build_model（小 CNN，2 conv block + FC head）
├── train.py            # compute_loss / build_dataloader / 训练入口（含离线降级）
├── test.py             # evaluate(model, loader, device) + 命令行评估入口
├── requirements.txt    # torch / torchvision（标准 DL 栈，不依赖无线/通信）
├── .gitignore
├── data/               # torchvision 自动下载 MNIST（gitignore）
└── README.md
```

## 安装

```bash
pip install -r requirements.txt
```

CPU 版 torch 即可（MNIST 很小，一个 epoch 数秒到数十秒视机器而定）。

## 使用

### Smoke：模型前向校验

```bash
python model.py
# 期望输出：OK MnistCNN conv1=16 conv2=32 fc=64 params=... out=(1, 10)
```

### 训练

```bash
python train.py --epochs 2          # 默认 batch_size=128, lr=1e-3, device=cpu
```

每个 epoch 打印 `loss` 和 `test_acc`，训练结束保存 checkpoint 到 `mnist_cnn.pt`。
真实 MNIST 下 2~3 epoch 即可达 ~0.99 的 test accuracy（足够 E2E 烟测）。

### 评估

```bash
python test.py --ckpt mnist_cnn.pt   # 打印 ACCURACY / ACCURACY_KIND
```

### 离线降级（冒烟用）

`build_dataloader` 先尝试 torchvision MNIST（`download=True`）。若取数失败（离线 / 沙箱无网络），
**显式打 stderr 警告**后退化为 `smoke_n=1024` 个 `torch.randn` 张量，保证 `python train.py`
始终能跑通 forward/backward/save 路径。**此模式仅用于冒烟**——随机数据下 accuracy 无意义；
真实 E2E 必须在有网机器上跑真 MNIST，accuracy 才有意义。

## 模型结构

```
input  [B, 1, 28, 28]
  conv1: Conv2d(1,  C1, 3, pad=1) -> BN -> ReLU -> MaxPool(2)   # -> [B, C1, 14, 14]
  conv2: Conv2d(C1, C2, 3, pad=1) -> BN -> ReLU -> MaxPool(2)   # -> [B, C2, 7, 7]
  flatten                                                        # -> [B, 7*7*C2]
  fc1: Linear(7*7*C2, H) -> ReLU -> Dropout
  fc2: Linear(H, num_classes)                                    # logits
```

构造函数参数化：`conv1_channels` / `conv2_channels` / `fc_hidden` / `num_classes` / `dropout`。
这些通道/隐层宽度正是 `expand-to-supernet` 会张开成搜索空间的天然维度。结构只用标准
`Conv2d/BN/ReLU/MaxPool/Linear/Dropout`，无自定义 op，sandwich-rule 友好。

## 作为 nas-supernet 输入

`workflows/nas-supernet.yaml`（计划见 `docs/plans/2026-08-04-nas-agent-pipeline-rebuild.md` §12 / §17）
的 inputs 指向本 fixture：

| input | 值 / 来源 |
|---|---|
| `user_project_root` | `<abs>/tests/e2e_nas_supernet/fixtures/mnist` |
| `model_path` | `<abs>/tests/e2e_nas_supernet/fixtures/mnist/model.py` |
| `target_latency_ms` | 先实测全开超网时延，再按压缩目标定（如 baseline × 0.7） |
| `latency_script_path` | （可选）本 fixture 不自带；未提供则用 nas-agent 内置 onnx 时延 |
| `seed` | `0` |

> `<abs>` = 本仓库根的绝对路径。
