"""Tests for check_progress_contract.py + warmup_poll.sh 收紧闸门 + 静态防线。

范式中 test_monitor_until_done.py：_REPO 路径常量 + class 组织 + parametrize 镜像 +
subprocess bash -n 静态门。Contract 校验用 subprocess 端到端跑（真实 returncode，最贴近
warmup_poll.sh 的调用方式）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_AGENTS = _REPO / "workflows" / "agents"
_DIRS = ["ns_run_train", "ns2_run_train", "ns_retrain", "ns2_retrain"]


def _check_script(d: str) -> Path:
    return _AGENTS / d / "scripts" / "check_progress_contract.py"


def _run(check_py: Path, progress: Path | str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(check_py), "--progress", str(progress)],
        capture_output=True,
        text=True,
    )


class TestContractValidation:
    """check_progress_contract.py 对各种 progress.jsonl 的契约判定。"""

    def test_valid_multi_metric(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text(
            '{"step": 1, "metrics": {"loss": 0.5, "acc": 0.9}}\n'
            '{"step": 2, "metrics": {"loss": 0.3}}\n',
            encoding="utf-8",
        )
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 0, r.stderr
        assert "OK" in r.stdout

    def test_missing_file(self, tmp_path):
        r = _run(_check_script("ns2_run_train"), tmp_path / "nope.jsonl")
        assert r.returncode == 1
        assert "不存在" in r.stderr

    def test_blank_only(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text("\n\n\n", encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "无任何非空行" in r.stderr

    def test_malformed_json_names_line(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"loss": 0.5}}\n{bad}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "第 2 行" in r.stderr

    def test_missing_step(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"metrics": {"loss": 0.5}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "step" in r.stderr

    def test_metrics_not_dict(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": [1, 2]}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "metrics" in r.stderr

    def test_empty_metrics_rejected(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "为空" in r.stderr

    def test_bool_metric_rejected(self, tmp_path):
        # bool 是 int 子类但非 metric —— 必须 reject（与 progress_watcher._is_number 同源）。
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"flag": true}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1
        assert "非数值" in r.stderr

    def test_string_metric_rejected(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"loss": "high"}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1

    def test_nan_inf_accepted_as_number(self, tmp_path):
        # 契约只要求 number；NaN/inf 的发散检测由 warmup_poll.sh 发散段单独管，不在此重复。
        p = tmp_path / "progress.jsonl"
        p.write_text(
            '{"step": 1, "metrics": {"loss": NaN}}\n'
            '{"step": 2, "metrics": {"loss": Infinity}}\n',
            encoding="utf-8",
        )
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 0, r.stderr

    def test_runs_identical_across_mirrors(self, tmp_path):
        # 4 份镜像脚本对同一输入行为一致（字节相同 → 行为必同，显式验证以锁定）。
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"loss": 0.5}}\n', encoding="utf-8")
        rcs = [_run(_check_script(d), p).returncode for d in _DIRS]
        assert rcs == [0, 0, 0, 0]

    def test_step_float_accepted(self, tmp_path):
        # 契约要求 number（非严格 int）——与 progress_watcher._is_number 对齐，避免
        # "check 拒绝 float step 但 watcher 接受" 分裂（code-reviewer SHOULD-FIX #2 选 a）。
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1.5, "metrics": {"loss": 0.5}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 0, r.stderr

    def test_metric_list_rejected(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"loss": [1, 2]}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1

    def test_metric_nested_dict_rejected(self, tmp_path):
        p = tmp_path / "progress.jsonl"
        p.write_text('{"step": 1, "metrics": {"loss": {"a": 1}}}\n', encoding="utf-8")
        r = _run(_check_script("ns2_run_train"), p)
        assert r.returncode == 1


class TestWarmupGate:
    """4 份 warmup_poll.sh 注入了契约校验调用 + 收紧 WARMUP_OK；bash -n 过。"""

    @pytest.mark.parametrize("d", _DIRS)
    def test_warmup_calls_contract_check(self, d):
        content = (_AGENTS / d / "scripts" / "warmup_poll.sh").read_text(encoding="utf-8")
        assert "check_progress_contract.py" in content
        assert "WARMUP_FAIL reason=progress-contract" in content

    @pytest.mark.parametrize("d", _DIRS)
    def test_warmup_uses_progress_var(self, d):
        # 路径前缀差异（runs/train vs runs/retrain）由 $PROGRESS 变量承载，调用处统一。
        content = (_AGENTS / d / "scripts" / "warmup_poll.sh").read_text(encoding="utf-8")
        assert 'check_progress_contract.py" --progress "$PROGRESS"' in content

    @pytest.mark.parametrize("d", _DIRS)
    def test_warmup_bash_n(self, d):
        warmup = _AGENTS / d / "scripts" / "warmup_poll.sh"
        r = subprocess.run(["bash", "-n", str(warmup)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    def test_warmup_check_inside_epoch_gate_block(self):
        # 控制流意图（code-reviewer NIT #2）：契约校验必须在 EPOCH_CNT>=2 块内，
        # 过早校验会误杀早期训练。锁行序：gate < check < WARMUP_OK。
        for d in _DIRS:
            lines = (_AGENTS / d / "scripts" / "warmup_poll.sh").read_text(encoding="utf-8").splitlines()
            i_gate = next(i for i, ln in enumerate(lines) if "EPOCH_CNT" in ln and "-ge 2" in ln)
            i_check = next(i for i, ln in enumerate(lines) if "check_progress_contract.py" in ln)
            i_ok = next(i for i, ln in enumerate(lines) if "WARMUP_OK epoch_cnt=" in ln)
            assert i_gate < i_check, f"{d}: check must be inside EPOCH_CNT>=2 block"
            assert i_check < i_ok, f"{d}: check must run before WARMUP_OK emitted"


class TestMirrorSync:
    """镜像铁律：4 份 check_progress_contract.py 字节相同。"""

    def test_check_scripts_byte_identical(self):
        contents = [_check_script(d).read_bytes() for d in _DIRS]
        first = contents[0]
        assert all(c == first for c in contents), "check_progress_contract.py 4 份必须字节相同"


class TestStaticChecks:
    """静态防线：v2 check_*.sh 含 progress.jsonl 段；v1/v2 checklist 含 item 38。"""

    def test_v2_check_train_script_has_progress_jsonl(self):
        content = (_AGENTS / "ns2_train_script" / "scripts" / "check_train_script.sh").read_text(encoding="utf-8")
        assert "progress.jsonl" in content
        assert "json.dumps" in content

    def test_v2_check_retrain_has_progress_jsonl(self):
        content = (_AGENTS / "ns2_retrain" / "scripts" / "check_retrain.sh").read_text(encoding="utf-8")
        assert "progress.jsonl" in content
        assert "json.dumps" in content

    def test_v2_check_retrain_bash_n(self):
        r = subprocess.run(
            ["bash", "-n", str(_AGENTS / "ns2_retrain" / "scripts" / "check_retrain.sh")],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr

    def test_v2_check_train_script_bash_n(self):
        r = subprocess.run(
            ["bash", "-n", str(_AGENTS / "ns2_train_script" / "scripts" / "check_train_script.sh")],
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0, r.stderr

    @pytest.mark.parametrize("wf", ["ns_train_script", "ns2_train_script"])
    def test_checklist_has_progress_jsonl_item(self, wf):
        content = (
            _AGENTS / wf / "references" / "workflow-checklists" / "train_supernet_script_generation.md"
        ).read_text(encoding="utf-8")
        assert "Progress JSONL Write Loop" in content
        assert "CRITICAL" in content
