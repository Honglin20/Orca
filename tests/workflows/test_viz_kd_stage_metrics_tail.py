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


def test_baseline_stage_removed_no_chart(viz_stage):
    """flatten 不推图：--stage baseline 已从 viz_kd_stage 移除 → argparse choices 拒绝
    （render_stage 走 unknown-stage WARN 分支，charts 记 _unknown_stage）。baseline 信息
    由 setup baseline_seed_table 承载。"""
    r = viz_stage.mod.render_stage(
        stage="baseline", ledger_path="", champions_path="",
        baseline_latency_us=5.0, baseline_accuracy=None, target_latency_us=None,
        accuracy_baseline_kind="", teacher_latency_us=None, champion_latency_us=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert "baseline_latency_bar" not in r["charts"]
    assert r["charts"].get("_unknown_stage", {}).get("pushed") is False


def test_teacher_stage_pushes_compare_bar(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="teacher", ledger_path="", champions_path="",
        baseline_latency_us=5.0, baseline_accuracy=None, target_latency_us=None,
        accuracy_baseline_kind="", teacher_latency_us=15.0, champion_latency_us=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["teacher_vs_baseline_bar"]["pushed"] is True


def test_student_stage_parses_round_hypothesis(viz_stage):
    rh = json.dumps([
        {"round": 1, "variant_id": "r1", "hypothesis": "scale -1", "direction_id": "d1", "status": "OK"},
    ])
    r = viz_stage.mod.render_stage(
        stage="student", ledger_path="", champions_path="",
        baseline_latency_us=None, baseline_accuracy=None, target_latency_us=None,
        accuracy_baseline_kind="", teacher_latency_us=None, champion_latency_us=None,
        champion_accuracy=None, round_hypothesis=rh, env_anchor="",
    )
    assert r["charts"]["student_hypothesis_table"]["pushed"] is True


def test_student_stage_empty_hypothesis_skip(viz_stage):
    r = viz_stage.mod.render_stage(
        stage="student", ledger_path="", champions_path="",
        baseline_latency_us=None, baseline_accuracy=None, target_latency_us=None,
        accuracy_baseline_kind="", teacher_latency_us=None, champion_latency_us=None,
        champion_accuracy=None, round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["student_hypothesis_table"]["pushed"] is False


def test_decide_stage_pushes_trajectory_from_champions(tmp_path, viz_stage):
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_us": 10.0, "accuracy": 0.02,
                    "delta_vs_baseline_us": 0, "snapshot": ""}) + "\n" +
        json.dumps({"round": 1, "id": "r1_student", "latency_us": 4.0, "accuracy": 0.018,
                    "delta_vs_baseline_us": -6.0, "snapshot": "/snap/r1.py"}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="decide", ledger_path="", champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.02, target_latency_us=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_us=None,
        champion_latency_us=None, champion_accuracy=None,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["champion_trajectory"]["pushed"] is True
    assert r["charts"]["champion_summary_table"]["pushed"] is True


def test_distill_table_stage_reads_ledger(tmp_path, viz_stage):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"variant_id": "r1_student", "student_path": "/s/r1.py", "round": 1,
                    "parent": "baseline", "latency_us": 4.0, "accuracy": 0.018,
                    "met_latency": True, "met_accuracy": True, "accuracy_kind": "nmse",
                    "direction_id": "d1", "hypothesis": "x", "accepted_cfg": {},
                    "cfg_hash": "h", "ckpt": "/c/r1.pt", "status": "SUCCESS"}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="distill_table", ledger_path=str(ledger), champions_path="",
        baseline_latency_us=10.0, baseline_accuracy=0.02, target_latency_us=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_us=None,
        champion_latency_us=None, champion_accuracy=None,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["distill_round_table"]["pushed"] is True


def test_final_stage_uses_champion_cli_over_champions_file(tmp_path, viz_stage):
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_us": 10.0, "accuracy": 0.02,
                    "delta_vs_baseline_us": 0, "snapshot": ""}) + "\n",
        encoding="utf-8",
    )
    r = viz_stage.mod.render_stage(
        stage="final", ledger_path="", champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.02, target_latency_us=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_us=15.0,
        champion_latency_us=3.5, champion_accuracy=0.019,
        round_hypothesis="", env_anchor="",
    )
    assert r["charts"]["final_compare_bar"]["pushed"] is True


def test_final_stage_all_models_table_covers_all_architectures(tmp_path, viz_stage):
    """全模型总表：baseline + teacher + student + champion 各一行（latency+accuracy）。

    意图：一张表覆盖所有架构，accuracy 来自 evaluate（teacher 行读 teacher_meta.json）。
    """
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_us": 10.0, "accuracy": 0.02,
                    "delta_vs_baseline_us": 0, "snapshot": ""}) + "\n" +
        json.dumps({"round": 2, "id": "r2_champ", "latency_us": 4.0, "accuracy": 0.018,
                    "delta_vs_baseline_us": -6.0, "snapshot": "/snap/r2.py"}) + "\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"variant_id": "r1_student", "round": 1, "latency_us_median": 6.0,
                    "accuracy": 0.022, "met_latency": False, "met_accuracy": False,
                    "status": "FAIL_latency"}) + "\n" +
        json.dumps({"variant_id": "r2_champ", "round": 2, "latency_us_median": 4.0,
                    "accuracy": 0.018, "met_latency": True, "met_accuracy": True,
                    "status": "SUCCESS"}) + "\n",
        encoding="utf-8",
    )
    teacher_meta = tmp_path / "teacher_meta.json"
    teacher_meta.write_text(json.dumps({
        "teacher_latency_us": 35.0, "teacher_accuracy": 0.015,
        "teacher_accuracy_known": True,
    }), encoding="utf-8")

    r = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(ledger), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.02, target_latency_us=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_us=35.0,
        champion_latency_us=4.0, champion_accuracy=0.018,
        teacher_meta_path=str(teacher_meta), env_anchor="",
    )
    assert r["charts"]["all_models_table"]["pushed"] is True
    # 从 mock 捕获的 render_chart 调用里找到总表（按 title）
    table_calls = [c for c in viz_stage.calls if c.get("title") == "All Models (accuracy × latency)"]
    assert table_calls, "all_models_table 未被推送"
    rows = table_calls[-1]["data"]
    roles = {row["role"] for row in rows}
    assert {"baseline", "teacher", "student", "champion"} <= roles, roles
    # teacher 行带真实 accuracy
    teacher_row = next(row for row in rows if row["role"] == "teacher")
    assert teacher_row["accuracy"] == 0.015
    assert teacher_row["status"] == "teacher"
    # r2_champ 标为 champion（命中 champion id 集合）
    champ_row = next(row for row in rows if row["id"] == "r2_champ")
    assert champ_row["role"] == "champion"


# ── SPEC §3.4 终态帕累托前沿 + FAIL 分布（viz_kd 可复用不变量迁移）──────────────


def _write_pareto_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """写 SPEC §3.4 fixture：
    - 2 行 SUCCESS（一 min-kind 方向 db，一 max-kind 方向 acc）；
    - 1 行 FAIL_accuracy + accuracy_kind 非空（真测值，应计入前沿）；
    - 1 行 FAIL_latency（accuracy_kind 空，哨兵，不计入）；
    - 1 行 FAIL_train（accuracy_kind 空，哨兵，不计入）；
    - 1 行 db-kind accuracy=-1.0 真测（NEW-2 回归守护：不误剔）。
    """
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        # min-kind (db) SUCCESS
        {"variant_id": "r1_min", "round": 1, "latency_us_median": 6.0,
         "accuracy": -20.0, "accuracy_kind": "db", "met_accuracy": True,
         "status": "SUCCESS"},
        # max-kind (acc) SUCCESS
        {"variant_id": "r2_max", "round": 2, "latency_us_median": 8.0,
         "accuracy": 0.95, "accuracy_kind": "acc", "met_accuracy": True,
         "status": "SUCCESS"},
        # FAIL_accuracy + accuracy_kind 非空 = 真测，应计入前沿（与 viz_kd 一致）
        {"variant_id": "r3_facc", "round": 3, "latency_us_median": 7.0,
         "accuracy": -25.0, "accuracy_kind": "db", "met_accuracy": False,
         "status": "FAIL_accuracy"},
        # FAIL_latency 哨兵（accuracy_kind 空 → is_measured_row False → 不计入）
        {"variant_id": "r4_flat", "round": 4, "latency_us_median": 100.0,
         "accuracy": 0, "accuracy_kind": "", "met_accuracy": False,
         "status": "FAIL_latency"},
        # FAIL_train 哨兵（accuracy_kind 空 → 不计入）
        {"variant_id": "r5_ftrain", "round": 5, "latency_us_median": 5.0,
         "accuracy": 0, "accuracy_kind": "", "met_accuracy": False,
         "status": "FAIL_train"},
        # db-kind accuracy=-1.0 真测点（NEW-2：旧 !=-1 过滤会误剔）
        {"variant_id": "r6_dbneg", "round": 6, "latency_us_median": 9.0,
         "accuracy": -1.0, "accuracy_kind": "db", "met_accuracy": False,
         "status": "FAIL_accuracy"},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_us": 10.0,
                    "accuracy": -22.0}) + "\n",
        encoding="utf-8",
    )
    return ledger, champs


def test_final_stage_pareto_front_and_fail_status_bar_pushed(tmp_path, viz_stage):
    """SPEC §3.4：final stage 推 pareto_front + fail_status_bar；5 不变量守护。"""
    ledger, champs = _write_pareto_fixture(tmp_path)
    r = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(ledger), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=-22.0, target_latency_us=5.0,
        accuracy_baseline_kind="db", teacher_latency_us=35.0,
        champion_latency_us=6.0, champion_accuracy=-20.0,
        env_anchor="",
    )
    assert r["charts"]["pareto_front"]["pushed"] is True, r["charts"]["pareto_front"]
    assert r["charts"]["fail_status_bar"]["pushed"] is True, r["charts"]["fail_status_bar"]

    pareto_calls = [c for c in viz_stage.calls if c.get("chart_type") == "pareto"]
    assert pareto_calls, "pareto_front 未推送"
    pts = pareto_calls[-1]["data"]
    # ① min-kind (db) → y 取负显示（原 -20 → 20）
    r1 = next(p for p in pts if p.get("met_accuracy") != "ref" and p["latency_us"] == 6.0)
    assert r1["accuracy"] == 20.0, f"min-kind 取负显示失败：{r1}"
    # baseline 参考点 hue="ref"
    ref = next(p for p in pts if p.get("met_accuracy") == "ref")
    assert ref["latency_us"] == 10.0 and ref["accuracy"] == 22.0
    # ③ FAIL_latency / FAIL_train 哨兵不计入（accuracy_kind 空 → is_measured_row False）
    pt_lats = {p["latency_us"] for p in pts if p.get("met_accuracy") != "ref"}
    assert 100.0 not in pt_lats, "FAIL_latency 哨兵行不应计入 pareto"
    assert 5.0 not in pt_lats, "FAIL_train 哨兵行不应计入 pareto"
    # ④ FAIL_accuracy + accuracy_kind 非空 计入前沿（与 SUCCESS 同列）
    assert 7.0 in pt_lats, "FAIL_accuracy 真测行应计入 pareto"
    # ⑤ db-kind accuracy=-1.0 真测点不误剔（NEW-2 回归）
    r6 = next(p for p in pts if p["latency_us"] == 9.0)
    assert r6["accuracy"] == 1.0, f"db-kind -1.0 应取负显示为 1.0：{r6}"
    # pareto 点数 == 有效测量行（r1/r2/r3/r6 = 4）+ baseline ref
    non_ref = [p for p in pts if p.get("met_accuracy") != "ref"]
    assert len(non_ref) == 4, f"应有 4 个真测 student 点，实际 {len(non_ref)}：{pts}"
    # 方向参数
    assert pareto_calls[-1]["pareto_x_direction"] == "min"
    assert pareto_calls[-1]["pareto_y_direction"] == "max"

    # fail_status_bar：6 status 计数（SUCCESS=2, FAIL_accuracy=2, FAIL_latency=1, FAIL_train=1）
    bar_calls = [c for c in viz_stage.calls
                 if c.get("chart_type") == "bar" and "status counts" in c.get("title", "")]
    assert bar_calls, "fail_status_bar 未推送"
    counts = {row["status"]: row["count"] for row in bar_calls[-1]["data"]}
    assert counts["SUCCESS"] == 2
    assert counts["FAIL_latency"] == 1
    assert counts["FAIL_train"] == 1
    assert counts["FAIL_accuracy"] == 2


def test_pareto_front_unknown_kind_warn_skip(viz_stage, tmp_path):
    """不变量 ②：unknown kind → pareto WARN-skip（pushed=False），不 auto 猜方向。"""
    ledger, champs = _write_pareto_fixture(tmp_path)
    r = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(ledger), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.5, target_latency_us=5.0,
        accuracy_baseline_kind="mystery",  # 未知 kind
        teacher_latency_us=35.0, champion_latency_us=6.0, champion_accuracy=0.5,
        env_anchor="",
    )
    assert r["charts"]["pareto_front"]["pushed"] is False
    assert "unknown kind" in r["charts"]["pareto_front"]["reason"]


def test_pareto_front_min_kind_negates_y_for_max_direction(viz_stage, tmp_path):
    """不变量 ① 单测：min-kind (snr/mse/db/nmse/ber) → display 取负使「越大越好」统一。

    用 snr（max-kind）作对照：原值不取负。同 fixture 跑两遍 kind=db vs kind=snr 验证变换。
    """
    ledger, champs = _write_pareto_fixture(tmp_path)
    # min-kind (db)：y 取负
    r_min = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(ledger), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=-22.0, target_latency_us=5.0,
        accuracy_baseline_kind="db", teacher_latency_us=35.0,
        champion_latency_us=6.0, champion_accuracy=-20.0, env_anchor="",
    )
    assert r_min["charts"]["pareto_front"]["pushed"] is True
    pts_min = [c for c in viz_stage.calls if c.get("chart_type") == "pareto"][-1]["data"]
    # r1_min latency=6.0 accuracy=-20.0 → display 20.0
    r1_min = next(p for p in pts_min if p["latency_us"] == 6.0)
    assert r1_min["accuracy"] == 20.0

    # max-kind (acc)：原值不取负
    # 改 fixture：r2_max accuracy=0.95（acc 方向）
    r_max = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(ledger), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.5, target_latency_us=5.0,
        accuracy_baseline_kind="acc", teacher_latency_us=35.0,
        champion_latency_us=6.0, champion_accuracy=0.95, env_anchor="",
    )
    assert r_max["charts"]["pareto_front"]["pushed"] is True
    pts_max = [c for c in viz_stage.calls if c.get("chart_type") == "pareto"][-1]["data"]
    # r2_max latency=8.0 accuracy=0.95 → display 原值 0.95
    r2_max = next(p for p in pts_max if p["latency_us"] == 8.0)
    assert r2_max["accuracy"] == 0.95


def test_fail_status_bar_empty_ledger_skip(viz_stage, tmp_path):
    """空 ledger → fail_status_bar WARN 跳过（pushed=False）。"""
    champs = tmp_path / "champions.jsonl"
    champs.write_text(
        json.dumps({"round": 0, "id": "baseline", "latency_us": 10.0}) + "\n",
        encoding="utf-8",
    )
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    r = viz_stage.mod.render_stage(
        stage="final", ledger_path=str(empty), champions_path=str(champs),
        baseline_latency_us=10.0, baseline_accuracy=0.02, target_latency_us=5.0,
        accuracy_baseline_kind="nmse", teacher_latency_us=35.0,
        champion_latency_us=6.0, champion_accuracy=0.02, env_anchor="",
    )
    assert r["charts"]["fail_status_bar"]["pushed"] is False


# ── viz_kd_stage: render_chart 抛异常时单图不阻断 ──────────────────────────


def test_render_chart_exception_does_not_block(viz_stage):
    """teacher stage render_chart 抛异常 → charts.teacher_vs_baseline_bar.pushed=False reason=generic:...
    （单图异常不阻断其他图；原用 baseline stage 验证，baseline stage 已移除，改 teacher 保覆盖。）"""
    h = _MockOrca(
        "viz_kd_stage.py",
        render_impl=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    try:
        r = h.mod.render_stage(
            stage="teacher", ledger_path="", champions_path="",
            baseline_latency_us=5.0, baseline_accuracy=None, target_latency_us=None,
            accuracy_baseline_kind="", teacher_latency_us=15.0, champion_latency_us=None,
            champion_accuracy=None, round_hypothesis="", env_anchor="",
        )
        assert r["charts"]["teacher_vs_baseline_bar"]["pushed"] is False
        assert "generic:RuntimeError" in r["charts"]["teacher_vs_baseline_bar"]["reason"]
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
    """正常 stage（baseline_seed）下，即便 orca.chart 不可用（env_missing/import_failed），
    _main 兜底仍 emit 合法 JSON（returncode 0）。注：baseline stage 已移除（flatten 不推图）。"""
    proc = subprocess.run(
        [sys.executable, str(KD_SCRIPTS / "viz_kd_stage.py"),
         "--stage", "baseline_seed", "--baseline_latency_us", "5.0"],
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
