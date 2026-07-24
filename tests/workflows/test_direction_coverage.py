"""test_direction_coverage.py —— plan sprightly-questing-donut §2.1 direction_coverage.py 单测。

覆盖 code-reviewer 标出的关键契约（Rule 9：测意图，纯函数确定性逻辑）：
- tiers 族（wireless）meta.json 枚举 catalog（D0-D21）；单层族（cnn/transformer 无 meta.json）catalog 空。
- ledger tried direction_id 收集 → untried = catalog − tried；旧 ledger 无 direction_id 向后兼容（tried 空）。
- all_exhausted = catalog 非空且 untried 空。
- near_target = champion（SUCCESS & met_accuracy）latency ≤ target × near_band。
- KB 根缺失 → fail loud（main 返回 1 + stderr）。

不依赖 torch / ts_quant / orca.chart（纯 stdlib + importlib 加载脚本）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STRUCT_SCRIPTS = REPO / "workflows" / "agents" / "_struct_scripts"
KB = REPO / "knowledge_base"


def _load_dc():
    spec = importlib.util.spec_from_file_location("dc_under_test", STRUCT_SCRIPTS / "direction_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dc_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_ledger(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    return p


# ── catalog 枚举 ──────────────────────────────────────────────

def test_wireless_catalog_has_22_directions():
    dc = _load_dc()
    cat = dc._load_catalog(KB, "wireless_receiver")
    ids = [d["id"] for d in cat]
    assert len(cat) == 22
    assert {"D0", "D5", "D21"} <= set(ids)
    # 每条带 name + 标签
    assert all(d["name"] for d in cat)


def test_single_layer_family_empty_catalog():
    """cnn/transformer 无 meta.json → catalog 空（覆盖闸 N/A）。"""
    dc = _load_dc()
    assert dc._load_catalog(KB, "cnn") == []
    assert dc._load_catalog(KB, "transformer") == []


def test_unknown_family_empty_catalog():
    dc = _load_dc()
    assert dc._load_catalog(KB, "nonexistent_family") == []


# ── compute_coverage ──────────────────────────────────────────

def test_empty_ledger_all_untried(tmp_path):
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=None, near_band=1.15,
    )
    assert r["catalog_size"] == 22
    assert len(r["untried"]) == 22
    assert r["tried"] == []
    assert r["all_exhausted"] is False
    assert r["near_target"] is False  # 无 target


def test_tried_directions_subtract_from_untried(tmp_path):
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [
        {"direction_id": "D5"}, {"direction_id": "D0"},
        {"direction_id": "off_catalog:novel_mamba"},  # catalog 外不计入 catalog 覆盖
    ])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=None, near_band=1.15,
    )
    assert set(r["tried"]) == {"D0", "D5", "off_catalog:novel_mamba"}
    assert r["tried_in_catalog"] == ["D0", "D5"]
    assert "D5" not in r["untried"] and "D0" not in r["untried"]
    assert len(r["untried"]) == 20
    assert r["all_exhausted"] is False


def test_old_ledger_without_direction_id_backward_compat(tmp_path):
    """旧 ledger 行无 direction_id → 跳过（tried 空，向后兼容）。"""
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [
        {"id": "r0_c0", "tag": "structural", "status": "SUCCESS"},  # 无 direction_id
    ])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=None, near_band=1.15,
    )
    assert r["tried"] == []
    assert len(r["untried"]) == 22


def test_all_exhausted_when_catalog_fully_covered(tmp_path):
    dc = _load_dc()
    all_ids = [f"D{i}" for i in range(22)]
    ledger = _write_ledger(tmp_path, [{"direction_id": d} for d in all_ids])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=None, near_band=1.15,
    )
    assert r["untried"] == []
    assert r["all_exhausted"] is True


def test_single_layer_family_all_exhausted_false(tmp_path):
    """cnn catalog 空 → all_exhausted 恒 False（无方向可耗尽，hypothesizer 靠 latency_moves）。"""
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="cnn",
        target_latency_ms=None, near_band=1.15,
    )
    assert r["catalog_size"] == 0
    assert r["all_exhausted"] is False
    assert r["coverage_ratio"] == 0.0


# ── near_target ───────────────────────────────────────────────

def test_near_target_true_when_champion_within_band(tmp_path):
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [
        {"status": "SUCCESS", "met_accuracy": True, "latency_ms": 0.018},  # ≤ 0.02*1.15=0.023
    ])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=0.02, near_band=1.15,
    )
    assert r["near_target"] is True


def test_near_target_false_when_champion_outside_band(tmp_path):
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [
        {"status": "SUCCESS", "met_accuracy": True, "latency_ms": 0.05},  # > 0.02*1.15
    ])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=0.02, near_band=1.15,
    )
    assert r["near_target"] is False


def test_near_target_false_without_champion(tmp_path):
    """无达标 champion（FAIL 行 / 无 SUCCESS）→ near_target False。"""
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [
        {"status": "FAIL_latency", "met_accuracy": False, "latency_ms": 0.1},
    ])
    r = dc.compute_coverage(
        ledger=str(ledger), kb_dir=KB, family="wireless_receiver",
        target_latency_ms=0.02, near_band=1.15,
    )
    assert r["near_target"] is False


# ── fail-loud ─────────────────────────────────────────────────

def test_main_fails_loud_when_kb_missing(tmp_path):
    """KB 根不存在 → main 返回 1（fail loud，stderr 带详情）。"""
    dc = _load_dc()
    ledger = _write_ledger(tmp_path, [])
    rc = dc.main([
        "--ledger", str(ledger), "--kb-dir", "/nonexistent/kb/path",
        "--family", "wireless_receiver",
    ])
    assert rc == 1
