"""DDP-aware metric accumulation utilities."""

import torch
import torch.distributed as dist

from nas_agent.train.distributed import is_distributed


class AverageMeter:
    """Tracks a running sum and count with optional cross-rank reduction.

    Works in both distributed and non-distributed contexts. When a
    process group is initialized, `.avg`, `.sum`, and `.count`
    `all_reduce` across ranks before returning. Otherwise they return
    local values directly.

    Example::

        meter = AverageMeter(device)
        for inputs, targets in val_loader:
            loss = criterion(model(inputs), targets)
            meter.update(loss.item(), n=inputs.shape[0])

        print(meter.avg)   # global average (all_reduce if distributed)

    Args:
        device: Device for the reduction tensor.
    """

    def __init__(self, device: torch.device) -> None:
        self._device = device
        self._sum = 0.0
        self._count = 0
        self._cache: tuple[float, int] | None = None

    def update(self, value: float, n: int = 1) -> None:
        """Accumulate one observation.

        Args:
            value: Per-sample metric mean for this batch.
            n: Number of samples.
        """
        self._sum += value * n
        self._count += n
        self._cache = None

    def _reduced(self) -> tuple[float, int]:
        if self._cache is not None:
            return self._cache
        s, c = self._sum, self._count
        if is_distributed():
            # float32 for NPU (HCCL) compatibility; sufficient for metric sums
            data = torch.tensor([s, c], device=self._device, dtype=torch.float32)
            dist.all_reduce(data, op=dist.ReduceOp.SUM)
            s, c = data[0].item(), int(data[1].item())
        self._cache = (s, c)
        return self._cache

    @property
    def avg(self) -> float:
        """Per-sample average (globally reduced when distributed)."""
        s, c = self._reduced()
        return s / c if c > 0 else 0.0

    @property
    def count(self) -> int:
        """Sample count (globally reduced when distributed)."""
        return self._reduced()[1]

    def reset(self) -> None:
        """Zero accumulators for the next round."""
        self._sum = 0.0
        self._count = 0
        self._cache = None


def format_params(n: int) -> str:
    """Format a parameter count for human-readable display.

    Args:
        n: Raw parameter count (number of scalar parameters).

    Returns:
        Formatted string with auto-selected unit suffix (M, K, or raw).
    """
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
