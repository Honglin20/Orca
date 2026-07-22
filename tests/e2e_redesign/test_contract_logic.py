"""test_contract_logic.py —— contract 纯函数的函数边界单测（Rule 9：验证分类**意图**）。

契约闸的端到端 parametrize（test_workflow_contracts.py）只证「当前 8 workflow 通过」；本文件
用**植入的 fixture** 证明 check 的判别力：detect 该 detect 的 / skip 该 skip 的 / 零容忍该零容忍的。
防 refactor 静默削弱判别力（如 _is_legit_rand_context 改坏 → 造假漏过 / 误报）。
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from tests.e2e_redesign import contract
from tests.e2e_redesign.contract import (
    Finding,
    _check_rand_in_py,
    _is_legit_rand_context,
    check_chart_labels,
    check_no_fabrication,
)


# ── _check_rand_in_py：AST 上下文分类判别力 ─────────────────────────────────────


def _write_py(tmp_path: Path, name: str, src: str) -> Path:
    p = tmp_path / name
    p.write_text(dedent(src), encoding="utf-8")
    return p


def test_rand_in_production_function_flagged(tmp_path: Path) -> None:
    """production-path torch.randn（非 smoke/dummy/proxy 上下文）→ flagged。"""
    p = _write_py(tmp_path, "prod.py", """
        def load_calib():
            return torch.randn(1, 3)  # 造假兜底
    """)
    findings = _check_rand_in_py(p)
    assert len(findings) == 1
    assert "torch.randn" in findings[0].detail or "randn" in findings[0].detail


def test_rand_in_smoke_generator_skipped(tmp_path: Path) -> None:
    """smoke generator（函数名含 smoke）→ skipped（合法）。"""
    p = _write_py(tmp_path, "smoke.py", """
        def make_smoke_batch():
            return torch.randn(1, 3)
    """)
    assert _check_rand_in_py(p) == []


def test_rand_in_dummy_generator_skipped(tmp_path: Path) -> None:
    """dummy generator（函数名含 dummy）→ skipped。"""
    p = _write_py(tmp_path, "dummy.py", """
        def dummy_input_gen():
            yield torch.randn(1, 3)
    """)
    assert _check_rand_in_py(p) == []


def test_rand_in_proxy_docstring_skipped(tmp_path: Path) -> None:
    """函数 docstring 含 proxy → skipped（KD proxy dataset 语义）。"""
    p = _write_py(tmp_path, "proxy.py", """
        def materialize():
            '''build proxy dataset for short-train ranking.'''
            return torch.randn(1, 3)
    """)
    assert _check_rand_in_py(p) == []


def test_rand_in_onnx_materialize_skipped(tmp_path: Path) -> None:
    """materialize_dummy（ONNX dummy input）→ skipped。"""
    p = _write_py(tmp_path, "onnx.py", """
        def materialize_dummy_input():
            return torch.randn(1, 3)
    """)
    assert _check_rand_in_py(p) == []


def test_rand_in_docstring_text_not_flagged(tmp_path: Path) -> None:
    """docstring 里的 torch.randn 字面量（非 ast.Call）→ 不构成 Call，AST 不命中。"""
    p = _write_py(tmp_path, "doc.py", '''
        def f():
            """不要用 torch.randn 造假。"""
            return 0
    ''')
    assert _check_rand_in_py(p) == []


def test_module_level_randn_flagged(tmp_path: Path) -> None:
    """模块级（非函数内）torch.randn → func_context 无映射 → flagged。"""
    p = _write_py(tmp_path, "mod.py", "X = torch.randn(1, 3)\n")
    findings = _check_rand_in_py(p)
    assert len(findings) == 1


def test_is_legit_rand_context_markers() -> None:
    """_is_legit_rand_context 各标记判定。"""
    assert _is_legit_rand_context("make_smoke_batch", "") is True
    assert _is_legit_rand_context("_dummy_input", "") is True
    assert _is_legit_rand_context("f", "proxy dataset for ranking") is True
    assert _is_legit_rand_context("materialize_onnx", "") is True
    assert _is_legit_rand_context("load_calib", "production eval loader") is False
    assert _is_legit_rand_context("get_eval_loader", "") is False


# ── check_no_fabrication：fake_data/dummy_calib 零容忍 ──────────────────────────


def test_fake_data_zero_tolerance_anywhere(tmp_path: Path, monkeypatch) -> None:
    """fake_data 在任何上下文都 = finding（无歧义造假标记）。"""
    p = _write_py(tmp_path, "x.py", """
        def smoke():  # 即使在 smoke 函数里
            data = fake_data  # 仍 flagged
            return data
    """)
    monkeypatch.setattr(contract, "active_script_files", lambda _wf: [p])
    monkeypatch.setattr(contract, "active_agent_md_files", lambda _wf: [])
    findings = check_no_fabrication("quant-ptq-sweep")
    assert any("fake_data" in f.detail for f in findings), \
        "fake_data 应零容忍（即使在 smoke 上下文）"


def test_dummy_calib_zero_tolerance(tmp_path: Path, monkeypatch) -> None:
    """dummy_calib 零容忍。"""
    p = _write_py(tmp_path, "x.py", "X = dummy_calib\n")
    monkeypatch.setattr(contract, "active_script_files", lambda _wf: [p])
    monkeypatch.setattr(contract, "active_agent_md_files", lambda _wf: [])
    findings = check_no_fabrication("quant-ptq-sweep")
    assert any("dummy_calib" in f.detail for f in findings)


# ── check_chart_labels：table vs axis-bearing 判别 ──────────────────────────────


def _chart_label_findings_for(tmp_path: Path, src: str, monkeypatch) -> list[Finding]:
    p = _write_py(tmp_path, "viz.py", src)
    monkeypatch.setattr(contract, "active_script_files", lambda _wf: [p])
    monkeypatch.setattr(contract, "active_agent_md_files", lambda _wf: [])
    return check_chart_labels("quant-ptq-sweep")


def test_table_missing_caption_flagged(tmp_path: Path, monkeypatch) -> None:
    """table 缺 caption → finding（table 只要求 caption）。"""
    src = """
        from x import render_chart
        render_chart(chart_type="table", data=[], label="x", title="t", columns=["a"])
    """
    findings = _chart_label_findings_for(tmp_path, src, monkeypatch)
    assert len(findings) == 1 and "caption" in findings[0].detail


def test_table_missing_only_xlabel_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """table 有 caption、缺 x_label/y_label → 不 finding（table 无轴）。"""
    src = """
        from x import render_chart
        render_chart(chart_type="table", data=[], label="x", title="t",
                     columns=["a"], caption="ok")
    """
    assert _chart_label_findings_for(tmp_path, src, monkeypatch) == []


def test_bar_missing_xlabel_flagged(tmp_path: Path, monkeypatch) -> None:
    """axis-bearing（bar）缺 x_label → finding（必传 x_label+y_label+caption）。"""
    src = """
        from x import render_chart
        render_chart(chart_type="bar", data=[], label="x", title="t",
                     x="a", y="b", y_label="Y", caption="c")
    """
    findings = _chart_label_findings_for(tmp_path, src, monkeypatch)
    assert len(findings) == 1 and "x_label" in findings[0].detail


def test_scatter_fully_labeled_not_flagged(tmp_path: Path, monkeypatch) -> None:
    """scatter 三标签齐全 → 不 finding。"""
    src = """
        from x import render_chart
        render_chart(chart_type="scatter", data=[], label="x", title="t",
                     x="a", y="b", x_label="X", y_label="Y", caption="c")
    """
    assert _chart_label_findings_for(tmp_path, src, monkeypatch) == []


def test_orca_alias_render_chart_also_checked(tmp_path: Path, monkeypatch) -> None:
    """``_orca_render_chart`` 别名（viz_struct/viz_kd 用）同样检查。"""
    src = """
        from x import _orca_render_chart
        _orca_render_chart(chart_type="table", data=[], label="x", title="t", columns=["a"])
    """
    findings = _chart_label_findings_for(tmp_path, src, monkeypatch)
    assert len(findings) == 1
