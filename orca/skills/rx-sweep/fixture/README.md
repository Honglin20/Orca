# rx-sweep fixture

这是 **rx-sweep skill 的端到端测试用假工程（fixture）**，不是真实接收机训练代码。
所有数据由 `torch.manual_seed(0)` 固定，结果确定性可复现。

## 文件
- `model8_baseline.py` —— 脱敏的 attention 参考模型（KD teacher 结构源）。
- `pure_cnn_model.py` —— 纯 CNN 族（DualAxisConvBlock）：`pure_cnn` / `_pilot` / `_lmmse` / `_pilot_lmmse`。
- `kd_helper.py` —— 自包含 KD 包装（FitNets 风格输出 + 特征双蒸馏）。
- `train.py` —— 入口（导入 `utils/train_rx.py`）。
- `utils/train_rx.py` —— 确定性假训练脚本，打印 `[RX-GATE]` / `[train]` / `[RESULT]` 供 gate_check / launch_sweep 解析。
- `data/gen.py` —— 生成 tiny 合成 OFDM `.pt`（可选数据管线演示）。

## 跑

在 fixture 目录下：

```
python train.py --variant pure_cnn --epochs 1
python train.py --variant model8 --epochs 1
python train.py --variant pure_cnn_pilot --epochs 1
python train.py --variant pure_cnn --kd --epochs 1
python train.py --variant pure_cnn_pilot_lmmse --kd --teacher-ckpt <假ckpt> --epochs 1
```

可选：先跑 `python data/gen.py` 生成 `data/ofdm_tiny.pt`，再 `python train.py --variant pure_cnn --data data/ofdm_tiny.pt --epochs 1`。

## stdout 契约（contracts §3 / §4）
- 载入后第一行 `[RX-GATE] variant=... pilot=on|off lmmse=on|off kd=on|off ... gate=PASS|FAIL`
- 每轮 `[train] epoch=N loss_avg=F`
- 末行 `[RESULT] exp_id=... accuracy=... accuracy_kind=nmse latency_ms=... status=SUCCESS`
