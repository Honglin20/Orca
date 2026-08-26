"""Stage-based Gene -> ArchConfig decoder for generated hierarchical search scripts.

The EvoX evolutionary algorithm operates on fixed-length integer genes;
the generated supernet accepts project-specific `ArchConfig` objects.
This module bridges the two through one-way decoding (gene -> ArchConfig).
It does not encode an ArchConfig back into a gene.

NOTE: This example assumes a staged / hierarchical schema.  For isotropic
supernets, adapt the gene layout to match the actual `SearchSpace` / `ArchConfig`
schema instead of copying the staged iteration verbatim.

This example follows the staged CNN layout:

- one depth candidate-index gene per stage;
- then flattened active-layer slots, each with one block-choice gene followed by
  one gene for every searchable parameter key seen across all block choices.

For staged models, block choices are typically the same across stages (only
candidate value ranges differ), but the codec handles per-stage block sets for
robustness.  Parameter keys are unioned across all stages so that every layer
slot has the same number of gene positions.  Bounds use per-stage maximums
(across blocks within each stage); decode clamps to the selected block's count.
"""

import math
from typing import Any

# Generated scripts should replace this with the concrete supernet import.
from supernet import SearchSpace, ArchConfig


def _to_integer_gene(gene: list[float]) -> list[int]:
    """Round raw optimizer floats to integer candidate indices.

    EvoX Algorithm like NSGA2 emits continuous floats; this helper clamps
    non-finite values to 0 and rounds each element to the nearest integer
    so downstream codec functions can use them as list indices.
    """
    integer_gene = []
    for value in gene:
        value = float(value)
        if not math.isfinite(value):
            value = 0.0
        integer_gene.append(int(round(value)))
    return integer_gene


class ArchCodec:
    """One-way decoder from fixed-length integer genes to the generated ArchConfig.

    The EvoX evolutionary algorithm operates on gene vectors; the supernet
    accepts ArchConfig. This class bridges the two through decoding
    (gene -> ArchConfig). It does not encode ArchConfig back into a gene.

    Precomputes the gene layout (bounds, segment sizes) once from `search_space`
    so that per-gene decode calls are cheap.
    """

    def __init__(self, search_space: SearchSpace):
        """Initialize codec by precomputing the gene layout and bounds.

        Derives the maximum active network depth, parameter segment sizes, and integer
        bounds for every slot in the gene vector based on the provided SearchSpace.
        These precomputed values make per-gene decoding fast and stateless.
        """
        self.search_space = search_space

        # Per-stage block choices (each stage may have a different block set).
        self.stage_block_choices: list[list[str]] = [
            sorted(lc.keys()) for lc in search_space.stage_layer_configs
        ]

        # Union all parameter keys across all stage entries and block choices.
        keys: set[str] = set()
        for layer_configs in search_space.stage_layer_configs:
            for block_space in layer_configs.values():
                keys.update(block_space.keys())
        self.all_param_keys = sorted(keys)

        self.genes_per_layer = 1 + len(self.all_param_keys)

        stage_depth_candidates = search_space.stage_depth_candidates
        self.num_stages = len(search_space.stage_names)
        self.max_active_layers = sum(
            max(candidates) for candidates in stage_depth_candidates
        )

        # --- build per-position bounds --------------------------------------
        lower_bounds: list[int] = []
        upper_bounds: list[int] = []

        # Depth genes: one per stage.
        for candidates in stage_depth_candidates:
            lower_bounds.append(0)
            upper_bounds.append(len(candidates) - 1)

        # Layer-slot genes: bounds use per-stage max candidate count across
        # blocks (no cross-stage max). Decode clamps to the selected block.
        for stage_block_choices, depth_candidates, layer_configs in zip(
            self.stage_block_choices,
            stage_depth_candidates,
            search_space.stage_layer_configs,
            strict=True,
        ):
            max_depth = max(depth_candidates)
            # Per-param candidate count for this stage (max across block types).
            stage_param_max: dict[str, int] = {}
            for key in self.all_param_keys:
                max_len = 0
                for block_space in layer_configs.values():
                    if key in block_space:
                        max_len = max(max_len, len(block_space[key]))
                stage_param_max[key] = max_len

            for _ in range(max_depth):
                lower_bounds.append(0)
                upper_bounds.append(len(stage_block_choices) - 1)
                for key in self.all_param_keys:
                    lower_bounds.append(0)
                    upper_bounds.append(max(0, stage_param_max[key] - 1))

        self.gene_len = len(lower_bounds)
        self.lower_bounds = lower_bounds
        self.upper_bounds = upper_bounds

    def get_gene_space(self) -> dict[str, Any]:
        """Return gene space specification for the EvoX evolutionary algorithm."""
        return {
            "gene_len": self.gene_len,
            "lower_bounds": self.lower_bounds,
            "upper_bounds": self.upper_bounds,
            "metadata": {
                "stage_block_choices": self.stage_block_choices,
                "all_param_keys": self.all_param_keys,
                "num_stages": self.num_stages,
                "max_active_layers": self.max_active_layers,
                "genes_per_layer": self.genes_per_layer,
            },
        }

    def gene_to_arch(self, gene: list[float]) -> ArchConfig:
        """Decode one fixed-length candidate-index gene into the generated ArchConfig."""
        gene = _to_integer_gene(gene)

        cursor = 0
        stage_depths = []
        for candidates in self.search_space.stage_depth_candidates:
            # Genes store candidate indices, not the candidate values themselves.
            idx = gene[cursor]
            stage_depths.append(candidates[idx])
            cursor += 1

        arch_layer_configs: dict[str, tuple[dict[str, Any], ...]] = {}
        for stage_name, active_depth, depth_candidates, stage_block_choices, layer_configs in zip(
            self.search_space.stage_names,
            stage_depths,
            self.search_space.stage_depth_candidates,
            self.stage_block_choices,
            self.search_space.stage_layer_configs,
            strict=True,
        ):
            stage_layers = []
            max_depth = max(depth_candidates)
            for layer_idx in range(max_depth):
                # Each layer slot: [choice, param_0, param_1, ...].
                slot_start = cursor + layer_idx * self.genes_per_layer
                if layer_idx < active_depth:
                    choice = stage_block_choices[gene[slot_start]]
                    block_space = layer_configs[choice]
                    config = {}
                    for param_offset, key in enumerate(self.all_param_keys):
                        if key in block_space:
                            candidates = block_space[key]
                            # Clamp: bounds use max candidate count across blocks,
                            # but the selected block may have fewer candidates.
                            idx = min(gene[slot_start + 1 + param_offset], len(candidates) - 1)
                            config[key] = candidates[idx]
                    stage_layers.append({"choice": choice, "config": config})
            arch_layer_configs[stage_name] = tuple(stage_layers)
            cursor += max_depth * self.genes_per_layer

        return ArchConfig(
            stage_depths=tuple(stage_depths),
            layer_configs=arch_layer_configs,
        )
