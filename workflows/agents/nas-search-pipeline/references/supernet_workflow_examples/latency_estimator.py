"""Online latency estimator for hierarchical supernets.

Measures whole-architecture latency on-the-fly using PyTorch directly.
"""


import torch

from nas_agent.latency.pytorch_latency_utils import measure_module_latency
from nas_agent.search.arch_utils import hash_arch
from nas_agent.train import empty_cache

# Generated scripts should replace this with the concrete supernet import.
from supernet import ArchConfig, SearchSpace, SuperNet


class LatencyEstimator:
    def __init__(
        self,
        search_space: SearchSpace,
        latency_cfg,
        device: str | torch.device = "cpu",
    ) -> None:
        self.search_space = search_space
        self.latency_cfg = latency_cfg
        self.device = torch.device(device)
        self.supernet = SuperNet(search_space)
        self.supernet.to(torch.device("cpu"))

    def get_latency(self, arch_config: ArchConfig) -> float:
        """Return estimated latency in ms for one sampled architecture.

        Extracts the active subnet and measures latency using PyTorch.
        """
        self.supernet.set_sample_config(arch_config)
        subnet = self.supernet.get_active_subnet()
        subnet.eval()

        # Single-tensor dummy input matching the supernet's forward signature.
        # For models with multi-arg or kwargs forward signatures, construct
        # the corresponding inputs here (e.g. a tuple of tensors).
        dummy_input = torch.randn(self.latency_cfg.batch_size, 3, 224, 224)

        # Generate a unique model name based on the architecture config.
        # Reserved for future ONNX-based latency measurements that require unique filenames.
        # Keep `# noqa: F841` to suppress the unused-variable lint.
        arch_id = hash_arch(arch_config)
        model_name = f"subnet_{arch_id}"  # noqa: F841

        latency = measure_module_latency(
            subnet,
            dummy_input,
            device=self.device,
            warmup=self.latency_cfg.warmup,
            repetitions=self.latency_cfg.repetitions,
        )
        
        # Free device memory occupied by the subnet.
        del subnet
        empty_cache(self.device)
        
        return latency


if __name__ == "__main__":
    import argparse

    from omegaconf import OmegaConf

    parser = argparse.ArgumentParser(
        description="Smoke-test online latency estimator."
    )
    parser.add_argument(
        "--device", default="cpu",
        help="Target device for latency measurement (e.g. cpu, cuda:0, npu:0).",
    )
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--repetitions", type=int, default=100)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--num_samples", type=int, default=5)

    args = parser.parse_args()

    latency_cfg = OmegaConf.create(
        {
            "warmup": args.warmup,
            "repetitions": args.repetitions,
            "batch_size": args.batch_size,
        }
    )

    search_space = SearchSpace()
    estimator = LatencyEstimator(search_space, latency_cfg, device=args.device)
    for i in range(args.num_samples):
        arch_config = search_space.sample()
        latency = estimator.get_latency(arch_config)
        print(f"[{i + 1}/{args.num_samples}] latency = {latency:.3f} ms")
        if latency < 0.0:
            raise AssertionError(f"Latency must be non-negative, got {latency}.")
    print(f"Checked {args.num_samples} sampled architectures.")
