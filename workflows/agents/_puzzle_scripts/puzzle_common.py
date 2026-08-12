"""puzzle_common.py —— Puzzle workflow 算法脚本层共享 helper（U6 适配器架构）。

U6 范式翻转（SPEC puzzle-u6-design-draft §2）：脚本不再假设任何用户代码形态，
所有项目相关性收敛到 agent 在 pz_expand 移植生成的 ``puzzle_adapters.py``。本模块
提供 ``load_puzzle_adapters`` 加载器 + 校验，以及若干项目无关的算法 helper。

核心契约（确定性，fail loud）：
  - ``Slot`` / ``BlockMap``：可替换 sub-block slot（SPEC v2 §4.1，kind 开放标签）。
  - ``load_puzzle_adapters(path)``：动态加载 puzzle_adapters.py 并校验能力 API
    （SPEC U6 §2.1）。脚本唯一项目接口。
  - ``_LoadResult``：ckpt 加载结果（missing/unexpected/from_scratch）。
  - ``candidate_registry`` / ``load_catalog`` / ``get_candidate``：catalog loader API。
  - ``is_valid_ffn_prune`` / ``is_candidate_valid_for_slot``：E6/E8 结构验证器。
  - ``capture_parent_activations``：forward hook 抓每个 slot 的 (in, out) 作 BLD teacher
    信号——经 ``adapters.forward_model(model, batch)`` 喂模型（不再假设单 tensor）。
  - ``measure_whole_model_latency``：整模 latency（DRY：measure_baseline + gate_report）。
  - ``build_student_from_arch``：从 selected_arch + block_library 重建异构 student。

兄弟 import（禁 sys.path 魔改）：同目录脚本 ``from puzzle_common import ...``。
"""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

import torch
import torch.nn as nn

# 候选块实现库（puzzle_blocks）由 load_catalog 函数内 lazy import（避免循环）。
# builtin factory 字符串形如 ``puzzle_blocks::make_<name>``。


# ── Slot / BlockMap ───────────────────────────────────────────────────────────

@dataclass
class Slot:
    """一个可替换的 transformer sub-block slot（SPEC v2 §4.1）。

    ``kind`` 替代 v1 的 ``slot_type``（E3，开放标签 attention/ffn/conv/moe/custom）。
    字段语义详见 SPEC v2 §4.1；本 dataclass 仅承载结构信息，与项目 forward 签名无关。
    """
    layer_idx: int
    kind: str                 # E3：开放标签（attention/ffn/conv/moe/custom）
    in_dim: int
    out_dim: int
    num_heads: int
    head_dim: int
    source_class: str         # 原块类名（溯源 + 结构验证用）
    parent_module_path: str   # ``model.get_submodule(path)`` 可定位
    forward_arity: str = "single"
    return_arity: str = "single"
    original_intermediate: int | None = None
    activation: str | None = None
    ffn_struct: str = "standard"
    mask_load_bearing: bool = False


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


# ── ckpt 加载结果（U6 §2.1 _LoadResult）─────────────────────────────────────────

class _LoadResult(NamedTuple):
    """``adapters.load_pretrained(model)`` 的返回契约（SPEC U6 §2.1）。

    - ``missing`` / ``unexpected``：load_state_dict(strict=False) 的结果，供脚本记录。
    - ``from_scratch``：适配器判定本次 load 实质未恢复预训练权重（如 ckpt 缺失 /
      schema 完全不齐 → 适配器选择 from-scratch）。脚本据此标注 baseline_metrics。
    """
    missing: list[str]
    unexpected: list[str]
    from_scratch: bool


# ── 适配器能力 API 加载层（SPEC U6 §2.1，脚本唯一项目接口）────────────────────────

# 脚本依赖的适配器能力（缺则 fail loud）。每条 (name, kind, required)。
# kind: "callable" | "str_const" | "float_const" | "dict_const"
_ADAPTER_REQUIRED_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("build_model", "callable"),
    ("FORWARD_CALLING_CONVENTION", "str_const"),
    ("forward_model", "callable"),
    ("calib_iter", "callable"),
    ("train_iter", "callable"),
    ("extract_labels", "callable"),
    ("kd_loss", "callable"),
    ("task_loss", "callable"),
    ("evaluate", "callable"),
    ("METRIC_DIRECTION", "str_const"),
    ("EVAL_NOISE_ATOL", "float_const"),
    ("load_pretrained", "callable"),
    ("DUMMY_INPUT", "dict_const"),
)

_ALLOWED_FORWARD_CONVENTIONS: frozenset[str] = frozenset({"positional", "dict", "single"})
_ALLOWED_METRIC_DIRECTIONS: frozenset[str] = frozenset({"higher-better", "lower-better"})


def load_puzzle_adapters(path: str | Path) -> Any:
    """从 ``path`` import puzzle_adapters.py 模块，校验能力 API，返回模块对象。

    SPEC U6 §2.1：脚本的唯一项目接口。模块须暴露下列能力（签名稳定）：
      - ``build_model() -> nn.Module``：零参实例化（agent 把 config 烧进去）。
      - ``FORWARD_CALLING_CONVENTION``：``"positional"|"dict"|"single"``。
      - ``forward_model(model, batch) -> output``：按 convention 调 model(...)。
      - ``calib_iter(device=None) -> Iterator[batch]``：真实 calib 数据。
      - ``train_iter(device=None) -> Iterator[batch]``：真实训练数据（含 labels）。
      - ``extract_labels(batch) -> Tensor | None``：从 native batch 抽标签。
      - ``kd_loss(s_out, t_out, labels=None) -> Tensor``：项目正确的 KD loss。
      - ``task_loss(s_out, labels) -> Tensor | None``：硬标签监督（None 则无）。
      - ``evaluate(model) -> float``：移植用户 eval 协议。
      - ``METRIC_DIRECTION``：``"higher-better"|"lower-better"``。
      - ``EVAL_NOISE_ATOL``：float，eval-stability 容差。
      - ``load_pretrained(model) -> _LoadResult``：宽松 ckpt 加载。
      - ``DUMMY_INPUT``：dict，真实 I/O 维度声明。

    缺关键能力 → fail loud（点名缺哪个）。文件目录加入 sys.path（让 adapter 的本地
    import 可解，如 ``from flat_model import build_model``）。
    """
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"puzzle_adapters 文件不存在：{p}")
    here = str(p.parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    spec = importlib.util.spec_from_file_location("_puzzle_adapters", p)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {p} 构建 module spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 校验：逐项检查能力存在 + 类型对（fail loud 点名缺哪个）。
    for name, kind in _ADAPTER_REQUIRED_CAPABILITIES:
        if not hasattr(mod, name):
            raise AttributeError(
                f"puzzle_adapters {p.name} 缺能力 {name!r}（SPEC U6 §2.1 契约）"
            )
        val = getattr(mod, name)
        if kind == "callable":
            if not callable(val):
                raise TypeError(
                    f"puzzle_adapters.{name} 须为 callable，得到 {type(val).__name__}"
                )
        elif kind == "str_const":
            if not isinstance(val, str):
                raise TypeError(
                    f"puzzle_adapters.{name} 须为 str，得到 {type(val).__name__}"
                )
        elif kind == "float_const":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise TypeError(
                    f"puzzle_adapters.{name} 须为 float，得到 {type(val).__name__}"
                )
        elif kind == "dict_const":
            if not isinstance(val, dict):
                raise TypeError(
                    f"puzzle_adapters.{name} 须为 dict，得到 {type(val).__name__}"
                )

    fc = mod.FORWARD_CALLING_CONVENTION
    if fc not in _ALLOWED_FORWARD_CONVENTIONS:
        raise ValueError(
            f"puzzle_adapters.FORWARD_CALLING_CONVENTION={fc!r} 非法"
            f"（允许：{sorted(_ALLOWED_FORWARD_CONVENTIONS)}）"
        )
    md = mod.METRIC_DIRECTION
    if md not in _ALLOWED_METRIC_DIRECTIONS:
        raise ValueError(
            f"puzzle_adapters.METRIC_DIRECTION={md!r} 非法"
            f"（允许：{sorted(_ALLOWED_METRIC_DIRECTIONS)}）"
        )
    return mod


def build_pretrained_model(adapters: Any) -> nn.Module:
    """``adapters.build_model()`` + ``adapters.load_pretrained(model)``。

    脚本创建预训练 father/teacher 的统一入口。U6：脚本不再做 strict-load 双零硬门——
    load 的前缀剥离 / 多字段 dict / module./_orig_mod./ema. 由适配器消化，结果记入
    baseline_metrics（``_LoadResult``）。
    """
    model = adapters.build_model()
    if not isinstance(model, nn.Module):
        raise TypeError(
            f"adapters.build_model() 返回非 nn.Module：{type(model).__name__}"
        )
    result = adapters.load_pretrained(model)
    if not isinstance(result, _LoadResult) and not (
        hasattr(result, "missing") and hasattr(result, "unexpected")
        and hasattr(result, "from_scratch")
    ):
        raise TypeError(
            f"adapters.load_pretrained(model) 须返 _LoadResult，得到 {type(result).__name__}"
        )
    if result.from_scratch:
        print(
            f"[puzzle_common] WARN: adapters.load_pretrained 标记 from_scratch=True"
            f"（missing={len(result.missing)}, unexpected={len(result.unexpected)}）"
            f"——baseline 可能近随机 init，检查 ckpt 路径与 schema 对齐",
            file=sys.stderr,
        )
    model.eval()
    return model


# ── flat model 动态加载（仍保留：flat 是架构源；adapter 是项目接口）─────────────

def load_flat_model(
    flat_path: str | Path,
    build_fn: str,
    build_cfg: str | None = None,
) -> nn.Module:
    """动态 import flat model 文件并调 ``build_fn(**build_cfg_kwargs)``。

    U6：脚本主要通过 ``adapters.build_model()`` 实例化；本 helper 保留给
    ``build_student_from_arch`` 等需要直接重建架构骨架的路径（flat 与 adapter 共存，
    adapter 内部典型地 ``from <flat> import build_model``）。
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


def get_module_dummy_input(flat_module_path: str | Path) -> dict[str, Any]:
    """从 flat model 文件读 ``DUMMY_INPUT``（含 shape/dtype）。

    U6：DUMMY_INPUT 也可由 adapter 提供（``adapters.DUMMY_INPUT``）；本 helper 仍读 flat
    文件，给 ``build_student_from_arch`` 等内部路径用。fail loud：无 DUMMY_INPUT → raise。
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


# ── passthrough 候选（SPEC v2 §3：identity = 保留 father-loaded 模块）──────────

PASSTHROUGH_VARIANTS: set[str] = {"identity"}


def is_passthrough(variant: str) -> bool:
    """identity 候选 = 保留父块，不替换。"""
    return variant in PASSTHROUGH_VARIANTS


# ── candidate catalog loader（SPEC v2 §5）──────────────────────────────────────

_ALL_KINDS: tuple[str, ...] = ("attention", "ffn", "conv", "moe", "custom")
_CATALOG_PATH = Path(__file__).resolve().parent / "candidate_catalog.yaml"


@dataclass
class CatalogEntry:
    """candidate catalog 单条目（SPEC v2 §5.1）。"""
    name: str
    kinds: set[str]
    source: str
    factory: Callable[[Slot], nn.Module] | None
    params: dict[str, Any]
    align: str
    trainable: bool
    mask_aware: bool
    requires_ffn_struct: list[str]
    description: str


def _resolve_builtin_factory(
    spec: str, params: dict[str, Any], mask_aware: bool = False
) -> Callable[[Slot], nn.Module]:
    """``puzzle_blocks::make_<name>`` + params → 统一 factory(slot)。

    ``mask_aware=True`` 时用 ``puzzle_blocks._wrap_mask``（保留 attn_mask kwarg，
    SPEC U6 §3 root cause F），否则用 ``puzzle_blocks._wrap``（剥 kwargs）。
    """
    import puzzle_blocks  # lazy import，断循环

    if "::" not in spec:
        raise ValueError(f"builtin factory 须为 'module::func' 形态，得到 {spec!r}")
    mod_name, func_name = spec.split("::", 1)
    if mod_name != "puzzle_blocks":
        raise ValueError(
            f"builtin factory 模块必须是 puzzle_blocks（catalog 契约），得到 {mod_name!r}"
        )
    fn = getattr(puzzle_blocks, func_name, None)
    if not callable(fn):
        raise AttributeError(f"puzzle_blocks 无 callable {func_name!r}（catalog 引用）")
    bound: Callable[[Slot], nn.Module] = (
        functools.partial(fn, **params) if params else fn
    )
    wrapper = puzzle_blocks._wrap_mask if mask_aware else puzzle_blocks._wrap
    return wrapper(bound)


def load_catalog(path: str | Path | None = None) -> dict[str, CatalogEntry]:
    """读 candidate_catalog.yaml → ``{name: CatalogEntry}``。"""
    import yaml

    p = Path(path) if path else _CATALOG_PATH
    if not p.is_file():
        raise FileNotFoundError(f"candidate_catalog.yaml 不存在：{p}")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, list):
        raise ValueError(f"{p} 顶层须为 list（每条候选一个 dict），得到 {type(raw).__name__}")

    catalog: dict[str, CatalogEntry] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{p} 条目须为 dict，得到 {type(item).__name__}：{item!r}")
        try:
            name = item["name"]
            kinds_list = item["kind"]
            source = item["source"]
        except KeyError as e:
            raise ValueError(f"{p} 候选条目缺字段 {e}：{item!r}") from e
        if not isinstance(name, str) or not name:
            raise ValueError(f"{p} 候选 name 须为非空 str：{item!r}")
        if not isinstance(kinds_list, list) or not all(
            isinstance(k, str) for k in kinds_list
        ):
            raise ValueError(f"{p} 候选 {name!r} kind 须为 list[str]")
        params = item.get("params", {}) or {}
        if not isinstance(params, dict):
            raise ValueError(f"{p} 候选 {name!r} params 须为 dict")
        factory: Callable[[Slot], nn.Module] | None
        if source == "passthrough":
            factory = None
            if name not in PASSTHROUGH_VARIANTS:
                raise ValueError(
                    f"{p} source=passthrough 但 name={name!r} 不在 PASSTHROUGH_VARIANTS"
                )
        elif source == "builtin":
            spec = item.get("factory")
            if not isinstance(spec, str):
                raise ValueError(f"{p} builtin 候选 {name!r} 缺 factory 字符串")
            mask_aware_flag = bool(item.get("mask_aware", False))
            factory = _resolve_builtin_factory(spec, params, mask_aware=mask_aware_flag)
        else:
            raise ValueError(f"{p} 候选 {name!r} source 须为 builtin|passthrough，得到 {source!r}")
        catalog[name] = CatalogEntry(
            name=name,
            kinds=set(kinds_list),
            source=source,
            factory=factory,
            params=params,
            align=str(item.get("align", "passthrough")),
            trainable=bool(item.get("trainable", True)),
            mask_aware=bool(item.get("mask_aware", False)),
            requires_ffn_struct=list(item.get("requires_ffn_struct", [])),
            description=str(item.get("description", "")),
        )

    for pt in PASSTHROUGH_VARIANTS:
        if pt not in catalog:
            raise ValueError(f"{p} 缺 passthrough 候选 {pt!r}（identity 必入 catalog）")
        if catalog[pt].factory is not None:
            raise ValueError(f"{p} passthrough 候选 {pt!r} 必须无 factory")
    return catalog


candidate_registry: dict[str, CatalogEntry] = load_catalog()


def get_candidate(name: str, catalog: dict[str, CatalogEntry] | None = None) -> CatalogEntry:
    """按名查 catalog 条目；未注册 fail loud。"""
    c = catalog if catalog is not None else candidate_registry
    if name not in c:
        raise ValueError(
            f"候选 {name!r} 未在 catalog 注册（可用：{sorted(c)}）"
        )
    return c[name]


def get_default_candidates() -> dict[str, list[str]]:
    """默认候选集（SPEC v2 §5.4 / D4）。"""
    return {
        "attention": [
            "identity",
            "random_synthesizer",
            "relu_attention",
            "fnet",
            "softs_star",
            "vanilla",
            "masked_vanilla",
            "no_op",
        ],
        "ffn": ["identity", "ffn_75", "ffn_50", "linear", "no_op"],
        "conv": ["identity"],
        "moe": ["identity"],
        "custom": ["identity"],
    }


def parse_block_candidates(raw: str | None) -> dict[str, list[str]]:
    """解析 inputs.block_candidates（JSON 或空）→ ``{kind: [candidate, ...]}``。"""
    if not raw or not raw.strip():
        return get_default_candidates()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"block_candidates 非 JSON：{raw!r}（{e}）") from e
    if not isinstance(parsed, dict):
        raise ValueError(f"block_candidates 须为 JSON object，得到 {type(parsed).__name__}")
    if not parsed:
        raise ValueError("block_candidates 须为非空 dict（至少一个 kind key）")

    out: dict[str, list[str]] = {}
    for kind, val in parsed.items():
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise ValueError(f"block_candidates.{kind} 须为 list[str]")
        if not val:
            raise ValueError(f"block_candidates.{kind} 须为非空 list[str]")
        for name in val:
            entry = candidate_registry.get(name)
            if entry is None:
                raise ValueError(
                    f"候选 {name!r} 未在 catalog 注册（可用：{sorted(candidate_registry)}）"
                )
            if kind not in entry.kinds:
                raise ValueError(
                    f"候选 {name!r} 不适用于 kind={kind!r}（适用集：{sorted(entry.kinds)}）"
                )
        if "identity" not in val:
            raise ValueError(
                f"block_candidates.{kind} 缺 identity（identity 必入每 kind 候选）"
            )
        out[kind] = val
    return out


# ── candidate-slot 结构验证器（SPEC v2 §5.2 E6/E8）─────────────────────────────

def is_valid_ffn_prune(slot: Slot) -> bool:
    """E6：FFN 剪枝候选仅适用 ``slot.ffn_struct='standard'``。"""
    return slot.ffn_struct == "standard"


def is_candidate_valid_for_slot(
    name: str,
    slot: Slot,
    catalog: dict[str, CatalogEntry] | None = None,
) -> bool:
    """candidate-slot 联合结构校验（SPEC v2 §5.2，E6 + E8 + 跨 kind 适用性）。

    返回 False 表示该 candidate 不适用此 slot，应在枚举处被过滤（不进 BLD/score/
    build_selected）。规则：
      - passthrough（identity）：永远 valid。
      - no_op（零输出块）要求 in_dim == out_dim；非方 slot 在此收缩候选，避免 factory
        在 BLD/score 期 raise 崩整链。
      - 跨 kind 适用性：``slot.kind not in entry.kinds`` → False。
      - E6：entry.requires_ffn_struct 非空 → ``slot.ffn_struct`` 必须在其中。
      - E8：slot.mask_load_bearing=True 且 entry.mask_aware=False → 拒绝
        （mask-bearing slot 至少能选 mask_aware 候选 + identity）。
    """
    entry = get_candidate(name, catalog)
    if entry.source == "passthrough":
        return True
    if name == "no_op" and slot.in_dim != slot.out_dim:
        return False
    if slot.kind not in entry.kinds:
        return False
    if entry.requires_ffn_struct:
        if slot.ffn_struct not in entry.requires_ffn_struct:
            return False
    if slot.mask_load_bearing and not entry.mask_aware:
        return False
    return True


# ── 整模 latency 测量（DRY：measure_baseline + gate_report 复用）──────────────

def build_latency_dummy(adapters, device=None) -> Any:
    """构造**单样本（batch=1）per-inference** 输入用于整模 latency 测量。

    为何 batch-1（非 calib batch）：整模 latency 必须与 per-block latency（latency_table 用
    slot 单样本主路张量测）**同尺度**——都用 per-inference（batch 1）。若整模用 calib batch（如 64
    样本）而 block 用单样本，MIP 的 ``overhead = baseline_whole − Σ identity_block`` 会混入 batch
    缩放因子成垃圾值，导致「block 看似只占零头 → 全 identity 无优化」的假象。batch-1 是标准 NAS 延迟语义。

    实现：取 ``adapters.calib_iter()`` 首个 batch（保证与 ``adapters.forward_model`` 期望的
    native 格式一致——dict / list / Tensor 皆可），再把首维（batch dim）切到 1。这比从 DUMMY_INPUT
    合成更稳：不依赖 FORWARD_CALLING_CONVENTION 与 forward_model 实际签名一致（adapter 生成期可能
    标错 convention），直接复用 calib batch 的真实结构。
    """
    batch = next(iter(adapters.calib_iter(device=device)))

    def _to_batch1(b):
        if isinstance(b, dict):
            return {k: _to_batch1(v) for k, v in b.items()}
        if isinstance(b, (list, tuple)):
            return type(b)(_to_batch1(x) for x in b)
        if isinstance(b, torch.Tensor):
            return b[:1].to(device) if device is not None else b[:1]
        return b

    return _to_batch1(batch)


def measure_whole_model_latency(
    model: nn.Module,
    forward_fn: Callable[[nn.Module, Any], Any],
    batch: Any,
    device: torch.device,
    latency_script_path: str = "",
    repetitions: int = 100,
    warmup: int = 30,
) -> float:
    """整模 forward latency（默认 median ms；``latency_script_path`` 提供则包装外部 script）。

    U6：``forward_fn(model, batch)`` 由调用方传入（典型为 ``adapters.forward_model``），
    本函数不再假设 ``model(single_tensor)``。``batch`` 是 native batch（来自
    ``adapters.calib_iter()`` 的首个 batch，或据 ``adapters.DUMMY_INPUT`` 合成）。

    ``latency_script_path`` 形如 ``path::func``，签名 ``fn(model, batch) -> float``
    （agent 在 adapter 同目录生成的 latency 测量脚本，项目可定义自己的计时协议）。
    """
    if latency_script_path:
        fn = load_external_callable(latency_script_path)
        return float(fn(model, batch))
    model.eval().to(device)
    with torch.no_grad():
        for _ in range(max(1, warmup)):
            forward_fn(model, batch)
        times: list[float] = []
        for _ in range(max(1, repetitions)):
            t0 = time.perf_counter()
            forward_fn(model, batch)
            times.append(time.perf_counter() - t0)
    if not times:
        raise RuntimeError("measure_whole_model_latency：无有效 timing 样本")
    times.sort()
    return times[len(times) // 2] * 1000.0  # median ms


# ── 父激活捕获（BLD teacher 信号）─────────────────────────────────────────────

def capture_parent_activations(
    model: nn.Module,
    block_map: BlockMap,
    calib_iter: Iterator[Any],
    forward_fn: Callable[[nn.Module, Any], Any],
    device: torch.device,
) -> dict[str, tuple[torch.Tensor, torch.Tensor]]:
    """用 forward hooks 捕获每个 slot 的 (input, output)。

    U6：``forward_fn(model, batch)`` 由适配器提供（处理多输入/dict batch）；``calib_iter``
    是 ``adapters.calib_iter()`` 返回的迭代器。返回 ``{parent_module_path: (in, out)}``。

    hook 抓的是 slot 模块自身的 input/output（slot 内部契约，与整模 forward 签名无关）——
    主数据路径张量作为 teacher 信号。fail loud：任何 slot path 无法定位 → raise。
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
                return
            in_t: Any
            if isinstance(inputs, tuple) and inputs:
                in_t = inputs[0]
            elif isinstance(inputs, (list, tuple)) and inputs:
                in_t = inputs[0]
            else:
                in_t = inputs
            if isinstance(in_t, (tuple, list)):
                in_t = in_t[0] if in_t else inputs
            if isinstance(output, (tuple, list)):
                out_t = output[0]
            elif isinstance(output, torch.Tensor):
                out_t = output
            else:
                out_t = output
            captured[path] = (
                in_t.detach() if isinstance(in_t, torch.Tensor) else in_t,
                out_t.detach() if isinstance(out_t, torch.Tensor) else out_t,
            )
        return hook

    for path, mod in targets.items():
        handles.append(mod.register_forward_hook(make_hook(path)))

    try:
        with torch.no_grad():
            for batch in calib_iter:
                forward_fn(model, batch)
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

def slot_key(layer_idx: int, kind: str) -> str:
    """统一 slot 唯一 key。"""
    return f"L{layer_idx}_{kind}"


def variant_file_name(layer_idx: int, kind: str, variant: str) -> str:
    """block_library 内单 variant 权重文件名。"""
    return f"L{layer_idx}_{kind}_{variant}.pt"


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


# ── 外部 callable 解析（path::func，DRY：expand/latency_table/gate 复用）──────

def load_external_callable(path_func: str) -> Callable:
    """解析 ``path::func`` 字符串 → callable。"""
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
    """载入 variant 的 state_dict；fail loud 检查 missing/unexpected。"""
    if not sd:
        return
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
    adapters: Any,
    block_map: "BlockMap",
    selected_arch: dict,
    block_library_dir: str | Path,
    device: torch.device,
    flat_model_path: str | Path | None = None,
    build_fn: str | None = None,
    build_cfg: str | None = None,
) -> nn.Module:
    """通用：从 selected_arch + block_library 重建异构 student 模型。

    U6：基座通过 ``adapters.build_model()`` 实例化 + ``adapters.load_pretrained(model)``
    注入预训练父权重（identity slot 保留 father 权重）。若 ``flat_model_path`` 提供，
    则优先用 ``load_flat_model`` 构建架构骨架（与 adapter 共享同一架构类），再走
    ``adapters.load_pretrained`` 注入权重——这条路径用于兼容 ``build_student_from_arch``
    的精细架构控制（flat 文件是架构真相源）。

    - identity（passthrough）：跳过替换，保留父块。
    - 其他 variant：factory 实例化 + load ckpt（``load_variant_state_dict`` 严格）。
    - no_op / 零参 variant：照常 factory，空 state_dict 跳过 load。

    ``selected_arch`` 接受两种 dict 形态（自动 unwrap ``selected_arch`` 键）：
      - mip_select 结果：``{"selected_arch": {layer: {kind: variant}}, ...}``。
      - 裸架构：``{layer: {kind: variant}}``。
    """
    if flat_model_path and build_fn:
        model = load_flat_model(flat_model_path, build_fn, build_cfg or "")
    else:
        model = adapters.build_model()
    if not isinstance(model, nn.Module):
        raise TypeError(
            f"build_student_from_arch：build 出非 nn.Module（{type(model).__name__}）"
        )
    # 注入预训练父权重（U6：load_pretrained 由适配器消化前缀/schema 差异）。
    adapters.load_pretrained(model)

    arch = selected_arch.get("selected_arch", selected_arch) if isinstance(
        selected_arch, dict
    ) else {}
    chosen: dict[tuple[int, str], str] = {}
    for layer_str, slot_dict in arch.items():
        for kind, variant in slot_dict.items():
            chosen[(int(layer_str), kind)] = str(variant)

    lib = Path(block_library_dir).resolve()
    for slot in block_map.slots:
        key = (slot.layer_idx, slot.kind)
        if key not in chosen:
            continue
        variant = chosen[key]
        if is_passthrough(variant):
            continue
        entry = get_candidate(variant)
        if slot.kind not in entry.kinds:
            raise ValueError(
                f"variant {variant!r} 不适用 kind={slot.kind!r}"
            )
        new_module = entry.factory(slot).to(device).eval()
        ckpt_path = lib / variant_file_name(slot.layer_idx, slot.kind, variant)
        if ckpt_path.is_file():
            ckpt = torch.load(
                ckpt_path, map_location=device, weights_only=False
            )
            sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
            load_variant_state_dict(new_module, sd, variant, strict_unexpected=True)
        replace_slot(model, slot.parent_module_path, new_module)
    return model


# ── final_status.json 统一终态（SPEC U6 §5，DRY：gate_report + 未来 terminate）──

def write_final_status(
    output_dir: str | Path,
    stage: str,
    status: str,
    reason: str,
    metrics: dict[str, Any] | None = None,
) -> str:
    """落盘 ``final_status.json``（first-match 状态机字段，对齐 ns3_report）。

    ``stage``：触发的节点名（如 ``pz_report`` / ``terminate_*``）。
    ``status``：``pass`` / ``fail`` / ``skipped``。
    ``reason``：人读诊断（terminate 路径保留具体原因）。
    ``metrics``：可选，关键指标（baseline/final acc/latency 等）。
    """
    p = Path(output_dir) / "final_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": stage,
        "status": status,
        "reason": reason,
        "metrics": metrics or {},
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return str(p)
