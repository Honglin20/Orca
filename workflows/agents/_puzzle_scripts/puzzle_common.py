"""puzzle_common.py —— Puzzle workflow 算法脚本层共享 helper。

无 LLM、无网络、确定性。fail loud（错误抛异常 + 调用方 exit 2）。

核心契约：
  - ``Slot`` dataclass：可替换 sub-block slot（SPEC v2 §4.1，kind 开放标签）。
  - ``BlockMap``：slot 列表 + JSON 读写。
  - ``candidate_registry``：``load_catalog()`` 读 ``candidate_catalog.yaml`` 产
    ``{name: CatalogEntry}``；factory 签名 ``factory(slot: Slot) -> nn.Module``，
    输出末维 = ``slot.out_dim``（外部维度固定不可搜索，铁律）。
  - ``load_catalog`` / ``get_candidate``：catalog loader API（取代硬编码 registry）。
  - ``load_flat_model``：动态加载 flat model 文件并调 build_fn。
  - ``capture_parent_activations``：forward hook 抓每个 slot 的 (in, out) 作
    BLD teacher 信号。
  - ``build_calib_loader``：合成随机张量 DataLoader（latency / score 等只需 I/O
    shape 的场景用；**禁** 给 BLD teacher 信号用——会 OOD）。
  - ``build_real_calib_loader``：调外部 loader_fn 抽首个 batch 真实数据（E14，
    BLD teacher 信号的正确来源）。
  - ``is_valid_ffn_prune`` / ``is_candidate_valid_for_slot``：E6/E8 结构验证器。
  - ``measure_whole_model_latency``：整模 latency 测量（DRY：measure_baseline +
    gate_report 复用）。

兄弟 import（禁 sys.path 魔改）：同目录脚本 ``from puzzle_common import ...``。
"""

from __future__ import annotations

import functools
import importlib.util
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

# 候选块实现库（puzzle_blocks）由 load_catalog 函数内 lazy import（避免循环）。
# builtin factory 字符串形如 ``puzzle_blocks::make_<name>``。


# ── Slot / BlockMap ───────────────────────────────────────────────────────────

@dataclass
class Slot:
    """一个可替换的 transformer sub-block slot（SPEC v2 §4.1）。

    ``kind`` 替代 v1 的 ``slot_type``（E3，开放标签 attention/ffn/conv/moe/custom）。
    新增字段：return_arity（E15）/original_intermediate（E7 ratio 基准）/
    activation（E23，ffn required）/ffn_struct（E6 结构验证）/mask_load_bearing（E8）。
    ``parent_module_path`` 保留脚本侧字段名（search_space.yaml 的 ``path`` 是 loader 别名）。
    """
    layer_idx: int
    kind: str                 # E3：开放标签（attention/ffn/conv/moe/custom）
    in_dim: int
    out_dim: int
    num_heads: int
    head_dim: int
    source_class: str         # 原块类名（溯源 + 结构验证用）
    parent_module_path: str   # ``model.get_submodule(path)`` 可定位
    # E2：输入 arity（single/multi）——记录字段，evaluator 审 mask_load_bearing 一致性
    forward_arity: str = "single"
    # E15：输出 arity——multi-return slot 拒绝 single-output 候选
    return_arity: str = "single"
    # E7：FFN 原中间维（ratio 基准，非 in_dim）；非 ffn slot 为 None
    original_intermediate: int | None = None
    # E23：FFN 激活（gelu/relu/silu/...）；ffn factory required，null → raise
    activation: str | None = None
    # E6：FFN 结构类型 standard/bypass/glu/dual；非 standard → 禁剪枝候选
    # （is_valid_ffn_prune 消费，bld/score/build_selected 在枚举时过滤）。
    ffn_struct: str = "standard"
    # E8：父层是否传 functionally-load-bearing kwargs（attention_mask 等）
    # （is_candidate_valid_for_slot 消费，mask-bearing slot 拒绝 mask-blind candidate）。
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
    出现在顶层）→ 取内层；否则认为是裸 state_dict 原样返回。father 权重加载在
    measure_baseline / bld / score / build_selected / gkd / gate 共用此 helper（DRY）。
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


# ── passthrough 候选（SPEC v2 §3：identity = 保留 father-loaded 模块）──────────
# identity 候选 = 不替换 slot 的架构与权重——保留 father-loaded 模块（架构来自
# flat.build_fn，权重来自 father_state_dict）。build/score/latency 遇到时 NOT 替换
# slot。与 no_op（零输出块，residual 不变）严格区分。
PASSTHROUGH_VARIANTS: set[str] = {"identity"}


def is_passthrough(variant: str) -> bool:
    """identity 候选 = 保留父块，不替换。"""
    return variant in PASSTHROUGH_VARIANTS


# ── candidate catalog loader（SPEC v2 §5，取代硬编码 registry）─────────────────

# 第一版开放 kind 标签（D4：builtin 只覆盖 attention/ffn；conv/moe/custom 仅 identity）。
_ALL_KINDS: tuple[str, ...] = ("attention", "ffn", "conv", "moe", "custom")
_CATALOG_PATH = Path(__file__).resolve().parent / "candidate_catalog.yaml"


@dataclass
class CatalogEntry:
    """candidate catalog 单条目（SPEC v2 §5.1）。

    - ``factory``：``functools.partial`` 绑定 params 后、再经 ``_wrap`` 包
      ``_KwargPassthrough`` 的统一 ``factory(slot) -> nn.Module``。passthrough
      候选（identity）factory=None（永不实例化）。
    - ``kinds``：适用 kind 集合（按 catalog 的 ``kind`` list）。
    - ``requires_ffn_struct``：FFN 结构约束（如 ffn_75 要求 ["standard"]）；
      is_candidate_valid_for_slot 消费此字段对非 standard FFN 收缩剪枝候选（E6）。
    - ``mask_aware``：is_candidate_valid_for_slot 消费——mask_load_bearing slot
      拒绝 mask_aware=False 的候选（E8，只留 identity）。
    """
    name: str
    kinds: set[str]
    source: str                       # builtin | passthrough | user
    factory: Callable[[Slot], nn.Module] | None
    params: dict[str, Any]
    align: str
    trainable: bool
    mask_aware: bool
    requires_ffn_struct: list[str]
    description: str


def _resolve_builtin_factory(spec: str, params: dict[str, Any]) -> Callable[[Slot], nn.Module]:
    """``puzzle_blocks::make_<name>`` + params → 统一 factory(slot)。

    用 ``functools.partial`` 绑定 params（E4），再经 ``puzzle_blocks._wrap`` 包
    ``_KwargPassthrough``（适配异构父层 forward 签名，统一所有 builtin）。
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
    return puzzle_blocks._wrap(bound)


def load_catalog(path: str | Path | None = None) -> dict[str, CatalogEntry]:
    """读 candidate_catalog.yaml → ``{name: CatalogEntry}``。

    builtin：``puzzle_blocks::make_<name>`` + params → ``functools.partial`` + ``_wrap``。
    passthrough（identity）：factory=None（is_passthrough 短路，永不实例化）。
    fail loud：YAML 缺文件 / 条目缺字段 / factory 不可解析 → raise。
    """
    import yaml  # 顶层依赖已声明（runtime 必须）

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
            factory = _resolve_builtin_factory(spec, params)
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

    # E1：identity 必入 catalog（每 slot 候选列表的 MIP floor 锚）
    for pt in PASSTHROUGH_VARIANTS:
        if pt not in catalog:
            raise ValueError(f"{p} 缺 passthrough 候选 {pt!r}（E1：identity 必入 catalog）")
        if catalog[pt].factory is not None:
            raise ValueError(f"{p} passthrough 候选 {pt!r} 必须无 factory")
    return catalog


# 模块级 catalog（load_catalog 一次性加载；puzzle_blocks 无运行时反向依赖，无循环）。
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
    """默认候选集（SPEC v2 §5.4 / D4）。

    attention/ffn 给 builtin 全集；conv/moe/custom 仅 identity（框架预留）。
    每个 kind 列表都含 identity（E1：MIP floor 锚）。

    注：no_op 工厂要求 in_dim==out_dim（puzzle_blocks.make_zero）；对非方 slot，
    U1 阶段会在 bld 调 factory 时 fail-loud（exit 2）。U3 的 is_valid 上线后会
    自动按 slot 形状收缩候选，而非杀整链。
    """
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
        "conv": ["identity"],
        "moe": ["identity"],
        "custom": ["identity"],
    }


def parse_block_candidates(raw: str | None) -> dict[str, list[str]]:
    """解析 inputs.block_candidates（JSON 或空）→ ``{kind: [candidate, ...]}``。

    kind-keyed dict（动态 key，非硬编码 attention/ffn）——适配 SPEC v2 §4 开放 kind。
    空 → 默认集（get_default_candidates）。非空 JSON 须为非空 dict。

    fail loud（E1/E3/E4）：
    - 非法 JSON / 非 dict / kind 值非 list[str] → raise
    - 候选名未注册 / 候选不适用该 kind → raise
    - 某 kind 列表缺 identity（E1：identity 必入每 slot 候选）→ raise
    """
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
            # identity 也由 load_catalog 载入（source=passthrough），故统一查表。
            entry = candidate_registry.get(name)
            if entry is None:
                raise ValueError(
                    f"候选 {name!r} 未在 catalog 注册（可用：{sorted(candidate_registry)}）"
                )
            if kind not in entry.kinds:
                raise ValueError(
                    f"候选 {name!r} 不适用于 kind={kind!r}（适用集：{sorted(entry.kinds)}）"
                )
        # E1：identity 必入每 kind 候选列表
        if "identity" not in val:
            raise ValueError(
                f"block_candidates.{kind} 缺 identity（E1：identity 必入每 slot 候选）"
            )
        out[kind] = val
    return out


# ── candidate-slot 结构验证器（SPEC v2 §5.2 E6/E8）─────────────────────────────

def is_valid_ffn_prune(slot: Slot) -> bool:
    """E6：FFN 剪枝候选（ffn_75/ffn_50/linear）仅适用 ``slot.ffn_struct='standard'``。

    bypass/GLU/dual 等非标准 FFN 结构禁剪枝——剪枝会静默破坏结构（如 bypass 的
    残差路径被截断、GLU 的门控被丢弃）。返回 False → 该 slot 的 ffn_75/ffn_50/linear
    被 ``is_candidate_valid_for_slot`` 过滤，候选自动收缩到 {identity, no_op}。

    注：本函数只判 slot 侧的结构许可；候选侧的 ``requires_ffn_struct`` 约束由
    ``is_candidate_valid_for_slot`` 联合判定。
    """
    return slot.ffn_struct == "standard"


def is_candidate_valid_for_slot(
    name: str,
    slot: Slot,
    catalog: dict[str, CatalogEntry] | None = None,
) -> bool:
    """candidate-slot 联合结构校验（SPEC v2 §5.2，E6 + E8 + 跨 kind 适用性）。

    返回 False 表示该 candidate 不适用此 slot，应在枚举处被过滤（不进 BLD/score/
    build_selected）。规则：
      - passthrough（identity）：永远 valid（SPEC §3 铁律——保留父块不破坏结构）。
      - 跨 kind 适用性：``slot.kind not in entry.kinds`` → False（attention 候选
        不适用 ffn slot，反之亦然——catalog ``kind`` list 是 single source of truth）。
      - E6：entry.requires_ffn_struct 非空 → ``slot.ffn_struct`` 必须在
        ``entry.requires_ffn_struct`` 中（非 standard FFN 拒绝剪枝候选
        ffn_75/ffn_50/linear，自动收缩到 {identity, no_op}）。
      - E8：slot.mask_load_bearing=True 且 entry.mask_aware=False → 拒绝
        （mask-bearing slot 选 mask-blind 候选会丢 mask 语义，只留 identity）。

    本函数是 candidate-slot 结构校验的 single source of truth——下游
    bld/score/build_selected 应在枚举 / 防御性关卡处统一调它。
    fail loud：name 未在 catalog 注册 → raise（经 get_candidate）。
    """
    entry = get_candidate(name, catalog)
    if entry.source == "passthrough":
        return True  # identity 永远 valid（SPEC §3 铁律）
    # 跨 kind 适用性（catalog 的 kinds × slot.kind）
    if slot.kind not in entry.kinds:
        return False
    # E6：FFN 剪枝结构约束（catalog 的 requires_ffn_struct × slot.ffn_struct）
    if entry.requires_ffn_struct:
        # 进入此分支已保证 slot.kind == 'ffn'（entry.requires_ffn_struct 非空
        # 意味 entry 适用 ffn，跨 kind 检查上面已过）
        if slot.ffn_struct not in entry.requires_ffn_struct:
            return False  # 非 standard FFN 拒绝剪枝（bypass/GLU/dual）
    # E8：mask-bearing slot 拒绝 mask-blind candidate
    if slot.mask_load_bearing and not entry.mask_aware:
        return False
    return True


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


def build_real_calib_loader(
    loader_fn_str: str,
    device: torch.device | None = None,
) -> DataLoader:
    """调外部 ``loader_fn_str`` (path::func) → 抽首个 batch 真实数据 → 包装为 DataLoader。

    SPEC v2 §9.1 E14 修正：BLD teacher 信号必须来自真实数据 sample（非 ``torch.randn``
    OOD——candidates 学 noise→teacher_on_noise 在真实数据上全错）。manifest.yaml 的
    ``data_and_environment.data_loader_entry`` 经 agent 桥接为本函数入参（脚本不解析
    manifest，E9）。

    契约：``loader_fn_str`` 形如 ``"proj/train.py::build_dataloader"``，零参调用返回
    re-iterable DataLoader。本函数取首个 batch 物化到 ``_TensorDataset``（便于
    ``capture_parent_activations`` 重复 forward + 多 variant 共享同一份 teacher 信号）。

    fail loud（Rule 12）：
      - loader_fn 不可调用 / 未返回可迭代 → raise
      - 空 DataLoader（无 batch）→ raise（禁静默回退 randn）
      - 首个 batch 非 tensor → raise（puzzle 契约 slot 输入须 tensor）
    """
    fn = load_external_callable(loader_fn_str)
    full_loader = fn()
    if not hasattr(full_loader, "__iter__"):
        raise TypeError(
            f"{loader_fn_str!r} 未返回可迭代 DataLoader（E14 calib 数据契约）"
        )
    try:
        first_batch = next(iter(full_loader))
    except StopIteration as e:
        raise RuntimeError(
            f"{loader_fn_str!r} 返回空 DataLoader——无法抽真实 calib 数据（E14）。"
            f"manifest.data_and_environment.data_loader_entry 必须指向非空数据集"
        ) from e
    inp = first_batch[0] if isinstance(first_batch, (list, tuple)) else first_batch
    if not isinstance(inp, torch.Tensor):
        raise TypeError(
            f"{loader_fn_str!r} 首个 batch 非 tensor（{type(inp).__name__}）——"
            f"E14 calib 契约要求 tensor 输入"
        )
    inp = inp.to(device) if device is not None else inp
    # 拆为单样本再 stack 回原 batch（_TensorDataset 契约；保留原 batch 形状）
    samples = [inp[i] for i in range(inp.shape[0])]
    return DataLoader(
        _TensorDataset(samples), batch_size=inp.shape[0], shuffle=False
    )


# ── 整模 latency 测量（DRY：measure_baseline + gate_report 复用）──────────────

def measure_whole_model_latency(
    model: nn.Module,
    dummy_input: torch.Tensor,
    device: torch.device,
    latency_script_path: str = "",
) -> float:
    """整模 forward latency（默认 ``measure_module_latency`` PyTorch median ms；
    ``latency_script_path`` 提供则包装外部 script）。

    DRY：从 measure_baseline.py + gate_report.py 抽出（两处原本各持一份）。
    """
    if latency_script_path:
        fn = load_external_callable(latency_script_path)
        return float(fn(model, dummy_input))
    from nas_agent.latency import measure_module_latency
    return float(
        measure_module_latency(
            model, dummy_input, device, repetitions=100, warmup=30
        )
    )


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

def slot_key(layer_idx: int, kind: str) -> str:
    """统一 slot 唯一 key（jsonl/gkd 复用）。``kind`` 替代 v1 的 slot_type（E3）。"""
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
        for kind, variant in slot_dict.items():
            chosen[(int(layer_str), kind)] = str(variant)

    lib = Path(block_library_dir).resolve()
    for slot in block_map.slots:
        key = (slot.layer_idx, slot.kind)
        if key not in chosen:
            continue
        variant = chosen[key]
        if is_passthrough(variant):
            continue  # 保留父块，不替换
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
