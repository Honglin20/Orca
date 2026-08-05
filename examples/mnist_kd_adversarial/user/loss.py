"""loss.py — adversarial fixture leaf (intentionally hand-authored).

See examples/mnist_kd_adversarial/README.md. This leaf is a faithful port of
the user's compute_loss; the adversarial deviation lives in optim.py only.
"""

import torch
import torch.nn.functional as F


def compute_loss(s_out: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
    """Task loss = cross-entropy. Ported verbatim from train.py."""
    return F.cross_entropy(s_out, y)
