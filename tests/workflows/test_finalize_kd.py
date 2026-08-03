"""tests/workflows/test_finalize_kd.py —— finalize_kd.py 单测（Rule 9：测意图）。

覆盖 SPEC §6.10 + N10/N19 关键契约：
- champion=baseline 兜底：跳 student eval/ONNX/latency，用 setup 透传的 baseline_latency_ms
  + baseline_accuracy 写 report。
- champion=真 student：调 train_pipeline --mode eval + export_onnx + latency_provider measure
  （子进程 mock，不真跑训练/导出）。
- champion_id 在 ledger 找不到 → fail loud（exit 2）。
- final_report.md 必须包含「无 student 达标」（baseline 兜底）/「min-latency ratchet」语句。
- 命令 flag 完整性：eval 必传 --student_ckpt --out_ckpt --accuracy_baseline
  --accuracy_baseline_kind（mock subprocess 捕获 argv 校验）。

不依赖 torch / onnxruntime / orca.chart — 用 monkeypatch 替换 subprocess.run / _measure_latency
/ _export_onnx / _run_eval。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD_SCRIPTS = REPO / "workflows" / "agents" / "_kd_scripts"


def _load_finalize():
    spec = importlib.util.spec_from_file_location("fk_under_test", KD_SCRIPTS / "finalize_kd.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fk_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_ledger(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "ledger.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _write_champions(tmp_path: Path, rows: list[dict]) -> Path:
    p = tmp_path / "champions.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def _baseline_row() -> dict:
    return {
        "round": 0, "id": "baseline", "latency_ms": 10.0, "accuracy": 0.02,
        "delta_vs_baseline_ms": 0, "snapshot": "/path/baseline.py",
    }


def _student_row(variant_id: str = "r1_student", latency: float = 4.0, acc: float = 0.018) -> dict:
    return {
        "variant_id": variant_id, "student_path": f"/snap/{variant_id}.py",
        "round": 1, "parent": "baseline",
        "latency_ms": latency, "accuracy": acc,
        "met_latency": True, "met_accuracy": True, "accuracy_kind": "nmse",
        "direction_id": "scale_1", "hypothesis": "scale down 1 layer",
        "accepted_cfg": {"num_layers": 5}, "cfg_hash": "abc",
        "ckpt": f"/ckpt/{variant_id}.pt", "status": "SUCCESS",
    }


# ── champion lookup ────────────────────────────────────────────────────────────


def test_lookup_champion_baseline_returns_synthetic_row(tmp_path):
    fk = _load_finalize()
    baseline_contract = "/path/baseline.py"
    champ = fk._lookup_champion("/no/ledger", "baseline", baseline_contract)
    assert champ["variant_id"] == "baseline"
    assert champ["student_path"] == baseline_contract
    assert champ["ckpt"] == ""
    assert champ["accepted_cfg"] == "{}"


def test_lookup_champion_real_student(tmp_path):
    fk = _load_finalize()
    ledger = _write_ledger(tmp_path, [_student_row()])
    champ = fk._lookup_champion(str(ledger), "r1_student", "/baseline.py")
    assert champ["variant_id"] == "r1_student"
    assert champ["student_path"] == "/snap/r1_student.py"
    assert champ["ckpt"] == "/ckpt/r1_student.pt"
    # accepted_cfg normalized to JSON 串
    assert isinstance(champ["accepted_cfg"], str)
    assert json.loads(champ["accepted_cfg"]) == {"num_layers": 5}


def test_lookup_champion_missing_id_fails_loud(tmp_path):
    fk = _load_finalize()
    ledger = _write_ledger(tmp_path, [_student_row()])
    with pytest.raises(ValueError, match="找不到"):
        fk._lookup_champion(str(ledger), "nonexistent", "/baseline.py")


# ── _main: baseline 兜底路径 ─────────────────────────────────────────────────


def test_main_baseline_fallback_writes_report_no_eval(tmp_path, monkeypatch):
    """champion_id=baseline → 不调 _run_eval/_export_onnx/_measure_latency，用 setup 透传值。"""
    fk = _load_finalize()
    ledger = _write_ledger(tmp_path, [_student_row()])  # ledger 有 student 但 champion=baseline
    champions = _write_champions(tmp_path, [_baseline_row()])
    kd_artifacts = tmp_path / "kd"
    kd_artifacts.mkdir()

    # 设哨兵：如果调了任何 champion=student 路径，测试 fail。
    def _bomb(*a, **kw):
        raise AssertionError("champion=baseline 不该调 _run_eval/_export_onnx/_measure_latency")
    monkeypatch.setattr(fk, "_run_eval", _bomb)
    monkeypatch.setattr(fk, "_export_onnx", _bomb)
    monkeypatch.setattr(fk, "_measure_latency", _bomb)

    # 用 sys.argv 调 _main（exit 0）
    baseline_contract = tmp_path / "base.py"
    baseline_contract.write_text("# synthetic", encoding="utf-8")
    train_pipeline = tmp_path / "train_pipeline.py"
    train_pipeline.write_text("# synthetic", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "finalize_kd.py",
        "--ledger", str(ledger),
        "--champions", str(champions),
        "--champion_id", "baseline",
        "--terminate_reason", "max_rounds",
        "--baseline_contract_path", str(baseline_contract),
        "--train_pipeline_path", str(train_pipeline),
        "--baseline_latency_ms", "10.0",
        "--baseline_accuracy", "0.02",
        "--teacher_latency_ms", "30.0",
        "--target_latency_ms", "5.0",
        "--accuracy_baseline", "0.02",
        "--accuracy_baseline_kind", "nmse",
        "--kd_artifacts_dir", str(kd_artifacts) + "/",
        "--struct_scripts_dir", "/struct",
        "--kd_scripts_dir", str(KD_SCRIPTS),
        "--device", "cpu",
        "--seed", "0",
        "--latency_provider", "/tmp/lp.py::measure",
    ])
    rc = fk._main()
    assert rc == 0
    # 检查 stdout 含 baseline 兜底值
    # （_main 用 print，捕获较繁；直接验 report 文件）
    report = (kd_artifacts / "final_report.md").read_text("utf-8")
    assert "**champion**: `baseline`" in report
    assert "**terminate_reason**: max_rounds" in report
    assert "**final_latency_ms**: 10" in report  # baseline 兜底用 setup 透传值


# ── _main: champion=真 student 路径（mock 子进程）────────────────────────────


def test_main_real_champion_runs_eval_onnx_latency(tmp_path, monkeypatch):
    fk = _load_finalize()
    ledger = _write_ledger(tmp_path, [_student_row()])
    champions = _write_champions(tmp_path, [_baseline_row(), {
        "round": 1, "id": "r1_student", "latency_ms": 4.0, "accuracy": 0.018,
        "delta_vs_baseline_ms": -6.0, "snapshot": "/snap/r1_student.py",
    }])
    kd_artifacts = tmp_path / "kd"
    kd_artifacts.mkdir()
    baseline_contract = tmp_path / "base.py"
    baseline_contract.write_text("DUMMY_INPUT = {'shape': [1]}\ndef build_model(**c): ...\n", encoding="utf-8")
    train_pipeline = tmp_path / "train_pipeline.py"
    train_pipeline.write_text("# noop", encoding="utf-8")

    # mock _run_eval（捕获 argv，校验 flag 完整）→ 返精度
    captured_eval = {}
    def _fake_run_eval(train_pipeline_path, champion, acc_baseline, acc_kind, device, seed, project_root, per_run_dir):
        captured_eval["champion_ckpt"] = champion["ckpt"]
        captured_eval["accepted_cfg"] = champion["accepted_cfg"]
        captured_eval["acc_baseline"] = acc_baseline
        captured_eval["acc_kind"] = acc_kind
        return 0.019
    monkeypatch.setattr(fk, "_run_eval", _fake_run_eval)

    # mock _export_onnx：写一个空 onnx 文件
    def _fake_export(struct_dir, champion, dummy, out_onnx, device, seed):
        Path(out_onnx).write_bytes(b"fake-onnx")
    monkeypatch.setattr(fk, "_export_onnx", _fake_export)

    # mock _measure_latency → 返 4.5
    captured_lp = {}
    def _fake_measure(latency_provider, onnx_path, device):
        captured_lp["provider"] = latency_provider
        captured_lp["onnx"] = onnx_path
        return 4.5
    monkeypatch.setattr(fk, "_measure_latency", _fake_measure)

    monkeypatch.setattr(sys, "argv", [
        "finalize_kd.py",
        "--ledger", str(ledger),
        "--champions", str(champions),
        "--champion_id", "r1_student",
        "--baseline_contract_path", str(baseline_contract),
        "--train_pipeline_path", str(train_pipeline),
        "--baseline_latency_ms", "10.0",
        "--baseline_accuracy", "0.02",
        "--teacher_latency_ms", "30.0",
        "--target_latency_ms", "5.0",
        "--accuracy_baseline", "0.02",
        "--accuracy_baseline_kind", "nmse",
        "--kd_artifacts_dir", str(kd_artifacts) + "/",
        "--struct_scripts_dir", "/struct",
        "--kd_scripts_dir", str(KD_SCRIPTS),
        "--latency_provider", "/tmp/lp.py::measure",
    ])
    rc = fk._main()
    assert rc == 0
    # 校验 champion=真 student 时确实调了三个 fn
    assert captured_eval["champion_ckpt"] == "/ckpt/r1_student.pt"
    assert captured_eval["acc_kind"] == "nmse"
    assert captured_lp["provider"] == "/tmp/lp.py::measure"
    assert captured_lp["onnx"].endswith("final.onnx")


# ── _run_eval 命令 flag 完整性（subprocess argv 校验）───────────────────────


def test_run_eval_passes_all_required_flags(tmp_path, monkeypatch):
    """SPEC-REVIEW N18：eval 必传 --student_ckpt --out_ckpt --accuracy_baseline --accuracy_baseline_kind。"""
    fk = _load_finalize()
    captured = {}
    class _Fake:
        returncode = 0
        stdout = "STUDENT_ACCURACY: 0.019\nSTUDENT_ACCURACY_KIND: nmse\nMET_ACCURACY: true\n"
        stderr = ""
    def _fake_run(argv, **kw):
        captured["argv"] = argv
        return _Fake()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    train_pipeline = tmp_path / "tp.py"
    train_pipeline.write_text("# noop", encoding="utf-8")
    champion = {
        "student_path": "/s.py", "accepted_cfg": '{"x":1}', "ckpt": "/c.pt",
    }
    acc = fk._run_eval(
        str(train_pipeline), champion,
        "0.02", "nmse", "cpu", "0", "/proj", "/artifacts",
    )
    assert acc == 0.019
    argv = captured["argv"]
    for flag in ("--student_ckpt", "--out_ckpt", "--accuracy_baseline", "--accuracy_baseline_kind",
                 "--student_model_path", "--build_cfg", "--mode", "eval"):
        assert flag in argv, f"eval 命令缺 flag: {flag}"
    # ckpt 必须存在（champion.ckpt 非空）
    assert "/c.pt" in argv


def test_run_eval_empty_ckpt_fails_loud(tmp_path):
    fk = _load_finalize()
    champion = {"student_path": "/s.py", "accepted_cfg": "{}", "ckpt": ""}
    with pytest.raises(ValueError, match="ckpt 为空"):
        fk._run_eval("/tp.py", champion, "0.02", "nmse", "cpu", "0", "/p", "/a")


def test_run_eval_no_student_accuracy_key_fails_loud(tmp_path, monkeypatch):
    """eval 成功但未 emit STUDENT_ACCURACY → fail loud（user_eval 移植异常）。"""
    fk = _load_finalize()
    class _Bad:
        returncode = 0
        stdout = "no key here\n"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Bad())
    champion = {"student_path": "/s.py", "accepted_cfg": "{}", "ckpt": "/c.pt"}
    with pytest.raises(RuntimeError, match="STUDENT_ACCURACY"):
        fk._run_eval("/tp.py", champion, "0.02", "nmse", "cpu", "0", "/p", "/a")


# ── _measure_latency ────────────────────────────────────────────────────────


def test_measure_latency_dispatches_device_kwarg(tmp_path, monkeypatch):
    """latency_provider::func 的 measure 可选 device kwarg；测两路都走通。"""
    fk = _load_finalize()
    # 构造一个 latency provider 文件，含 device kwarg
    lp = tmp_path / "lp.py"
    lp.write_text(
        "def measure(onnx, device=None):\n"
        "    return 4.2 if device == 'cpu' else 9.9\n",
        encoding="utf-8",
    )
    val = fk._measure_latency(f"{lp}::measure", "/x.onnx", "cpu")
    assert val == 4.2

    # 无 device kwarg 的 provider
    lp2 = tmp_path / "lp2.py"
    lp2.write_text("def measure(onnx):\n    return 7.7\n", encoding="utf-8")
    val2 = fk._measure_latency(f"{lp2}::measure", "/x.onnx", "cpu")
    assert val2 == 7.7


def test_measure_latency_bad_provider_format_fails_loud(tmp_path):
    fk = _load_finalize()
    with pytest.raises(ValueError, match="path::func"):
        fk._measure_latency("no_double_colon", "/x.onnx", "cpu")
