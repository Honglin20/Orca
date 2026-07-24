from dataclasses import dataclass, field
from typing import Any, Iterator
import itertools
import random



@dataclass
class ArchConfig:  # Record variables only (e.g.: no global_dim)
    # Global settings
    depth: int

    # Per-layer settings: A tuple of configs, one for each active layer.
    # The length of this tuple must equal 'depth'.
    # Config keys stay as raw search-space names like "num_heads" / "ffn_dim".
    # Only when calling set_sample_config(...) should the caller rename them to
    # "sample_num_heads" / "sample_ffn_dim" / ...
    # (
    #     {
    #         "choice": "some_custom_block",
    #         "config": {
    #             "num_heads": 8,
    #             "ffn_dim": 2048,
    #         },
    #     },
    #     ...  # one dict per active layer
    # )
    layers_config: tuple[dict[str, Any], ...]

    def validate(self) -> bool:
        """Validate the architecture config."""
        assert self.depth == len(self.layers_config), (
            "Depth must equal to the number of layer configs"
        )
        # Then check if each layer's raw config is valid by calling the corresponding
        # is_valid_*_block function. These keys are still unprefixed here.
        for layer_config in self.layers_config:
            choice = layer_config["choice"]
            config = layer_config["config"]
            if choice == "some_custom_block":
                if not is_valid_some_custom_block(config):
                    return False
            # elif choice == "another_block":
            #     if not is_valid_another_block(config):
            #         return False
            else:
                return False  # Unknown block choice
        return True

@dataclass
class SearchSpace:
    # 1. Global Dimensions
    # Fixed input & output dim per layer, not searched.
    global_dim: int = 1024

    # Fixed head_dim for all layers. embed_dim = num_heads * head_dim
    head_dim: int = 32

    # Global depth is searchable.
    depth_candidates: tuple[int, ...] = (8, 10, 12)

    # 2. Per-Layer Search Space (independent choices for each layer):
    # Each subnet chooses one block choice and sample from the block's search space.
    # Make sure the default search space range does not violate their corresponding is_valid_*_block()
    layer_configs: dict[str, dict[str, tuple]] = field(default_factory=lambda: {
        "some_custom_block": {
            "num_heads": (16, 8, 4),
            "ffn_dim": (1024, 512, 256),
        },
    })

    def sample(self) -> ArchConfig:
        # Logic:
        # 1. sample global depth.
        # 2. Loop 'depth' times: sample one block choice and its raw architecture params.
        depth = random.choice(self.depth_candidates)

        layers_config = []
        for _ in range(depth):
            choice = random.choice(tuple(self.layer_configs.keys()))
            raw_config_space = self.layer_configs[choice]

            config = {}
            for key, values in raw_config_space.items():
                config[key] = random.choice(values)

            layers_config.append({
                "choice": choice,
                "config": config,
            })

        return ArchConfig(
            depth=depth,
            layers_config=tuple(layers_config),
        )

    def validate(self) -> bool:
        """Validate the entire search space by iterating all possible layer configs and checking if they are all valid.

        Returns:
            bool: True if all combinations of layer configs are valid, False otherwise.
        """
        for block_name, block_space in self.layer_configs.items():
            for config in iter_layer_config(block_space):
                if block_name == "some_custom_block":
                    if not is_valid_some_custom_block(config):
                        return False
                # elif block_name == "another_block":
                #     if not is_valid_another_block(config):
                #         return False
                else:
                    return False  # Unknown block choice

        return True


def iter_layer_config(layer_config: dict[str, tuple]) -> Iterator[dict[str, Any]]:
    keys = layer_config.keys()
    values = layer_config.values()
    for arch_val in itertools.product(*values):
        yield dict(zip(keys, arch_val))
