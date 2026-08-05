"""optim.py — adversarial fixture leaf (intentionally hand-authored).

See examples/mnist_kd_adversarial/README.md.

ADVERSARIAL DEVIATION:
  The user's train.py::build_optimizer constructs ``torch.optim.Adam(params,
  lr=lr)`` — i.e. ``weight_decay`` defaults to 0. This leaf constructs
  ``torch.optim.Adam(params, lr=lr, weight_decay=1e-3)`` — same class name
  (so the deterministic L3 check ``OPT_TYPE_OK`` PASSES, since it compares
  only the class name) but a different ``weight_decay`` kwarg, which is a
  caller-visible semantic drift in the optimizer's regularization strength.
  The semantic fidelity audit (project-fidelity-verifier-kd) compares
  optimizer kwargs, not just the class name, and is expected to flag this
  as a Static Fidelity finding.
"""

import torch


def build_optimizer(params, lr):
    return torch.optim.Adam(params, lr=lr, weight_decay=1e-3)


def build_scheduler(optimizer, epochs):
    return torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(epochs, 1)
    )
