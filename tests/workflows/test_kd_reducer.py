"""test_kd_reducer.py —— SPEC §6.9 + §13 kd_reducer.py 单测（Rule 9：测意图）。

覆盖 KD 专版决策契约：
- 准入门 = SUCCESS ∧ met_latency ∧ met_accuracy（FAIL_* 即使 met_latency=true 也不入）。
- champion ratchet = admitted 内 latency 最小；**tie 不 ratchet（FIFO 最早）**（N12）。
- 无达标 → 维持 baseline（champion_id="baseline"）。
- continue_loop：target_met（admitted 非空）/ max_rounds（round ≥ max）/ else true。
- ledger append 一行；new_champion 时 champions append 一行（否则不 append）。
- schema 缺字段 / 类型错 / status 非法 → fail loud（exit 2）。
- dry_run 不写盘。
- 坏 ledger 行 → fail loud。

不依赖 torch / orca.chart（纯 stdlib + importlib 加载脚本）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD_SCRIPTS = REPO / "workflows" / "agents" / "_kd_scripts"


def _load_reducer():
    spec = importlib.util.spec_from_file_location("kdr_under_test", KD_SCRIPTS / "kd_reducer.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kdr_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _cand(
    *,
    variant_id: str = "r1_student",
    round_: int = 1,
    parent: str = "baseline",
    latency_us: float = 5.0,
    accuracy: float = 0.018,
    met_latency: bool = True,
    met_accuracy: bool = True,
    status: str = "SUCCESS",
    student_path: str = "/snap/r1.py",
    ckpt: str = "/ckpt/r1.pt",
    direction_id: str = "scale_1",
    hypothesis: str = "scale down 1 layer",
    accepted_cfg: dict | None = None,
    cfg_hash: str = "h0",
    accuracy_kind: str = "nmse",
) -> dict:
    return {
        "variant_id": variant_id,
        "student_path": student_path,
        "round": round_,
        "parent": parent,
        "latency_us": latency_us,
        "accuracy": accuracy,
        "met_latency": met_latency,
        "met_accuracy": met_accuracy,
        "accuracy_kind": accuracy_kind,
        "direction_id": direction_id,
        "hypothesis": hypothesis,
        "accepted_cfg": accepted_cfg or {"num_layers": 5},
        "cfg_hash": cfg_hash,
        "ckpt": ckpt,
        "status": status,
    }


def _seed_baseline(tmp_path: Path) -> tuple[Path, Path]:
    """空 ledger + 未 seed champions（让 reducer 自己 seed baseline）。"""
    return tmp_path / "ledger.jsonl", tmp_path / "champions.jsonl"


# ── 准入门 ──────────────────────────────────────────────────────────────────


def test_admitted_requires_all_three_conditions(tmp_path):
    """SUCCESS 但 met_accuracy=false → 不入 admitted → 维持 baseline + continue_loop true."""
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(met_accuracy=False, accuracy=0.05),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["champion_id"] == "baseline"
    assert res["new_champion_this_round"] is False
    assert res["continue_loop"] is True
    assert res["terminate_reason"] == ""


def test_fail_train_not_admitted_even_with_met_latency(tmp_path):
    """SPEC §6.8：FAIL_train 时 met_latency=true，但仍不算达标（accuracy=-1, met_acc=false）。"""
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(status="FAIL_train", met_latency=True, met_accuracy=False, accuracy=-1),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["champion_id"] == "baseline"
    assert res["continue_loop"] is True  # neither target_met nor max_rounds


def test_fail_latency_skipped_from_admit(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(status="FAIL_latency", met_latency=False, met_accuracy=False, accuracy=-1),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["champion_id"] == "baseline"


# ── Y1 fix：FAIL_accuracy / FAIL_export 合法 status（对齐 kd_common.ALL_TERMINAL_STATUSES）──


@pytest.mark.parametrize("status", ["FAIL_accuracy", "FAIL_export"])
def test_fail_accuracy_or_export_appends_ok(tmp_path, status):
    """Y1：``_LEDGER_STATUS`` 必须包含 FAIL_accuracy / FAIL_export，与
    ``kd_common.ALL_TERMINAL_STATUSES`` / ``viz_kd_stage._push_fail_status_bar`` /
    ``finalize_kd.known_statuses`` 四处对齐。否则 candidate 带 FAIL_accuracy/export 时
    ``_validate_candidate`` fail loud（exit 2），下游 measure_student / export_onnx 失败的
    ledger append 会断流（延时炸弹）。本测守护合约：合法 append + 维持 baseline + continue_loop true。
    """
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(
            status=status,
            met_latency=False,
            met_accuracy=False,
            accuracy=-1,
        ),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    # FAIL_* 不入 admitted → champion 维持 baseline，但 ledger 行应正常 append。
    assert res["champion_id"] == "baseline"
    assert res["new_champion_this_round"] is False
    assert res["ledger_entry_written"] is True
    assert res["champions_entry_written"] is False  # 仅 seed baseline（首次）+ 无新 champion
    assert res["status_final"] == status
    # ledger 文件 1 行（FAIL_accuracy/export candidate）+ champions 文件 1 行（baseline seed）
    assert ledger_path.is_file()
    lines = [l for l in ledger_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1
    import json as _json
    assert _json.loads(lines[0])["status"] == status


# ── champion ratchet ──────────────────────────────────────────────────────────


def test_first_success_becomes_champion(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(latency_us=4.0),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["champion_id"] == "r1_student"
    assert res["new_champion_this_round"] is True
    # admitted 非空 → target_met → stop
    assert res["continue_loop"] is False
    assert res["terminate_reason"] == "target_met"


def test_min_latency_ratchet_strict_improvement(tmp_path):
    """第二轮 latency 更小 → ratchet；champion_id 切到 r2。"""
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    common = dict(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        target_latency_us=100.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    r.reduce_ledger(candidate=_cand(variant_id="r1_student", latency_us=5.0, round_=1), **common)
    res = r.reduce_ledger(candidate=_cand(variant_id="r2_student", latency_us=3.0, round_=2), **common)
    assert res["champion_id"] == "r2_student"
    assert res["new_champion_this_round"] is True


def test_tie_does_not_ratchet_fifo_earliest_wins(tmp_path):
    """SPEC §13 N12：admitted 内 latency 相等 → tie 不 ratchet，FIFO 最早胜出。

    构造 r1=5.0, r2=5.0（tie）：r2 不应成为新 champion（new_champion_this_round=False），
    champion_id 维持 r1。
    """
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    common = dict(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        target_latency_us=100.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    r.reduce_ledger(candidate=_cand(variant_id="r1_student", latency_us=5.0, round_=1), **common)
    res = r.reduce_ledger(candidate=_cand(variant_id="r2_student", latency_us=5.0, round_=2), **common)
    assert res["new_champion_this_round"] is False
    assert res["champion_id"] == "r1_student"


def test_higher_latency_admitted_does_not_replace_champion(tmp_path):
    """admitted 内更大 latency 不替换 champion（ratchet 只降不升）。"""
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    common = dict(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        target_latency_us=100.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    r.reduce_ledger(candidate=_cand(variant_id="r1_student", latency_us=3.0, round_=1), **common)
    res = r.reduce_ledger(candidate=_cand(variant_id="r2_student", latency_us=8.0, round_=2), **common)
    assert res["champion_id"] == "r1_student"
    assert res["new_champion_this_round"] is False


# ── continue_loop / terminate_reason ────────────────────────────────────────


def test_max_rounds_terminate(tmp_path):
    """无 admitted 且 round ≥ max_rounds → continue_loop=false, reason=max_rounds。"""
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(round_=5, met_accuracy=False, status="FAIL_train", accuracy=-1),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["continue_loop"] is False
    assert res["terminate_reason"] == "max_rounds"


def test_continue_loop_true_when_under_budget_no_admit(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    res = r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(round_=2, met_accuracy=False, status="FAIL_train", accuracy=-1),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    assert res["continue_loop"] is True
    assert res["terminate_reason"] == ""


# ── 写盘 + dry_run ────────────────────────────────────────────────────────────


def test_ledger_appended_one_line_champions_seeded(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(latency_us=4.0),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
    )
    ledger_rows = [json.loads(l) for l in ledger_path.read_text("utf-8").splitlines() if l.strip()]
    champ_rows = [json.loads(l) for l in champions_path.read_text("utf-8").splitlines() if l.strip()]
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["variant_id"] == "r1_student"
    assert ledger_rows[0]["timestamp"] is None
    # 首次 seed baseline + 新 champion → 2 行（baseline + r1）
    assert len(champ_rows) == 2
    assert champ_rows[0]["id"] == "baseline"
    assert champ_rows[1]["id"] == "r1_student"
    assert champ_rows[1]["delta_vs_baseline_us"] == round(4.0 - 10.0, 6)


def test_dry_run_writes_nothing(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    r.reduce_ledger(
        ledger_path=str(ledger_path),
        champions_path=str(champions_path),
        candidate=_cand(latency_us=4.0),
        target_latency_us=6.0,
        accuracy_baseline=0.02,
        accuracy_baseline_kind="nmse",
        max_rounds=5,
        baseline_latency_us=10.0,
        baseline_accuracy=0.02,
        dry_run=True,
    )
    assert not ledger_path.exists()
    assert not champions_path.exists()


# ── fail loud ────────────────────────────────────────────────────────────────


def test_missing_field_fails_loud(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    bad = _cand()
    del bad["ckpt"]
    with pytest.raises(ValueError, match="ckpt"):
        r.reduce_ledger(
            ledger_path=str(ledger_path),
            champions_path=str(champions_path),
            candidate=bad,
            target_latency_us=6.0,
            accuracy_baseline=0.02,
            accuracy_baseline_kind="nmse",
            max_rounds=5,
            baseline_latency_us=10.0,
            baseline_accuracy=0.02,
        )


def test_invalid_status_fails_loud(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    with pytest.raises(ValueError, match="status"):
        r.reduce_ledger(
            ledger_path=str(ledger_path),
            champions_path=str(champions_path),
            candidate=_cand(status="REJECT_struct"),
            target_latency_us=6.0,
            accuracy_baseline=0.02,
            accuracy_baseline_kind="nmse",
            max_rounds=5,
            baseline_latency_us=10.0,
            baseline_accuracy=0.02,
        )


def test_corrupted_ledger_fails_loud(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    ledger_path.write_text("{not valid json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="非合法 JSON"):
        r.reduce_ledger(
            ledger_path=str(ledger_path),
            champions_path=str(champions_path),
            candidate=_cand(),
            target_latency_us=6.0,
            accuracy_baseline=0.02,
            accuracy_baseline_kind="nmse",
            max_rounds=5,
            baseline_latency_us=10.0,
            baseline_accuracy=0.02,
        )


def test_non_bool_met_flag_fails_loud(tmp_path):
    r = _load_reducer()
    ledger_path, champions_path = _seed_baseline(tmp_path)
    bad = _cand()
    bad["met_latency"] = "true"  # string, not bool
    with pytest.raises(ValueError, match="met_latency"):
        r.reduce_ledger(
            ledger_path=str(ledger_path),
            champions_path=str(champions_path),
            candidate=bad,
            target_latency_us=6.0,
            accuracy_baseline=0.02,
            accuracy_baseline_kind="nmse",
            max_rounds=5,
            baseline_latency_us=10.0,
            baseline_accuracy=0.02,
        )


# ── CLI 烟测 ──────────────────────────────────────────────────────────────────


def test_cli_runs_end_to_end(tmp_path):
    """CLI --candidate '@file' 读 fixture + stdout 合法 JSON + exit 0。"""
    import subprocess
    ledger_path = tmp_path / "ledger.jsonl"
    champions_path = tmp_path / "champions.jsonl"
    cand_file = tmp_path / "cand.json"
    cand_file.write_text(json.dumps(_cand(latency_us=4.0)), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(KD_SCRIPTS / "kd_reducer.py"),
            "--ledger", str(ledger_path),
            "--champions", str(champions_path),
            "--candidate", f"@{cand_file}",
            "--target_latency_us", "6.0",
            "--accuracy_baseline", "0.02",
            "--accuracy_baseline_kind", "nmse",
            "--max_rounds", "5",
            "--baseline_latency_us", "10.0",
            "--baseline_accuracy", "0.02",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["champion_id"] == "r1_student"
    assert out["terminate_reason"] == "target_met"
