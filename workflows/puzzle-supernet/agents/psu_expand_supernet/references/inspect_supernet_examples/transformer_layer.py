import torch

from nas_agent.latency import measure_module_latency
from nas_agent.train.distributed import resolve_device
from nas_agent.train.metrics import format_params
from supernet import SearchSpace, build_supernet


def main() -> None:
    device = resolve_device("auto")
    search_space = SearchSpace()
    assert search_space.validate(), "Search space is invalid!"
    supernet = build_supernet()
    supernet.set_sample_config(search_space.all_original())

    print("Transformer Layer Slot Supernet Summary")
    print(f"Slots (fixed layer count): {search_space.depth}")
    print(f"Pinned dims: global_dim={search_space.global_dim}, "
          f"head_dim={search_space.head_dim}, num_heads={search_space.num_heads}, "
          f"ffn_dim={search_space.ffn_dim}, max_seq_len={search_space.max_seq_len}")
    print(f"Branch set (the only searchable dimension): "
          f"{tuple(search_space.branch_choices)}")

    # --- ChoiceLayer input shape (from trace_choice_layer_inputs output) ---
    # All slots share the same input shape. Shape obtained from the trace step:
    # layers.0 -> (1, 64, 128)
    choice_input = torch.randn(1, 64, 128).to(device)  # (batch, seq, dim)

    # --- Measure representative slot (first slot, shared structure) ---
    layer0 = supernet.layers[0]

    print("\nRepresentative slot: first slot")
    print(f"  ChoiceLayer input shape: {tuple(choice_input.shape)}")
    print("  Branch parameter and latency distribution:")
    for branch_name, branch in layer0.branches.items():
        branch.to(device)
        params = branch.elastic_num_params
        subnet = branch.get_active_subnet().to(device)
        lat = measure_module_latency(subnet, choice_input, device)
        lat_str = f"{lat:.3f}" if isinstance(lat, float) else str(lat)
        print(
            f"    {branch_name}: "
            f"params={format_params(params)}, "
            f"latency_ms={lat_str}"
        )

    # All-original path total (inherited-baseline anchor).
    total = supernet.elastic_num_params
    print(f"\nAll-original path total params: {format_params(total)}")


if __name__ == "__main__":
    main()
