"""Example evaluator used by worker.py.

The generated evaluator should copy the data pipeline and validation-metric
behavior from generated train_supernet.py and the user's project code.
Only CandidateEvaluator and CandidateEvaluator.evaluate are framework-facing
interfaces; helper details should stay private to the generated file.

This example implements the fixed "validate" paradigm — zero-training
evaluation. The supernet checkpoint (produced by the upstream KD training
run) is loaded strictly once; each candidate is a per-slot choice path
applied via set_sample_config and evaluated by direct inference on the
supernet's weights. The evaluator contains no optimizer, no training loop,
no checkpoint writes, no teacher, and no KD loss.
"""

from typing import Any
import torch
import torch.nn as nn

from nas_agent.search.arch_utils import hash_arch
from nas_agent.train import autocast, load_checkpoint
from nas_agent.train.metrics import AverageMeter

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
        # strict=True: the KD training contract saves the full-module
        # state_dict, so any key mismatch is a real contract break and must
        # fail loud here (a silent partial load corrupts the ranking).
        load_checkpoint(self.cfg.supernet_ckpt_path, self.supernet, self.device, strict=True)

        _, self.val_loader, num_classes = build_dataloaders(
            data_dir=self.cfg.data_dir,
            batch_size=self.cfg.batch_size,
            num_workers=self.cfg.num_workers,
        )

        self.criterion = nn.CrossEntropyLoss()

    def evaluate(self, arch_config: ArchConfig) -> dict[str, float]:
        """Configure the supernet and return smaller-is-better metric values.

        Returns a dict whose keys match the quality objectives in
        `search_config.yaml` `objs` (excluding `latency`).
        """
        arch_id = hash_arch(arch_config)
        print(f"[Eval Start] arch={arch_id} | paradigm=validate", flush=True)

        self.supernet.set_sample_config(arch_config)

        # --- validate paradigm: run validation directly on the supernet ---
        # Compute metrics in their natural direction (e.g. accuracy 0-1).
        self.supernet.eval()
        acc_meter = AverageMeter(self.device)
        use_amp = self.cfg.get("amp", False) if self.cfg else False

        with torch.no_grad():
            for inputs, target in self.val_loader:
                inputs = inputs.to(self.device, non_blocking=True)
                target = target.to(self.device, non_blocking=True)
                with autocast(self.device, enabled=use_amp):
                    output = self.supernet(inputs)

                batch_metrics = compute_metrics(output, target, ...)  # project-specific
                acc_meter.update(batch_metrics["acc"], n=target.size(0))

        metrics = {"acc": acc_meter.avg}  # natural direction

        print(f"[Eval Done ] arch={arch_id} | metrics={metrics}", flush=True)

        # Negate larger-is-better metrics for the search framework (smaller-is-better convention).
        # Leave naturally smaller-is-better metrics (e.g. loss) as-is.
        return {"acc": -metrics["acc"]}
