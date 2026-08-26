from dataclasses import dataclass, field
from typing import Any, Iterator
import itertools
import random



@dataclass
class ArchConfig:  # Record searched variables only (e.g. no fixed stage widths or hard-downsampling modules)
    # 1. Stage-level settings
    # One active depth value for each searchable stage.
    stage_depths: tuple[int, ...]

    # 2. Per-layer settings
    # A dictionary mapping stage name to a tuple of active searchable layer configs in execution order.
    # The length of the tuple for each stage must equal its active depth.
    # Config keys stay as raw search-space names like "kernel_size" / "expand_channels".
    # Only when calling set_sample_config(...) should the caller rename them to
    # "sample_kernel_size" / "sample_expand_channels" / ...
    # {
    #     "stage1": (
    #         {
    #             "choice": "some_custom_block",
    #             "config": {
    #                 "kernel_size": 5,
    #                 "expand_channels": 64,
    #             },
    #         },
    #         ...  # one dict per active layer
    #     ),
    # }
    layer_configs: dict[str, tuple[dict[str, Any], ...]]

    def validate(self) -> bool:
        """Validate the architecture config."""
        assert len(self.stage_depths) > 0, "stage_depths must not be empty."
        assert len(self.layer_configs) == len(self.stage_depths), (
            "layer_configs must have one entry per stage."
        )
        for depth, stage_configs in zip(self.stage_depths, self.layer_configs.values()):
            assert depth == len(stage_configs), "Active depth must match the number of layer configs for the stage."

        # Then check if each active layer's raw config is valid by calling the
        # corresponding is_valid_*_block function. These keys are still unprefixed here.
        for stage_configs in self.layer_configs.values():
            for layer_config in stage_configs:
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
    # 1. Per-stage channel widths (fixed, not searched).
    # Each entry corresponds to one searchable stage.
    stage_widths: tuple[int, ...] = (32, 64, 128, 256)

    # 2. Fixed stage skeleton
    # Stage names are fixed metadata, not searched.
    # This is a schema example only: derive stage count and names from the input model.
    stage_names: tuple[str, ...] = ("stage1", "stage2", "stage3", "stage4")

    # 3. Per-stage search space
    # Active depth candidates for each searchable stage.
    # Replace placeholder tuples with values derived from the input model.
    # Use the original repeat count as the central reference, while keeping
    # mandatory shape-changing searchable blocks within the minimum depth.
    stage_depth_candidates: tuple[tuple[int, ...], ...] = (
        (1, 2),       # stage1
        (2, 3, 4),    # stage2
        (2, 4, 6),    # stage3
        (1, 2),       # stage4
    )

    # 4. Per-stage, per-layer search space.
    # Dimension-related candidate values scale with each stage's fixed width.
    # Default ranges must not violate the corresponding is_valid_*_block().
    stage_layer_configs: tuple[dict[str, dict[str, tuple[Any, ...]]], ...] = field(
        default_factory=lambda: (
            {  # stage1
                "some_custom_block": {
                    "kernel_size": (3, 5),
                    "expand_channels": (32, 64),
                },
                # ... add more block choices as needed
            },
            {  # stage2
                "some_custom_block": {
                    "kernel_size": (3, 5),
                    "expand_channels": (64, 96, 128),
                },
                # ...
            },
            {  # stage3
                "some_custom_block": {
                    "kernel_size": (3, 5),
                    "expand_channels": (128, 192, 256),
                },
                # ...
            },
            {  # stage4
                "some_custom_block": {
                    "kernel_size": (3, 5),
                    "expand_channels": (256, 384, 512),
                },
                # ...
            },
        )
    )

    def sample(self) -> ArchConfig:
        # Logic:
        # 1. sample per-stage depth.
        # 2. sample one block choice and its raw architecture params for each active layer,
        #    using the stage-specific stage_layer_configs entry.
        stage_depths = tuple(
            random.choice(depth_candidates)
            for depth_candidates in self.stage_depth_candidates
        )
        
        sampled_layer_configs = {}
        for stage_name, depth, layer_configs in zip(
            self.stage_names, stage_depths, self.stage_layer_configs
        ):
            stage_configs = []
            for _ in range(depth):
                choice = random.choice(tuple(layer_configs.keys()))
                raw_config_space = layer_configs[choice]
                
                config = {}
                for key, values in raw_config_space.items():
                    config[key] = random.choice(values)
                    
                stage_configs.append({
                    "choice": choice,
                    "config": config,
                })
            sampled_layer_configs[stage_name] = tuple(stage_configs)
            
        return ArchConfig(
            stage_depths=stage_depths,
            layer_configs=sampled_layer_configs,
        )

    def validate(self) -> bool:
        """Validate the entire search space by iterating all possible layer configs and checking if they are all valid.

        Returns:
            bool: True if all combinations of layer configs are valid, False otherwise.
        """
        assert len(self.stage_names) == len(self.stage_depth_candidates), (
            "Each searchable stage must have one depth-candidate tuple."
        )
        assert len(self.stage_widths) == len(self.stage_names), (
            "stage_widths must have one entry per stage."
        )
        assert len(self.stage_layer_configs) == len(self.stage_names), (
            "stage_layer_configs must have one entry per stage."
        )

        for layer_configs in self.stage_layer_configs:
            for block_name, block_space in layer_configs.items():
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



def iter_layer_config(layer_config: dict[str, tuple[Any, ...]]) -> Iterator[dict[str, Any]]:
    keys = tuple(layer_config.keys())
    values = tuple(layer_config.values())
    for arch_values in itertools.product(*values):
        yield dict(zip(keys, arch_values))
