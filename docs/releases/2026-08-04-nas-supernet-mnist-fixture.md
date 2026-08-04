# 2026-08-04 — nas-supernet E2E MNIST fixture

## 背景

`nas-supernet` workflow（计划 `docs/plans/2026-08-04-nas-agent-pipeline-rebuild.md`，§17）需要
一个标准 PyTorch MNIST 项目作为 E2E 真实输入：`expand_to_supernet` 节点要把它「张开成可搜索超网」，
后续 train/search/select/retrain/visualize 全链以此为目标。MNIST 小、CPU/单 GPU 可跑，是理想靶子。

## 复用判定

全仓搜索命中既有 `examples/mnist_kd/`（kd-nas 专用）。评估后**不复用其契约面**，仅复用其 CNN 架构设计：

- `mnist_kd` 是 kd-nas 的真实输入，暴露 `KNOBS`（min/step/leverage）/ `feature_hook_names` /
  `DUMMY_INPUT` / `latency_provider` 等 **kd-nas 专属契约**。
- nas-supernet 的 `expand_to_supernet` 设计意图是「从**普通用户模型**里发现可搜索维度」。若喂入
  预先 instrument 的 KNOBS，E2E 就不反映真实用户场景，且会把 kd-nas 与 supernet 两套契约混淆。
- 故新建一个**干净的普通用户项目**：CNN 架构（2 conv block + FC head，参数化 conv1/conv2/fc_hidden）
  借鉴 `examples/mnist_kd/model.py`（已验证可被张开），但去掉所有 kd-nas 装饰。

## 落点

`tests/e2e_nas_supernet/fixtures/mnist/` —— 对齐 `tests/e2e_mxint/target_project/`（E2E 目标项目
置于 `tests/e2e_<name>/` 下的先例），把测试 fixture 与用户向 `examples/` 分开，且明确「测试专用」。
计划 §17 同时允许 `examples/mnist_nas/` 与 `tests/e2e_nas_supernet/fixtures/mnist/`，本处选后者。

## 文件清单（6 files，commit `bfe857f`）

| 文件 | 作用 |
|---|---|
| `model.py` | `MnistCNN`（Conv-BN-ReLU-MaxPool×2 + FC head）+ `build_model` 工厂 + `count_parameters` + `__main__` 前向 smoke。构造参数 `conv1_channels`/`conv2_channels`/`fc_hidden`/`num_classes`/`dropout` 即 expand 节点的天然搜索维度。纯标准 op，sandwich-rule 友好。 |
| `train.py` | `compute_loss`(CE) / `build_dataloader`(torchvision MNIST，`download=True`；离线降级 `torch.randn` 小张量，stderr 显式 WARNING，**仅冒烟**) / `build_optimizer`(Adam) / `train_one_epoch` + CLI 入口。默认 `epochs=2/batch=128/lr=1e-3`，CPU 可快速跑完。 |
| `test.py` | `evaluate(model, loader, device)` 返回 top-1 acc + CLI 入口；打印 `ACCURACY` / `ACCURACY_KIND: acc`。 |
| `requirements.txt` | `torch>=2.0` / `torchvision>=0.15`（标准 DL 栈，**无**无线/通信依赖）。 |
| `.gitignore` | `data/` / `*.pt` / `*.onnx` / `__pycache__/`。 |
| `README.md` | fixture 说明 + 用法 + 离线降级说明 + 作为 nas-supernet inputs 的映射表（对齐计划 §12）。 |

## 如何跑

```bash
cd tests/e2e_nas_supernet/fixtures/mnist
pip install -r requirements.txt
python model.py                 # 前向 smoke：OK MnistCNN ... out=(1, 10)
python train.py --epochs 2      # 每 epoch 打印 loss + test_acc；存 mnist_cnn.pt
python test.py --ckpt mnist_cnn.pt   # 打印 ACCURACY / ACCURACY_KIND
```

真实 MNIST 下 2~3 epoch test_acc ≈ 0.99。离线/沙箱无网时 `build_dataloader` 显式降级为随机张量，
脚本仍 exit 0 跑通 forward/backward/save 路径（accuracy 无意义，仅冒烟）。真实 E2E 须在有网机器跑真 MNIST。

## 设计要点

- **pathlib 铁律**：所有路径用 `pathlib.Path`，`torch.save/load`、`datasets.MNIST` 仅在 API 边界 `str()` 转换；零 f-string/字符串拼路径。
- **fail loud**：`train_one_epoch` 与 `evaluate` 均在空 loader 时 `raise RuntimeError`；`test.py` 缺 checkpoint 时 exit 2 + stderr；`--epochs<1`/`--batch_size<1` 经 `parser.error` 拒绝；离线降级打 stderr WARNING（非静默）。
- **循环依赖**：`train.py`/`test.py` 模块级零相互 import；`from test import evaluate` / `from train import build_dataloader` 均在各自 `main()` 内 lazy import，运行期无环。
- **英文标识符/注释**（项目约定）；README 双语对齐既有 fixture 风格。

## 验证

- **静态**：经 `code-reviewer` 一轮静态审计（本机无 Python 解释器，仅 Windows Store stub，无法实跑）。
- **review 闭环**（4 项修复 + 1 项对齐）：
  - M1 `train_one_epoch` 空 loader 静默返回 0.0 → 改 raise（fail loud，且与 `evaluate` 内部一致）。
  - S1 argparse 边界：`--epochs<1` / `--batch_size<1` 拒绝（train.py + test.py 对齐）。
  - N1 `torch.load(..., weights_only=True)`（仅加载 state_dict，安全 + 去 deprecation 警告）。
  - N2 `_random_loader` 注释 10 类 ↔ `MnistCNN.num_classes`。
- **未实跑**：环境无 Python 解释器；运行期正确性靠静态追踪 + lazy-import 环路验证 + 既有 `examples/mnist_kd` 同构架构的运行经验背书。首次真实 E2E 由后续 test-agent 阶段（计划 §18）执行。

## 偏离 / 决策

- **保留文件名 `test.py`**（code-reviewer 建议 S2 改 `eval.py`）：任务说明与计划 §17 均显式指定 `test.py`；`from test import evaluate` 仅在 fixture 目录用 `python train.py` 原地运行时触发（`sys.path[0]` = fixture 目录），最坏情况是外部误 import 时的显式 `ImportError`（非静默损坏）。显式 spec 优先于约定论据。
- **不附 `test_smoke.py`**（reviewer 亦建议 skip）：fixture 自带 `model.py __main__` 前向 smoke + train/test CLI，且计划 §18 的 test-agent E2E 才是真正验收；按 YAGNI 不重复造（kd-nas-demo 的 `test_smoke.py` import-by-path 仪式对本单一模型 fixture 收益有限）。
- **不改 `workflows/` / `orca/`**：任务限定仅建 fixture；nas-supernet.yaml 与各 agent.md 由计划 §15 后续阶段实现。
- **不动 CURRENT.md**：当前活跃任务为 KD-NAS Trainer Phase 2；本 fixture 是 nas-supernet 计划（尚未开工实现）的前置准备，属独立子交付，仅入 CHANGELOG 索引。
