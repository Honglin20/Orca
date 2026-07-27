"""Discrete NSGA-II operators for fixed-length NAS candidate-index genes."""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from typing import Any

import torch
from evox.algorithms.mo import NSGA2
from evox.core import Mutable
from evox.operators.selection import nd_environmental_selection

from nas_agent.search.arch_utils import serialize_arch


def random_integer_population(
    *, pop_size: int, lb: torch.Tensor, ub: torch.Tensor
) -> torch.Tensor:
    span = (ub - lb + 1).clamp_min(1)
    random_values = torch.rand(pop_size, lb.numel(), device=lb.device)
    return torch.floor(random_values * span + lb)


def uniform_integer_crossover(x: torch.Tensor, pro_c: float = 1.0) -> torch.Tensor:
    offspring = x.round().clone()
    pair_count = offspring.shape[0] // 2
    if pair_count == 0:
        return offspring

    parent1 = offspring[:pair_count]
    parent2 = offspring[pair_count : pair_count * 2]
    pair_mask = torch.rand(pair_count, 1, device=x.device) < pro_c
    gene_mask = torch.rand(pair_count, x.shape[1], device=x.device) < 0.5
    swap_mask = pair_mask & gene_mask

    parent1_vals = parent1.clone()
    parent2_vals = parent2.clone()
    parent1[swap_mask] = parent2_vals[swap_mask]
    parent2[swap_mask] = parent1_vals[swap_mask]
    return offspring


def random_reset_integer_mutation(
    x: torch.Tensor,
    lb: torch.Tensor,
    ub: torch.Tensor,
    mutation_prob: float | None = None,
) -> torch.Tensor:
    offspring = x.round().clone()
    if mutation_prob is None:
        mutation_prob = 1.0 / max(1, offspring.shape[1])

    mutation_mask = torch.rand_like(offspring, dtype=torch.float32) < mutation_prob
    sampled = random_integer_population(
        pop_size=offspring.shape[0],
        lb=lb.to(offspring.device),
        ub=ub.to(offspring.device),
    )
    offspring[mutation_mask] = sampled[mutation_mask]
    return offspring


@torch.no_grad()
def unique_keep_first_rows(keys: torch.Tensor) -> torch.Tensor:
    """Return row indices that keep the first occurrence of each key."""
    if keys.ndim != 2:
        raise ValueError("keys must be a 2-D tensor")

    n_rows = keys.shape[0]
    if n_rows == 0:
        return torch.empty(0, dtype=torch.long, device=keys.device)

    unique_keys, inverse = torch.unique(keys, dim=0, return_inverse=True)
    row_indices = torch.arange(n_rows, dtype=torch.long, device=keys.device)
    first_indices = torch.full(
        (unique_keys.shape[0],),
        n_rows,
        dtype=torch.long,
        device=keys.device,
    )
    first_indices.scatter_reduce_(
        dim=0,
        index=inverse,
        src=row_indices,
        reduce="amin",
        include_self=True,
    )
    return torch.sort(first_indices).values


@torch.no_grad()
def filter_against_existing_rows(
    candidates: torch.Tensor,
    candidate_keys: torch.Tensor,
    existing_keys: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Remove candidates whose keys already exist, preserving candidate order."""
    if candidates.shape[0] == 0:
        return candidates, candidate_keys

    if existing_keys is None or existing_keys.numel() == 0:
        keep_indices = unique_keep_first_rows(candidate_keys)
        return candidates[keep_indices], candidate_keys[keep_indices]

    n_existing = existing_keys.shape[0]
    merged_keys = torch.cat([existing_keys, candidate_keys], dim=0)
    unique_keys, inverse = torch.unique(merged_keys, dim=0, return_inverse=True)

    total = merged_keys.shape[0]
    row_indices = torch.arange(total, dtype=torch.long, device=merged_keys.device)
    first_indices = torch.full(
        (unique_keys.shape[0],),
        total,
        dtype=torch.long,
        device=merged_keys.device,
    )
    first_indices.scatter_reduce_(
        dim=0,
        index=inverse,
        src=row_indices,
        reduce="amin",
        include_self=True,
    )

    candidate_global_indices = torch.arange(
        n_existing,
        total,
        dtype=torch.long,
        device=merged_keys.device,
    )
    candidate_groups = inverse[n_existing:]
    keep_mask = first_indices[candidate_groups] == candidate_global_indices
    return candidates[keep_mask], candidate_keys[keep_mask]


@torch.no_grad()
def filter_against_existing_items(
    candidates: torch.Tensor,
    candidate_keys: Sequence[Hashable],
    existing_keys: Sequence[Hashable] | None,
) -> tuple[torch.Tensor, list[Hashable]]:
    """Remove candidates whose Python keys already exist, preserving order."""
    if candidates.shape[0] == 0:
        return candidates, list(candidate_keys)

    seen = set(existing_keys or [])
    keep_indices: list[int] = []
    kept_keys: list[Hashable] = []
    for index, key in enumerate(candidate_keys):
        if key in seen:
            continue
        seen.add(key)
        keep_indices.append(index)
        kept_keys.append(key)

    keep_tensor = torch.tensor(
        keep_indices, dtype=torch.long, device=candidates.device
    )
    return candidates[keep_tensor], kept_keys


class DiscreteNSGA2(NSGA2):
    """NSGA-II configured for discrete candidate-index genes.

    EvoX's built-in NSGA2 initializes and mutates continuous vectors. NAS
    architecture codecs in this project use integer candidate indices, so this
    subclass keeps the population integer-valued at every algorithm boundary.
    """

    def __init__(
        self,
        pop_size: int,
        n_objs: int,
        lb: torch.Tensor,
        ub: torch.Tensor,
        device: torch.device | None = None,
        crossover_prob: float = 1.0,
        mutation_prob: float | None = None,
        eliminate_duplicates: bool = True,
        duplicate_max_iters: int = 100,
        codec: Any | None = None,
    ) -> None:
        if duplicate_max_iters < 1:
            raise ValueError("duplicate_max_iters must be >= 1")

        lb = lb.to(dtype=torch.float32)
        ub = ub.to(dtype=torch.float32)
        super().__init__(
            pop_size=pop_size,
            n_objs=n_objs,
            lb=lb,
            ub=ub,
            crossover_op=lambda x: uniform_integer_crossover(x, pro_c=crossover_prob),
            mutation_op=lambda x, lower, upper: random_reset_integer_mutation(
                x, lower, upper, mutation_prob=mutation_prob
            ),
            device=device,
        )
        self.eliminate_duplicates = eliminate_duplicates
        self.duplicate_max_iters = duplicate_max_iters
        self.codec = codec

        if eliminate_duplicates:
            initial_population = self._infill_unique_initial_population(pop_size)
        else:
            initial_population = random_integer_population(
                pop_size=pop_size, lb=self.lb, ub=self.ub
            )
        self.pop = Mutable(initial_population)

    def _dedup_keys(self, genes: torch.Tensor) -> torch.Tensor | list[Hashable]:
        """Return row keys used for duplicate detection."""
        if self.codec is None:
            return genes.to(dtype=torch.long)
        return self._architecture_dedup_keys(genes)

    def _architecture_dedup_keys(self, genes: torch.Tensor) -> list[str]:
        keys: list[str] = []
        for row in genes:
            gene = [int(value) for value in row.detach().cpu().tolist()]
            keys.append(serialize_arch(self.codec.gene_to_arch(gene)))
        return keys

    def _empty_population(self) -> torch.Tensor:
        return torch.empty((0, self.dim), dtype=self.lb.dtype, device=self.lb.device)

    def _sample_initial(self, n_samples: int) -> torch.Tensor:
        return random_integer_population(
            pop_size=n_samples,
            lb=self.lb,
            ub=self.ub,
        )

    def _sample_offspring(self, n_samples: int) -> torch.Tensor:
        mating_pool = torch.atleast_1d(self.selection(n_samples, [-self.dis, self.rank]))
        crossovered = self.crossover(self.pop[mating_pool])
        offspring = self.mutation(crossovered, self.lb, self.ub)
        return offspring

    @torch.no_grad()
    def eliminate_duplicates_do(
        self,
        candidates: torch.Tensor,
        existing_population: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """Pymoo-style duplicate elimination for candidate gene rows."""
        if candidates.shape[0] == 0:
            return candidates

        candidate_keys = self._dedup_keys(candidates)
        existing_keys = (
            None
            if existing_population is None
            else self._dedup_keys(existing_population)
        )
        if isinstance(candidate_keys, torch.Tensor):
            if existing_keys is not None and not isinstance(existing_keys, torch.Tensor):
                raise TypeError("existing_keys must be a tensor for gene-level keys")
            candidates, _ = filter_against_existing_rows(
                candidates,
                candidate_keys,
                existing_keys,
            )
        else:
            if isinstance(existing_keys, torch.Tensor):
                raise TypeError("existing_keys must be Python keys for architecture keys")
            candidates, _ = filter_against_existing_items(
                candidates,
                candidate_keys,
                existing_keys,
            )
        return candidates

    @torch.no_grad()
    def _infill_unique_initial_population(self, n_required: int) -> torch.Tensor:
        population = self._empty_population()

        for _ in range(self.duplicate_max_iters):
            n_remaining = n_required - population.shape[0]
            if n_remaining <= 0:
                break

            candidates = self._sample_initial(n_remaining)
            candidates = self.eliminate_duplicates_do(candidates, population)
            if candidates.shape[0] > 0:
                population = torch.cat([population, candidates[:n_remaining]], dim=0)

        if population.shape[0] < n_required:
            raise RuntimeError(
                "Could not generate enough unique initial individuals: "
                f"got {population.shape[0]}, required {n_required}."
            )
        return population

    @torch.no_grad()
    def _infill_unique_offspring(self, n_required: int) -> torch.Tensor:
        offspring = self._empty_population()

        for _ in range(self.duplicate_max_iters):
            n_remaining = n_required - offspring.shape[0]
            if n_remaining <= 0:
                break

            candidates = self._sample_offspring(n_remaining)
            existing_population = torch.cat([self.pop, offspring], dim=0)
            candidates = self.eliminate_duplicates_do(candidates, existing_population)

            if candidates.shape[0] > 0:
                candidates = candidates[:n_remaining]
                offspring = torch.cat([offspring, candidates], dim=0)

        if offspring.shape[0] < n_required:
            raise RuntimeError(
                "Mating could not produce enough unique offspring: "
                f"got {offspring.shape[0]}, required {n_required}."
            )
        return offspring

    def step(self) -> None:
        """Perform one NSGA-II step with optional gene duplicate elimination."""
        if self.eliminate_duplicates:
            offspring = self._infill_unique_offspring(self.pop_size)
        else:
            offspring = self._sample_offspring(self.pop_size)

        off_fit = self.evaluate(offspring)
        merge_pop = torch.cat([self.pop, offspring], dim=0)
        merge_fit = torch.cat([self.fit, off_fit], dim=0)

        self.pop, self.fit, self.rank, self.dis = nd_environmental_selection(
            merge_pop,
            merge_fit,
            self.pop_size,
        )
