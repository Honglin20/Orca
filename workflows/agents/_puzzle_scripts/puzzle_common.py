"""puzzle_common.py —— Puzzle workflow P2 算法脚本层共享 helper。

无 LLM、无网络、确定性。fail loud（错误抛异常 + 调用方 exit 2）。

核心契约：
  - ``Slot`` dataclass：逐字按 SPEC P2.1（不自加字段）。
  - ``BlockMap``：slot 列表 + JSON 读写。
  - ``candidate_registry``：name -> (factory_fn, applicable_slot_types)；
    factory 签名 ``factory(slot: Slot) -> nn.Module``，输出 shape 必须与
    ``[B, L, slot.out_dim]`` 对齐（输入 ``[B, L, slot.in_dim]``）。
  - ``load_flat_model``：动态加载 flat model 文件并调 build_fn。
  - ``capture_parent_activations``：forward hook 抓每个 slot 的 (in, out) 作
    BLD teacher 信号。
  - ``build_calib_loader``：合成随机张量 DataLoader（通用，不硬编码数据集）。

兄弟 import（禁 sys.path 魔改）：同目录脚本 ``from puzzle_common import ...``。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset


# ── Slot / BlockMap ───────────────────────────────────────────────────────────

@dataclass
class Slot:
    """一个可替换的 transformer sub-block slot。

    逐字按 SPEC P2.1 字段，不自加。
    """
    layer_idx: int
    slot_type: str            # "attention" | "ffn"
    in_dim: int
    out_dim: int
    num_heads: int
    head_dim: int
    source_class: str         # 原块类名（溯源用）
    parent_module_path: str   # ``model.get_submodule(path)`` 可定位


@dataclass
class BlockMap:
    """slot 清单 + JSON 读写。"""
    slots: list[Slot] = field(default_factory=list)

    def to_json(self, path: str | Path) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"slots": [asdict(s) for s in self.slots]}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return str(p)

    @classmethod
    def from_json(cls, path: str | Path) -> "BlockMap":
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        if "slots" not in payload or not isinstance(payload["slots"], list):
            raise ValueError(f"block_map.json 缺 'slots' list：{path}")
        slots = [Slot(**s) for s in payload["slots"]]
        return cls(slots=slots)


# ── flat model 动态加载 ────────────────────────────────────────────────────────

def load_flat_model(
    flat_path: str | Path,
    build_fn: str,
    build_cfg: str | None = None,
) -> nn.Module:
    """动态 import flat model 文件并调 ``build_fn(**build_cfg_kwargs)``。

    build_cfg 为 JSON 字符串（来自 workflow inputs.build_cfg），空串 → 零参调用。
    flat_path 文件目录加入 sys.path（让其本地 import 可解）。
    fail loud：文件不存在 / 无 build_fn / 调用失败 → raise。
    """
    p = Path(flat_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"flat model 文件不存在：{p}")
    here = str(p.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("_puzzle_flat_model", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {p} 构建 module spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, build_fn, None)
    if not callable(fn):
        raise AttributeError(
            f"{p} 无 callable {build_fn!r}（puzzle 契约必备 build_fn）"
        )
    cfg_kwargs: dict[str, Any] = {}
    if build_cfg and build_cfg.strip():
        try:
            parsed = json.loads(build_cfg)
        except json.JSONDecodeError as e:
            raise ValueError(f"build_cfg 非 JSON：{build_cfg!r}（{e}）") from e
        if not isinstance(parsed, dict):
            raise ValueError(f"build_cfg 须为 JSON object，得到 {type(parsed).__name__}")
        cfg_kwargs = parsed
    return fn(**cfg_kwargs)


def _extract_state_dict(ckpt: Any) -> dict[str, torch.Tensor]:
    """从 torch.load 的结果抽取 state_dict，解 wrapper 形态。

    形如 ``{state_dict: {...}, ...}`` 的 wrapper（无 blocks./patch_embed. 等模型层键
    出现在顶层）→ 取内层；否则认为是裸 state_dict 原样返回。与 expand_model.py 的
    ``# 2a)`` 段逻辑一致（DRY：father 权重加载在 expand/bld/score/build/gkd 共用）。
    """
    if (
        isinstance(ckpt, dict)
        and "state_dict" in ckpt
        and not any(k.startswith(("blocks.", "patch_embed.")) for k in ckpt.keys())
    ):
        return ckpt["state_dict"]
    return ckpt


def load_father_model(
    flat_path: str | Path,
    build_fn: str,
    build_cfg: str | None,
    father_state_path: str | Path | None,
) -> nn.Module:
    """加载 flat model + 预训练父权重（Puzzle father/teacher/baseline 契约）。

    Puzzle 的 father/teacher/baseline 必须是预训练模型——bld 的冻结 teacher、
    score 的冻结全模型、gkd 的 teacher 都靠本函数注入同一份预训练权重。

    - father_state_path 为空/None → 回退 load_flat_model（随机 init）+ stderr WARN
      （向后兼容；Puzzle 契约要求预训练 father，空串走随机只留给 dry-run fixture
      等非关键路径，生产路径必须给值）。
    - father_state_path 非空但文件不存在 → raise FileNotFoundError（fail loud——
      father ckpt 缺即 baseline=chance，Puzzle 无的放矢，禁静默降级）。
    - 文件存在 → torch.load + ``_extract_state_dict`` 解 wrapper +
      ``load_state_dict(strict=False)``（missing/unexpected 走 stderr WARN，不 raise
      ——flat model schema 与 ckpt 可能有不相关键）+ ``.eval()``。
    """
    model = load_flat_model(flat_path, build_fn, build_cfg)
    if not father_state_path:
        print(
            "[puzzle_common] WARN: father_state_path 空 → 用随机初始化 father"
            "（向后兼容；Puzzle 契约要求预训练 father,检查 --father_state 透传）",
            file=sys.stderr,
        )
        model.eval()
        return model
    p = Path(father_state_path)
    if not p.is_file():
        raise FileNotFoundError(
            f"father_state 文件不存在: {p}（Puzzle father/teacher/baseline 必须预训练）"
        )
    ckpt = torch.load(p, map_location="cpu", weights_only=False)
    state = _extract_state_dict(ckpt)
    if not isinstance(state, dict):
        raise TypeError(
            f"father_state 解出的 state_dict 非 dict: {type(state).__name__}（{p}）"
        )
    missing, unexpected = model.load_state_dict(state, strict=False)
    total_keys = len(model.state_dict())
    # 大面积 missing → father 权重与 flat_model schema 严重不齐,baseline 会 silent 退化为
    # 近随机 init,后续 score/gkd/gate 全失真而 gate 仍可能"通过"。>20% 即 raise(Rule 12)。
    if total_keys and len(missing) > 0.2 * total_keys:
        raise RuntimeError(
            f"father_state_dict 与 flat_model 严重不齐:{len(missing)}/{total_keys} keys missing "
            f"({100*len(missing)/total_keys:.0f}%)。检查 --father_state / --build_fn/--build_cfg "
            f"是否匹配预训练模型的架构。missing 前 8: {missing[:8]}"
        )
    if missing:
        print(
            f"[puzzle_common] WARN: father load_state_dict missing keys: "
            f"{missing[:8]}（共 {len(missing)} 个,<20% 可接受）",
            file=sys.stderr,
        )
    if unexpected:
        print(
            f"[puzzle_common] WARN: father load_state_dict unexpected keys: "
            f"{unexpected[:8]}（共 {len(unexpected)} 个）",
            file=sys.stderr,
        )
    model.eval()
    return model


def get_module_dummy_input(flat_module_path: str | Path) -> dict[str, Any]:
    """从 flat model 文件读 ``DUMMY_INPUT``（含 shape/dtype）。

    puzzle 不接外部数据集——合成 calibration 输入靠 DUMMY_INPUT 声明真实 I/O 维度。
    fail loud：无 DUMMY_INPUT / 无 shape → raise。
    """
    p = Path(flat_module_path).resolve()
    here = str(p.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("_puzzle_flat_dummy", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    di = getattr(mod, "DUMMY_INPUT", None)
    if not isinstance(di, dict) or not isinstance(di.get("shape"), list) or not di["shape"]:
        raise ValueError(
            f"{p} DUMMY_INPUT 缺 shape（list）——通用 calibration 需要真实 I/O 维度声明"
        )
    return di


# ── 候选块 factory ────────────────────────────────────────────────────────────

class _VanillaMHSA(nn.Module):
    """vanilla MHSA 包装：``nn.MultiheadAttention`` forward 三参 → 单参。"""

    def __init__(self, embed_dim: int, num_heads: int):
        super().__init__()
        if embed_dim % num_heads != 0:
            raise ValueError(
                f"vanilla MHSA: embed_dim={embed_dim} 必须能被 num_heads={num_heads} 整除"
            )
        self.attn = nn.MultiheadAttention(
            embed_dim=embed_dim, num_heads=num_heads, batch_first=True
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.attn(x, x, x, need_weights=False)
        return out


# ── passthrough 候选（SPEC puzzle-design-draft §2.2/§2.3）──────────────────────
# identity 候选 = "原块冻结（基线，不换）"——build/score/latency 遇到时 NOT 替换
# slot，保留父块。与 no_op（nn.Identity 跳过整块，ffn 专用）严格区分。
PASSTHROUGH_VARIANTS: set[str] = {"identity"}


def is_passthrough(variant: str) -> bool:
    """identity 候选 = 保留父块，不替换。"""
    return variant in PASSTHROUGH_VARIANTS


def _factory_random_synthesizer(slot: Slot) -> nn.Module:
    from nas_agent.blocks.random_synthesizer import ElasticRandomSynthesizerCore

    return ElasticRandomSynthesizerCore(
        super_num_heads=max(slot.num_heads, 1),
        global_dim=slot.in_dim,
        head_dim=max(slot.head_dim, 1),
        max_seq_len=512,
    )


def _factory_relu_attention(slot: Slot) -> nn.Module:
    from nas_agent.blocks.relu_attention import ElasticReluAttentionCore

    return ElasticReluAttentionCore(
        super_num_heads=max(slot.num_heads, 1),
        global_dim=slot.in_dim,
        head_dim=max(slot.head_dim, 1),
    )


def _factory_fnet(slot: Slot) -> nn.Module:
    from nas_agent.blocks.fnet_fourier_mixer import ElasticFNetFourierTransform

    return ElasticFNetFourierTransform()


def _factory_softs_star(slot: Slot) -> nn.Module:
    from nas_agent.blocks.softs_star_mixer import ElasticSOFTSSTARMixer

    return ElasticSOFTSSTARMixer(
        super_core_dim=slot.in_dim,
        global_dim=slot.in_dim,
    )


def _factory_vanilla(slot: Slot) -> nn.Module:
    return _VanillaMHSA(embed_dim=slot.in_dim, num_heads=max(slot.num_heads, 1))


def _factory_ffn_pristine(slot: Slot, ratio: float) -> nn.Module:
    """FFN 剪枝候选：Linear-GELU-Linear，中间维 = in_dim * ratio。"""
    intermediate = max(1, int(round(slot.in_dim * ratio)))
    return nn.Sequential(
        nn.Linear(slot.in_dim, intermediate),
        nn.GELU(),
        nn.Linear(intermediate, slot.out_dim),
    )


def _factory_ffn_75(slot: Slot) -> nn.Module:
    return _factory_ffn_pristine(slot, 0.75)


def _factory_ffn_50(slot: Slot) -> nn.Module:
    return _factory_ffn_pristine(slot, 0.50)


def _factory_linear(slot: Slot) -> nn.Module:
    return nn.Linear(slot.in_dim, slot.out_dim)


class _ZeroBlock(nn.Module):
    """零输出 no_op:forward 返回 zeros_like(input)——真·删块(residual 不变,latency≈0)。

    比 nn.Identity 更正确:Identity 让 residual 变 ``x + norm(x)``(加噪),零输出使
    ``x + 0 = x``(残差不变,块被真正旁路)。对 attention/ffn 均适用(in_dim==out_dim)。
    接受 **kwargs:父层可能传 attention_mask/norm_factor 等(异构 forward 签名),忽略。
    """

    def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        return torch.zeros_like(x)


class _KwargPassthrough(nn.Module):
    """variant 签名适配器:把 inner 包成 ``forward(x, *args, **kwargs) -> inner(x)``。

    父层(如 target 的 TransformerEncoderLayer)调 ``self_attn(src, attention_mask=...)``
    带 kwargs;nas_agent 的 Elastic 核 / 自定义 variant 的 forward(x) 不收 → TypeError。
    本包装忽略额外参,只把首参(输入 tensor)传给 inner。state_dict 加 ``inner.`` 前缀,
    在存(BLD)/载(score/latency/build)两侧一致(都经 candidate_registry),无需改 load 逻辑。
    """

    def __init__(self, inner: nn.Module):
        super().__init__()
        self.inner = inner

    def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        return self.inner(x)


def _wrap(inner_factory: Callable[[Slot], nn.Module]) -> Callable[[Slot], nn.Module]:
    """把 slot→module 工厂包成 slot→_KwargPassthrough(module)。"""

    def _w(slot: Slot) -> nn.Module:
        return _KwargPassthrough(inner_factory(slot))

    return _w


def _factory_no_op(slot: Slot) -> nn.Module:
    if slot.in_dim != slot.out_dim:
        raise ValueError(
            f"no_op 候选要求 in_dim==out_dim（slot {slot.parent_module_path}: "
            f"{slot.in_dim}!={slot.out_dim}）"
        )
    return _ZeroBlock()


# name -> (factory_fn, applicable slot_types)
# 注意：``identity`` 是 passthrough（保留父块），不在 registry——由
# ``is_passthrough()`` 单独判定，不在 build/score/latency 替换 slot。
# 非 no_op 的 variant 经 _wrap 包 _KwargPassthrough(适配异构 forward 签名,如 attention_mask)。
candidate_registry: dict[str, tuple[Callable[[Slot], nn.Module], set[str]]] = {
    # attention 候选（identity 走 PASSTHROUGH_VARIANTS 分支）
    "random_synthesizer": (_wrap(_factory_random_synthesizer), {"attention"}),
    "relu_attention": (_wrap(_factory_relu_attention), {"attention"}),
    "fnet": (_wrap(_factory_fnet), {"attention"}),
    "softs_star": (_wrap(_factory_softs_star), {"attention"}),
    "vanilla": (_wrap(_factory_vanilla), {"attention"}),
    # ffn 候选（identity 走 PASSTHROUGH_VARIANTS 分支）
    "ffn_75": (_wrap(_factory_ffn_75), {"ffn"}),
    "ffn_50": (_wrap(_factory_ffn_50), {"ffn"}),
    "linear": (_wrap(_factory_linear), {"ffn"}),
    "no_op": (_factory_no_op, {"ffn", "attention"}),
}

# 候选名 → 适用 slot_types（含 passthrough）。parse_block_candidates 用。
_CANDIDATE_APPLICABILITY: dict[str, set[str]] = {
    "identity": {"attention", "ffn"},  # passthrough
    **{k: v[1] for k, v in candidate_registry.items()},
}


def get_default_candidates() -> dict[str, list[str]]:
    """默认候选集（SPEC §2.2-2.3）。"""
    return {
        "attention": [
            "identity",
            "random_synthesizer",
            "relu_attention",
            "fnet",
            "softs_star",
            "vanilla",
            "no_op",
        ],
        "ffn": ["identity", "ffn_75", "ffn_50", "linear", "no_op"],
    }


def parse_block_candidates(raw: str | None) -> dict[str, list[str]]:
    """解析 inputs.block_candidates（JSON 或空）→ {attention: [...], ffn: [...]}。

    空 → 默认集；非空 JSON 必须是 dict 且 attention/ffn 字段为 list[str]。
    fail loud：非法 JSON / 缺字段 / 候选名未注册 / 候选不适用 slot_type → raise。
    """
    if not raw or not raw.strip():
        return get_default_candidates()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"block_candidates 非 JSON：{raw!r}（{e}）") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"block_candidates 须为 JSON object，得到 {type(parsed).__name__}")
    out: dict[str, list[str]] = {}
    for key in ("attention", "ffn"):
        val = parsed.get(key)
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise ValueError(f"block_candidates.{key} 须为 list[str]")
        for name in val:
            applic = _CANDIDATE_APPLICABILITY.get(name)
            if applic is None:
                raise ValueError(f"候选 {name!r} 未注册（registry ∪ PASSTHROUGH_VARIANTS）")
            if key not in applic:
                raise ValueError(
                    f"候选 {name!r} 不适用于 {key} slot（适用集：{applic}）"
                )
        out[key] = val
    if "attention" not in out or "ffn" not in out:
        raise ValueError("block_candidates 必须同时含 attention + ffn 两个 key")
    return out


# ── 合成 calibration DataLoader ────────────────────────────────────────────────

class _TensorDataset(Dataset):
    """每样本一个张量（per_sample_shape）；DataLoader 在 batch 维 stack。

    不存预 batch 的张量，避免 DataLoader 再 stack 一层（之前 bug）。
    """

    def __init__(self, samples: list[torch.Tensor]):
        self.samples = samples

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> torch.Tensor:
        return self.samples[idx]


def build_calib_loader(
    model: nn.Module,
    dummy_input: dict[str, Any] | None = None,
    batch_size: int = 2,
    num_batches: int = 2,
    device: torch.device | None = None,
) -> DataLoader:
    """合成随机张量 DataLoader（通用，不硬编码数据集）。

    dummy_input 形如 ``{"shape": [B, ...], "dtype": "float32"}``（取自 flat model
    的 ``DUMMY_INPUT``）。``shape[1:]`` 作单样本形状；DataLoader 把 ``batch_size``
    个样本 stack 成 ``[batch_size, *per_sample_shape]``。共生成
    ``num_batches * batch_size`` 个样本。
    """
    if dummy_input is None:
        raise ValueError("build_calib_loader 需要 dummy_input（DUMMY_INPUT 声明）")
    shape = list(dummy_input["shape"])
    if not shape:
        raise ValueError(f"DUMMY_INPUT.shape 空：{dummy_input!r}")
    dtype_name = str(dummy_input.get("dtype", "float32"))
    dtype = getattr(torch, dtype_name)
    per_sample_shape = shape[1:]  # 去掉 batch 维
    n_samples = max(1, num_batches) * max(1, batch_size)
    samples = [torch.randn(*per_sample_shape, dtype=dtype) for _ in range(n_samples)]
    if device is not None:
        samples = [s.to(device) for s in samples]
    return DataLoader(_TensorDataset(samples), batch_size=batch_size, shuffle=False)


# ── 父激活捕获（BLD teacher 信号）─────────────────────────────────────────────

def capture_parent_activations(
    model: nn.Module,
    block_map: BlockMap,
    calib_loader: DataLoader,
    device: torch.device,
) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    """用 forward hooks 捕获每个 slot 的 (input, output)。

    返回 ``{parent_module_path: (in_tensor, out_tensor)}``。取首个非空 batch。
    fail loud：任何 slot 的 module path 在 model 中无法定位 → raise。
    """
    model.eval().to(device)
    targets: dict[str, nn.Module] = {}
    for slot in block_map.slots:
        try:
            mod = model.get_submodule(slot.parent_module_path)
        except AttributeError as e:
            raise AttributeError(
                f"slot {slot.parent_module_path!r} 在 model 中找不到（get_submodule 失败）：{e}"
            ) from e
        targets[slot.parent_module_path] = mod

    captured: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    handles: list[Any] = []

    def make_hook(path: str):
        def hook(_mod: nn.Module, inputs: tuple, output: Any):
            if path in captured:
                return  # 已抓到，保留首个
            in_t = inputs[0] if isinstance(inputs, tuple) and inputs else inputs
            if not isinstance(in_t, torch.Tensor):
                # 非张量输入（如 tuple）→ 取首个张量
                in_t = inputs[0] if isinstance(inputs, tuple) else inputs
            if isinstance(output, tuple):
                out_t = output[0]
            elif isinstance(output, torch.Tensor):
                out_t = output
            else:
                out_t = output
            captured[path] = (in_t.detach(), out_t.detach())
        return hook

    for path, mod in targets.items():
        handles.append(mod.register_forward_hook(make_hook(path)))

    try:
        with torch.no_grad():
            for batch in calib_loader:
                if isinstance(batch, (list, tuple)):
                    inp = batch[0]
                else:
                    inp = batch
                inp = inp.to(device)
                model(inp)
                # 全部 slot 抓到就停（贪首个 batch）
                if len(captured) == len(targets):
                    break
    finally:
        for h in handles:
            h.remove()

    missing = [p for p in targets if p not in captured]
    if missing:
        raise RuntimeError(
            f"capture_parent_activations 未能捕获 {len(missing)} 个 slot：{missing[:3]}…"
        )
    return captured


# ── slot key / variant 文件名 ─────────────────────────────────────────────────

def slot_key(layer_idx: int, slot_type: str) -> str:
    """统一 slot 唯一 key（jsonl/gkd 复用）。"""
    return f"L{layer_idx}_{slot_type}"


def variant_file_name(layer_idx: int, slot_type: str, variant: str) -> str:
    """block_library 内单 variant 权重文件名。"""
    return f"L{layer_idx}_{slot_type}_{variant}.pt"


def split_parent_path(parent_module_path: str) -> tuple[str, str]:
    """``a.b.c`` -> ``("a.b", "c")``；顶层 ``c`` -> ``("", "c")``。"""
    if "." in parent_module_path:
        parent_path, attr = parent_module_path.rsplit(".", 1)
    else:
        parent_path, attr = "", parent_module_path
    return parent_path, attr


def replace_slot(
    model: nn.Module, parent_module_path: str, new_module: nn.Module
) -> nn.Module:
    """把 model 内 parent_module_path 处的子模块替换为 new_module，返回原子模块。"""
    parent_path, attr = split_parent_path(parent_module_path)
    parent = model.get_submodule(parent_path) if parent_path else model
    if not hasattr(parent, attr):
        raise AttributeError(
            f"无法替换 slot：{parent_module_path!r}（父 {type(parent).__name__} 无属性 {attr!r}）"
        )
    original = getattr(parent, attr)
    setattr(parent, attr, new_module)
    return original


# ── eval_fn 解析 ──────────────────────────────────────────────────────────────

def resolve_eval_fn(
    eval_fn: str, flat_model_path: str | Path
) -> Callable[[nn.Module], float]:
    """解析 eval_fn：``path::func`` 外部文件，或 flat module 内函数名。

    返回 ``fn(model) -> float``（acc 或 loss，方向由 eval_kind 决定）。
    fail loud：找不到 / 不是 callable → raise。
    """
    if "::" in eval_fn:
        ext_path, func = eval_fn.split("::", 1)
        p = Path(ext_path).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"eval_fn 文件不存在：{p}")
        here = str(p.parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        spec = importlib.util.spec_from_file_location("_puzzle_eval_ext", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, func, None)
    else:
        fp = Path(flat_model_path).resolve()
        here = str(fp.parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        spec = importlib.util.spec_from_file_location("_puzzle_flat_eval", fp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fn = getattr(mod, eval_fn, None)
    if not callable(fn):
        raise AttributeError(f"eval_fn {eval_fn!r} 不是 callable")
    return fn


# ── 外部 callable 解析（path::func，DRY：expand/latency_table/gate 复用）──────

def load_external_callable(path_func: str) -> Callable:
    """解析 ``path::func`` 字符串 → callable。

    文件目录加入 sys.path（让其本地 import 可解）。fail loud：
    缺 ``::`` / 文件不存在 / 不是 callable → raise。
    """
    if "::" not in path_func:
        raise ValueError(f"需 'path::func' 形态，得到 {path_func!r}")
    ext_path, func = path_func.split("::", 1)
    p = Path(ext_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"外部 callable 文件不存在：{p}")
    here = str(p.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location(
        f"_puzzle_ext_{p.stem}_{func}", p
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, func, None)
    if not callable(fn):
        raise TypeError(f"{path_func} 不是 callable")
    return fn


# ── variant state_dict 加载（统一 fail loud，DRY）─────────────────────────────

def load_variant_state_dict(
    module: nn.Module,
    sd: dict[str, torch.Tensor],
    variant: str,
    *,
    strict_unexpected: bool = True,
) -> None:
    """载入 variant 的 state_dict；fail loud 检查 missing/unexpected。

    - missing keys 非空 → raise（factory 与 ckpt schema 不对齐）
    - unexpected keys 非空 → raise（``strict_unexpected=True``，默认；
      `` False`` 时仅 stderr WARN——仅留给 BLD 刚训完即存的同源路径）
    """
    if not sd:
        return  # passthrough / 零参数 variant 的空 state_dict
    missing, unexpected = module.load_state_dict(sd, strict=False)
    if missing:
        raise RuntimeError(
            f"variant {variant!r} load_state_dict 缺 key：{missing[:5]}"
            f"（共 {len(missing)} 个）"
        )
    if unexpected and strict_unexpected:
        raise RuntimeError(
            f"variant {variant!r} load_state_dict 意外 key：{unexpected[:5]}"
            f"（共 {len(unexpected)} 个，factory 与 ckpt schema 不对齐）"
        )
    if unexpected:
        print(
            f"[puzzle_common] WARN: variant {variant!r} 忽略 unexpected "
            f"keys {len(unexpected)} 个",
            file=sys.stderr,
        )


# ── 从 selected_arch 重建异构 student（DRY：build/gkd/gate 复用）──────────────

def build_student_from_arch(
    flat_model_path: str | Path,
    build_fn: str,
    build_cfg: str,
    block_map: "BlockMap",
    selected_arch: dict,
    block_library_dir: str | Path,
    device: torch.device,
    father_state_path: str | Path | None = None,
) -> nn.Module:
    """通用：从 selected_arch + block_library 重建异构 student 模型。

    - identity（passthrough）：跳过替换，保留父块。
    - 其他 variant：factory 实例化 + load ckpt（``load_variant_state_dict`` 严格）。
    - no_op / 零参 variant：照常 factory，空 state_dict 跳过 load。

    father_state_path 非空 → base arch 用 ``load_father_model`` 注入预训练父权重，
    使 identity（passthrough）slot 保留的是 father 权重而非随机初始化。空/None →
    回退 ``load_flat_model``（随机 init；适用于其后还会用 selected/final state_dict
    覆盖的 student 场景，如 gkd/gate）。
    """
    if father_state_path:
        model = load_father_model(
            flat_model_path, build_fn, build_cfg, father_state_path
        )
    else:
        model = load_flat_model(flat_model_path, build_fn, build_cfg)
    arch = selected_arch.get("selected_arch", selected_arch) if isinstance(
        selected_arch, dict
    ) else {}
    chosen: dict[tuple[int, str], str] = {}
    for layer_str, slot_dict in arch.items():
        for slot_type, variant in slot_dict.items():
            chosen[(int(layer_str), slot_type)] = str(variant)

    lib = Path(block_library_dir).resolve()
    for slot in block_map.slots:
        key = (slot.layer_idx, slot.slot_type)
        if key not in chosen:
            continue
        variant = chosen[key]
        if is_passthrough(variant):
            continue  # 保留父块，不替换
        factory, applicable = candidate_registry[variant]
        if slot.slot_type not in applicable:
            raise ValueError(
                f"variant {variant!r} 不适用 slot_type={slot.slot_type}"
            )
        new_module = factory(slot).to(device).eval()
        ckpt_path = lib / variant_file_name(slot.layer_idx, slot.slot_type, variant)
        if ckpt_path.is_file():
            ckpt = torch.load(
                ckpt_path, map_location=device, weights_only=False
            )
            sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
            load_variant_state_dict(new_module, sd, variant, strict_unexpected=True)
        replace_slot(model, slot.parent_module_path, new_module)
    return model
