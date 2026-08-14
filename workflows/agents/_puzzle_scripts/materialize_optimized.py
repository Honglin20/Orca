"""materialize_optimized.py —— Puzzle 产出自包含最优架构文件（确定性装配 + 自检）。

读 ``<base>_flat.py``（架构源）+ ``selected_arch`` + ``block_map`` + ``selected_model.pt``
（父⊕BLD 合成权重）→ 装配 ``<base>_optimized_flat.py``：

  ① flat 架构类源逐字照抄（build_fn 工厂重命名为 ``_puzzle_flat_build``，__main__ 剥离）。
  ② 选中 layer variant 涉及的**去 elastic 原生 layer 类**内联（``transformer_layer_variants`` 的
     layer 骨架 + attention cores + helper 经 AST 抽取；``resolve_activation`` + ``_ACTIVATION_MAP``
     从 ``puzzle_blocks`` AST 抽取）——不经 nas_agent Elastic*Core，不经整模块内联。
  ③ ``_build_variant`` dispatcher 镜像 ``transformer_layer_variants.make_*_layer``（构造整层
     ``_PreLNTransformerLayer(slot, <attention_core>)``，不经 ``_KwargPassthrough``——layer 自包含
     forward 处理异构父层签名）→ state_dict key 与 ``build_student_from_arch`` 对齐。
  ④ ``build_model()`` = ``_puzzle_flat_build()`` + 确定性 setattr 循环换 slot。
  ⑤ ``load_model(ckpt)`` strict load；``__main__`` 自检（build + load + forward DUMMY_INPUT）。

产物自包含（仅依赖 torch + stdlib），与 ``<base>_flat.py`` 同构同习惯——交付态用户
``load_model('final_model.pt')`` 即得最优模型。

自检（stdout 单行 JSON）：
  - ``key_alignment_passed``：optimized_flat.build_model() 与 selected_model.pt 逐 key strict 对齐。
  - ``forward_selfcheck_passed``：``python optimized_flat.py <selected_model.pt>`` 子进程 exit 0
    （真正 standalone 证明，不依赖 adapters）。

设计见 puzzle 流水线 release note（pz_materialize 节点）。
"""

from __future__ import annotations

import argparse
import ast
import inspect
import json
import re
import sys
import textwrap
import traceback
from pathlib import Path

import torch

# ── layer variant → 构造镜像（design draft §4.2 + §6.4；对齐 transformer_layer_variants.make_*_layer）─
# key 对齐自检会兜底：任何漂移在装配期 fail loud。
# 每条：(factory_kind, params)。factory_kind 在 _variant_dispatcher_src 分派。
# layer 粒度：候选 = 完整 transformer encoder layer（attn 变体 + 标准 FFN + 2×LN + 2×residual），
# 维度全部从 slot 注入（L11 零硬编码；softs_star 的 core_dim 是算法超参例外）。
# ⚠ 同步契约：softs_star 的 core_dim 须与 candidate_catalog.yaml 的 params.core_dim 一致
# （+ transformer_layer_variants.make_softs_star_layer 签名默认）。三者（本表 / catalog / factory
# 默认）漂移由 _check_key_alignment 的 shape_mismatch 在装配期 fail loud 兜底；改 catalog 须同步本表。
_VARIANT_CONSTRUCTION: dict[str, tuple[str, dict]] = {
    "vanilla_layer": ("vanilla_layer", {}),
    "random_synthesizer_layer": ("random_synthesizer_layer", {}),
    "relu_attention_layer": ("relu_attention_layer", {}),
    "fnet_layer": ("fnet_layer", {}),
    "softs_star_layer": ("softs_star_layer", {"core_dim": 64}),
    "no_op_layer": ("no_op_layer", {}),
}

# 从 transformer_layer_variants.py AST 抽取的类 + helper（去 elastic 原生 layer 变体源，design draft §4）。
# optimized_flat 内联这些后 dispatcher 直接构造整层；所有 layer variant 共享同一份源（去重由调用方保证）。
_LAYER_VARIANT_DEFS: tuple[str, ...] = (
    "_MASK_KEYS", "_extract_mask",
    "_StandardFFN",
    "_VanillaAttention", "_RandomSynthesizerAttention", "_ReluAttention",
    "_FNetMixer", "_SoftsStarMixer",
    "_PreLNTransformerLayer",
    "_NoOpLayer",
)

# 从 puzzle_blocks.py AST 抽取的 helper（layer 变体 _StandardFFN 用；AST 抽取，DRY，免漂移）。
_PUZZLE_BLOCKS_HELPERS: tuple[str, ...] = (
    "_ACTIVATION_MAP", "resolve_activation",
)

# ── flat 源处理 ────────────────────────────────────────────────────────────────

_FUTURE_RE = re.compile(r"^\s*from __future__ import .+$")


def _hoist_future(src: str) -> tuple[list[str], str]:
    """抽出 ``from __future__ import ...`` 行（必须在文件顶），返回 (future_lines, 剩余源)。

    optimized_flat 把所有内联源的 __future__ 合并去重后置顶——__future__ 仅允许出现在
    模块第一条语句之前，埋在 Section 1/3 里会 SyntaxError。
    """
    futures: list[str] = []
    kept: list[str] = []
    for line in src.splitlines(keepends=True):
        if _FUTURE_RE.match(line):
            futures.append(line.strip())
        else:
            kept.append(line)
    return futures, "".join(kept)


_MAIN_RE = re.compile(r'^if __name__ == ["\']__main__["\']\s*:\s*$')


def _split_main_block(src: str) -> tuple[str, str]:
    """抽出 flat 的 ``if __name__ == "__main__":`` 整块 → 返回 (main_block, 剩余源)。

    flat 的 __main__ 必须挪到 optimized_flat **末尾**（Section 5 的 build_model 定义之后）
    才能运行——它在 Section 1 的原位置早于 build_model wrapper 定义，会 NameError。
    main_block 整块（含 suite + 其内/尾随空行）抽出，剩余源进 Section 1。
    """
    lines = src.splitlines(keepends=True)
    main_lines: list[str] = []
    rest: list[str] = []
    i = 0
    while i < len(lines):
        if _MAIN_RE.match(lines[i]):
            main_lines.append(lines[i])
            i += 1
            while i < len(lines) and (lines[i].startswith("    ") or lines[i].strip() == ""):
                main_lines.append(lines[i])
                i += 1
        else:
            rest.append(lines[i])
            i += 1
    return "".join(main_lines), "".join(rest)


def _rewire_flat_build_fn(src: str, build_fn: str) -> str:
    """flat 的 ``def <build_fn>`` → ``def _puzzle_flat_build``；调用点 ``<build_fn>`` → ``build_model``。

    步骤：
      a. 全文 whole-word 替换 ``<build_fn>`` → ``build_model``（def 行与所有调用点统一）。
      b. ``def build_model`` → ``def _puzzle_flat_build``（仅 def 行）。
    调用点随后解析到本文件后置的 ``build_model`` wrapper（= build flat + apply selected arch）。
    """
    if not build_fn or not re.search(rf"\b{re.escape(build_fn)}\b", src):
        raise ValueError(
            f"flat 源中找不到 build_fn={build_fn!r}（puzzle 契约：flat 须暴露此工厂）"
        )
    # a. 全文 whole-word 替换（def + 调用）
    src = re.sub(rf"\b{re.escape(build_fn)}\b", "build_model", src)
    # b. def 行重命名（顶层 def，允许缩进）
    src = re.sub(
        r"^([ \t]*)def build_model\b", r"\1def _puzzle_flat_build", src, count=1, flags=re.MULTILINE
    )
    return src


# ── 源内联 ──────────────────────────────────────────────────────────────────────

def _extract_top_level_defs(source: str, names: tuple[str, ...]) -> str:
    """从 source AST 抽取指定名字的顶级 ClassDef / FunctionDef / 顶层 Assign，拼成源串。

    用于从 puzzle_blocks.py 抽 wrapper helper（DRY，免漂移）。缺名 fail loud。
    """
    tree = ast.parse(source)
    wanted = set(names)
    found: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
            if node.name in wanted:
                found[node.name] = node
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in wanted:
                    found[tgt.id] = node
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in wanted:
                found[node.target.id] = node
    missing = wanted - found.keys()
    if missing:
        raise RuntimeError(
            f"puzzle_blocks 源缺少待抽取的 helper：{sorted(missing)}"
        )
    # 按 names 顺序输出（确定性）
    segs: list[str] = []
    for n in names:
        seg = ast.get_source_segment(source, found[n])
        if seg is None:
            raise RuntimeError(f"get_source_segment 失败：{n}")
        segs.append(textwrap.dedent(seg))
    return "\n\n\n".join(segs)


# ── optimized_flat 内容生成 ─────────────────────────────────────────────────────

_HEADER = '''"""{{BASE}}_optimized_flat.py — Puzzle 最优架构（self-contained, standalone）。

由 materialize_optimized.py 确定性生成（docs/plans/2026-08-13-puzzle-materialize-optimized-flat.md）。
架构 = 原 {{BASE}}_flat 的架构类 + selected_arch 选中的 transformer_layer slot 替换为 layer 变体（去 elastic 原生 layer 源内联）。
依赖：仅 torch + stdlib——不依赖用户项目源、不依赖 _puzzle_scripts/、不依赖 run artifacts。

权重加载（单次 strict load_state_dict）：
  - 流水线内 GKD 起点：load_model('selected_model.pt')   # = 父⊕BLD 合成
  - 交付态：            load_model('final_model.pt')      # GKD 训练后
"""
'''


def _variant_dispatcher_src(used_variants: set[str]) -> str:
    """生成 _build_variant dispatcher（layer 粒度，镜像 transformer_layer_variants.make_*_layer）。

    构造**完整 transformer layer**（``_PreLNTransformerLayer(slot, <attention_core>)``）——
    layer forward 自包含 ``(x, src_mask=None, *args, **kwargs)``，故不经 ``_KwargPassthrough``
    （区别于 v2 单块候选）。``no_op_layer`` 早返回 ``_NoOpLayer``（passthrough，非 attn+ffn 骨架）。

    slot 在 optimized_flat 是 dict（``_BLOCK_MAP_SLOTS``），包成 ``SimpleNamespace`` 给内联 layer 类
    用（它们按属性访问 ``slot.in_dim``/``original_intermediate``/``activation``/``max_seq_len``，
    对齐 ``transformer_layer_variants`` slot 契约）。attention core 维度从 SimpleNamespace 取。
    """
    attn_branches: list[str] = []  # 设 attn 后落到 return _PreLNTransformerLayer(s, attn)
    has_early_return = False       # no_op_layer 早返回（无 attn）
    for v in sorted(used_variants):
        kind, params = _VARIANT_CONSTRUCTION[v]
        if kind == "vanilla_layer":
            attn_branches.append(
                '    elif variant == "vanilla_layer":\n'
                "        attn = _VanillaAttention(in_dim, num_heads)\n"
            )
        elif kind == "random_synthesizer_layer":
            attn_branches.append(
                '    elif variant == "random_synthesizer_layer":\n'
                "        max_seq_len = s.max_seq_len\n"
                "        if not max_seq_len or int(max_seq_len) <= 0:\n"
                "            raise ValueError(\n"
                "                f'random_synthesizer_layer: slot {s.parent_module_path} 缺 '\n"
                "                f'max_seq_len（须 pz_baseline trace 原层输入序列长度回填，禁 fallback）'\n"
                "            )\n"
                "        attn = _RandomSynthesizerAttention(in_dim, num_heads, head_dim, int(max_seq_len))\n"
            )
        elif kind == "relu_attention_layer":
            attn_branches.append(
                '    elif variant == "relu_attention_layer":\n'
                "        attn = _ReluAttention(in_dim, num_heads, head_dim)\n"
            )
        elif kind == "fnet_layer":
            attn_branches.append(
                '    elif variant == "fnet_layer":\n'
                "        attn = _FNetMixer()\n"
            )
        elif kind == "softs_star_layer":
            # max(1, int(core_dim)) 钳制对齐 factory（transformer_layer_variants.make_softs_star_layer）。
            core_dim = max(1, int(params["core_dim"]))
            attn_branches.append(
                '    elif variant == "softs_star_layer":\n'
                f"        attn = _SoftsStarMixer(in_dim, {core_dim})\n"
            )
        elif kind == "no_op_layer":
            has_early_return = True
            attn_branches.append(
                '    elif variant == "no_op_layer":\n'
                "        return _NoOpLayer(s)\n"
            )
    if not attn_branches:
        # 无非-identity variant（全 identity）→ _build_variant 永不被调用，留 fail-loud 桩。
        return (
            "def _build_variant(variant, slot):\n"
            "    raise ValueError('无 variant（全 identity 架构不该走到 _build_variant）')\n"
        )
    # 首分支 elif → if
    attn_branches[0] = attn_branches[0].replace("    elif ", "    if ", 1)
    # 仅当存在设 attn 的分支时才落 return _PreLNTransformerLayer(s, attn)（no_op-only 时该行不可达）。
    final_return = ""
    if has_early_return and len(attn_branches) == 1:
        # 唯一分支是 no_op（早返回）：else raise 后无须 _PreLNTransformerLayer 落地。
        pass
    else:
        final_return = "    return _PreLNTransformerLayer(s, attn)\n"
    return (
        "def _build_variant(variant, slot):\n"
        "    s = SimpleNamespace(**slot)\n"
        "    in_dim = s.in_dim\n"
        "    num_heads = max(s.num_heads, 1)\n"
        "    head_dim = max(s.head_dim, 1)\n"
        + "".join(attn_branches)
        + "    else:\n"
        "        raise ValueError(f'未知 variant {variant!r}')\n"
        + final_return
    )


_APPLY_AND_BUILD = '''
_SELECTED_ARCH = {sel_arch}

_BLOCK_MAP_SLOTS = {slots}

# build_cfg 烧入（来自 inputs.build_cfg）——build_model 零参即可重建训练时同骨架，
# 避免 zero-arg 默认与训练 cfg 不一致致 strict load 失败（review MAJOR-2）。
_BUILD_CFG = {build_cfg}


def _setattr_slot(model, parent_module_path, new_module):
    if "." in parent_module_path:
        parent_path, attr = parent_module_path.rsplit(".", 1)
    else:
        parent_path, attr = "", parent_module_path
    parent = model.get_submodule(parent_path) if parent_path else model
    setattr(parent, attr, new_module)


def _apply_selected_arch(model):
    chosen = {{}}
    for layer, slot_dict in _SELECTED_ARCH.items():
        for kind, variant in slot_dict.items():
            chosen[(int(layer), kind)] = str(variant)
    for slot in _BLOCK_MAP_SLOTS:
        key = (slot['layer_idx'], slot['kind'])
        if key not in chosen:
            continue
        variant = chosen[key]
        if variant == "identity":
            continue
        new_module = _build_variant(variant, slot)
        _setattr_slot(model, slot['parent_module_path'], new_module)
    return model


def build_model():
    """构建最优异构架构（= flat 骨架[_BUILD_CFG] + selected_arch 替换 slot）。权重无关。"""
    model = _puzzle_flat_build(**_BUILD_CFG)
    _apply_selected_arch(model)
    return model


def load_model(ckpt_path):
    """构建架构 + strict 载入 ckpt（selected_model.pt / final_model.pt 同结构）。"""
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    if isinstance(ckpt, dict) and "state_dict" in ckpt:
        sd = ckpt["state_dict"]
    else:
        sd = ckpt
    model = build_model()
    model.load_state_dict(sd, strict=True)  # 死不变量：逐 key 对齐
    model.eval()
    return model
'''


# 通用 __main__ fallback（flat 无 __main__ 时用；单输入 DUMMY_INPUT convention）。
# 真 pz_expand flat 都自带 __main__（项目真实 forward 签名）；本 fallback 仅兜底合成 fixture。
_GENERIC_MAIN = '''
if __name__ == "__main__":
    m = build_model()
    shape = DUMMY_INPUT["shape"]
    out = m(torch.randn(*shape))
    if isinstance(out, (tuple, list)):
        out = out[0]
    assert torch.isfinite(out).all(), "forward 输出含 NaN/inf"
    print("OK")
'''


# ── 自检 ────────────────────────────────────────────────────────────────────────

def _load_optimized_module(path: Path):
    """自检路径加载 optimized_flat（与下游 gkd/gate 共用 puzzle_common.load_optimized_flat）。"""
    from puzzle_common import load_optimized_flat
    return load_optimized_flat(path)


def _check_key_alignment(
    mod, reference_state: dict, reference_label: str
) -> tuple[bool, str]:
    """optimized_flat.build_model() 的 state_dict vs reference 逐 key + shape 对齐。

    reference = ``build_student_from_arch(...)`` 的 state_dict（live 重建）。这是 fundamental
    不变量：optimized_flat 的架构装配必须与 ``build_student_from_arch``（build_selected 产
    selected_model.pt 的同一逻辑）逐 key 一致。一致则 optimized_flat 必能 strict-load
    selected_model.pt / final_model.pt（后者由 GKD 在同结构上训练产出）。
    """
    msd = mod.build_model().state_dict()
    mk, rk = set(msd.keys()), set(reference_state.keys())
    missing = rk - mk
    unexpected = mk - rk
    shape_mismatch = [
        k for k in (mk & rk)
        if tuple(msd[k].shape) != tuple(reference_state[k].shape)
    ]
    if missing or unexpected or shape_mismatch:
        detail = (
            f"vs {reference_label}: "
            f"missing(optimized缺)={sorted(missing)[:5]}...{len(missing)}; "
            f"unexpected(optimized多)={sorted(unexpected)[:5]}...{len(unexpected)}; "
            f"shape_mismatch={shape_mismatch[:5]}...{len(shape_mismatch)}"
        )
        return False, detail
    return True, f"vs {reference_label}: all keys + shapes aligned"


def _check_forward_inprocess(mod, adapters) -> tuple[bool, str]:
    """in-process forward 诊断：build_model + adapters.forward_model(dummy) → 有限 tensor。

    仅作诊断（``forward_inprocess_detail``）——鲁棒（经 adapters.forward_model 处理任意 forward
    convention），但不证 standalone（materialize 进程 sys.path 仍可 import puzzle_common）。
    真正 standalone 证明由 ``_check_forward_subprocess``（子进程跑 optimized_flat 的 __main__）担任。
    """
    import torch as _torch
    from puzzle_common import build_latency_dummy
    model = mod.build_model().eval()
    dummy = build_latency_dummy(adapters)
    with _torch.no_grad():
        out = adapters.forward_model(model, dummy)
    if isinstance(out, (tuple, list)):
        out = out[0]
    if not isinstance(out, _torch.Tensor):
        return False, f"forward 输出非 tensor（{type(out).__name__}）"
    if not _torch.isfinite(out).all():
        return False, "forward 输出含 NaN/inf"
    return True, f"in-process forward OK（out shape={tuple(out.shape)}）"


def _check_forward_subprocess(python_exe: str, optimized_path: Path) -> tuple[bool, str]:
    """子进程跑 ``python optimized_flat.py``（无 ckpt，跑 Section 6 __main__）—— 真 standalone 证明。

    ``forward_selfcheck_passed`` 的权威来源：optimized_flat 在**干净子进程**里独立 import +
    build_model + forward（不靠 materialize 进程的 sys.path / adapters）。exit 0 = 架构 standalone
    完整。权重 strict-load 路径由 key 对齐保证（key 对齐 ⇒ load_model(ckpt) 必成功）。
    """
    import subprocess
    try:
        proc = subprocess.run(
            [python_exe, str(optimized_path)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return False, "forward 自检子进程超时（180s）"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        return False, f"子进程 exit={proc.returncode}: {tail}"
    return True, "standalone forward OK（子进程 exit 0）"


# ── main ───────────────────────────────────────────────────────────────────────

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Puzzle 装配 optimized_flat.py（确定性 + 自检）")
    p.add_argument("--flat_model", required=True, help="<base>_flat.py（架构源）")
    p.add_argument("--build_fn", required=True, help="flat 的工厂函数名（manifest.model.build_entry）")
    p.add_argument("--selected_arch", required=True, help="selected_arch.json（mip_select 产出）")
    p.add_argument("--block_map", required=True, help="block_map.json")
    p.add_argument("--selected_model", default="",
                   help="selected_model.pt（可选；仅作 ckpt-load 一致性软报告，非硬门）")
    p.add_argument("--adapters", required=True,
                   help="puzzle_adapters.py（build_student_from_arch reference 用）")
    p.add_argument("--block_library", required=True,
                   help="block_library/ 目录（reference 用）")
    p.add_argument("--build_cfg", default="", help="传给 build_fn 的 JSON kwargs")
    p.add_argument("--output_dir", required=True, help="optimized_flat.py 输出目录")
    p.add_argument("--base_name", default="", help="产物文件名前缀（空则用 flat 文件 stem 去后缀）")
    p.add_argument(
        "--check-only", action="store_true",
        help="跳过装配，只对已存在的 optimized_flat.py 跑自检（agent 手动 edit 后重验）",
    )
    return p


def _resolve_arch(selected_arch_path: Path) -> dict:
    with open(selected_arch_path, encoding="utf-8") as f:
        data = json.load(f)
    arch = data.get("selected_arch", data)
    if not isinstance(arch, dict) or not arch:
        raise ValueError(f"selected_arch 无效或空：{data!r}")
    return arch


def _needed_slot_fields(block_map_path: Path) -> list[dict]:
    with open(block_map_path, encoding="utf-8") as f:
        bm = json.load(f)
    slots = bm.get("slots", [])
    keep = [
        "layer_idx", "kind", "parent_module_path",
        "in_dim", "out_dim", "num_heads", "head_dim",
        "activation", "original_intermediate",
        # layer 粒度（design draft §2.1）：random_synthesizer_layer 等 mixing-matrix 变体的
        # 序列上界（pz_baseline trace 原层输入序列长度回填，禁 fallback——spec-reviewer LV-7）。
        "max_seq_len",
    ]
    out = []
    for s in slots:
        out.append({k: s.get(k) for k in keep})
    return out


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    flat_path = Path(args.flat_model).resolve()

    # --check-only：跳过装配，对已存在的 optimized_flat.py 重跑自检（agent 手动 edit 后）。
    if args.check_only:
        base = args.base_name or re.sub(r"_flat$", "", flat_path.stem)
        out_path = output_dir / f"{base}_optimized_flat.py"
        if not out_path.is_file():
            print(f"ERROR: --check-only 但 {out_path} 不存在", file=sys.stderr)
            return 2
        try:
            from puzzle_common import (
                BlockMap, build_latency_dummy, build_student_from_arch, load_puzzle_adapters,
            )

            arch = _resolve_arch(Path(args.selected_arch))
            adapters = load_puzzle_adapters(args.adapters)
            bm = BlockMap.from_json(args.block_map)
            reference_state = build_student_from_arch(
                adapters=adapters, block_map=bm, selected_arch=arch,
                block_library_dir=str(Path(args.block_library).resolve()),
                device=torch.device("cpu"),
                flat_model_path=str(flat_path), build_fn=args.build_fn,
                build_cfg=args.build_cfg,
            ).state_dict()
            mod = _load_optimized_module(out_path)
            key_ok, key_detail = _check_key_alignment(mod, reference_state, "build_student_from_arch")
            # 快路径：check-only 故意只跑 key 对齐 + standalone 子进程（权威两门），省 in-process 诊断
            # （_check_forward_inprocess 仅诊断，非硬门；agent 手动 edit 后重验只需两道硬门）。
            fwd_ok, fwd_detail = _check_forward_subprocess(sys.executable, out_path)
            result = {
                "status": "executed" if (key_ok and fwd_ok) else "failed",
                "optimized_flat_path": str(out_path),
                "key_alignment_passed": bool(key_ok),
                "key_alignment_detail": key_detail,
                "forward_selfcheck_passed": bool(fwd_ok),
                "forward_selfcheck_detail": fwd_detail,
                "error": "" if (key_ok and fwd_ok) else f"{key_detail} | {fwd_detail}",
            }
            print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
            return 0 if (key_ok and fwd_ok) else 3
        except Exception as e:
            print(f"ERROR: check-only 失败 — {type(e).__name__}: {e}\n{traceback.format_exc()}",
                  file=sys.stderr)
            return 2

    try:
        flat_src = flat_path.read_text(encoding="utf-8")
        flat_futures, flat_src = _hoist_future(flat_src)
        flat_src = _rewire_flat_build_fn(flat_src, args.build_fn)
        flat_main, flat_src = _split_main_block(flat_src)
        all_futures: set[str] = set(flat_futures)
        # flat 的 __main__ 抽出（flat_main）→ 末尾追加（build_model 定义之后），rewire 后其
        # build_model() 调用落到下方 wrapper，自动成为最优模型的 standalone 自检（项目真实 forward）。

        arch = _resolve_arch(Path(args.selected_arch))
        slots = _needed_slot_fields(Path(args.block_map))

        # 用到的非-identity variant
        used_variants: set[str] = set()
        for slot_dict in arch.values():
            for v in slot_dict.values():
                vname = str(v)
                if vname != "identity":
                    used_variants.add(vname)
        # 校验所有 used variant 在构造表里
        unknown = used_variants - set(_VARIANT_CONSTRUCTION)
        if unknown:
            raise ValueError(f"selected_arch 含未支持的 variant：{sorted(unknown)}")

        # 抽 puzzle_blocks helper（resolve_activation + _ACTIVATION_MAP，layer 变体 _StandardFFN 用）。
        # future hoist：puzzle_blocks 的 ``from __future__ import annotations`` 须并入 optimized_flat 顶。
        import puzzle_blocks as _pb
        pb_src = inspect.getsource(_pb)
        pb_futures, pb_src = _hoist_future(pb_src)
        all_futures |= set(pb_futures)
        helpers_src = _extract_top_level_defs(pb_src, _PUZZLE_BLOCKS_HELPERS)

        # 抽 transformer_layer_variants 类 + helper（去 elastic 原生 layer 变体源，design draft §4）。
        # 所有 layer variant 共享同一份源；命名抽取（_extract_top_level_defs）避免内联 factory
        # ``make_*_layer``（dispatcher 取而代之）+ 模块级 smoke 代码。future 同理 hoist。
        import transformer_layer_variants as _tlv
        tlv_src = inspect.getsource(_tlv)
        tlv_futures, tlv_src = _hoist_future(tlv_src)
        all_futures |= set(tlv_futures)
        layer_defs_src = _extract_top_level_defs(tlv_src, _LAYER_VARIANT_DEFS)

        # optimized_flat 顶置 import：layer 类用 torch/nn/F/Any；dispatcher 用 SimpleNamespace
        # （slot dict → 属性访问 wrapper，对齐内联 layer 类的 slot 契约）。math 不需要——layer 变体
        # 用 torch.pi（非 math.pi），flat 若用 math 会自带 import（Section 1 逐字照抄）。
        keep_imports: set[str] = {
            "import torch", "import torch.nn as nn",
            "import torch.nn.functional as F", "from typing import Any",
            "from types import SimpleNamespace", "import sys",
        }

        base = args.base_name or re.sub(r"_flat$", "", flat_path.stem)
        header = _HEADER.replace("{{BASE}}", base)

        # 解析 build_cfg → bake 进 optimized_flat（review MAJOR-2）
        build_cfg_kwargs: dict = {}
        if args.build_cfg and args.build_cfg.strip():
            try:
                parsed = json.loads(args.build_cfg)
            except json.JSONDecodeError as e:
                raise ValueError(f"build_cfg 非 JSON：{args.build_cfg!r}（{e}）") from e
            if not isinstance(parsed, dict):
                raise ValueError(f"build_cfg 须为 JSON object，得到 {type(parsed).__name__}")
            build_cfg_kwargs = parsed

        future_block = ""
        if all_futures:
            future_block = "\n".join(sorted(all_futures)) + "\n\n"
        imports_block = "\n".join(sorted(keep_imports)) + "\n\n"

        optimized = (
            header + "\n"
            + future_block
            + imports_block + "\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + "# Section 1: flat 架构类（逐字照抄自 " + flat_path.name + "；build_fn 已重命名）\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + flat_src.rstrip() + "\n\n\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + "# Section 2: helper（resolve_activation/_ACTIVATION_MAP ← puzzle_blocks.py）\n"
            + "#         + 去 elastic 原生 layer 变体类（← transformer_layer_variants.py）\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + helpers_src + "\n\n\n"
            + layer_defs_src + "\n\n\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + "# Section 3: variant 构造 dispatcher（镜像 transformer_layer_variants.make_*_layer）\n"
            + "# ════════════════════════════════════════════════════════════════\n"
            + _variant_dispatcher_src(used_variants) + "\n\n"
        )
        optimized += (
            "# ════════════════════════════════════════════════════════════════\n"
            "# Section 4: selected_arch 应用 + build_model + load_model + __main__\n"
            "# ════════════════════════════════════════════════════════════════\n"
            + _APPLY_AND_BUILD.format(sel_arch=repr(arch),
                                      slots=repr(slots),
                                      build_cfg=repr(build_cfg_kwargs))
        )
        # Section 5: standalone __main__——flat 有 __main__ 则用其（项目真实 forward 签名，
        # rewire 后测最优模型）；否则发通用 fallback（单输入 DUMMY_INPUT，兜底合成 fixture）。
        # forward_selfcheck 经子进程跑此 __main__（真 standalone 证明，review BLOCKER-1）。
        if flat_main.strip():
            main_block = flat_main.rstrip()
            main_note = "flat 原 __main__，rewire 后测最优模型"
        else:
            main_block = _GENERIC_MAIN.strip()
            main_note = "通用 fallback（flat 无 __main__，单输入 DUMMY_INPUT）"
        optimized += (
            "\n\n\n"
            "# ════════════════════════════════════════════════════════════════\n"
            f"# Section 5: standalone 自检（{main_note}）\n"
            "# ════════════════════════════════════════════════════════════════\n"
            + main_block + "\n"
        )

        out_path = output_dir / f"{base}_optimized_flat.py"
        out_path.write_text(optimized, encoding="utf-8")

        # ── 自检：optimized_flat.build_model() vs build_student_from_arch（live reference）──
        from puzzle_common import (
            BlockMap, build_latency_dummy, build_student_from_arch, load_puzzle_adapters,
        )

        adapters = load_puzzle_adapters(args.adapters)
        bm = BlockMap.from_json(args.block_map)
        reference_model = build_student_from_arch(
            adapters=adapters, block_map=bm, selected_arch=arch,
            block_library_dir=str(Path(args.block_library).resolve()),
            device=torch.device("cpu"),
            flat_model_path=str(flat_path), build_fn=args.build_fn,
            build_cfg=args.build_cfg,
        )
        reference_state = reference_model.state_dict()

        mod = _load_optimized_module(out_path)
        key_ok, key_detail = _check_key_alignment(mod, reference_state, "build_student_from_arch")
        # forward_selfcheck 权威 = 子进程 standalone（review BLOCKER-1）；in-process 仅诊断。
        fwd_ok, fwd_detail = _check_forward_subprocess(sys.executable, out_path)
        _, fwd_inproc_detail = _check_forward_inprocess(mod, adapters)

        # 软报告：selected_model.pt 能否 strict-load（生产链路应一致；stale fixture 可能 fail）
        ckpt_report = "skipped (no selected_model)"
        if args.selected_model and Path(args.selected_model).is_file():
            try:
                m2 = mod.build_model()
                ckpt = torch.load(args.selected_model, map_location="cpu", weights_only=False)
                sd = ckpt.get("state_dict", ckpt) if isinstance(ckpt, dict) else ckpt
                m2.load_state_dict(sd, strict=True)
                ckpt_report = "strict-load OK"
            except Exception as e:  # noqa: BLE001
                ckpt_report = f"strict-load FAIL (软报告，非硬门): {type(e).__name__}: {e}"

        result = {
            "status": "executed" if (key_ok and fwd_ok) else "failed",
            "optimized_flat_path": str(out_path),
            "used_variants": sorted(used_variants),
            "inlined_sources": ["puzzle_blocks::helpers", "transformer_layer_variants::defs"],
            "key_alignment_passed": bool(key_ok),
            "key_alignment_detail": key_detail,
            "forward_selfcheck_passed": bool(fwd_ok),
            "forward_selfcheck_detail": fwd_detail,
            "forward_inprocess_detail": fwd_inproc_detail,
            "ckpt_load_report": ckpt_report,
            "error": "" if (key_ok and fwd_ok) else f"自检失败：{key_detail} | {fwd_detail}",
        }
        print(f"OPTIMIZED_FLAT: {out_path}")
        print(f"RESULT_JSON: {json.dumps(result, ensure_ascii=False)}")
        return 0 if (key_ok and fwd_ok) else 3
    except Exception as e:
        tb = traceback.format_exc()
        print(
            f"ERROR: materialize_optimized 失败 — {type(e).__name__}: {e}\n{tb}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
