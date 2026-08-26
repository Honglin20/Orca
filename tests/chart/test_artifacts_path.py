"""tests/chart/test_artifacts_path.py —— ``orca.chart._paths.artifacts_dir_for_run`` 单测（P8 / Phase 4-A）。

验证：单一真相源路径派生 ``<runs_dir>/<run_id>/artifacts/`` 的纯函数行为（无副作用）。

意图（Rule 9）：守 ``in_session/cli.py`` 的 bootstrap、``exec/script.py`` / ``exec/claude/executor.py``
的 spawn overlay、以及 ``gc`` 删除路径都共用同一约定（防止任一消费者重新发明路径字面）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orca.chart._paths import artifacts_dir_for_run


# ── 纯派生：形态正确 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "runs_dir, run_id, expected_suffix",
    [
        ("runs", "wf-20260722-abc123", "runs/wf-20260722-abc123/artifacts"),
        (Path("runs"), "myrun", "runs/myrun/artifacts"),
        (Path("/abs/path"), "r1", "/abs/path/r1/artifacts"),
        ("runs/", "x", "runs/x/artifacts"),  # trailing slash on input tolerated
    ],
)
def test_artifacts_dir_derivation(runs_dir, run_id, expected_suffix):
    """路径 = ``<runs_dir>/<run_id>/artifacts``（末尾无斜杠，与 Path 约定一致）。"""
    result = artifacts_dir_for_run(runs_dir, run_id)
    assert str(result).replace("//", "/").rstrip("/") == Path(expected_suffix).as_posix().rstrip("/")


def test_artifacts_dir_is_path_instance():
    """返 Path 实例（非 str），让调用方可直接 ``mkdir`` / ``resolve``。"""
    result = artifacts_dir_for_run("runs", "r1")
    assert isinstance(result, Path)


def test_artifacts_dir_no_mkdir_side_effect(tmp_path: Path):
    """纯函数：调用方负责 mkdir（不在本函数副作用）。"""
    runs_dir = tmp_path / "runs"
    result = artifacts_dir_for_run(runs_dir, "never-created")
    # 路径派生完成但目录不存在（无 mkdir 副作用）。
    assert not result.exists()
    assert not runs_dir.exists()


def test_artifacts_dir_deterministic():
    """同输入两次调用返相等路径（hash 稳定）。"""
    a = artifacts_dir_for_run("runs", "r-1")
    b = artifacts_dir_for_run("runs", "r-1")
    assert a == b


def test_artifacts_dir_distinct_per_run_id():
    """不同 run_id 派生不同路径（per-run 隔离）。"""
    a = artifacts_dir_for_run("runs", "r-1")
    b = artifacts_dir_for_run("runs", "r-2")
    assert a != b
    # 同 runs_dir（grandparent 一致），但不同 run_id 子目录（per-run 隔离）。
    assert a.parent.parent == b.parent.parent  # 同 runs_dir
    assert a.parent != b.parent  # 不同 run 子目录


def test_artifacts_dir_accepts_str_and_path():
    """``runs_dir`` 形态（str / Path）灵活；返回恒 Path。"""
    from_str = artifacts_dir_for_run("runs", "r")
    from_path = artifacts_dir_for_run(Path("runs"), "r")
    assert from_str == from_path
