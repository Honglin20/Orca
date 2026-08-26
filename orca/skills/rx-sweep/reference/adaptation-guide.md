# 适配指南（rx-sweep）

> 把用户的训练工程适配成可批量实验。接口/格式见 `contracts.md`。

## 用户工程假设

- 入口 `train.py`，导入 `utils/<实际训练脚本>.py` 做训练。
- 模型在某处实例化（`utils/` 或 `model.py`），输入 `[B,4,48,64,1]`，输出同形（OFDM 接收机自编码器）。
- 有自己的 loss / optimizer / scheduler / eval metric / 数据管道。

## 铁律：用户逻辑即不可替代权威

适配**只做两件事**，绝不越界：

1. **加 variant 选择**：训练入口接受 `--variant <v>`（+ 可选 `--kd` / `--teacher-ckpt`），据此选 `build_model(variant=...)` 实例化模型（用本 skill 的 `pure_cnn_model.py`）。
2. **加 GATE 打印**：载入模型后、训练循环前，打印一行 `[RX-GATE] ...`（格式见 contracts.md §3），`gate=PASS` 仅当 I/O shape 对齐 + 一次前向 smoke 通过。

**禁止**：
- 替换用户的 loss 公式 / 常量。
- 换 optimizer / scheduler 类。
- 改 eval metric 的名 / 方向 / 变换。
- 改数据管道。
- 用随机数冒充数据/标签。

用户的 loss/metric 是 ground truth——你只包装，不改写。

## 两种适配方式

### 方式 A（优先）：固化包装器 `rx_runner.py`

跑 `adapt_project.py --project-root <工程>`。它生成 `<工程>/rx_runner.py`，包装用户训练脚本：
- 解析 `--variant/--kd/--teacher-ckpt`。
- 用 `pure_cnn_model.build_model(variant=...)` 建模型，替换用户脚本里的模型实例化点（或注入）。
- 打印 `[RX-GATE]` 行。
- 其余（loss/optim/data/eval）**回调用户原脚本**，不动。

`sweep` 时 `launch_sweep.py` 调 `python rx_runner.py --variant ... --kd ...`。

### 方式 B：脚本够不到 → 手工适配

用户训练脚本结构特殊（模型实例化点不标准 / 深嵌），`adapt_project.py` 自动模式够不到时：
1. Read 用户的 `utils/<训练脚本>.py`，定位模型实例化点（grep `model = ` / `Model(` / `build`）。
2. Edit：把实例化改成 `from rx_sweep_models.pure_cnn_model import build_model; model = build_model(variant=args.variant, ...)`。
3. 加 `--variant` 等 argparse 项。
4. 加 `[RX-GATE]` 打印（载入模型后立即打，先做一次 `model(dummy)` 验 I/O，gate 据 smoke 结果 PASS/FAIL）。
5. KD：若 `--kd`，用 `kd_helper.KDHelper` 包，训练步里把 `loss = task_loss(...)` 改成 `loss = kd_helper(model, x, task_loss, y)`。

> 用户原 forward / 训练步结构尽量不动——只在模型实例化点 + loss 计算点插桩。

## 验证适配成功

跑 `gate_check.py --project-root <工程> --variant pure_cnn`：
- 打印 `[GATE-RESULT] passed=true` = 适配对（I/O 对齐 + smoke 通）。
- `passed=false` = 按 reason 修（常见：模型实例化点没换干净、I/O 不一致、dummy 输入 dtype 错）。

全 variant 过 gate 才进 sweep。
