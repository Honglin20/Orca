"""Problem layer with Ray-based distributed workers."""

import asyncio
from typing import Any

import ray
import torch
from evox.core import Problem
from omegaconf import DictConfig, OmegaConf

from nas_agent.search.arch_utils import serialize_arch
from nas_agent.search.dynamic_import import load_generated_component
from nas_agent.train import empty_cache, isolate_device, resolve_device, set_device

# Unified custom resource name registered with every Ray node.
# Single-node: auto-registered by the CLI (search.py) during `ray.init()`.
# Multi-node: each node must be started with
#   ray start --resources='{"ACCELERATOR": <num_devices>}'
ACCELERATOR_RESOURCE = "ACCELERATOR"
WORST_FITNESS = float(torch.finfo(torch.float32).max)

# 结构属性类目标名集合：这些目标在 infeasible（超 latency_constraint）候选上保留实测值，
# 不走 death-penalty。语义：图轴需看到真实 latency 才能判断「超多少」，质量目标则按
# 「超约束 = 不进前沿」惩罚为 WORST_FITNESS。集合按命名约定（含 'lat' 子串）判定，
# 避免每处硬编码具体目标名。新增 latency-like 目标只要名字含 'lat' 即自动纳入。
_PRESERVED_OBJECTIVE_TOKENS = ("lat",)


def _is_preserved_objective(name: str) -> bool:
    """判定目标是否结构属性类（infeasible 时仍保留实测值，不走 death-penalty）。

    抽成纯函数便于单测（Rule 9）：钉死「latency 类目标保留实测」契约。
    """
    nm = (name or "").lower()
    return any(tok in nm for tok in _PRESERVED_OBJECTIVE_TOKENS)


def _infeasible_result(latency: float, objective_names: list[str]) -> dict[str, float]:
    """构造超 latency 约束候选的 objective dict（death-penalty）。

    - 结构属性类目标（含 'lat'，如 latency）→ 保留实测 latency（即函数入参）；
    - 其余目标（质量类）→ WORST_FITNESS（不进前沿）。

    抽成独立纯函数便于单测（Rule 9）：钉死方向与字段名映射契约。
    注意：所有 preserved 目标在超约束时都取 ``latency`` 入参——目前只有 latency 一个
    latency-like 目标，未来若加 npu_latency 等多 latency-like 目标，需要 caller 各自传入
    实测值（当前 caller 只测 latency 一个）。
    """
    return {
        name: (latency if _is_preserved_objective(name) else WORST_FITNESS)
        for name in objective_names
    }


@ray.remote
class DeviceSyncActor:
    """Per-device async coordinator for cross-actor GPU synchronization.

    Workers sharing the same physical GPU use one `DeviceSyncActor` so
    that `evaluate()` calls may overlap while `get_latency()` runs
    exclusively with no in-flight evaluate.

    This is a Ray async actor: methods are coroutines processed on the
    same event loop, allowing `acquire_*` to yield while waiting and
    other calls (e.g. `release_*`) to be processed concurrently.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._active_evals = 0
        self._latency_running = False

    async def acquire_eval(self) -> None:
        """Wait until no latency measurement is running, then increment."""
        async with self._cond:
            while self._latency_running:
                await self._cond.wait()
            self._active_evals += 1

    async def release_eval(self) -> None:
        """Decrement active-eval counter and notify waiters."""
        async with self._cond:
            self._active_evals -= 1
            self._cond.notify_all()

    async def acquire_latency(self) -> None:
        """Wait until no evals/latency running, then set exclusive flag."""
        async with self._cond:
            while self._active_evals > 0 or self._latency_running:
                await self._cond.wait()
            self._latency_running = True

    async def release_latency(self) -> None:
        """Clear the latency flag and notify all waiters."""
        async with self._cond:
            self._latency_running = False
            self._cond.notify_all()


@ray.remote
class GlobalDeviceAllocator:
    """Global coordinator that assigns local device ranks to workers.

    Tracks node-local device occupancy and ensures that workers on the same
    physical node are load-balanced across the node's local devices.
    """

    def __init__(self) -> None:
        self.node_allocations: dict[str, dict[int, int]] = {}

    def _get_node_device_count(self, node_id: str) -> int:
        """Query per-node ACCELERATOR count from Ray cluster info."""
        for node in ray.nodes():
            if node["NodeID"] == node_id and node["Alive"]:
                return int(node["Resources"].get(ACCELERATOR_RESOURCE, 0))
        return 1

    def allocate_device(
        self, node_id: str
    ) -> int:
        """Allocate a local rank.

        The per-node device count is automatically queried from Ray
        cluster resources on first call for each node, so heterogeneous
        nodes (different device counts) are handled correctly.
        """
        if node_id not in self.node_allocations:
            cnt = max(1, self._get_node_device_count(node_id))
            self.node_allocations[node_id] = {i: 0 for i in range(cnt)}

        alloc = self.node_allocations[node_id]

        # Round-robin: pick the local rank with the fewest workers.
        rank = min(alloc, key=alloc.get)
        alloc[rank] += 1

        return rank


@ray.remote
class EvalWorkerActor:
    """Ray Actor that evaluates architecture candidates on a single device.

    Each actor owns one device (possibly shared via fractional GPU) and
    maintains one set of evaluator/latency instances.  It accepts
    individual tasks via `submit_task` and returns results.
    """

    def __init__(
        self,
        cfg_dict: dict,
        allocator: ray.actor.ActorHandle,
    ) -> None:
        """Initialize worker with model, evaluator, and latency estimator.

        Args:
            cfg_dict: Serialized config dict (will be wrapped in OmegaConf).
            allocator: Handle to the GlobalDeviceAllocator for acquiring rank.
        """
        cfg = OmegaConf.create(cfg_dict)

        node_id = ray.get_runtime_context().get_node_id()

        # Acquire a local rank from the global allocator so that workers
        # on the same node are load-balanced across devices.
        local_rank = ray.get(
            allocator.allocate_device.remote(node_id)
        )

        # Restrict this process to see only the allocated device before
        # any accelerator runtime initialises.  After this call the
        # target device appears as device 0, which sidesteps ACL's
        # thread-level context binding: every thread in this process
        # can only fall back to device 0, preventing cross-device
        # context/stream mismatches.
        isolate_device(local_rank)

        set_device(0)
        self.device = resolve_device(local_rank=0)

        SearchSpace = load_generated_component(cfg.search_space)
        ArchCodec = load_generated_component(cfg.arch_codec)
        CandidateEvaluator = load_generated_component(cfg.evaluator)
        LatencyEstimator = load_generated_component(cfg.latency_estimator)

        search_space = SearchSpace()
        self.codec = ArchCodec(search_space)
        self.evaluator = CandidateEvaluator(
            device=self.device, evaluator_cfg=cfg.evaluator_cfg
        )
        self.latency_estimator = LatencyEstimator(
            search_space, cfg.latency_cfg, device=self.device
        )

        self.objective_names = [str(name) for name in list(cfg.objs)]
        latency_constraint = OmegaConf.select(cfg, "latency_constraint", default=None)
        self.latency_constraint = (
            None if latency_constraint is None else float(latency_constraint)
        )

    def submit_task(self, idx: int, gene: list[int]) -> tuple[int, dict[str, float]]:
        """Evaluate one candidate architecture.

        Args:
            idx: Task index for result correlation.
            gene: Integer gene vector to decode and evaluate.

        Returns:
            Tuple of `(idx, result_dict)` where `result_dict` maps
            objective names to float values.

        Raises:
            KeyError: If the evaluator result is missing objective keys.
        """
        arch_config = self.codec.gene_to_arch(gene)
        latency = float(self.latency_estimator.get_latency(arch_config))

        if self.latency_constraint is not None and latency > self.latency_constraint:
            # Death-penalty: 超时延的候选不进质量前沿（quality 目标写 WORST_FITNESS，
            # 避免拖累帕累托前沿），但 latency 目标**保留实测值**——否则图轴看不到这些
            # 超约束候选的真实时延，前沿 latency 维度被伪造成 WORST_FITNESS，误导选型。
            # 泛化原则：结构属性类目标（如 latency）保留实测；其余目标走 death-penalty。
            result = _infeasible_result(latency, self.objective_names)
        else:
            result = self.evaluator.evaluate(arch_config)
            result["latency"] = latency

        missing = [name for name in self.objective_names if name not in result]
        if missing:
            raise KeyError(
                f"Candidate evaluator result is missing objective keys: {missing}"
            )

        empty_cache(self.device)
        return (idx, result)


class NASProblem(Problem):
    """EvoX Problem that evaluates architecture candidates via Ray actors.

    The main process handles cache lookup and preemptive task dispatch
    using `ray.wait`.
    """

    def __init__(self, cfg: DictConfig) -> None:
        """Initialize NASProblem with Ray worker actors.

        Args:
            cfg: Config with `objs`, `concurrency`, `arch_codec`,
                `evaluator`, `latency_estimator`, `search_space`,
                `latency_cfg`, and `evaluator_cfg` attributes.
                Optional `num_cpus_per_worker` controls CPU cores per
                worker (default 1).

        Raises:
            ValueError: If no `ACCELERATOR` resource is registered in
                the Ray cluster, or if the computed worker count is
                less than one.
        """
        super().__init__()
        self.objs = [str(name) for name in list(cfg.objs)]
        self.n_objs = len(self.objs)

        ArchCodec = load_generated_component(cfg.arch_codec)
        SearchSpace = load_generated_component(cfg.search_space)
        self.search_space = SearchSpace()
        self.codec = ArchCodec(self.search_space)

        self.cache: dict[str, tuple[float, ...]] = {}
        self.last_cache_hits: list[bool] = []

        concurrency = int(cfg.concurrency)
        total_devices = int(ray.cluster_resources().get(ACCELERATOR_RESOURCE, 0))
        if total_devices < 1:
            raise ValueError(
                f"No '{ACCELERATOR_RESOURCE}' resource found in the Ray "
                "cluster.  For single-node usage the CLI registers it "
                "automatically; for multi-node, start each node with: "
                "ray start --resources='{\"ACCELERATOR\": <N>}'"
            )

        worker_count = total_devices * concurrency
        num_cpus_per_worker = int(
            OmegaConf.select(cfg, "num_cpus_per_worker", default=1)
        )
        cfg_dict = OmegaConf.to_container(cfg, resolve=True)

        self.allocator = GlobalDeviceAllocator.remote()

        # Epsilon prevents float-precision rounding from exceeding 1.0
        # per device (e.g. 3 × 0.33334 > 1.0 would block the 3rd actor).
        fraction = (1.0 - 1e-5) / concurrency

        self.workers: list[ray.actor.ActorHandle] = []
        self._closed = False

        for _ in range(worker_count):
            actor = EvalWorkerActor.options(
                num_cpus=num_cpus_per_worker,
                resources={ACCELERATOR_RESOURCE: fraction},
            ).remote(cfg_dict, self.allocator)
            self.workers.append(actor)

    def close(self) -> None:
        """Shut down Ray worker actors."""
        if self._closed:
            return
        self._closed = True

        for worker in self.workers:
            ray.kill(worker)

        ray.kill(self.allocator)

    def evaluate(self, population: torch.Tensor | list[Any]) -> torch.Tensor:
        """Evaluate a population of architecture candidates.

        Cached architectures are returned immediately.  Uncached ones are
        dispatched to Ray worker actors using preemptive scheduling via
        `ray.wait`: whenever a worker finishes, it is immediately
        assigned the next pending task.

        Args:
            population: Genes as a 2-D tensor or list of rows.

        Returns:
            Fitness tensor of shape `(pop_size, n_objs)` on CPU.
        """
        raw_genes: list[list[int]] = []
        cache_keys: list[str] = []
        for row in population:
            raw = (
                row.detach().cpu().tolist()
                if isinstance(row, torch.Tensor)
                else list(row)
            )
            # Round genes to integers so that the cache key matches the
            # architecture actually evaluated by the worker.
            raw = [round(v) for v in raw]
            arch = self.codec.gene_to_arch(raw)
            raw_genes.append(raw)
            cache_keys.append(serialize_arch(arch))

        fitness: list[tuple[float, ...] | None] = [None] * len(cache_keys)
        pending_genes: list[list[int]] = []
        pending_keys: list[str] = []
        pending_indices: list[list[int]] = []

        # Deduplicate within this generation: identical architectures are
        # dispatched only once; all matching population indices share the
        # result.
        seen: dict[str, int] = {}
        for idx, key in enumerate(cache_keys):
            if key in self.cache:
                fitness[idx] = self.cache[key]
            elif key in seen:
                pending_indices[seen[key]].append(idx)
            else:
                seen[key] = len(pending_genes)
                pending_genes.append(raw_genes[idx])
                pending_keys.append(key)
                pending_indices.append([idx])

        if pending_genes:
            self._dispatch_and_collect(
                pending_genes, pending_keys, pending_indices, fitness
            )

        default = (WORST_FITNESS,) * self.n_objs
        safe_fitness = [item if item is not None else default for item in fitness]
        pending_set = set(pending_keys)
        self.last_cache_hits = [key not in pending_set for key in cache_keys]
        return torch.tensor(safe_fitness, dtype=torch.float32, device="cpu")

    def _dispatch_and_collect(
        self,
        pending_genes: list[list[int]],
        pending_keys: list[str],
        pending_indices: list[list[int]],
        fitness: list[tuple[float, ...] | None],
    ) -> None:
        """Dispatch pending tasks to workers and collect results.

        Uses preemptive scheduling: initially fills each worker with one
        task, then whenever a worker finishes it is immediately assigned
        the next pending task via `ray.wait`.

        Args:
            pending_genes: Genes to evaluate.
            pending_keys: Cache keys for each gene.
            pending_indices: Lists of original population indices for each gene.
            fitness: Mutable fitness list to fill in-place.
        """
        # ref -> (task_cursor_index, worker_handle)
        pending_refs: dict[ray.ObjectRef, tuple[int, ray.actor.ActorHandle]] = {}
        task_cursor = 0

        # Initial dispatch: one task per worker (or fewer if not enough).
        for worker in self.workers:
            if task_cursor >= len(pending_genes):
                break
            ref = worker.submit_task.remote(task_cursor, pending_genes[task_cursor])
            pending_refs[ref] = (task_cursor, worker)
            task_cursor += 1

        # Collect results and keep workers busy.
        while pending_refs:
            done_refs, _ = ray.wait(list(pending_refs.keys()), num_returns=1)
            for ref in done_refs:
                task_idx, worker = pending_refs.pop(ref)
                _, result_dict = ray.get(ref)

                key = pending_keys[task_idx]
                result = tuple(float(result_dict[name]) for name in self.objs)
                self.cache[key] = result
                
                for idx in pending_indices[task_idx]:
                    fitness[idx] = result

                # Assign next task to the now-idle worker.
                if task_cursor < len(pending_genes):
                    new_ref = worker.submit_task.remote(
                        task_cursor, pending_genes[task_cursor]
                    )
                    pending_refs[new_ref] = (task_cursor, worker)
                    task_cursor += 1
