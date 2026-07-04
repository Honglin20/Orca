# Target Project — ConfigurableMLP on sklearn digits

mxint_analysis e2e 的**真实 PyTorch 目标项目**。bitx 量化分析的真实对象：
3 层全连接 MLP（`Linear(64,64) → ReLU → Linear(64,64) → ReLU → Linear(64,10)`），
在 sklearn digits (8x8=64 输入，10 类，1797 样本) 上训练到 ~90% 测试精度。

## 结构

- `models/model.py` — `ConfigurableMLP(nn.Module)`（bitx 量化目标）
- `data/loader.py` — `get_data()` 返回 `(calib_data, eval_loader)`（bitx adapter 契约）
- `checkpoint.pt` — 训练好的 state_dict（由 `tests/e2e_mxint/tools/train_target.py` 生成）

## 入口

- `from models.model import ConfigurableMLP` — 模型类
- `from data.loader import get_data` — bitx adapter 的数据来源
- `checkpoint.pt` — `torch.load()` 含 `model_state` / `config` / `acc`

## 重训

```bash
python tests/e2e_mxint/tools/train_target.py
```

约 5 秒（CPU），30 epoch，写到 `checkpoint.pt`（覆盖现有）。
