"""Canonical choice-only SearchSpace example for the transformer_layer family.

Field names, container shapes, and validation style here are the reference the
generated ``supernet.py`` must follow (see ``spec.md`` in this directory).

Hard constraints encoded by this example:
  * The ONLY public list/tuple attribute is ``branch_choices`` (the choice
    container). Schema reflection walks ``dir(SearchSpace())`` and reports every
    public non-empty list/tuple as a searchable dimension — any second container
    would corrupt the search-record schema.
  * Pinned dimensions (``depth`` / ``global_dim`` / ``head_dim`` / ``num_heads``
    / ``ffn_dim`` / ``max_seq_len``) are plain scalars, never single-value
    tuples (a flat single-value tuple is misreported as a searchable list).
  * Zero-argument construction, no module-level side effects, no checkpoint
    dependency (the checkpoint enters only through ``SuperNet.__init__`` /
    ``build_supernet(pretrained_state=...)``).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Canonical branch set, in canonical enumeration order. "original" is mandatory
# and first: it carries the inherited parent weights and anchors equivalence.
BRANCH_CHOICES: tuple[str, ...] = (
    "original",
    "vanilla",
    "random_synthesizer",
    "relu_attention",
    "fnet",
    "softs_star",
)


@dataclass
class ArchConfig:
    """A single sampled architecture: one branch choice per transformer layer slot.

    ``choices`` records the ONLY searchable variable. Its length equals
    ``SearchSpace.depth`` (the fixed original layer count) — depth itself is
    never a field here.
    """

    choices: tuple[str, ...]

    def validate(self) -> bool:
        """Structural validity: non-empty, and every choice is a known branch."""
        if not self.choices:
            return False
        return all(choice in BRANCH_CHOICES for choice in self.choices)


@dataclass
class SearchSpace:
    # ── The only searchable dimension: the choice container ────────────────
    # Must stay the ONLY public list/tuple attribute (schema reflection).
    branch_choices: tuple[str, ...] = BRANCH_CHOICES

    # ── Pinned dimensions — fixed to the original model's measured values ──
    # Scalars only. Fill with the real measured facts of the user model, never
    # with defaults or guesses.
    depth: int = 4          # original layer count (no layer is added or dropped)
    global_dim: int = 128   # residual stream width (layer I/O width)
    head_dim: int = 32      # original attention head dim (as measured)
    num_heads: int = 4      # original attention head count (as measured)
    ffn_dim: int = 256      # original FFN intermediate width
    max_seq_len: int = 64   # real input sequence length of the workload
    activation: str = "gelu"  # original FFN activation name

    def sample(self) -> ArchConfig:
        """Sample one architecture: each slot independently picks one branch."""
        return ArchConfig(
            choices=tuple(
                random.choice(self.branch_choices) for _ in range(self.depth)
            )
        )

    def all_original(self) -> ArchConfig:
        """Default config: every slot chooses "original" (equivalence premise)."""
        return ArchConfig(choices=("original",) * self.depth)

    def validate(self) -> bool:
        """Whole-space validity: branch set integrity + pinned-dim consistency."""
        if "original" not in self.branch_choices:
            return False
        if len(set(self.branch_choices)) != len(self.branch_choices):
            return False
        if len(self.branch_choices) < 2:
            return False  # no real choice to search
        if self.depth < 1 or self.global_dim < 1 or self.max_seq_len < 1:
            return False
        if self.num_heads < 1 or self.head_dim < 1 or self.ffn_dim < 1:
            return False
        # The vanilla branch builds nn.MultiheadAttention at global_dim, which
        # requires global_dim % num_heads == 0. Keep a check here only for
        # constraints a branch in the set actually imposes.
        if self.global_dim % self.num_heads != 0:
            return False
        return True


if __name__ == "__main__":
    # Smoke: construct zero-arg, validate, sample, all_original.
    space = SearchSpace()
    assert space.validate(), "canonical SearchSpace failed its own validate()"
    cfg = space.sample()
    assert cfg.validate() and len(cfg.choices) == space.depth
    assert space.all_original().choices == ("original",) * space.depth
    print(f"branch_choices={space.branch_choices} depth={space.depth}")
    print(">>> transformer_layer search_space canonical example OK")
