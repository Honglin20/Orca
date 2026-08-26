"""Minimal CNN supernet template —— slim elastic_optimizer 的结构基准。

针对 3-conv + 1-linear 的 CNN（对齐 demo_target/model.py: TinyCNN）。把卷积换成
ElasticConv2d + ElasticBatchNorm2d、head 换 ElasticLinear、每个位置用 ChoiceLayer
给 ≥1 个 block 候选；SearchSpace 默认候选集 + validate()。

合法超网契约（nas-search / push_describe 消费）：
  - SearchSpace（@dataclass）：stage_names / stage_widths / stage_depth_candidates /
    stage_layer_configs + sample() + validate()。
  - ArchConfig（@dataclass）：stage_depths / layer_configs + validate()。
  - SuperNet：set_sample_config(ArchConfig) / forward / get_active_subnet / elastic_num_params。
  - python supernet.py 自测：超网与 get_active_subnet() 子网前向一致 < 1e-5。

slim agent 仿本文件生成 <output_dir>/supernet.py。本模板**可独立运行**：
    python workflows/agents/elastic_optimizer/references/supernet_template.py
"""

from __future__ import annotations

import copy
import itertools
import random
from dataclasses import dataclass, field
from typing import Any

import torch
import torch.nn as nn

from nas_agent.blocks.choice_layer import ChoiceLayer
from nas_agent.blocks.primitive_blocks import ElasticBatchNorm2d, ElasticConv2d, ElasticLinear
from nas_agent.blocks.res_conv import ElasticResConvBlock, is_valid_res_conv_block


# ── 自定义 elastic block：ElasticConv2d + ElasticBatchNorm2d + ReLU ────────────


class ElasticConvBlock(nn.Module):
    """最小 elastic conv block —— 展示 ElasticConv2d + ElasticBatchNorm2d 组合。

    out_channels 固定（= stage width，跨 stage 对齐），kernel 经 candidate_kernel_sizes 弹性。
    自定义 block 契约：set_sample_config / forward / get_active_subnet / elastic_num_params。
    """

    def __init__(
        self,
        *,
        in_channels: int,
        out_channels: int,
        stride: int = 1,
        candidate_kernel_sizes: tuple[int, ...] = (3, 5),
    ):
        super().__init__()
        if not candidate_kernel_sizes:
            raise ValueError("candidate_kernel_sizes must not be empty.")
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.candidate_kernel_sizes = tuple(sorted(set(candidate_kernel_sizes)))
        max_k = max(self.candidate_kernel_sizes)

        self.conv = ElasticConv2d(
            super_in_channels=in_channels,
            super_out_channels=out_channels,
            kernel_size=max_k,
            stride=stride,
            padding=max_k // 2,  # same padding；切小 kernel 时 ElasticConv2d 自动重算
            bias=False,
            candidate_kernel_sizes=self.candidate_kernel_sizes,
        )
        self.bn = ElasticBatchNorm2d(super_num_features=out_channels)
        self.act = nn.ReLU(inplace=True)
        self.set_sample_config(sample_kernel_size=max_k)

    def set_sample_config(self, *, sample_kernel_size: int):
        if sample_kernel_size not in self.candidate_kernel_sizes:
            raise ValueError(f"Unsupported kernel size: {sample_kernel_size}")
        self.conv.set_sample_config(
            sample_in_channels=self.in_channels,
            sample_out_channels=self.out_channels,
            sample_kernel_size=sample_kernel_size,
        )
        self.bn.set_sample_config(sample_num_features=self.out_channels)

    def forward(self, x):
        return self.act(self.bn(self.conv(x)))

    def get_active_subnet(self) -> nn.Module:
        class _SubConvBlock(nn.Module):
            def __init__(self, conv, bn, act):
                super().__init__()
                self.conv = conv
                self.bn = bn
                self.act = act

            def forward(self, x):
                return self.act(self.bn(self.conv(x)))

        return _SubConvBlock(
            conv=self.conv.get_active_subnet(),
            bn=self.bn.get_active_subnet(),
            act=copy.deepcopy(self.act),
        )

    @property
    def elastic_num_params(self):
        return self.conv.elastic_num_params + self.bn.elastic_num_params


def is_valid_conv_block(layer_config: dict[str, Any]) -> bool:
    kernel_size = layer_config.get("kernel_size")
    return isinstance(kernel_size, int) and kernel_size > 0 and kernel_size % 2 == 1


# ── 数据类 ─────────────────────────────────────────────────────────────────────


@dataclass
class ArchConfig:
    stage_depths: tuple[int, ...]
    layer_configs: dict[str, tuple[dict[str, Any], ...]]

    def validate(self) -> bool:
        assert len(self.stage_depths) > 0, "stage_depths must not be empty."
        assert len(self.layer_configs) == len(self.stage_depths), (
            "layer_configs must have one entry per stage."
        )
        for depth, stage_configs in zip(self.stage_depths, self.layer_configs.values()):
            assert depth == len(stage_configs), (
                "Active depth must match the number of layer configs for the stage."
            )
        for stage_configs in self.layer_configs.values():
            for layer_config in stage_configs:
                choice = layer_config["choice"]
                config = layer_config["config"]
                if choice == "conv":
                    if not is_valid_conv_block(config):
                        return False
                elif choice == "res":
                    if not is_valid_res_conv_block(config):
                        return False
                else:
                    return False
        return True


def iter_layer_config(
    layer_config: dict[str, tuple[Any, ...]],
) -> itertools.chain[dict[str, Any]]:
    keys = tuple(layer_config.keys())
    values = tuple(layer_config.values())
    for arch_values in itertools.product(*values):
        yield dict(zip(keys, arch_values))


@dataclass
class SearchSpace:
    """默认候选集对齐 TinyCNN（3 stages: 16/32/64 channels, depth (1,2), kernel (3,5)）。"""

    stage_names: tuple[str, ...] = ("stage0", "stage1", "stage2")
    stage_widths: tuple[int, ...] = (16, 32, 64)
    stage_depth_candidates: tuple[tuple[int, ...], ...] = ((1, 2), (1, 2), (1, 2))
    stage_layer_configs: tuple[dict[str, dict[str, tuple[Any, ...]]], ...] = field(
        default_factory=lambda: (
            {"conv": {"kernel_size": (3, 5)}, "res": {"kernel_size": (3, 5), "hidden_channels": (8, 16)}},
            {"conv": {"kernel_size": (3, 5)}, "res": {"kernel_size": (3, 5), "hidden_channels": (16, 32)}},
            {"conv": {"kernel_size": (3, 5)}, "res": {"kernel_size": (3, 5), "hidden_channels": (32, 64)}},
        )
    )

    def sample(self) -> ArchConfig:
        stage_depths = tuple(
            random.choice(depth_candidates)
            for depth_candidates in self.stage_depth_candidates
        )
        sampled_layer_configs: dict[str, tuple[dict[str, Any], ...]] = {}
        for stage_name, depth, layer_configs in zip(
            self.stage_names, stage_depths, self.stage_layer_configs
        ):
            stage_configs = []
            for _ in range(depth):
                choice = random.choice(tuple(layer_configs.keys()))
                config = {k: random.choice(v) for k, v in layer_configs[choice].items()}
                stage_configs.append({"choice": choice, "config": config})
            sampled_layer_configs[stage_name] = tuple(stage_configs)
        return ArchConfig(stage_depths=stage_depths, layer_configs=sampled_layer_configs)

    def validate(self) -> bool:
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
                    if block_name == "conv":
                        if not is_valid_conv_block(config):
                            return False
                    elif block_name == "res":
                        if not is_valid_res_conv_block(config):
                            return False
                    else:
                        return False
        return True


# ── SuperNet ───────────────────────────────────────────────────────────────────


def _build_block(block_name, stage_cfg, in_channels, out_channels, stride):
    block_space = stage_cfg[block_name]
    if block_name == "conv":
        return ElasticConvBlock(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
            candidate_kernel_sizes=block_space["kernel_size"],
        )
    if block_name == "res":
        return ElasticResConvBlock(
            in_channels=in_channels,
            out_channels=out_channels,
            stride=stride,
            super_hidden_channels=max(block_space["hidden_channels"]),
            candidate_kernel_sizes=block_space["kernel_size"],
        )
    raise ValueError(f"Unknown block: {block_name}")


class SuperNet(nn.Module):
    def __init__(self, search_space: SearchSpace, *, num_classes: int = 10, stem_channels: int = 16):
        super().__init__()
        self.search_space = search_space
        self.num_classes = num_classes

        self.stem = nn.Sequential(
            nn.Conv2d(3, stem_channels, 3, padding=1, bias=True),
            nn.ReLU(inplace=True),
        )

        self.layers = nn.ModuleList()
        prev_channels = stem_channels
        for stage_idx, (stage_name, stage_width) in enumerate(
            zip(search_space.stage_names, search_space.stage_widths)
        ):
            stage_cfgs = search_space.stage_layer_configs[stage_idx]
            max_depth = max(search_space.stage_depth_candidates[stage_idx])
            blocks = nn.ModuleList()
            for pos in range(max_depth):
                stride = 2 if pos == 0 else 1
                in_ch = prev_channels if pos == 0 else stage_width
                branches = {
                    name: _build_block(name, stage_cfgs, in_ch, stage_width, stride)
                    for name in stage_cfgs
                }
                blocks.append(ChoiceLayer(branches=branches))
            self.layers.append(blocks)
            prev_channels = stage_width

        self.gap = nn.AdaptiveAvgPool2d(1)
        # head 弹性：in_dim 跟随最后 stage 宽度（固定），out_dim=num_classes。
        self.head = ElasticLinear(super_in_dim=prev_channels, super_out_dim=num_classes)
        self._active_arch_config: ArchConfig | None = None
        self.set_sample_config(search_space.sample())

    def set_sample_config(self, arch_config: ArchConfig):
        arch_config.validate()
        self._active_arch_config = arch_config
        for stage_idx, (stage_name, stage_depth) in enumerate(
            zip(self.search_space.stage_names, arch_config.stage_depths)
        ):
            stage_blocks = self.layers[stage_idx]
            stage_configs = arch_config.layer_configs[stage_name]
            stage_cfgs = self.search_space.stage_layer_configs[stage_idx]
            for pos in range(stage_depth):
                layer_cfg = stage_configs[pos]
                choice = layer_cfg["choice"]
                sample_kwargs = {f"sample_{k}": v for k, v in layer_cfg["config"].items()}
                stage_blocks[pos].set_sample_config(choice_name=choice, **sample_kwargs)
            # 超过激活深度的位置 → 默认配置（前向不触达，但保持内部状态自洽）
            for pos in range(stage_depth, len(stage_blocks)):
                default_choice = next(iter(stage_blocks[pos].branches))
                default_cfg = stage_cfgs[default_choice]
                default_kwargs = {f"sample_{k}": v[0] for k, v in default_cfg.items()}
                stage_blocks[pos].set_sample_config(choice_name=default_choice, **default_kwargs)
        # head：in_dim = 最后 stage 宽度（固定），out_dim = num_classes
        last_width = self.search_space.stage_widths[-1]
        self.head.set_sample_config(sample_in_dim=last_width, sample_out_dim=self.num_classes)

    def forward(self, x):
        x = self.stem(x)
        for stage_idx, stage_blocks in enumerate(self.layers):
            stage_depth = (
                self._active_arch_config.stage_depths[stage_idx]
                if self._active_arch_config is not None
                else len(stage_blocks)
            )
            for pos in range(stage_depth):
                x = stage_blocks[pos](x)
        x = self.gap(x)
        x = x.flatten(1)
        return self.head(x)

    def get_active_subnet(self) -> nn.Module:
        class Subnet(nn.Module):
            def __init__(self, stem, stages, gap, head):
                super().__init__()
                self.stem = stem
                self.stages = stages
                self.gap = gap
                self.head = head

            def forward(self, x):
                x = self.stem(x)
                for stage_blocks in self.stages:
                    for block in stage_blocks:
                        x = block(x)
                x = self.gap(x)
                x = x.flatten(1)
                return self.head(x)

        stages = []
        for stage_idx, stage_blocks in enumerate(self.layers):
            stage_depth = (
                self._active_arch_config.stage_depths[stage_idx]
                if self._active_arch_config is not None
                else len(stage_blocks)
            )
            sub_blocks = nn.ModuleList(
                stage_blocks[pos].get_active_subnet() for pos in range(stage_depth)
            )
            stages.append(sub_blocks)
        return Subnet(
            stem=copy.deepcopy(self.stem),
            stages=nn.ModuleList(stages),
            gap=copy.deepcopy(self.gap),
            head=self.head.get_active_subnet(),
        )

    @property
    def elastic_num_params(self):
        total = sum(p.numel() for p in self.stem.parameters())
        for stage_blocks in self.layers:
            for layer in stage_blocks:
                total += layer.elastic_num_params
        total += sum(p.numel() for p in self.gap.parameters())
        total += self.head.elastic_num_params
        return total


def _main():
    search_space = SearchSpace()
    assert search_space.validate(), "SearchSpace.validate() failed"
    print(f"[template] SearchSpace.validate: True  stages={search_space.stage_names} "
          f"widths={search_space.stage_widths}")

    supernet = SuperNet(search_space, num_classes=10).eval()
    print(f"[template] SuperNet elastic_num_params (default config): {supernet.elastic_num_params}")

    x = torch.randn(2, 3, 16, 16)
    with torch.no_grad():
        for trial in range(5):
            arch = search_space.sample()
            assert arch.validate(), f"ArchConfig.validate failed: {arch}"
            supernet.set_sample_config(arch)
            out_super = supernet(x)
            subnet = supernet.get_active_subnet().eval()
            out_sub = subnet(x)
            diff = (out_super - out_sub).abs().max().item()
            assert diff < 1e-5, f"Consistency check failed (trial {trial}): {diff}"
            choices = [c["choice"] for cfgs in arch.layer_configs.values() for c in cfgs]
            print(f"  [Trial {trial + 1}] depths={arch.stage_depths} choices={choices} "
                  f"params={supernet.elastic_num_params} out={tuple(out_super.shape)} diff={diff:.2e}")
    print(">>> All supernet template tests passed!")


if __name__ == "__main__":
    _main()
