import torch

from nas_agent.latency import measure_module_latency
from nas_agent.train.distributed import resolve_device
from nas_agent.train.metrics import format_params
from supernet import SearchSpace, SuperNet


def build_extreme_configs(choice_name, config_space):
    """Build min-param and max-param configs for one candidate branch.

    Build extreme configs based on the SEMANTIC MEANING of each search field.
    Do NOT blindly min()/max() all candidate tuples — reason about how each
    field affects parameter count in the actual block implementation.
    """
    match choice_name:
        case "some_custom_block":
            # num_heads: more heads -> larger embed_dim -> more params
            # ffn_dim:   wider hidden dim -> more FFN weight params
            min_cfg = {
                "num_heads": min(config_space["num_heads"]),
                "ffn_dim": min(config_space["ffn_dim"]),
            }
            max_cfg = {
                "num_heads": max(config_space["num_heads"]),
                "ffn_dim": max(config_space["ffn_dim"]),
            }
        # TODO: add one case per candidate block in search_space.stage_layer_configs
        case _:
            raise ValueError(f"Unknown block: {choice_name}")
    return min_cfg, max_cfg


def measure_config(branch, cfg, choice_input, device):
    """Measure params and latency for a single config."""
    sample_cfg = {f"sample_{k}": v for k, v in cfg.items()}
    branch.set_sample_config(**sample_cfg)
    p = branch.elastic_num_params
    subnet = branch.get_active_subnet().to(device)
    lat = measure_module_latency(subnet, choice_input, device)
    return {"config": cfg, "params": p, "latency_ms": lat}


def inspect_stage(search_space, stage_idx, stage, choice_input, device) -> None:
    stage_name = search_space.stage_names[stage_idx]
    layer_configs = search_space.stage_layer_configs[stage_idx]
    blocks = list(stage)
    representative = blocks[0]
    print(f"Representative layer for {stage_name}: first layer")
    print(f"  Layers in stage: {len(blocks)}")
    print(f"  ChoiceLayer input shape: {tuple(choice_input.shape)}")

    print("  Candidate block parameter and latency distribution:")
    for choice_name, branch in representative.branches.items():
        min_cfg, max_cfg = build_extreme_configs(
            choice_name, layer_configs[choice_name],
        )
        for label, cfg in [("min", min_cfg), ("max", max_cfg)]:
            item = measure_config(branch, cfg, choice_input, device)
            lat = f"{item['latency_ms']:.3f}" if isinstance(item["latency_ms"], float) else item["latency_ms"]
            print(
                f"    {choice_name} ({label}): "
                f"params={format_params(item['params'])}, "
                f"latency_ms={lat}, "
                f"config={item['config']}"
            )


def main() -> None:
    device = resolve_device("auto")
    search_space = SearchSpace()
    assert search_space.validate(), "Search space is invalid!"
    supernet = SuperNet(search_space)

    print("Hierarchical Transformer Supernet Summary")
    print(f"Stage emb dims: {search_space.stage_emb_dims} (fixed)")
    print(f"Head dim: {search_space.head_dim}")
    print(f"Stages: {tuple(search_space.stage_names)}")
    print(
        f"Total layers across stages: {sum(len(stage) for stage in supernet.layers)}"
    )

    print("Stage search ranges:")
    for stage_idx, stage_name in enumerate(search_space.stage_names):
        print(
            f"  {stage_name}: "
            f"depth={search_space.stage_depth_candidates[stage_idx]}, "
            f"emb_dim={search_space.stage_emb_dims[stage_idx]} (fixed)"
        )

    print("Layer config space (per-stage):")
    for stage_idx, (stage_name, layer_configs) in enumerate(
        zip(search_space.stage_names, search_space.stage_layer_configs)
    ):
        print(f"  {stage_name}:")
        for choice_name, config_space in layer_configs.items():
            print(f"    {choice_name}: {config_space}")

    # --- Per-stage ChoiceLayer input shapes (from trace_choice_layer_inputs output) ---
    # Shapes obtained from the trace step, one per stage (first ChoiceLayer in each).
    # Example trace output:
    #   layers.0.0: (1, 64, 256)
    #   layers.1.0: (1, 32, 512)
    stage_choice_inputs = [
        torch.randn(1, 64, 256).to(device),   # stage 0
        torch.randn(1, 32, 512).to(device),   # stage 1
    ]

    print("\nRepresentative candidate sizes and latency:")
    for stage_idx, stage in enumerate(supernet.layers):
        inspect_stage(
            search_space, stage_idx, stage,
            stage_choice_inputs[stage_idx], device,
        )


if __name__ == "__main__":
    main()
