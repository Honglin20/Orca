# kd_anneal_scheduler.py.md — D11/D12/D13/D14/D15 通用：三段 anneal λ 调度器

> **这是什么 / 一句话**：KD 权重 λ 的三段 anneal 调度器——warmup（task loss 主导，KD 权重低）→ KD 主推（KD 权重升至目标）→ task 收尾（KD 权重降回 0，让 task loss 主导精修）。适配所有 KD 家族（D11-D15），复用 CONTRACTS §3 的 `KDWeightScheduler`。

---

## 为什么需要 anneal

- **warmup**：student 起始随机，teacher 软目标会把它拉到一个无意义的"输出尺度对齐"区域；warmup 让 student 先用 task loss 找到大致方向。
- **KD 主推**：student 已学到 task 基本结构后，KD 软目标帮助迁移 teacher 的 dark knowledge / 关系结构 / 特征抽象。
- **task 收尾**：最后几个 epoch 降 λ_KD → 0，让 task loss（hard label）主导，避免 KD 过度平滑导致 student 在 task metric 上掉点。

---

## 可跑骨架（对齐 CONTRACTS §3 `KDWeightScheduler`）

```python
import math
from dataclasses import dataclass
from typing import Dict


@dataclass
class KDWeightScheduler:
    """三段 anneal：
        epoch < warmup_end           → 线性 ramp 0 → target
        warmup_end ≤ epoch < cool_start → 保持 target
        cool_start ≤ epoch < epochs   → 线性 cool target → 0
        epoch == epochs-1              → 0（最后一 epoch 纯 task loss）

    target: dict of {loss_name: weight_at_plateau}
        例: {"mse": 1.0, "rkd": 0.1, "ofd": 0.3, "cons": 0.5}

    使用:
        sched = KDWeightScheduler(
            target={"mse": 1.0, "rkd": 0.1},
            warmup_end=5, cool_start=40, epochs=50,
        )
        for epoch in range(epochs):
            weights = sched.get_weights(epoch)
            # weights: dict[str, float]
    """
    target: Dict[str, float]
    warmup_end: int = 5
    cool_start: int = 40
    epochs: int = 50

    def get_weights(self, epoch: int) -> Dict[str, float]:
        if epoch < self.warmup_end:
            # warmup: linear ramp 0 → target
            ratio = (epoch + 1) / self.warmup_end
            return {k: v * ratio for k, v in self.target.items()}
        elif epoch < self.cool_start:
            # plateau
            return dict(self.target)
        elif epoch < self.epochs - 1:
            # cool: linear target → 0
            total = self.epochs - 1 - self.cool_start
            elapsed = epoch - self.cool_start
            ratio = max(0.0, 1.0 - elapsed / max(total, 1))
            return {k: v * ratio for k, v in self.target.items()}
        else:
            # 最后一 epoch: 纯 task loss
            return {k: 0.0 for k in self.target}

    # CONTRACTS §3 签名：单 loss 版本（保留兼容）
    def get_weight(self, epoch: int) -> float:
        """单 loss 的标量版（CONTRACTS §3 兼容签名）。
        返回 target 中第一个 loss 的权重。
        """
        ws = self.get_weights(epoch)
        if not ws:
            return 0.0
        return next(iter(ws.values()))


# ============================================================
# 变体：cosine anneal（比三段更平滑）
# ============================================================
@dataclass
class CosineKDWeightScheduler:
    """cosine anneal：epoch=0 时 λ=0，epoch=epochs-1 时 λ=0，中间 cosine 升降。
    target 在中点达到。
    """
    target: Dict[str, float]
    epochs: int = 50

    def get_weights(self, epoch: int) -> Dict[str, float]:
        if self.epochs <= 1:
            return dict(self.target)
        # 0 → 1 → 0 的 cosine：用 sin(π·t) 其中 t=epoch/(epochs-1)
        t = epoch / (self.epochs - 1)
        ratio = math.sin(math.pi * t)
        return {k: v * ratio for k, v in self.target.items()}


# ============================================================
# smoke test
# ============================================================
if __name__ == "__main__":
    import numpy as np
    sched = KDWeightScheduler(
        target={"mse": 1.0, "rkd": 0.1, "ofd": 0.3},
        warmup_end=5, cool_start=40, epochs=50,
    )
    print("epoch | mse   rkd    ofd")
    for e in [0, 2, 5, 20, 40, 45, 49]:
        w = sched.get_weights(e)
        print(f"{e:5d} | {w['mse']:.3f}  {w['rkd']:.3f}  {w['ofd']:.3f}")

    print("\nCosine variant:")
    sched2 = CosineKDWeightScheduler(target={"rkd": 0.1}, epochs=50)
    for e in [0, 12, 25, 37, 49]:
        w = sched2.get_weights(e)
        print(f"{e:5d} | rkd={w['rkd']:.3f}")
```

---

## 变异提示（不要照抄）

- **三段 vs cosine**：三段更可控（plateau 期明确），cosine 更平滑；默认三段；若调参时发现 plateau 期 KD 过度平滑，换 cosine。
- **warmup_end**：默认 5 epoch（约 10% 总训练）；student 起始随机性强时延长到 10；student 从 warm-start ckpt 起时可缩短到 2。
- **cool_start**：默认 epochs-10；过早 cool 让 KD 时间不够；过晚 cool 让 student 在最后还受 teacher 软目标约束，掉 task 精度。
- **最后一 epoch 强制 0**：避免 teacher 软目标在最后一步把 student 拉偏；可在 measure 时确保 student 看的是纯 task loss 的输出。
- **target 各 loss 独立 anneal**：本骨架所有 loss 共用一个 ratio（同步升/降）；变体——让 cons loss（D15 Mean-Teacher）延迟 warmup（student 稳定后才启用一致性），避免初期影子未稳定。
- **CONTRACTS §3 兼容**：`get_weight(epoch)` 单 loss 版本保留（CONTRACTS §3 原签名），但**推荐用 `get_weights(epoch)` 多 loss 版本**——kd-nas workflow 的 SelectionSpec 通常组合多个 KD loss。
- **短训 proxy 不 anneal**：kd-nas Phase1 短训（10 epoch）不做 anneal——直接固定 `target` 全程（cool 无意义），省调度复杂度。
- **fail-loud**：若 `warmup_end > cool_start`，逻辑错乱；`__post_init__` 加 assert（本骨架省略，用前自检）。
