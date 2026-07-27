"""Unified search logger: human-readable .log + structured .jsonl."""

import json
import logging
from pathlib import Path
from typing import List

import torch


class SearchLogger:
    """Manages all NAS search output: a .log for humans and a .jsonl for downstream tools.

    The .log file consolidates population-level debugging information (per-individual
    objectives, cache-hit status, Pareto membership, and per-objective statistics).

    The .jsonl file records every individual per generation.  Each line contains
    `{generation, gene, objs, cached, pareto, arch}`.  Downstream tools like
    `nas-select-architecture` filter by `pareto=true` to extract the Pareto front.
    """

    def __init__(self, log_path: Path, objective_names: List[str]) -> None:
        """Initialize the SearchLogger.

        Args:
            log_path (Path): The path to the log file (without suffix, or replacing it).
            objective_names (List[str]): A list of objective names to track.
        """
        self.objective_names = objective_names
        self.jsonl_path = log_path.with_suffix(".jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger("nas_search")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        fmt = logging.Formatter(
            "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )

        fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        self._logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        self._logger.addHandler(sh)

        self._handlers = [fh, sh]
        self._jsonl = open(self.jsonl_path, "w", encoding="utf-8")

    def log_generation(
        self,
        generation: int,
        elapsed_s: int,
        genes: List[List[int]],
        fit: torch.Tensor,
        pf_mask: torch.Tensor,
        cache_hits: List[bool],
        arch_strs: List[str],
    ) -> None:
        """Record one generation: all individuals with Pareto and cache flags.

        Args:
            generation (int): The generation number.
            elapsed_s (int): Elapsed time in seconds for the generation.
            genes (List[List[int]]): Integer gene vectors, one per individual.
            fit (torch.Tensor): Objective values tensor of shape `[pop_size, n_objs]`.
            pf_mask (torch.Tensor): Boolean tensor indicating Pareto-front membership.
            cache_hits (List[bool]): Per-individual cache-hit flags.
            arch_strs (List[str]): Serialized architecture JSON strings produced by
                `serialize_arch` from `nas_agent.search.arch_utils`. Each string
                is a valid JSON object used as a hashable cache key in
                `NASProblem`; `json.loads` converts it back to a dict for
                proper nesting in the JSONL.
        """
        log = self._logger.info
        pop_size = len(fit)
        n_cached = sum(cache_hits)
        n_pareto = int(pf_mask.sum().item())

        # ── generation header ─────────────────────────────────────────
        log(
            "Gen %d | pop=%d eval=%d cached=%d pareto=%d time=%ds",
            generation, pop_size, pop_size - n_cached, n_cached, n_pareto, elapsed_s,
        )

        # ── per-objective statistics ──────────────────────────────────
        for j, name in enumerate(self.objective_names):
            col = fit[:, j]
            log(
                "  %s: min=%.6f max=%.6f mean=%.6f",
                name, float(col.min()), float(col.max()), float(col.mean()),
            )

        # ── all individuals (.log + .jsonl) ───────────────────────────
        for i in range(pop_size):
            is_pareto = bool(pf_mask[i])
            is_cached = cache_hits[i]
            obj_str = " ".join(
                f"{name}={float(fit[i, j]):.6f}"
                for j, name in enumerate(self.objective_names)
            )
            log(
                "  %04d %s cached=%s pareto=%s arch=%s",
                i, obj_str,
                "Y" if is_cached else "N",
                "Y" if is_pareto else "N",
                arch_strs[i],
            )

            record = {
                "generation": generation,
                "gene": genes[i],
                "objs": {
                    name: float(fit[i, j])
                    for j, name in enumerate(self.objective_names)
                },
                "cached": is_cached,
                "pareto": is_pareto,
                "arch": json.loads(arch_strs[i]),
            }
            self._jsonl.write(json.dumps(record) + "\n")
        self._jsonl.flush()

    def close(self) -> None:
        """Close the JSONL file and all logger handlers."""
        self._jsonl.close()
        for h in self._handlers:
            h.close()
            self._logger.removeHandler(h)
