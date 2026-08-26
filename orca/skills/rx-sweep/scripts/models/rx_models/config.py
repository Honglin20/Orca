"""config.py —— rx_models 统一配置（维度单一真相源）。

根治 pure_cnn_model.py 时代的维度漂移：原 ``build_model`` 默认
``num_symbols=64`` 与工程 ``beam_num=32`` 不一致，需手工 patch。本 dataclass
把 P/F/S 收敛到一处，所有模型方案文件**只读 RxConfig、不存硬编码维度默认**。

I/O 契约（全方案一致）：``[B, num_ports, num_subcarriers, num_symbols, 1]``。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class RxConfig:
    """rx_models 全方案共享配置。

    - I/O 维度字段（num_ports/num_subcarriers/num_symbols）：单一真相源，
      ``adapt_project`` 从用户工程 cfg 推一份注入，杜绝 64/32 漂移。
    - 主干结构字段（num_blocks/embed_dim/dilations）：全方案共用默认。
    - 特征前端专属字段（adjbeam_k/diff_orders/fft_axis/cnn_trf_pattern）：
      各特征方案按需读，未用方案忽略。
    """

    # ---- I/O 维度（单一真相源）----
    num_ports: int = 4            # P = polar × {re,im}
    num_subcarriers: int = 48     # F = time_wnd_len_pre + time_wnd_len_aft
    num_symbols: int = 32         # S = beam_num（工程实际值，非 64）

    # ---- 主干结构 ----
    num_blocks: int = 4
    embed_dim: int = 16           # 须 ÷16（昇腾 Cube 对齐），__post_init__ 校验
    dilations: tuple[int, ...] = (1, 2, 4, 8)
    noise_var: float = 1e-2

    # ---- 特征前端专属（各方案按需，未用方案忽略）----
    adjbeam_k: int = 3            # B4 邻波束窗口（相邻 beam 数，奇数为佳）
    diff_orders: tuple[int, ...] = (1, 2)   # B2 差分阶数集合
    fft_axis: str = "F"           # B3 FFT 轴："F" 子载波 / "S" 波束
    # 方案2 交替模式：按 num_blocks 循环填充；"cnn"=DualAxisConvBlock, "trf"=SignalTransformerBlock
    cnn_trf_pattern: tuple[str, ...] = ("cnn", "trf")

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        if self.embed_dim % 16 != 0:
            raise ValueError(
                f"embed_dim 须 ÷16（昇腾 Cube 对齐），got embed_dim={self.embed_dim}"
            )
        if self.num_blocks < 1:
            raise ValueError(f"num_blocks 须 ≥ 1，got {self.num_blocks}")
        if self.num_ports < 1 or self.num_subcarriers < 1 or self.num_symbols < 1:
            raise ValueError(
                f"P/F/S 须 ≥ 1，got "
                f"({self.num_ports},{self.num_subcarriers},{self.num_symbols})"
            )
        dilations = tuple(int(d) for d in self.dilations)
        if not dilations or any(d < 1 for d in dilations):
            raise ValueError(f"dilations 非法：{self.dilations}")
        self.dilations = dilations

        if self.fft_axis not in ("F", "S"):
            raise ValueError(f"fft_axis 须为 'F'/'S'，got {self.fft_axis!r}")
        for i, t in enumerate(self.cnn_trf_pattern):
            if t not in ("cnn", "trf"):
                raise ValueError(
                    f"cnn_trf_pattern[{i}]={t!r} 须为 'cnn'/'trf'"
                )
        if not self.cnn_trf_pattern:
            raise ValueError("cnn_trf_pattern 不能为空")
        diffs = tuple(int(d) for d in self.diff_orders)
        if not diffs or any(d < 1 for d in diffs):
            raise ValueError(f"diff_orders 非法：{self.diff_orders}")
        self.diff_orders = diffs
        if self.adjbeam_k < 1:
            raise ValueError(f"adjbeam_k 须 ≥ 1，got {self.adjbeam_k}")

    # ------------------------------------------------------------------
    # I/O 派生
    # ------------------------------------------------------------------
    @property
    def io_shape(self) -> list[int]:
        """``[1, P, F, S, 1]`` —— 全方案统一 I/O shape。"""
        return [1, self.num_ports, self.num_subcarriers, self.num_symbols, 1]

    @property
    def dummy_input(self) -> dict:
        """gate_check / export_onnx 共用的 dummy 输入声明。"""
        return {"shape": self.io_shape, "dtype": "float32"}

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_kwargs(self) -> dict[str, Any]:
        """展开为 dict（供日志/JSON 落盘；build_model 接收 RxConfig 对象本身）。"""
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str | None) -> "RxConfig":
        d = json.loads(s) if (s and s.strip()) else {}
        if not isinstance(d, dict):
            raise ValueError(f"RxConfig JSON 须为 object，got {type(d).__name__}")
        return cls(**d)

    @classmethod
    def from_project_cfg(cls, project_cfg: Any) -> "RxConfig":
        """从用户工程 cfg 对象推维度（num_symbols=beam_num 等）。

        project_cfg 须有 ``beam_num`` / ``time_wnd_len_pre`` / ``time_wnd_len_aft``
        属性（缺字段 → 用默认）。这是 adapt_project 注入维度的标准入口，
        让维度从用户工程一处流向模型，不在模型里硬编码。
        """
        kw: dict[str, Any] = {}
        beam_num = getattr(project_cfg, "beam_num", None)
        if isinstance(beam_num, int) and beam_num > 0:
            kw["num_symbols"] = beam_num
        pre = getattr(project_cfg, "time_wnd_len_pre", None)
        aft = getattr(project_cfg, "time_wnd_len_aft", None)
        if isinstance(pre, int) and isinstance(aft, int) and (pre + aft) > 0:
            kw["num_subcarriers"] = pre + aft
        return cls(**kw)
