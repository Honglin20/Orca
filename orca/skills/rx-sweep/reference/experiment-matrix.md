# 实验矩阵（rx-sweep）

> 接口/schema 见 `contracts.md`。本文件讲矩阵设计 + 每个优化点的预期。

## 矩阵

纯 CNN 族 4 个优化点组合 × {从头训, 蒸馏}，加 model8 baseline 参考，共 9 实验：

| exp_id | variant | pilot | lmmse | kd | 说明 |
|---|---|---|---|---|---|
| `model8_baseline` | model8 | — | — | — | 原模型参考线（仅 fixture；用户工程若有原模型也可纳入） |
| `pure_cnn_scratch` | pure_cnn | off | off | no | 纯 CNN 替 attention：时间 dilated conv。基线优化点 |
| `pure_cnn_kd` | pure_cnn | off | off | yes | 同上，蒸馏版（teacher=model8） |
| `pure_cnn_pilot_scratch` | pure_cnn_pilot | on | off | no | + 输入富化 [Y, Y⊙Xp*, Xp, mask] |
| `pure_cnn_pilot_kd` | pure_cnn_pilot | on | off | yes | 同上蒸馏 |
| `pure_cnn_lmmse_scratch` | pure_cnn_lmmse | off | on | no | + LMMSE 前置，NN 学残差 |
| `pure_cnn_lmmse_kd` | pure_cnn_lmmse | off | on | yes | 同上蒸馏 |
| `pure_cnn_pilot_lmmse_scratch` | pure_cnn_pilot_lmmse | on | on | no | 两特征处理全开 |
| `pure_cnn_pilot_lmmse_kd` | pure_cnn_pilot_lmmse | on | on | yes | 同上蒸馏 |

## 优化点逻辑（为什么这样切）

- **纯 CNN（替 attention）**：原结构频率轴 conv（局部）+ symbol 轴 attention（全局时间相关）。把 attention 换成时间 dilated conv（dilation 跨 block 翻倍，RF 覆盖 64 symbol）→ 全 conv，消 conv↔matmul 格式切换的开销。频率 k=3 conv 原样保留（局部先验，承重不动）。
- **pilot 富化**：把导频信息显式拼进输入通道，模型不用自己「找导频」。近零精度风险。
- **LMMSE 前置**：闭式线性均衡把线性部分解掉，NN 只学非线性残差 → NN 可更小更快。特征处理在模型 forward 内，I/O 不变。
- **蒸馏**：teacher=训好的原模型，student=优化后小模型，输出 MSE + FitNets 特征对齐补精度。每个优化点都跑 scratch + kd 两版，对比蒸馏增益。

## 跑法

- 8 卡：一批 8 个并行，余 1 个。`launch_sweep.py` 自动分卡。
- KD 实验需 `teacher_ckpt`（用户训好的原模型）。无 → 标 SKIP，仍跑 scratch。
- 结果落 `results.jsonl`（schema 见 contracts.md §4），含 accuracy / latency_ms / gate_passed / status。

## 读图

- pareto（latency×accuracy）：非支配前沿，左下优（latency 低 + 精度高，方向依 accuracy_kind）。
- bar（variant×accuracy，hue=kd）：直观对比每优化点 + 蒸馏增益。
- table：全实验总表（accuracy/latency/status/gate）。
