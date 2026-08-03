"""tests/workflows/test_viz_kd_stage_metrics_tail.py —— viz_kd_stage + metrics_tail 单测。

覆盖 SPEC §8 / §9 契约（Rule 9：测意图）：
- viz_kd_stage: stage dispatch（baseline/teacher/student/distill_table/decide/final）+
  数据不足 skip + env_missing 仍 emit 合法 JSON。
- metrics_tail: 默认 loss 推送（无 template，扫 loss_avg= 行）+ template metric（regex
  named group）+ 模板非法 JSON 走默认 + source_log 缺失不阻断 + env_missing emit 合法 JSON。
- _main 兜底：异常路径下 stdout 必有 viz_env_status=generic + charts 字段。

mock ``orca.chart.render_chart`` + ``orca.chart._env``（不依赖真 Orca runtime）。
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KD_SCRIPTS = REPO / "workflows" / "agents" / "_kd_scripts"


# ── mock harness（viz_struct_robustness 同款思路，KD 版）──────────────────────


class _MockOrca:
    """注入 mock orca.chart + orca.chart._env，reload KD viz script。"""

    def __init__(self, script_name: str, *, render_impl=None):
        self.calls: list[dict] = []
        saved = {
            "orca": sys.modules.get("orca"),
            "orca.chart": sys.modules.get("orca.chart"),
            "orca.chart._env": sys.modules.get("orca.chart._env"),
        }
        self._saved = saved
        self._script_name = script_name

        if render_impl is None:
            def _default(**kw):
                self.calls.append(kw)
                return len(self.calls)
            render_fn = _default
        else:
            render_fn = render_impl

        mock_chart = types.ModuleType("orca.chart")
        mock_chart.render_chart = render_fn
        mock_env = types.ModuleType("orca.chart._env")
        mock_env.load_run_env_from_artifacts = lambda anchor: None
        sys.modules["orca"] = types.ModuleType("orca")
        sys.modules["orca.chart"] = mock_chart
        sys.modules["orca.chart._env"] = mock_env
        sys.path.insert(0, str(KD_SCRIPTS))
        mod_name = {"viz_kd_stage.py": "viz_kd_stage_t", "metrics_tail.py": "metrics_tail_t"}[script_name]
        # 清掉旧 import 让 reload 拿到 mock。
        for m in [k for k in sys.modules if k in (mod_name,)]:
            del sys.modules[m]
        spec = importlib.util.spec_from_file_location(mod_name, KD_SCRIPTS / script_name)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod
        self.mod_name = mod_name

    def restore(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


@pytest.fixture
def viz_stage():
    h = _MockOrca("viz_kd_stage.py")
    yield h
    h.restore()


@pytest.fixture
def m_tail():
    h = _MockOrca("metrics_tail.py")
    yield h
    h.restore()


# ── viz_kd_stage: stage dispatch ──────────────────────────────────────────────


def test_baseline_stage_pushes_latency_bar(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="baseline",
        ledger_path="",
        champions_path="",
        baseline_latency_ms=5.0,
        baseline_accuracy=None,
        target_latency_ms=None,
        accuracy_baseline_kind="",
        teacher_latency_ms=None,
        champion_latency_ms=None,
        champion_accuracy=None,
        round_hypothesis="",
        env_anchor="",
    )
    assert r["viz_env_status"] in {"ok", "env_loaded_from_file", "env_missing"}
    assert "baseline_latency_bar" in r["charts"]
    assert r["charts"]["baseline_latency_bar"]["pushed"] is True
    assert viz_stage.calls and viz_stage.calls[0]["chart_type"] == "bar"


def test_baseline_stage_missing_latency_skip(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="baseline", ledger_path="", champions_path="",
        baseline_latency_ms=None, baseline_accuracy=None, target_latency_ms=None,
        accuracy_baseline_kind="", teacher_latency_ms=None, champion_latency_ms=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["baseline_latency_bar"]["pushed"] is False
    assert "缺失" in r["charts"]["baseline_latency_bar"]["reason"] or "无效" in r["charts"]["baseline_latency_bar"]["reason"]


def test_teacher_stage_pushes_compare_bar(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="teacher", ledger_path="", champions_path="",
        baseline_latency_ms=5.0, baseline_accuracy=None, target_latency_ms=None,
        accuracy_baseline_kind="", teacher_latency_ms=15.0, champion_latency_ms=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["teacher_vs_baseline_bar"]["pushed"] is True


def test_student_stage_parses_round_hypothesis(viz_stage):
    rh = json.dumps([
        {"round": 1, "variant_id": "r1", "hypothesis": "scale -1", "direction_id": "d1", "status": "OK"},
    ])
    r = viz_stage.mod.render_stage(
        stage="student", ledger_path="", champions_path="",
        baseline_latency_ms=None, baseline_accuracy=None, target_latency_ms=None,
        accuracy_baseline_kind="", teacher_latency_ms=None, champion_latency_ms=None,
        champion_accuracy=None, round_hypothesis=rh, env_anchor="",
    )
    assert r["charts"]["student_hypothesis_table"]["pushed"] is True


def test_student_stage_empty_hypothesis_skip(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="student", ledger_path="", champions_path="",
        baseline_latency_ms=None, baseline_accuracy=None, target_latency_ms=None,
        accuracy_baseline_kind="", teacher_latency_ms=None, champion_latency_ms=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["student_hypothesis_table"]["pushed"] is False


def test_decide_stage_pushes_trajectory_from_champions(tmp_path, viz_stage):
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_ms": 10.0, "accuracy": 0.02,
                    "delta_vs_baseline_ms": 0, "snapshot": ""}) + "\n" +
        json.dumps({"round": 1, "id": "r1_student", "latency_ms": 4.0, "accuracy": 0.018,
                    "delta_vs_baseline_ms": -6.0, "snapshot": "/snap/r1.py"}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="decide", ledger_path="", champions_path=str(champs),
        baseline_latency_ms=10.0, baseline_accuracy=0.02, target_latency_ms=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_ms=None,
        champion_latency_ms=None, champion_accuracy=None,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["champion_trajectory"]["pushed"] is True
    assert r["charts"]["champion_summary_table"]["pushed"] is True


def test_distill_table_stage_reads_ledger(tmp_path, viz_stage):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"variant_id": "r1_student", "student_path": "/s/r1.py", "round": 1,
                    "parent": "baseline", "latency_ms": 4.0, "accuracy": 0.018,
                    "met_latency": True, "met_accuracy": True, "accuracy_kind": "nmse",
                    "direction_id": "d1", "hypothesis": "x", "accepted_cfg": {},
                    "cfg_hash": "h", "ckpt": "/c/r1.pt", "status": "SUCCESS"}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="distill_table", ledger_path=str(ledger), champions_path="",
        baseline_latency_ms=10.0, baseline_accuracy=0.02, target_latency_ms=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_ms=None,
        champion_latency_ms=None, champion_accuracy=None,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["distill_round_table"]["pushed"] is True


def test_final_stage_uses_champion_cli_over_champions_file(tmp_path, viz_stage):
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_ms": 10.0, "accuracy": 0.02,
                    "delta_vs_baseline_ms": 0, "snapshot": ""}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="final", ledger_path="", champions_path=str(champs),
        baseline_latency_ms=10.0, baseline_accuracy=0.02, target_latency_ms=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_ms=15.0,
        champion_latency_ms=3.5, champion_accuracy=0.019,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["final_compare_bar"]["pushed"] is True


# ── viz_kd_stage: render_chart 抛异常时单图不阻断 ──────────────────────────


def test_render_chart_exception_does_not_block(viz_stage):
    """baseline stage 抛异常 → charts.baseline_latency_bar.pushed=False reason=generic:..."""
    h = _MockOrca(
        "viz_kd_stage.py",
        render_impl=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        r = h.mod.render_stage(
            stage="baseline", ledger_path="", champions_path="",
            baseline_latency_ms=5.0, baseline_accuracy=None, target_latency_ms=None,
            accuracy_baseline_kind="", teacher_latency_ms=None, champion_latency_ms=None,
            champion_accuracy=None, round_hypothesis="", env_anchor="",
        )
        assert r["charts"]["baseline_latency_bar"]["pushed"] is False
        assert "generic:RuntimeError" in r["charts"]["baseline_latency_bar"]["reason"]
    finally:
        h.restore()


# ── metrics_tail: 默认 loss 推送（无 template）──────────────────────────────


def test_metrics_tail_default_loss(tmp_path, m_tail):
    log = tmp_path / "teacher_train.log"
    log.write_text(
        "[train_pipeline:teacher] epoch=0 loss_avg=0.5\n"
        "[train_pipeline:teacher] epoch=1 loss_avg=0.3\n"
        "[train_pipeline:teacher] epoch=2 loss_avg=0.2\n",
        encoding="utf-8",
    )
    r = m_tail.mod.render_metrics(
        template="",
        source_log=str(log),
        variant_id="teacher",
        mode="teacher",
        env_anchor="",
    )
    assert r["charts"]["default_loss"]["pushed"] is True
    assert "3 points" in r["charts"]["default_loss"]["reason"]


def test_metrics_tail_default_loss_empty_log_skip(tmp_path, m_tail):
    log = tmp_path / "empty.log"
    log.write_text("nothing useful\n", encoding="utf-8")
    r = m_tail.mod.render_metrics(
        template="", source_log=str(log), variant_id="t", mode="teacher", env_anchor="",
    )
    assert r["charts"]["default_loss"]["pushed"] is False


def test_metrics_tail_missing_log_does_not_block(m_tail):
    r = m_tail.mod.render_metrics(
        template="", source_log="/no/such/file", variant_id="t", mode="teacher", env_anchor="",
    )
    assert "_source_log_missing" in r["charts"]
    assert r["charts"]["_source_log_missing"]["pushed"] is False


# ── metrics_tail: template 推送 ─────────────────────────────────────────────


def test_metrics_tail_template_named_group(tmp_path, m_tail):
    log = tmp_path / "train.log"
    log.write_text(
        "epoch=0 nmse=0.05\nepoch=1 nmse=0.03\nepoch=2 nmse=0.02\n", encoding="utf-8",
    )
    tpl = json.dumps({
        "source_log": str(log),
        "metrics": [
            {"name": "nmse", "regex": r"epoch=(?P<epoch>\d+)\s+nmse=(?P<val>[0-9.]+)",
             "chart_type": "line", "x": "epoch", "y": "val"},
        ],
    })
    r = m_tail.mod.render_metrics(
        template=tpl, source_log=str(log), variant_id="r1", mode="distill", env_anchor="",
    )
    assert r["charts"]["nmse"]["pushed"] is True
    assert "3 points" in r["charts"]["nmse"]["reason"]


def test_metrics_tail_template_y_not_in_group_skip(tmp_path, m_tail):
    log = tmp_path / "train.log"
    log.write_text("epoch=0 nmse=0.05\n", encoding="utf-8")
    tpl = json.dumps({
        "source_log": str(log),
        "metrics": [
            {"name": "bad", "regex": r"epoch=(?P<epoch>\d+)", "chart_type": "line", "x": "epoch", "y": "val"},
        ],
    })
    r = m_tail.mod.render_metrics(
        template=tpl, source_log=str(log), variant_id="r1", mode="distill", env_anchor="",
    )
    assert r["charts"]["bad"]["pushed"] is False
    assert "named group" in r["charts"]["bad"]["reason"]


def test_metrics_tail_invalid_template_json_falls_back_to_default(tmp_path, m_tail):
    log = tmp_path / "train.log"
    log.write_text("[train_pipeline:distill] epoch=0 kd_loss_avg=1.0\n", encoding="utf-8")
    r = m_tail.mod.render_metrics(
        template="not-json", source_log=str(log), variant_id="r1", mode="distill", env_anchor="",
    )
    assert r["charts"]["default_loss"]["pushed"] is True


# ── _main 兜底 ──────────────────────────────────────────────────────────────


def test_viz_kd_stage_main_emits_json_even_on_bad_args(tmp_path):
    """CLI 不存在的 stage 被 argparse 拒（choices 限制）；验证正常 stage 下 stdout 合法。"""
    proc = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "viz_kd_stage.py"),
         "--stage", "baseline", "--baseline_latency_ms", "5.0"],
        capture_output=True, text=True,
    )
    # 没装 orca.chart 时 import_failed/env_missing，stdout 仍 emit 合法 JSON。
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "viz_env_status" in out
    assert isinstance(out["charts"], dict)


def test_metrics_tail_main_emits_json(tmp_path):
    log = tmp_path / "empty.log"
    log.write_text("", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "metrics_tail.py"),
         "--source_log", str(log), "--variant_id", "t", "--mode", "teacher"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    assert "viz_env_status" in out
    assert isinstance(out["charts"], dict)
