"""test_psu_search_pipeline.py —— psu_search_pipeline P3 内容层契约测试。

锁定两处 intent：
  - arch_codec.py 示例 choice-only 重写：gene = 每 slot 一个分支索引基因
    （gene_len = slot 数、bounds = [0, |branches|-1]），解码 = branch_choices[idx]
    逐位映射进 ArchConfig.choices；无 depth 段 / param 段 / padding / clamp。
  - generate_schema.py 双闸断言（B3）：反射发现的搜索维度必须唯一为 choice
    容器 branch_choices——多候选伪维度元组 / 单值伪维度元组 / 无 choice 容器
    均 FATAL（fail loud），合规 choice-only 空间 PASS 并产出 schema。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_search_pipeline" / "references" / "supernet_workflow_examples"
GENERATE_SCHEMA = REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_search_pipeline" / "scripts" / "generate_schema.py"
sys.path.insert(0, str(REPO / "tests"))

from _psu_test_fixtures import write_toy_expand_artifacts  # noqa: E402


# ── arch_codec.py：choice-only 编解码 round-trip ────────────────────────────


def _load_codec(tmp_path: Path):
    """把 arch_codec.py 示例装进 toy expand 产物目录后 import（sibling supernet 可解析）。"""
    write_toy_expand_artifacts(tmp_path, with_inspect=False)
    shutil.copy2(EXAMPLES_DIR / "arch_codec.py", tmp_path / "arch_codec.py")

    for name in ("supernet", "psu_arch_codec_under_test"):
        sys.modules.pop(name, None)
    sys.path.insert(0, str(tmp_path))
    try:
        spec = importlib.util.spec_from_file_location(
            "psu_arch_codec_under_test", tmp_path / "arch_codec.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["psu_arch_codec_under_test"] = mod
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(tmp_path))
    return mod


def _drop_codec_modules():
    for name in ("supernet", "psu_arch_codec_under_test"):
        sys.modules.pop(name, None)


def test_gene_space_is_one_index_per_slot(tmp_path):
    """gene_len = slot 数（depth 钉原层数），bounds = [0, |branch_choices|-1]，逐位等值。"""
    mod = _load_codec(tmp_path)
    try:
        supernet = sys.modules["supernet"]
        ss = supernet.SearchSpace()
        codec = mod.ArchCodec(ss)
        space = codec.get_gene_space()
        assert space["gene_len"] == ss.depth
        assert space["lower_bounds"] == [0] * ss.depth
        assert space["upper_bounds"] == [len(ss.branch_choices) - 1] * ss.depth
        assert space["metadata"]["branch_choices"] == list(ss.branch_choices)
        assert space["metadata"]["num_slots"] == ss.depth
    finally:
        _drop_codec_modules()


def test_gene_to_arch_round_trip_exhaustive(tmp_path):
    """全枚举 round-trip：gene_to_arch 逐位 branch_choices[idx]，产出真实 ArchConfig。"""
    mod = _load_codec(tmp_path)
    try:
        supernet = sys.modules["supernet"]
        ss = supernet.SearchSpace()
        codec = mod.ArchCodec(ss)
        import itertools

        for gene in itertools.product(range(len(ss.branch_choices)), repeat=ss.depth):
            arch = codec.gene_to_arch(list(gene))
            assert isinstance(arch, supernet.ArchConfig)
            assert arch.choices == tuple(ss.branch_choices[i] for i in gene)
        # 全 original 锚路径：全 0 基因 → 全 original choices。
        all_original = codec.gene_to_arch([0] * ss.depth)
        assert all_original.choices == ss.all_original().choices
    finally:
        _drop_codec_modules()


def test_integer_gene_rounding_and_fail_loud(tmp_path):
    """_to_integer_gene 舍入/非有限值归零；长度不匹配 fail loud。"""
    mod = _load_codec(tmp_path)
    try:
        supernet = sys.modules["supernet"]
        codec = mod.ArchCodec(supernet.SearchSpace())
        assert mod._to_integer_gene([0.6, 1.5]) == [1, 2]
        assert mod._to_integer_gene([float("nan"), float("inf")]) == [0, 0]
        arch = codec.gene_to_arch([2.4, 0.6])  # 连续浮点基因 → 索引舍入
        assert arch.choices == tuple(
            supernet.SearchSpace().branch_choices[i] for i in (2, 1)
        )
        import pytest

        with pytest.raises(ValueError):
            codec.gene_to_arch([0])  # 长度 != slot 数 → fail loud
    finally:
        _drop_codec_modules()


def test_constructor_fail_loud_on_degenerate_space(tmp_path):
    """空分支集 / depth<1 → 构造期 fail loud（不静默产出非法布局）。"""
    mod = _load_codec(tmp_path)
    try:
        from dataclasses import replace

        ss = sys.modules["supernet"].SearchSpace()
        for bad in (
            replace(ss, branch_choices=()),
            replace(ss, depth=0),
        ):
            try:
                mod.ArchCodec(bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"degenerate space {bad!r} must fail loud")
    finally:
        _drop_codec_modules()


# ── generate_schema.py：双闸断言（choice 容器唯一性）───────────────────────


SYNTH_GOOD = '''
from dataclasses import dataclass

@dataclass
class ArchConfig:
    choices: tuple

@dataclass
class SearchSpace:
    branch_choices: tuple = ("original", "vanilla", "fnet")
    depth: int = 3
    num_heads: int = 4
'''

# 伪维度（多候选平铺元组）：反射会记成 type=list 的假搜索维度 → 双闸 FATAL。
SYNTH_BAD_MULTI = SYNTH_GOOD.replace(
    "    depth: int = 3", "    depth: int = 3\n    depth_candidates: tuple = (1, 2, 4)"
)

# 伪维度（单值元组）：钉死维度误写成单值元组，反射误报为 list → 双闸 FATAL。
SYNTH_BAD_SINGLE = SYNTH_GOOD.replace(
    "    num_heads: int = 4", "    num_heads: tuple = (4,)"
)

# 无 choice 容器：全部标量、无公有容器 → 'no searchable choice fields' FATAL。
SYNTH_NO_CONTAINER = '''
from dataclasses import dataclass

@dataclass
class SearchSpace:
    depth: int = 3
    num_heads: int = 4
'''


def _run_generate_schema(cwd: Path, latency_unit: str = "ms") -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(GENERATE_SCHEMA), "--latency-unit", latency_unit],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_generate_schema_passes_on_choice_only_space(tmp_path):
    (tmp_path / "supernet.py").write_text(SYNTH_GOOD, encoding="utf-8")
    rc, out = _run_generate_schema(tmp_path, latency_unit="us")
    assert rc == 0, out
    schema = json.loads((tmp_path / "search_record_schema.json").read_text(encoding="utf-8"))
    assert set(schema["arch_fields"]) == {"branch_choices"}
    assert schema["arch_fields"]["branch_choices"]["type"] == "list"
    assert schema["arch_fields"]["branch_choices"]["values"] == [
        "original", "vanilla", "fnet",
    ]
    assert schema["latency_unit"] == "us"


def test_generate_schema_fatal_on_multi_candidate_tuple(tmp_path):
    (tmp_path / "supernet.py").write_text(SYNTH_BAD_MULTI, encoding="utf-8")
    rc, out = _run_generate_schema(tmp_path)
    assert rc == 1
    assert "FATAL" in out and "branch_choices" in out and "depth_candidates" in out


def test_generate_schema_fatal_on_single_value_tuple(tmp_path):
    """平铺单值元组（generate_schema.py:44-49 反射分支）被双闸拦截。"""
    (tmp_path / "supernet.py").write_text(SYNTH_BAD_SINGLE, encoding="utf-8")
    rc, out = _run_generate_schema(tmp_path)
    assert rc == 1
    assert "FATAL" in out and "branch_choices" in out and "num_heads" in out


def test_generate_schema_fatal_without_choice_container(tmp_path):
    (tmp_path / "supernet.py").write_text(SYNTH_NO_CONTAINER, encoding="utf-8")
    rc, out = _run_generate_schema(tmp_path)
    assert rc == 1
    assert "no searchable choice fields" in out
    assert not (tmp_path / "search_record_schema.json").exists()


def test_generate_schema_on_toy_supernet(tmp_path):
    """真实 toy 产物（torch 级 supernet.py）上 schema 产出 PASS。"""
    write_toy_expand_artifacts(tmp_path, with_inspect=False)
    rc, out = _run_generate_schema(tmp_path)
    assert rc == 0, out
    schema = json.loads((tmp_path / "search_record_schema.json").read_text(encoding="utf-8"))
    assert set(schema["arch_fields"]) == {"branch_choices"}
    assert schema["arch_fields"]["branch_choices"]["values"] == [
        "original", "vanilla", "synthesizer",
    ]


# ── search_table.py：choice-only arch（{"choices": [...]}）渲染 ────────────────
# 真实 E2E 事故：transformer_layer 前沿的 arch = {"choices": ["random_synthesizer", ...]}
# 不被 _arch_digest 消化 → 全部行被 skip → 表格 "empty data"。渲染器须支持 per-slot
# 分支名 list（含 {"choice": name} dict entry），表格列 = slot × branch。

SEARCH_TABLE = REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_run_search" / "scripts" / "search_table.py"

_CHOICE_CONFIG_YAML = """\
objs:
  - "acc"
  - "latency"
"""

# 3 slots；acc 存储为 NAS 取反（负值）→ 展示时还原。pareto 前沿 = 前 2 个 arch。
_CHOICE_RECORDS = [
    {"generation": 0, "gene": [0, 0, 0], "objs": {"acc": -0.9, "latency": 1.0}, "cached": False,
     "pareto": True, "arch": {"choices": ["original", "original", "original"]}},
    {"generation": 0, "gene": [1, 0, 0], "objs": {"acc": -0.8, "latency": 1.2}, "cached": False,
     "pareto": True, "arch": {"choices": ["vanilla", "original", "original"]}},
    {"generation": 1, "gene": [1, 0, 0], "objs": {"acc": -0.7, "latency": 1.4}, "cached": False,
     "pareto": False, "arch": {"choices": ["vanilla", "original", "original"]}},  # 跨代重复 arch
    {"generation": 1, "gene": [0, 2, 0], "objs": {"acc": -0.6, "latency": 1.6}, "cached": False,
     "pareto": False, "arch": {"choices": [{"choice": "original"}, {"choice": "fnet"},
                                          {"choice": "original"}]}},  # dict entry 形态
]


def _run_search_table(ad: Path) -> tuple[int, str]:
    # HTML 断言按 stdlib 兜底渲染器（<tr> 表格）书写；plotly/matplotlib 装机与否
    # 会让 rendered_static 走不同层（plotly 静态页无字面 <tr>、JS 里含轴刻度子串），
    # 故用 PYTHONPATH shim 强制两级可视化依赖 ImportError → 确定性走 pure-html floor。
    shims = ad / "_shims_no_viz"
    shims.mkdir(exist_ok=True)
    (shims / "plotly.py").write_text('raise ImportError("disabled in test: force stdlib chart floor")\n', encoding="utf-8")
    (shims / "matplotlib.py").write_text('raise ImportError("disabled in test: force stdlib chart floor")\n', encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(shims) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, str(SEARCH_TABLE), "--artifacts-dir", str(ad)],
        capture_output=True, text=True, cwd=str(ad), env=env,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _marker_lines(ad: Path) -> list[dict]:
    out = []
    with (ad / ".psu_charts.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def test_search_table_renders_choice_only_arch(tmp_path):
    """choice-only arch：slot 列渲染 + arch 去重 + 前沿过滤；不再 empty-data skip。"""
    (tmp_path / "search_config.yaml").write_text(_CHOICE_CONFIG_YAML, encoding="utf-8")
    with (tmp_path / "search_results.jsonl").open("w", encoding="utf-8") as fh:
        for rec in _CHOICE_RECORDS:
            fh.write(json.dumps(rec) + "\n")

    rc, out = _run_search_table(tmp_path)
    assert rc == 0, out

    markers = _marker_lines(tmp_path)
    assert markers, "no chart marker recorded"
    last = markers[-1]
    assert last["name"] == "search_table"
    assert last["status"] in ("pushed", "rendered_static"), last

    # 前沿过滤：3 个去重 arch 中 2 个 pareto → 只展示 2 行（去重后跨代重复 arch 一行）。
    if last["status"] == "rendered_static":
        html_text = (tmp_path / "charts" / "search_table.html").read_text(encoding="utf-8")
        assert len(html_text) > 200
        # slot 列 + 分支名（dict-entry 的 fnet 也在；无 fnet 行属前沿过滤，列头仍需 slot_3）。
        assert "slot_1" in html_text and "slot_3" in html_text
        assert "original" in html_text
        assert "2 Pareto-front architectures" in html_text
        assert html_text.count("<tr>") == 1 + 2  # thead 1 + 前沿 2 行


def test_search_table_choice_arch_dedup_keeps_pareto_representative(tmp_path):
    """跨代重复 arch：pareto=true 的代表胜出（-0.8 展示值 0.8），非前沿代表值（0.7）不进表。"""
    (tmp_path / "search_config.yaml").write_text(_CHOICE_CONFIG_YAML, encoding="utf-8")
    with (tmp_path / "search_results.jsonl").open("w", encoding="utf-8") as fh:
        for rec in _CHOICE_RECORDS:
            fh.write(json.dumps(rec) + "\n")

    rc, out = _run_search_table(tmp_path)
    assert rc == 0, out

    markers = _marker_lines(tmp_path)
    assert markers[-1]["status"] in ("pushed", "rendered_static")
    if markers[-1]["status"] == "rendered_static":
        html_text = (tmp_path / "charts" / "search_table.html").read_text(encoding="utf-8")
        assert "0.8" in html_text  # pareto 代表的还原值（-0.8 → 0.8）
        assert "0.7" not in html_text  # 非 pareto 重复代表不落表
        assert "fnet" not in html_text  # 非 pareto 的 dict-entry arch 亦不落表
