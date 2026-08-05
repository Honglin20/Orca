"""data.py — adversarial fixture leaf (intentionally hand-authored).

See examples/mnist_kd_adversarial/README.md. Ports the user's MNIST
DataLoader verbatim (including the Normalize transform).
"""

import os

from torch.utils.data import DataLoader
from torchvision import datasets, transforms

_MNIST_MEAN = (0.1307,)
_MNIST_STD = (0.3081,)
_DATA_ROOT = os.environ.get(
    "MNIST_DATA_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data"),
)


def _transform() -> transforms.Compose:
    return transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize(_MNIST_MEAN, _MNIST_STD),
        ]
    )


def build_dataloader(batch_size: int = 128):
    """Re-iterable training DataLoader (torchvision MNIST)."""
    ds = datasets.MNIST(
        _DATA_ROOT, train=True, download=True, transform=_transform()
    )
    return DataLoader(
        ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=0,
        pin_memory=False,
    )
