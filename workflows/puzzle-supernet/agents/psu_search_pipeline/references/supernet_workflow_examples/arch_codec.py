"""Choice-index Gene -> ArchConfig decoder for generated search scripts.

The EvoX evolutionary algorithm operates on fixed-length integer genes;
the generated supernet accepts project-specific `ArchConfig` objects.
This module bridges the two through one-way decoding (gene -> ArchConfig).
It does not encode an ArchConfig back into a gene.

This example follows the choice-only layout — the only searchable variable
is, per transformer layer slot, which branch runs at that position:

- one branch-choice gene per layer slot (a candidate index into
  `SearchSpace.branch_choices`, not the branch name itself);
- gene length = the slot count (`SearchSpace.depth`) — depth is pinned to
  the original layer count and is never a gene;
- no depth segment, no padding slots, and no branch-local parameter
  segments: every branch has a fixed shape derived from the pinned slot
  facts, so there is nothing else to search.
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

    Precomputes the gene layout (bounds, slot count) once from `search_space`
    so that per-gene decode calls are cheap and stateless.
    """

    def __init__(self, search_space: SearchSpace):
        """Initialize codec by precomputing the gene layout and bounds.

        Derives the branch set and the slot count from the provided
        SearchSpace: one gene per transformer layer slot, each storing a
        branch candidate index. These precomputed values make per-gene
        decoding fast and stateless.
        """
        self.search_space = search_space

        # The only searchable dimension: which branch runs at each slot.
        self.branch_choices: tuple[str, ...] = tuple(search_space.branch_choices)
        # Slot count = the pinned original layer count (depth is not searched).
        self.num_slots: int = int(search_space.depth)

        if not self.branch_choices:
            raise ValueError("SearchSpace.branch_choices is empty: no branch to search")
        if self.num_slots < 1:
            raise ValueError("SearchSpace.depth < 1: no layer slot to search")

        # --- build per-position bounds --------------------------------------
        # One gene per slot; each stores a branch candidate index.
        self.gene_len = self.num_slots
        self.lower_bounds: list[int] = [0] * self.gene_len
        self.upper_bounds: list[int] = [len(self.branch_choices) - 1] * self.gene_len

    def get_gene_space(self) -> dict[str, Any]:
        """Return gene space specification for the EvoX evolutionary algorithm."""
        return {
            "gene_len": self.gene_len,
            "lower_bounds": self.lower_bounds,
            "upper_bounds": self.upper_bounds,
            "metadata": {
                "branch_choices": list(self.branch_choices),
                "num_slots": self.num_slots,
            },
        }

    def gene_to_arch(self, gene: list[float]) -> ArchConfig:
        """Decode one fixed-length branch-index gene into the generated ArchConfig."""
        gene = _to_integer_gene(gene)
        if len(gene) != self.gene_len:
            raise ValueError(
                f"gene length {len(gene)} != slot count {self.gene_len}"
            )

        # Genes store candidate indices, not the branch names themselves.
        return ArchConfig(
            choices=tuple(self.branch_choices[idx] for idx in gene)
        )
