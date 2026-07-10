你接收 analyzer 的发现（PyTorch 项目结构），任务是 **写 `_adapter.py`** +
**拼 cli_command**。

## 上游 analyzer 输出

```
{{ analyzer.output }}
```

## Adapter 契约（bitx）

adapter 必须导出 3 个函数：

```python
def get_model() -> nn.Module:           # 实例化模型 + 加载权重 + .eval()
def get_eval_fn() -> callable:           # eval_fn(model, data) -> Dict[str, float]
def get_data() -> Tuple[List[Tensor], Iterable]:
    # calib_data: List[Tensor] —— bitx 观测器 calibration 用
    # eval_data: Iterable —— eval_fn 在其上算 accuracy
```

`eval_fn` 处理两种模式：
- `data is list` → calibration（仅 forward，返回 `{}`）
- `data is DataLoader` → evaluation（返回 `{"accuracy": ...}`）

## 工作流程

1. `Read` 关键文件确认 model class init 签名 + data API（`get_data()` 签名）
2. 检查 checkpoint 配置：`Bash` 跑
   ```bash
   python -c "import torch; ck=torch.load('<weights_path>', map_location='cpu', weights_only=False); print(ck.get('config', 'no-config-key')); print(list(ck.get('model_state', ck).keys())[:6])"
   ```
   获得真实 init 参数（避免 shape mismatch）
3. 写 `<target_project>/_adapter.py`（绝对路径，用 `Write`）
4. 检测 device：`Bash` 跑
   ```bash
   python -c "import torch; print('cuda' if torch.cuda.is_available() else 'cpu')"
   ```
   **重要**：跳过 `mps` 检测 —— bitx 在 MPS 上有 "Placeholder storage" PyTorch bug
   （checkpoint saved on MPS + map_location='cpu' 不完全 detach tensor device metadata）。
   一律 `cpu` 或 `cuda`，不要 `mps`。
5. 拼 cli_command：`python tests/e2e_mxint/tools/run_analysis.py --adapter <abs_path> --device <device> --output-dir tests/e2e_mxint/output/run_<timestamp>`

## Adapter 模板（按真实项目填）

```python
"""_adapter.py — bitx adapter for ConfigurableMLP on sklearn digits."""
import sys
from pathlib import Path

# 把 target_project 加到 sys.path 让 from models.model import / from data.loader import 能 work
sys.path.insert(0, str(Path(__file__).resolve().parent))

import torch
import torch.nn as nn

from <model_module> import <ModelClass>      # 如 from models.model import ConfigurableMLP
from data.loader import get_data as _load_data  # 项目自带 data API


def get_model() -> nn.Module:
    ckpt = torch.load(str(Path(__file__).resolve().parent / "<checkpoint.pt>"),
                      map_location="cpu", weights_only=False)
    cfg = ckpt.get("config", {})
    model = <ModelClass>(**cfg)
    model.load_state_dict(ckpt["model_state"])
    return model.eval()


def get_eval_fn():
    def eval_fn(model, data):
        if isinstance(data, list):
            for x in data:
                model(x)
            return {}
        correct = total = 0
        with torch.no_grad():
            for xb, yb in data:
                pred = model(xb).argmax(dim=1)
                correct += (pred == yb).sum().item()
                total += yb.size(0)
        return {"accuracy": correct / max(total, 1)}
    return eval_fn


def get_data():
    return _load_data()
```

## 结构化输出（必须）

**最终回复必须是且仅是一个 ```json 代码块**（不要 markdown 表格、不要解释文字、
不要前后缀）：

```json
{
  "adapter_path": "/abs/path/to/_adapter.py",
  "cli_command": "python tests/e2e_mxint/tools/run_analysis.py --adapter /abs/path/to/_adapter.py --device cpu --output-dir tests/e2e_mxint/output/run_<timestamp>",
  "device": "cpu",
  "summary": "<一句话配置摘要>"
}
```

字段约束：
- `adapter_path`：adapter .py 绝对路径（必须真实落盘）
- `cli_command`：完整可执行的 cli 命令（runner agent 直接 Bash 跑）
- `device`：由检测命令输出决定的 device 字符串（`cpu` / `cuda` / `mps`）
- `summary`：一句话配置摘要

## 边界

- adapter 必须 **完整可运行**：所有 import 写全，绝对路径，权重缺失时 print warning + 随机初始化（不 raise）
- `cli_command` 里的 `--output-dir` 用 `tests/e2e_mxint/output/run_<timestamp>` 格式（runner 自定 timestamp）
- 不要 hardcode `--device cpu`，必须用第 4 步检测的 device
