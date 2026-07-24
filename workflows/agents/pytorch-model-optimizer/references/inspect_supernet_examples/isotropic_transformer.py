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
        # TODO: add one case per candidate block in search_space.layer_configs
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


def main() -> None:
    device = resolve_device("auto")
    search_space = SearchSpace()
    assert search_space.validate(), "Search space is invalid!"
    supernet = SuperNet(search_space)

    print("Isotropic Transformer Supernet Summary")
    print(f"Total layers: {len(supernet.layers)}")
    print(f"Global dim: {search_space.global_dim}")
    print(f"Head dim: {search_space.head_dim}")
    print(f"Depth candidates: {tuple(search_space.depth_candidates)}")

    print("Layer config space:")
    for choice_name, config_space in search_space.layer_configs.items():
        print(f"  {choice_name}: {config_space}")

    # --- ChoiceLayer input shape (from trace_choice_layer_inputs output) ---
    # All layers share the same input shape for isotropic models.
    # Shape obtained from the trace step: layers.0 -> (1, 64, 512)
    choice_input = torch.randn(1, 64, 512).to(device)  # (batch, seq, dim)

    # --- Measure representative layer (first layer, shared structure) ---
    layer0 = supernet.layers[0]

    print("\nRepresentative layer: first layer")
    print(f"  ChoiceLayer input shape: {tuple(choice_input.shape)}")
    print("  Candidate block parameter and latency distribution:")
    for choice_name, branch in layer0.branches.items():
        min_cfg, max_cfg = build_extreme_configs(
            choice_name, search_space.layer_configs[choice_name],
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


if __name__ == "__main__":
    main()
