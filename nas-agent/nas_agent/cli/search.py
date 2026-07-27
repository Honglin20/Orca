"""Command-line entry for fixed NAS search over generated supernets.

This runner keeps NSGA-II, workflow, problem dispatch, and Pareto logging in
the framework.  Project-specific generated code is injected through config
paths: `arch_codec` for gene/ArchConfig conversion and worker-side
`evaluator` / `latency_estimator` modules.

Single-node usage (default)::

    nas_search --config search_config.yaml

A local Ray cluster is started automatically and `ACCELERATOR` resources
are registered based on locally detected devices (CUDA / NPU).

Multi-node usage:

    1. Start a Ray head node on the first machine::

        ray start --head --resources='{"ACCELERATOR": <N>}'

    2. On every additional machine, join the cluster::

        ray start --address=<head_ip>:6379 --resources='{"ACCELERATOR": <N>}'

       Replace `<N>` with the number of accelerator devices on that node.

    3. Set `ray_address` in `search_config.yaml` to the head node address
       (e.g. `ray://<head_ip>:10001`) or `auto` to auto-discover.

    4. Run the search on the head node (or any machine that can reach it)::

        nas_search --config search_config.yaml
"""

import argparse
import os
import time
from pathlib import Path

import ray
import torch
from evox.operators.selection import non_dominate_rank
from evox.workflows import EvalMonitor, StdWorkflow
from omegaconf import OmegaConf

from nas_agent.search.arch_utils import serialize_arch
from nas_agent.search.discrete_nsga2 import DiscreteNSGA2
from nas_agent.search.dynamic_import import load_generated_component
from nas_agent.search.logger import SearchLogger
from nas_agent.search.problem import ACCELERATOR_RESOURCE, NASProblem
from nas_agent.train import get_device_count


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for NAS search.

    Returns:
        Parsed arguments containing the path to the search configuration.
    """
    parser = argparse.ArgumentParser(description="Run NAS search for a supernet")
    parser.add_argument("--config", required=True, help="Path to search_config.yaml.")
    return parser.parse_args()


def main() -> None:
    """Main execution entry point for running NAS search."""
    args = parse_args()

    cfg = OmegaConf.load(args.config)
    if len(cfg.objs) < 2:
        raise ValueError(
            f"At least 2 objectives are required in 'objs', got {len(cfg.objs)}"
        )

    ArchCodec = load_generated_component(cfg.arch_codec)

    SearchSpace = load_generated_component(cfg.search_space)
    search_space = SearchSpace()
    codec = ArchCodec(search_space)
    gene_space = codec.get_gene_space()
    lower = gene_space["lower_bounds"]
    upper = gene_space["upper_bounds"]
    objective_names = [str(name) for name in cfg.objs]

    # Prevent Ray from overriding accelerator visible-device env vars
    # (e.g. ASCEND_RT_VISIBLE_DEVICES for NPU) when num_gpus=0.  We use
    # the custom ACCELERATOR resource for scheduling, not Ray's built-in
    # GPU tracking, so workers must inherit the host's device visibility.
    os.environ.setdefault("RAY_ACCEL_ENV_VAR_OVERRIDE_ON_ZERO", "0")

    # Initialize Ray before creating NASProblem.
    # Single-node: start a local cluster and auto-register the ACCELERATOR
    #   resource based on locally detected devices (CUDA / NPU).
    # Multi-node: connect to an existing cluster where each node was
    #   started with  ray start --resources='{"ACCELERATOR": <N>}' .
    ray_address = OmegaConf.select(cfg, "ray_address", default=None)
    if ray_address is None:
        num_accelerators = max(get_device_count(), 1)
        ray.init(resources={ACCELERATOR_RESOURCE: num_accelerators})
    else:
        ray.init(address=ray_address)

    problem = NASProblem(cfg)
    algorithm = DiscreteNSGA2(
        pop_size=cfg.population_size,
        n_objs=len(objective_names),
        lb=torch.tensor(lower, dtype=torch.float32),
        ub=torch.tensor(upper, dtype=torch.float32),
        device=torch.device("cpu"),
        eliminate_duplicates=OmegaConf.select(
            cfg, "eliminate_duplicates", default=True
        ),
        duplicate_max_iters=OmegaConf.select(
            cfg, "duplicate_max_iters", default=100
        ),
        codec=codec,
    )
    monitor = EvalMonitor(
        multi_obj=True,
        full_fit_history=True,
        full_sol_history=True,
        full_pop_history=True,
        device=torch.device("cpu"),
    )
    workflow = StdWorkflow(algorithm, problem, monitor)
    workflow.init_step()

    logger = SearchLogger(
        log_path=Path(cfg.search_log_path),
        objective_names=objective_names,
    )
    try:
        for generation in range(cfg.num_generations):
            start = time.time()
            workflow.step()
            elapsed = int(time.time() - start)

            aux = monitor.auxiliary_history
            pop = aux["pop"][-1]
            fit = aux["fit"][-1]

            pop_list = pop.tolist()
            genes = [[int(round(v)) for v in g] for g in pop_list]
            arch_strs = [serialize_arch(codec.gene_to_arch(g)) for g in pop_list]

            logger.log_generation(
                generation=generation,
                elapsed_s=elapsed,
                genes=genes,
                fit=fit,
                pf_mask=non_dominate_rank(fit) == 0,
                cache_hits=problem.last_cache_hits,
                arch_strs=arch_strs,
            )
    finally:
        logger.close()
        problem.close()
        ray.shutdown()


if __name__ == "__main__":
    main()
