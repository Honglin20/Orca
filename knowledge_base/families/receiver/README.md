# receiver KB —— KD-NAS model8 变体仓

本目录是 **KD-NAS workflow 的 student 候选池**：每个 `.py` 文件是一个 model8 结构变体，
workflow 按**确定性顺序**遍历、latency-fit 调参、完整蒸馏训练。

> 历史：本目录曾是 LLM 消费的 `.md` 知识族（primitives/latency_moves/...）。重构后改为
> `.py` 变体仓——KB 机制（`index.json` agent_slices）是 LLM-prompt 驱动的、对本目录无运行时
> 依赖；workflow 用确定性脚本 `pick_variant.py` 直接 glob 本目录。

## 变体契约（每个变体 `.py` 必须暴露）

```python
from _model8_blocks import SignalProcessingTransformer   # 同目录共享积木（或自包含拷贝）
import torch.nn as nn

DUMMY_INPUT = {"shape": [1, 4, 48, 64, 1], "dtype": "float32"}  # 用户真实 I/O 维度（禁硬编码回退）
BUILD_FN = "build_model"

KNOBS = {                       # 可调旋钮：latency 超阈时 tune_latency.py 按 leverage 高→低缩容
    "num_blocks": {"default": 3, "min": 1, "step": -1, "leverage": "high"},   # step<0 必填
    "embed_dim":  {"default": 16, "min": 8, "step": -4, "leverage": "medium"}, # leverage∈{high,medium,low}
}

def build_model(**cfg) -> nn.Module: ...   # 零参用 KNOBS.default；cfg 覆盖旋钮
# feature_hook_names() 由共享基类提供（OFD/FitNets 特征对齐）；自包含变体自行实现。
```

### 规则
- **文件名 = variant_id**（stem）。`pick_variant.py` 按文件名排序遍历；stem 必须唯一（撞 → fail loud）。
- **`_*.py` 是共享模块**（如 `_model8_blocks.py`），不是变体候选（glob 排除）。
- **KNOBS**：`step` 必须 `<0`（缩容方向）；`leverage` 必须 ∈ `{high,medium,low}`（缩的优先级）；
  `min` 是地板。无 `KNOBS` 的变体视为不可调（超阈即 FAIL_latency）。
- **DUMMY_INPUT**：用户按真实模型 I/O 指定；改模型维度时同步改这里。workflow 全程透传给
  `export_onnx`，**禁硬编码 shape 回退**。
- **自包含 vs 共享**：默认 `from _model8_blocks import ...`（DRY）；若变体结构与积木差异大，
  可自包含拷贝积木（仍须暴露上述契约）。`tune_latency.py` / `train_kd.py` 加载变体时会把本目录
  加入 `sys.path`，故 `from _model8_blocks import` 可用。

## seed 变体

- `spt_t1.py` —— 全 t1（symbol 轴 attention）。
- `spt_alt.py` —— t1/t2 交替（symbol/subcarrier 轴 attention 轮换）。

用户后续往本目录加变体 `.py` 即可，workflow 自动纳入 sweep（无需改 workflow / 索引）。

## teacher 不在此处

teacher（10 层 t1/t2 交替）是 KD 软标签源，**非候选**，单独放
`workflows/agents/_kd_scripts/teacher_model.py`。
