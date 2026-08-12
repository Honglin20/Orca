"""search_space_io.py —— search_space.yaml 读写（SPEC v2 §4 契约层）。

search_space.yaml 是「判断（LLM）」与「执行（脚本）」的契约边界：
  - LLM 产 slots（path/kind/layer_idx/确定性证据/ffn meta）+ candidates 引用 catalog；
    in_dim/out_dim 留 ``-1``（待 measure_baseline trace 回填）。
  - 脚本（measure_baseline）读 YAML → trace 形状 → 回填 in_dim/out_dim → 写回 YAML
    + 落 block_map.json（下游 bld/score/mip 的既有格式）。

YAML↔Slot 映射：``path``（yaml key）↔ ``parent_module_path``（Slot/script 字段，E3）。
YAML-only 元数据（``id`` / ``kind_evidence``）：load 时返回，save 时原样保留——BlockMap 不存
（id 可由 layer_idx+kind 派生；kind_evidence 是 LLM 审计证据，算法不消费）。

fail loud（Rule 12）：YAML 缺文件 / slot 缺字段 / kind 非法 / candidates 不合规 → raise。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from puzzle_common import BlockMap, Slot, parse_block_candidates

# SPEC §6.2：开放 kind 标签（D4 builtin 只覆盖 attention/ffn）。
_ALLOWED_KINDS: tuple[str, ...] = ("attention", "ffn", "conv", "moe", "custom")

# YAML 必填 slot 字段（path/kind/layer_idx 是识别核心；其余可由 measure_baseline 回填或默认）。
_REQUIRED_SLOT_FIELDS: tuple[str, ...] = ("id", "path", "kind", "layer_idx")

# YAML-only 元数据 key（不进 Slot，但 load/save 保留）。
_YAML_META_KEYS: tuple[str, ...] = ("id", "kind_evidence", "forward_arity")


def load_search_space_yaml(
    path: str | Path,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """读 search_space.yaml → (slot_dicts, candidates)。

    - ``slot_dicts``：每个 slot 的完整字段 dict（含 ``parent_module_path`` 已从 ``path``
      映射而来，含 YAML-only 元数据 id/kind_evidence）。in_dim/out_dim 保持原值
      （LLM 留 -1；measure_baseline 回填后由 save_search_space_yaml 落盘）。
    - ``candidates``：``{kind: [candidate, ...]}``，经 parse_block_candidates 校验
      （E1 identity 必入 / catalog 注册 / kind 适用）。

    fail loud：缺文件 / 非法结构 / slot 缺字段 / kind 非法 / candidates 不合规。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"search_space.yaml 不存在：{p}")
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(
            f"{p} 顶层须为 mapping（slots + candidates），得到 {type(raw).__name__}"
        )

    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError(
            f"{p} 缺 'slots' list（LLM 须声明逐层可替换 slot；空 list = 不支持）"
        )

    slot_dicts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for i, item in enumerate(raw_slots):
        if not isinstance(item, dict):
            raise ValueError(f"{p} slots[{i}] 须为 mapping，得到 {type(item).__name__}")
        for field in _REQUIRED_SLOT_FIELDS:
            if field not in item:
                raise ValueError(
                    f"{p} slots[{i}] 缺必填字段 {field!r}：{item!r}"
                )
        slot_id = str(item["id"])
        slot_path = str(item["path"])
        kind = str(item["kind"])
        if kind not in _ALLOWED_KINDS:
            raise ValueError(
                f"{p} slots[{i}] kind={kind!r} 非法（允许：{list(_ALLOWED_KINDS)}）"
            )
        if slot_id in seen_ids:
            raise ValueError(f"{p} slot id {slot_id!r} 重复（MIP 分组键须唯一）")
        if slot_path in seen_paths:
            raise ValueError(
                f"{p} slot path {slot_path!r} 重复（同一 module 不可双声明）"
            )
        seen_ids.add(slot_id)
        seen_paths.add(slot_path)

        # 拷贝 + 映射 path→parent_module_path（E3：脚本侧字段名不变）
        d = dict(item)
        d["parent_module_path"] = slot_path
        slot_dicts.append(d)

    # candidates：经 parse_block_candidates（catalog 注册 + E1 identity 必入）。
    # raw_candidates 已是 yaml.safe_load 解出的 dict；re-serialize 为 JSON 复用校验逻辑。
    raw_candidates = raw.get("candidates")
    if not slot_dicts:
        # 空 slots：candidates 不校验（terminate_unsupported 路径，E22）
        candidates: dict[str, list[str]] = {}
    elif raw_candidates is None or (
        isinstance(raw_candidates, dict) and not raw_candidates
    ):
        # slots 非空但 candidates 缺/空 dict —— fail loud（禁静默填默认，掩盖 LLM 漏产）
        raise ValueError(
            f"{p} slots 非空时 'candidates' 必须显式声明（每 kind 一组 candidate 列表，"
            f"含 identity）；得到空/缺 candidates"
        )
    elif not isinstance(raw_candidates, dict):
        raise ValueError(
            f"{p} 'candidates' 须为 mapping（kind → [candidate]），"
            f"得到 {type(raw_candidates).__name__}"
        )
    else:
        candidates = parse_block_candidates(json.dumps(raw_candidates))
    return slot_dicts, candidates


def save_search_space_yaml(
    path: str | Path,
    slot_dicts: list[dict[str, Any]],
    candidates: dict[str, list[str]],
) -> str:
    """写 search_space.yaml（path ← parent_module_path 映射回 yaml key）。

    保留 YAML-only 元数据（id / kind_evidence）；in_dim/out_dim 已由 measure_baseline
    回填。``path`` 作 yaml 键，``parent_module_path`` 不重复落盘。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    out_slots: list[dict[str, Any]] = []
    for d in slot_dicts:
        out: dict[str, Any] = {}
        # 保留 YAML key 顺序：id → path → kind → layer_idx → 其余
        out["id"] = d.get("id") or _derive_slot_id(d)
        out["path"] = d.get("path", d.get("parent_module_path", ""))
        for key in ("kind", "layer_idx", "source_class"):
            if d.get(key) is not None:
                out[key] = d[key]
        # arity / mask
        for key in ("forward_arity", "return_arity", "mask_load_bearing"):
            if key in d:
                out[key] = d[key]
        # attention fields
        for key in ("num_heads", "head_dim"):
            if d.get(key) is not None:
                out[key] = d[key]
        # ffn fields
        for key in ("original_intermediate", "activation", "ffn_struct"):
            if key in d:
                out[key] = d[key]
        # traced shapes
        out["in_dim"] = d.get("in_dim", -1)
        out["out_dim"] = d.get("out_dim", -1)
        # YAML-only 元数据
        if d.get("kind_evidence"):
            out["kind_evidence"] = d["kind_evidence"]
        out_slots.append(out)

    payload = {"slots": out_slots, "candidates": candidates}
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return str(p)


def to_block_map(slot_dicts: list[dict[str, Any]]) -> BlockMap:
    """slot_dicts → BlockMap（下游 bld/score/mip 既有契约）。

    ``parent_module_path`` 取自 slot_dict（load 时已从 path 映射）；未知字段忽略。
    """
    slots: list[Slot] = []
    for d in slot_dicts:
        slots.append(
            Slot(
                layer_idx=int(d["layer_idx"]),
                kind=str(d["kind"]),
                in_dim=int(d.get("in_dim", -1)),
                out_dim=int(d.get("out_dim", -1)),
                num_heads=int(d.get("num_heads", 0) or 0),
                head_dim=int(d.get("head_dim", 0) or 0),
                source_class=str(d.get("source_class", "") or ""),
                parent_module_path=str(d.get("parent_module_path", d.get("path", ""))),
                forward_arity=str(d.get("forward_arity", "single")),
                return_arity=str(d.get("return_arity", "single")),
                original_intermediate=(
                    int(d["original_intermediate"])
                    if d.get("original_intermediate") is not None
                    else None
                ),
                activation=(str(d["activation"]) if d.get("activation") else None),
                ffn_struct=str(d.get("ffn_struct", "standard")),
                mask_load_bearing=bool(d.get("mask_load_bearing", False)),
            )
        )
    return BlockMap(slots=slots)


def _derive_slot_id(d: dict[str, Any]) -> str:
    """``L{layer_idx}_{kind}``（与 puzzle_common.slot_key 一致）。

    fail loud：``kind`` 缺失 → raise（search_space 契约要求每 slot 有合法 kind；
    缺 kind 是 schema 违例，不用误导性 fallback 掩盖——``'slot'`` 是 v1 已退役
    字段名，作 fallback 会双重误导）。
    """
    if "kind" not in d:
        raise ValueError(
            f"search_space slot 缺 kind 字段（无法派生 id；slot dict={d!r}）"
        )
    return f"L{d.get('layer_idx', 0)}_{d['kind']}"
