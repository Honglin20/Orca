"""Example evaluator used by worker.py.

The generated evaluator should copy the data, loss, optimizer, scheduler, and
metric behavior from generated train_supernet.py and the user's project code.
Only CandidateEvaluator and CandidateEvaluator.evaluate are framework-facing
interfaces; helper details should stay private to the generated file.

This example implements the "validate" paradigm. For "finetune" or
"train_from_scratch" paradigms, see the Evaluator section in
search_supernet_script_generation.md for the required flow.
"""

from typing import Any
import torch
import torch.nn as nn

from nas_agent.train import autocast, load_checkpoint

# Generated scripts should replace this with the concrete supernet import.
from supernet import SearchSpace, SuperNet, ArchConfig

# Import dataset builders and utilities from previously generated scripts
# (e.g., data_utils.py or train_supernet.py) to ensure consistency.
from data_utils import build_dataloaders


class CandidateEvaluator:
    """Project-specific candidate architecture evaluator.

    Design assumptions:
    - The Evaluator runs entirely on a single device (GPU) managed by the framework.
    - It maintains one complete supernet instance in that device at all times.
    """

    def __init__(
        self,
        *,
        device: torch.device,
        evaluator_cfg: Any = None,
    ):
        self.device = device
        self.cfg = evaluator_cfg

        # ===== Shared Evaluation Resources =====
        # Resources below are independent of the candidate architecture and
        # reused across all evaluate() calls.
        self.supernet = SuperNet(SearchSpace()).to(self.device)
        load_checkpoint(self.cfg.supernet_ckpt_path, self.supernet, self.device, strict=False)

        _, self.val_loader, num_classes = build_dataloaders(
            data_dir=self.cfg.data_dir,
            batch_size=self.cfg.batch_size,
            num_workers=self.cfg.num_workers,
        )

        self.criterion = nn.CrossEntropyLoss()

    def compute_metrics(self, output: torch.Tensor, target: torch.Tensor) -> dict[str, float]:
        """Compute smaller-is-better metrics from project evaluation logic.

        Returns a dict keyed by the quality objective names from
        `search_config.yaml` `objs` (excluding `latency`).
        """
        pred = output.argmax(dim=1)
        acc = pred.eq(target).float().mean()
        return {"acc": -acc.item()}

    def evaluate(self, arch_config: ArchConfig) -> dict[str, float]:
        """Configure the supernet and return smaller-is-better metric values.

        Returns a dict whose keys match the quality objectives in
        `search_config.yaml` `objs` (excluding `latency`).
        """
        self.supernet.set_sample_config(arch_config)

        # --- validate paradigm: run validation directly on the supernet ---
        self.supernet.eval()
        metric_sums: dict[str, float] = {}
        sample_count = 0
        use_amp = self.cfg.get("amp", False) if self.cfg else False

        with torch.no_grad():
            for inputs, target in self.val_loader:
                inputs = inputs.to(self.device, non_blocking=True)
                target = target.to(self.device, non_blocking=True)
                batch_size = target.size(0)
                sample_count += batch_size
                with autocast(self.device, enabled=use_amp):
                    output = self.supernet(inputs)

                batch_metrics = self.compute_metrics(output, target)
                for key, val in batch_metrics.items():
                    metric_sums[key] = metric_sums.get(key, 0.0) + val * batch_size

        denom = max(sample_count, 1)
        return {k: v / denom for k, v in metric_sums.items()}
