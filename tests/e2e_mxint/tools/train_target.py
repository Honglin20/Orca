"""train_target.py —— 训练 ConfigurableMLP 写 checkpoint.pt（一次性脚本）。

跑 sklearn digits 80/20 split，~30 epoch，Adam lr=1e-3，到 ~95% test acc。
跑完写 ``tests/e2e_mxint/target_project/checkpoint.pt``，覆盖现 stub 的 weights/。

不接收参数。退出码 0 = 训练成功。
"""
from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

# 让 from models.model import 能找到
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "target_project"))

from models.model import ConfigurableMLP  # noqa: E402
from data.loader import _load_split  # noqa: E402


def main() -> int:
    torch.manual_seed(0)
    model = ConfigurableMLP(
        in_dim=64, num_classes=10,
        hidden_dim=64, num_layers=2,
        activation="relu", use_batchnorm=False,
    )

    x_train, y_train, x_eval, y_eval = _load_split()
    train_ds = TensorDataset(x_train.reshape(-1, 1, 8, 8), y_train)
    train_loader = DataLoader(train_ds, batch_size=64, shuffle=True)
    eval_loader = DataLoader(
        TensorDataset(x_eval.reshape(-1, 1, 8, 8), y_eval),
        batch_size=64, shuffle=False,
    )

    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    best_acc = 0.0
    best_state: dict[str, torch.Tensor] | None = None
    for epoch in range(30):
        model.train()
        for xb, yb in train_loader:
            opt.zero_grad()
            out = model(xb)
            loss = loss_fn(out, yb)
            loss.backward()
            opt.step()

        model.eval()
        correct = total = 0
        with torch.no_grad():
            for xb, yb in eval_loader:
                pred = model(xb).argmax(dim=1)
                correct += (pred == yb).sum().item()
                total += yb.size(0)
        acc = correct / total
        if acc > best_acc:
            best_acc = acc
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        print(f"epoch {epoch + 1:02d}: eval_acc={acc:.4f} (best={best_acc:.4f})", flush=True)

    assert best_state is not None, "training failed: no best_state"
    out_path = Path(__file__).resolve().parents[1] / "target_project" / "checkpoint.pt"
    torch.save(
        {
            "model_state": best_state,
            "config": {
                "in_dim": 64, "num_classes": 10,
                "hidden_dim": 64, "num_layers": 2,
                "activation": "relu", "use_batchnorm": False,
            },
            "acc": best_acc,
        },
        out_path,
    )
    print(f"[train_target] saved checkpoint → {out_path} (best_acc={best_acc:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
