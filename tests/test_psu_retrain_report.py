"""test_psu_retrain_report.py —— PSU P4 产出测试。

覆盖 intent（Rule 9：验证 intent 非 behavior）：

1. ``psu_report/scripts/emit_report.py`` 终态归因（fail loud——设计内的失败模式必须落在
   明确的 stage/reason，不得退化成 ``unknown terminal state``）：
   - ``original_equivalence``：``.equivalence.json`` passed=false → stage=expand；
   - ``expand_crashed``：supernet.py 在而无 .equivalence.json 且无下游产物 → stage=expand；
   - ``training_prerequisites_missing``：viable=false（无训练脚本、无 .train_rc、无搜索产物）
     → stage=train_script。
   另带一条 success 回归（链序重排不得破坏正常路径）。

2. ``psu_report/scripts/emit_report.py`` final_metrics 优先源（真实 E2E 事故：完成后的
   retrain_status.md 残留 running 文本被 final_metrics 读走）：
   - retrain log 尾部确定性指标行（``done best <metric> <v>`` / ``[eval] unit N <metric> <v>``，
     生成契约 §3(c) 固定）> 终态 retrain_status.md（status: completed）> 残留文本。

3. ``psu_retrain_script`` 硬 gate（``check_retrain_script.sh``）：progress.jsonl 写粒度
   （每 N 步 + unit 末必写）+ 确定性终态指标行（final_metrics 数据源）。

4. ``progress_watcher.py`` 静态兜底铁律：live push 不可用时每指标**必须**落静态图
   （plotly → matplotlib → 零依赖 SVG HTML 保底），单点数据也落（注明 single point）；
   attempt 已结束时补跑 → 立即 drain + 落盘 + exit 0（幂等）。

5. ``psu_run_search/scripts/append_anchor_candidates.py`` 幂等性：同一 artifacts 目录跑两次，
   第二次零追加（按 arch 键去重），L+1 条 anchor（all-original + 每 slot 单换首个非 original）
   一次补齐且数值确定性（无时钟/随机）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_EMIT_REPORT = _REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_report" / "scripts" / "emit_report.py"
_ANCHOR = (
    _REPO
    / "workflows"
    / "puzzle-supernet"
    / "agents"
    / "psu_run_search"
    / "scripts"
    / "append_anchor_candidates.py"
)
_RETRAIN_SCRIPT_AGENT = _REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_retrain_script"
_CHECK_RETRAIN_SCRIPT = _RETRAIN_SCRIPT_AGENT / "scripts" / "check_retrain_script.sh"
_TRAIN_WATCHER = (
    _REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_run_train" / "scripts" / "progress_watcher.py"
)
_RETRAIN_WATCHER = (
    _REPO / "workflows" / "puzzle-supernet" / "agents" / "psu_retrain" / "scripts" / "progress_watcher.py"
)


# ── emit_report.py：终态三分支 + success 回归 ──────────────────────────────────


def _run_emit_report(ad: Path) -> dict:
    env = dict(os.environ, ORCA_ARTIFACTS_DIR=str(ad))
    proc = subprocess.run(
        [sys.executable, str(_EMIT_REPORT)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(ad),
    )
    assert proc.returncode == 0, f"emit_report.py failed:\n{proc.stderr}"
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, "emit_report.py printed nothing"
    return json.loads(lines[-1])


def _write_expand_stage_base(ad: Path, *, equivalence_passed: bool | None) -> None:
    """flatten+expand 前置产物（manifest / flat / supernet / summary）± 等价 gate 落盘。"""
    (ad / "project_manifest.md").write_text(
        "# Project Manifest\n\n- num_classes: 10\n- input_size: 3x28x28\n", encoding="utf-8"
    )
    (ad / "model_flat.py").write_text("# flattened model\n", encoding="utf-8")
    (ad / "supernet.py").write_text("# supernet\n", encoding="utf-8")
    (ad / "supernet_summary.md").write_text(
        "# Supernet Summary\n\nbranch set wired, freeze groups recorded\n", encoding="utf-8"
    )
    if equivalence_passed is not None:
        (ad / ".equivalence.json").write_text(
            json.dumps({"passed": equivalence_passed, "max_abs_diff": 0.0 if equivalence_passed else 0.5}),
            encoding="utf-8",
        )


def test_emit_report_original_equivalence_branch(tmp_path: Path) -> None:
    """.equivalence.json passed=false → failed / expand / 等价 gate 归因。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_expand_stage_base(ad, equivalence_passed=False)

    report = _run_emit_report(ad)
    assert report["status"] == "failed"
    assert report["stage"] == "expand"
    assert "equivalence" in report["reason"].lower()
    assert report["error"] == report["reason"]  # fail loud：error 不为空


def test_emit_report_expand_crashed_branch(tmp_path: Path) -> None:
    """supernet.py 在而无 .equivalence.json 且无下游 → failed / expand / expand crashed 归因。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_expand_stage_base(ad, equivalence_passed=None)

    report = _run_emit_report(ad)
    assert report["status"] == "failed"
    assert report["stage"] == "expand"
    assert "expand crashed" in report["reason"].lower()


def test_emit_report_training_prerequisites_missing_branch(tmp_path: Path) -> None:
    """expand 成功（gate pass）但无训练脚本且训练从未跑 → failed / train_script 归因。

    这是 viable=false 路由到 psu_report 的兜底：v3 版会落 ``unknown terminal state``。
    """
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_expand_stage_base(ad, equivalence_passed=True)
    # 刻意不写 train_supernet.py / run_train_supernet.sh / .train_rc / search_results.jsonl。

    report = _run_emit_report(ad)
    assert report["status"] == "failed"
    assert report["stage"] == "train_script"
    assert "prerequisites" in report["reason"].lower()


def test_emit_report_success_path_regression(tmp_path: Path) -> None:
    """全链产物齐备 → success / retrain（三分支插入不得破坏首匹配链的正常路径）。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_expand_stage_base(ad, equivalence_passed=True)
    (ad / "train_supernet.py").write_text("# kd trainer\n", encoding="utf-8")
    (ad / "run_train_supernet.sh").write_text("# launcher\n", encoding="utf-8")
    (ad / "train_status.md").write_text("train done\n", encoding="utf-8")
    train_dir = ad / "runs" / "train"
    train_dir.mkdir(parents=True)
    (train_dir / ".train_rc").write_text("0", encoding="utf-8")
    (train_dir / "supernet_best.pth").write_text("ckpt-bytes", encoding="utf-8")
    (ad / "search_results.jsonl").write_text(
        '{"objs": {"acc": -0.9, "latency": 1.2}, "pareto": true, '
        '"arch": {"branch_choices": ["original", "original", "vanilla"]}}\n',
        encoding="utf-8",
    )
    (ad / ".selected_arch.json").write_text(
        json.dumps(
            {
                "selected_arch": {"branch_choices": ["original", "original", "vanilla"]},
                "selected_acc": 0.9,
                "selected_latency": 1.2,
                "latency_unit": "ms",
                "pareto_size": 3,
                "select_reason": "max-acc-under-target",
            }
        ),
        encoding="utf-8",
    )
    retrain_dir = ad / "runs" / "retrain"
    retrain_dir.mkdir(parents=True)
    (retrain_dir / ".retrain_rc").write_text("0", encoding="utf-8")
    (retrain_dir / "retrain_best.pth").write_text("ckpt-bytes", encoding="utf-8")
    (ad / "retrain_status.md").write_text("final acc 0.91\n", encoding="utf-8")

    report = _run_emit_report(ad)
    assert report["status"] == "success"
    assert report["stage"] == "retrain"
    # artifacts 增列 KD 训后超网 ckpt（§2.9）
    assert "runs/train/supernet_best.pth" in report["artifacts"]
    assert report["selected_arch"] == {"branch_choices": ["original", "original", "vanilla"]}


# ── append_anchor_candidates.py：幂等 + L+1 形状 ───────────────────────────────

_SUPERNET_PY = '''\
"""Toy choice-only supernet for the anchor idempotency test (3 slots, D5 branch set)."""
from dataclasses import dataclass

D5 = ("original", "vanilla", "random_synthesizer", "relu_attention", "fnet", "softs_star")


class SearchSpace:
    def __init__(self):
        # per-slot choice containers (PSU SearchSpace contract: the only public
        # list/tuple attributes are the choice containers).
        self.branch_choices = tuple(tuple(D5) for _ in range(3))


@dataclass
class ArchConfig:
    branch_choices: tuple


class SuperNet:
    def __init__(self, search_space):
        self.search_space = search_space
        self.arch_config = ArchConfig(
            branch_choices=tuple("original" for _ in search_space.branch_choices)
        )
'''

_EVALUATOR_PY = '''\
"""Toy evaluator: deterministic quality from the per-slot choices (no torch needed)."""
from supernet import ArchConfig  # canonical sibling import -- exercises the loader's path setup


class CandidateEvaluator:
    def __init__(self, *, device=None, evaluator_cfg=None):
        self.device = device
        self.cfg = evaluator_cfg

    def evaluate(self, arch_config: ArchConfig) -> dict:
        # higher accuracy with more vanilla swaps; stored negated (smaller-is-better)
        n_variant = sum(1 for b in arch_config.branch_choices if b != "original")
        return {"acc": -(0.50 + 0.10 * n_variant)}
'''

_LATENCY_PY = '''\
"""Toy latency estimator: deterministic latency from the per-slot choices."""


class LatencyEstimator:
    def __init__(self, search_space, latency_cfg=None):
        self.search_space = search_space
        self.latency_cfg = latency_cfg

    def get_latency(self, arch_config) -> float:
        n_variant = sum(1 for b in arch_config.branch_choices if b != "original")
        return 1.0 + 0.25 * n_variant
'''

_SEARCH_CONFIG_YAML = """\
objs:
  - "acc"
  - "latency"
latency_cfg:
  warmup: 1
  repetitions: 2
  batch_size: 1
evaluator_cfg:
  supernet_ckpt_path: "./runs/train/supernet_best.pth"
  data_dir: "./data"
  batch_size: 4
  num_workers: 0
"""

_SEED_RECORDS = [
    {"objs": {"acc": -0.8, "latency": 2.0}, "pareto": True,
     "arch": {"branch_choices": ["original", "vanilla", "fnet"]}},
    {"objs": {"acc": -0.6, "latency": 1.75}, "pareto": False,
     "arch": {"branch_choices": ["fnet", "original", "random_synthesizer"]}},
]


def _write_anchor_fixture(ad: Path) -> None:
    (ad / "supernet.py").write_text(_SUPERNET_PY, encoding="utf-8")
    (ad / "evaluator.py").write_text(_EVALUATOR_PY, encoding="utf-8")
    (ad / "latency_estimator.py").write_text(_LATENCY_PY, encoding="utf-8")
    (ad / "search_config.yaml").write_text(_SEARCH_CONFIG_YAML, encoding="utf-8")
    with (ad / "search_results.jsonl").open("w", encoding="utf-8") as fh:
        for rec in _SEED_RECORDS:
            fh.write(json.dumps(rec) + "\n")


def _run_anchor(ad: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(_ANCHOR), "--artifacts-dir", str(ad)],
        capture_output=True,
        text=True,
        cwd=str(ad),
    )
    assert proc.returncode == 0, f"append_anchor_candidates.py failed:\n{proc.stderr}"
    return proc.stdout


def _read_records(ad: Path) -> list[dict]:
    rows = []
    with (ad / "search_results.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_append_anchor_candidates_idempotent(tmp_path: Path) -> None:
    """跑两次：第一次补齐 L+1=4 条 anchor，第二次零追加（arch 键去重）。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_anchor_fixture(ad)

    out1 = _run_anchor(ad)
    assert "appended=4" in out1, out1
    assert "skipped_existing=0" in out1, out1

    out2 = _run_anchor(ad)
    assert "appended=0" in out2, out2
    assert "skipped_existing=4" in out2, out2

    rows = _read_records(ad)
    assert len(rows) == 2 + 4  # 2 seed + 4 anchors, 第二次零追加


def test_append_anchor_candidates_shape_and_records(tmp_path: Path) -> None:
    """anchor 集 = all-original + 每 slot 单换 vanilla（all-original 基底）；记录带 objs/anchor 标记。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_anchor_fixture(ad)
    _run_anchor(ad)

    rows = _read_records(ad)
    SEED = {("original", "vanilla", "fnet"), ("fnet", "original", "random_synthesizer")}
    anchors = [r for r in rows if tuple(r["arch"]["branch_choices"]) not in SEED]
    assert len(anchors) == 4

    archs = {tuple(r["arch"]["branch_choices"]) for r in anchors}
    assert ("original", "original", "original") in archs
    assert ("vanilla", "original", "original") in archs
    assert ("original", "vanilla", "original") in archs
    assert ("original", "original", "vanilla") in archs
    assert len(archs) == 4  # 无重复 arch

    # 记录形状 = 搜索 logger 规范六键（生成的 select 可消费 gene/generation，多余键即契约破坏）
    for rec in anchors:
        assert set(rec.keys()) == {"generation", "gene", "objs", "cached", "pareto", "arch"}
        assert rec["pareto"] is False
        assert rec["cached"] is False
        assert set(rec["objs"].keys()) == {"acc", "latency"}
        assert isinstance(rec["gene"], list) and len(rec["gene"]) == 3
    all_original_rec = next(
        r for r in anchors if r["arch"]["branch_choices"] == ["original"] * 3
    )
    assert all_original_rec["gene"] == [0, 0, 0]  # D5 枚举序 original 居首
    assert all_original_rec["objs"]["acc"] == -0.5  # toy evaluator 的确定值
    assert all_original_rec["objs"]["latency"] == 1.0  # toy estimator 的确定值

    # anchor 溯源在 sidecar，不进记录本体
    sidecar = json.loads((ad / ".anchor_appended.json").read_text(encoding="utf-8"))
    assert len(sidecar["appended"]) == 4

    # seed 记录原样保留（未被改写）
    seed_rows = [r for r in rows if tuple(r["arch"]["branch_choices"]) in SEED]
    assert len(seed_rows) == 2


def test_append_anchor_candidates_dedup_against_existing_records(tmp_path: Path) -> None:
    """搜索已采样出某 anchor arch（如 slot-1 单换 vanilla）→ 该条不重复追加（arch 键去重跨 seed 生效）。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_anchor_fixture(ad)
    with (ad / "search_results.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"objs": {"acc": -0.6, "latency": 1.25}, "pareto": False,
                 "arch": {"branch_choices": ["original", "vanilla", "original"]}}
            )
            + "\n"
        )

    out = _run_anchor(ad)
    assert "appended=3" in out, out  # slot-1 swap 已在 seed 里 → 只补 3 条
    assert "skipped_existing=1" in out, out

    rows = _read_records(ad)
    archs = [tuple(r["arch"]["branch_choices"]) for r in rows]
    assert len(archs) == len(set(archs))  # 全文件无重复 arch
    assert ("original", "vanilla", "original") in archs


# ── emit_report.py：final_metrics 优先源（log 契约行 > 终态 status.md > 残留） ───


def _write_success_pipeline(ad: Path, *, retrain_log_lines: list[str], status_md: str) -> None:
    """全链 success 产物 + 可控的 retrain log / retrain_status.md。"""
    _write_expand_stage_base(ad, equivalence_passed=True)
    (ad / "train_supernet.py").write_text("# kd trainer\n", encoding="utf-8")
    (ad / "run_train_supernet.sh").write_text("# launcher\n", encoding="utf-8")
    (ad / "train_status.md").write_text("train done\n", encoding="utf-8")
    train_dir = ad / "runs" / "train"
    train_dir.mkdir(parents=True)
    (train_dir / ".train_rc").write_text("0", encoding="utf-8")
    (train_dir / "supernet_best.pth").write_text("ckpt-bytes", encoding="utf-8")
    (ad / "search_results.jsonl").write_text(
        '{"objs": {"acc": -0.9, "latency": 1.2}, "pareto": true, '
        '"arch": {"choices": ["original", "vanilla"]}}\n',
        encoding="utf-8",
    )
    (ad / ".selected_arch.json").write_text(
        json.dumps(
            {
                "selected_arch": {"choices": ["original", "vanilla"]},
                "selected_acc": 0.9,
                "selected_latency": 1.2,
                "latency_unit": "ms",
                "pareto_size": 2,
                "select_reason": "max-acc-under-target",
            }
        ),
        encoding="utf-8",
    )
    retrain_dir = ad / "runs" / "retrain"
    retrain_dir.mkdir(parents=True)
    (retrain_dir / ".retrain_rc").write_text("0", encoding="utf-8")
    (retrain_dir / "retrain_best.pth").write_text("ckpt-bytes", encoding="utf-8")
    if retrain_log_lines:
        (retrain_dir / "retrain.attempt1.log").write_text(
            "\n".join(retrain_log_lines) + "\n", encoding="utf-8"
        )
    (ad / "retrain_status.md").write_text(status_md, encoding="utf-8")


def test_emit_report_final_metrics_prefers_log_contract_lines(tmp_path: Path) -> None:
    """log 契约行优先：stale running status.md 在场时 final_metrics 仍取 log 的真实数字。"""
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_success_pipeline(
        ad,
        retrain_log_lines=[
            "[retrain] epochs=1 batch_size=128",
            "epoch 1/1 kd_loss 0.1167",
            "[eval] unit 1 val_accuracy 0.9297",
            "[retrain] done best val_accuracy 0.9297 updates 469",
        ],
        status_md="- status: running\n- epoch: 1/1\n",  # 残留 running（真实 E2E 形态）
    )

    report = _run_emit_report(ad)
    assert report["status"] == "success"
    assert "0.9297" in report["final_metrics"]
    assert "running" not in report["final_metrics"].lower()
    assert "retrain.attempt1.log" in report["final_metrics"]  # 溯源可查


def test_emit_report_final_metrics_eval_line_and_status_md_fallback(tmp_path: Path) -> None:
    """无 done-best 行时退到最后 [eval] 行；log 全无契约行时退到终态 status.md。"""
    # (a) 只有 [eval] 行 → 取 eval 行数值。
    ad = tmp_path / "artifacts"
    ad.mkdir()
    _write_success_pipeline(
        ad,
        retrain_log_lines=["epoch 1/1 kd_loss 0.1", "[eval] unit 3 val_accuracy 0.9100"],
        status_md="- status: running\n",
    )
    report = _run_emit_report(ad)
    assert "0.9100" in report["final_metrics"]
    assert "val_accuracy" in report["final_metrics"]

    # (b) log 无契约行 + status.md 已终态（status.sh 刷新后形态，含 best 行）→ 取 status.md。
    ad2 = tmp_path / "artifacts2"
    ad2.mkdir()
    _write_success_pipeline(
        ad2,
        retrain_log_lines=["epoch 1/1 kd_loss 0.1"],
        status_md="- status: completed\n- best: val_accuracy 0.9200\n",
    )
    report2 = _run_emit_report(ad2)
    assert "status: completed" in report2["final_metrics"]
    assert "0.9200" in report2["final_metrics"]


# ── check_retrain_script.sh：写粒度 + 终态指标行 gate ─────────────────────────

# Minimal compliant retrain.py：PSU 静态 gate 全过（is_distributed 守卫 + progress.jsonl
# 步级写 + teacher/KD 三要素 + [eval]/done best 契约行）。
_GOOD_RETRAIN_PY = (
    "import argparse\n"
    "import json\n"
    "\n"
    "import torch\n"
    "\n"
    "\n"
    "def parse_args():\n"
    "    parser = argparse.ArgumentParser()\n"
    "    parser.add_argument('--output_dir', default='runs/retrain')\n"
    "    parser.add_argument('--eval_interval', type=int, default=1)\n"
    "    parser.add_argument('--device', default='auto')\n"
    "    parser.add_argument('--amp', action='store_true')\n"
    "    parser.add_argument('--lr', type=float, default=1e-3)\n"
    "    parser.add_argument('--max_grad_norm', type=float, default=1.0)\n"
    "    parser.add_argument('--progress-every', type=int, default=50)\n"
    "    parser.add_argument('--seed', type=int, default=0)\n"
    "    parser.add_argument('--supernet_ckpt', required=True)\n"
    "    parser.add_argument('--teacher_ckpt', required=True)\n"
    "    return parser.parse_args()\n"
    "\n"
    "\n"
    "def is_distributed():\n"
    "    return False\n"
    "\n"
    "\n"
    "def build_teacher(device):\n"
    "    from load_pretrained import build_pretrained_model\n"
    "\n"
    "    teacher = build_pretrained_model(device=device)\n"
    "    teacher.eval()\n"
    "    for p in teacher.parameters():\n"
    "        p.requires_grad_(False)\n"
    "    return teacher\n"
    "\n"
    "\n"
    "def main():\n"
    "    args = parse_args()\n"
    "    model = torch.nn.Linear(8, 8)\n"
    "    for p in model.parameters():\n"
    "        p.requires_grad_(False)\n"
    "    trainable_params = [p for p in model.parameters() if p.requires_grad]\n"
    "    optimizer = torch.optim.AdamW(trainable_params, lr=args.lr)\n"
    "    teacher = build_teacher(torch.device('cpu'))\n"
    "    with torch.no_grad():\n"
    "        teacher_outputs = teacher(torch.zeros(1, 8))\n"
    "    outputs = model(torch.zeros(1, 8))\n"
    "    kd_loss = (outputs - teacher_outputs).abs().mean()\n"
    "    kd_loss.backward()\n"
    "    global_step = 0\n"
    "    if global_step % args.progress_every == 0:\n"
    "        with open('runs/retrain/progress.jsonl', 'a') as f:\n"
    "            f.write(json.dumps({'step': global_step, 'metrics': {'kd_loss': float(kd_loss)}}) + '\\n')\n"
    "    print('[eval] unit 1 val_accuracy 0.9297')\n"
    "    print('done best val_accuracy 0.9297 updates 1')\n"
    "    from nas_agent.train import save_checkpoint_ddp\n"
    "\n"
    "    save_checkpoint_ddp('runs/retrain/retrain_best.pth', model, optimizer=optimizer)\n"
    "\n"
    "\n"
    "if __name__ == '__main__':\n"
    "    main()\n"
)

_FINETUNE_PY = (
    '"""finetune.py fixture -- weight-inheritance seam stub (py_compile target)."""\n'
    "\n"
    "\n"
    "def build_selected_subnet(device, supernet_ckpt):\n"
    "    return None\n"
)

_GOOD_RETRAIN_LAUNCHER = (
    "#!/usr/bin/env bash\n"
    "DATA_DIR=/data\n"
    "SUPERNET_CKPT=runs/train/supernet_best.pth\n"
    "TEACHER_CKPT=/ckpt/model.pth\n"
    "NUM_WORKERS=0\n"
    "AMP=false   # single-device default\n"
    'python3 retrain.py --supernet_ckpt "$SUPERNET_CKPT" --teacher_ckpt "$TEACHER_CKPT" \\\n'
    "  --progress-every 50\n"
)


def _run_retrain_check(ad: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(ad)
    env["ORCA_AGENT_RESOURCES"] = str(_RETRAIN_SCRIPT_AGENT)
    return subprocess.run(
        ["bash", str(_CHECK_RETRAIN_SCRIPT)], capture_output=True, text=True, env=env
    )


def _write_retrain_fixture(ad: Path, retrain_py: str) -> None:
    (ad / "retrain.py").write_text(retrain_py, encoding="utf-8")
    (ad / "finetune.py").write_text(_FINETUNE_PY, encoding="utf-8")
    (ad / "run_retrain.sh").write_text(_GOOD_RETRAIN_LAUNCHER, encoding="utf-8")


class TestCheckRetrainScriptGates:
    def test_bash_n(self):
        r = subprocess.run(
            ["bash", "-n", str(_CHECK_RETRAIN_SCRIPT)], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr

    def test_good_fixture_passes(self, tmp_path: Path) -> None:
        _write_retrain_fixture(tmp_path, _GOOD_RETRAIN_PY)
        r = _run_retrain_check(tmp_path)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PASS: check_retrain_script" in r.stdout

    def test_per_unit_only_progress_feed_rejected(self, tmp_path: Path) -> None:
        """写粒度 gate：progress.jsonl 只按 progress unit 写（无 progress-every / 无取模）→ FAIL。"""
        bad = _GOOD_RETRAIN_PY.replace(
            "    parser.add_argument('--progress-every', type=int, default=50)\n", ""
        ).replace(
            "    if global_step % args.progress_every == 0:\n"
            "        with open('runs/retrain/progress.jsonl', 'a') as f:\n"
            "            f.write(json.dumps({'step': global_step, 'metrics': {'kd_loss': float(kd_loss)}}) + '\\n')\n",
            "    with open('runs/retrain/progress.jsonl', 'a') as f:\n"
            "            f.write(json.dumps({'step': 1, 'metrics': {'kd_loss': float(kd_loss)}}) + '\\n')\n",
        )
        assert "progress_every" not in bad  # fixture sanity
        _write_retrain_fixture(tmp_path, bad)
        r = _run_retrain_check(tmp_path)
        assert r.returncode != 0
        assert "progress.jsonl 写粒度" in r.stdout

    def test_missing_terminal_metric_lines_rejected(self, tmp_path: Path) -> None:
        """终态指标行 gate：缺 [eval] unit / done best 契约行（final_metrics 数据源）→ FAIL。"""
        bad = _GOOD_RETRAIN_PY.replace(
            "    print('[eval] unit 1 val_accuracy 0.9297')\n"
            "    print('done best val_accuracy 0.9297 updates 1')\n",
            "",
        )
        assert "[eval] unit" not in bad and "done best" not in bad  # fixture sanity
        _write_retrain_fixture(tmp_path, bad)
        r = _run_retrain_check(tmp_path)
        assert r.returncode != 0
        assert "[eval] unit" in r.stdout
        assert "done best" in r.stdout


# ── progress_watcher.py：静态兜底铁律（落盘 floor + 单点 + 已完成补跑幂等） ─────


def _run_watcher(script: Path, ad: Path, progress: str, marker: str, label: str, title: str):
    env = dict(os.environ)
    env["ORCA_ARTIFACTS_DIR"] = str(ad)
    for k in ("ORCA_RUN_ID", "ORCA_NODE", "ORCA_SESSION_ID", "ORCA_CHART_SOCK"):
        env.pop(k, None)  # 强制 static 路径（无 live env）
    return subprocess.run(
        [
            sys.executable, str(script),
            "--progress", progress,
            "--done-marker", marker,
            "--label", label,
            "--title", title,
        ],
        capture_output=True, text=True, env=env, cwd=str(ad), timeout=60,
    )


def test_progress_watcher_static_floor_multi_point(tmp_path: Path) -> None:
    """train 5 点：static 兜底每指标落一张非空 HTML（SVG floor 亦含 polyline/点）；无 single-point 注记。"""
    ad = tmp_path / "artifacts"
    (ad / "runs" / "train").mkdir(parents=True)
    rows = [
        {"step": i, "metrics": {"kd_loss": 1.0 / i, "val_accuracy_kpath_mean": 0.3 + 0.1 * i}}
        for i in range(1, 6)
    ]
    with (ad / "runs" / "train" / "progress.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    (ad / "runs" / "train" / ".train_rc").write_text("0", encoding="utf-8")  # attempt 已结束

    r = _run_watcher(
        _TRAIN_WATCHER, ad, "runs/train/progress.jsonl", "runs/train/.train_rc",
        "puzzle-supernet/train", "Training Metrics (attempt 1)",
    )
    assert r.returncode == 0, r.stderr
    charts = sorted((ad / "charts").glob("puzzle-supernet_train_*.html"))
    assert len(charts) == 2, charts  # 每指标一张：kd_loss + val_accuracy_kpath_mean
    for chart in charts:
        content = chart.read_text(encoding="utf-8")
        assert len(content) > 200  # 非空（自包含 HTML）
        assert "single point" not in content
    kd_chart = (ad / "charts" / "puzzle-supernet_train_kd_loss.html").read_text(encoding="utf-8")
    assert kd_chart.count("circle") >= 5  # 5 个数据点


def test_progress_watcher_static_floor_single_point(tmp_path: Path) -> None:
    """retrain 1 点：单点也落盘且注明 single point；done-marker 已存在 → 立即退出（幂等补跑）。"""
    ad = tmp_path / "artifacts"
    (ad / "runs" / "retrain").mkdir(parents=True)
    with (ad / "runs" / "retrain" / "progress.jsonl").open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"step": 1, "metrics": {"val_accuracy": 0.9297}}) + "\n")
    (ad / "runs" / "retrain" / ".retrain_rc").write_text("0", encoding="utf-8")

    r = _run_watcher(
        _RETRAIN_WATCHER, ad, "runs/retrain/progress.jsonl", "runs/retrain/.retrain_rc",
        "puzzle-supernet/retrain", "Retrain Metrics (attempt 1)",
    )
    assert r.returncode == 0, r.stderr
    chart = ad / "charts" / "puzzle-supernet_retrain_val_accuracy.html"
    content = chart.read_text(encoding="utf-8")
    assert len(content) > 200
    assert "single point" in content
    assert "0.9297" in content  # 数值可见（min/max 轴标或数据）

    # 幂等：再跑一次同样成功（已完成 attempt 的补跑语义）。
    r2 = _run_watcher(
        _RETRAIN_WATCHER, ad, "runs/retrain/progress.jsonl", "runs/retrain/.retrain_rc",
        "puzzle-supernet/retrain", "Retrain Metrics (attempt 1)",
    )
    assert r2.returncode == 0, r2.stderr
