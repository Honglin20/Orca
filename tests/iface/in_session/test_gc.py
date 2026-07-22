"""tests/iface/in_session/test_gc.py —— ``orca gc`` 命令测试（P8 / plan 2026-07-21 §Phase 4-C）。

覆盖意图（Rule 9 —— verify intent, not just behavior）：
  - **safety first**：正在跑的 run（marker 存在 / flock 持有）**永不删**，即便 mtime 过 cutoff
  - **fail loud**：``--max-age`` 和 ``--keep`` 都不给 → BadParameter（防误操作全删）
  - **dry-run**：列路径不真删
  - **scope**：只删 ``runs/`` 下 + chart socket temp 根下的 ``orca-*.sock``；逃逸路径拒绝
  - **orphan 捕获**：abort/crash 路径留下的孤儿目录 / marker / lock 都被收集
  - **mtime 过滤**：``--max-age 14d`` 按 tape mtime 判老
  - **keep N**：保留最新 N 个 inactive run，其余进入 age 过滤

辅助覆盖 env 注入（P8 / Phase 4-A）：
  - bootstrap 后 ``runs/<run_id>/artifacts/`` 目录存在
  - ``runs/<run_id>/orca_env.sh`` 含 ``export ORCA_ARTIFACTS_DIR=<abs>``
  - ``--dry-run`` 列出的路径与删后剩余一致
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.chart._paths import chart_sock_path
from orca.iface.in_session.cli import (
    _collect_gc_candidates,
    _delete_candidate,
    _is_run_active,
    app,
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir 到 tmp_path，让 ``./runs`` 写到临时目录（隔离真实 repo 的 runs/）。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_run(
    rundir: Path, run_id: str, *, age_seconds: float = 0,
    active: bool = False, with_dir: bool = True,
) -> Path:
    """构造一个 run 的全套文件：tape + per-run 目录（含 artifacts/）+ 可选 marker。

    ``age_seconds`` > 0 → 把 tape mtime 倒拨（模拟老 run）。
    ``active=True`` → 写 marker（``runs/orca-<run_id>.json``）模拟「正在跑」。
    """
    rundir.mkdir(parents=True, exist_ok=True)
    tape = rundir / f"{run_id}.jsonl"
    tape.write_text('{"seq":0,"type":"workflow_started"}\n', encoding="utf-8")
    if age_seconds > 0:
        ts = time.time() - age_seconds
        os.utime(tape, (ts, ts))
    if with_dir:
        run_root = rundir / run_id
        (run_root / "artifacts").mkdir(parents=True, exist_ok=True)
        (run_root / "orca_env.sh").write_text("# env\n", encoding="utf-8")
    if active:
        from orca.iface.in_session.marker import ActivationMarker, write_marker
        from orca.iface.in_session.marker import marker_path
        write_marker(marker_path(rundir, run_id), ActivationMarker(run_id=run_id))
    return tape


def _gc(
    runner: CliRunner, *args: str, expect_exit: int = 0,
) -> dict:
    """跑 ``orca gc``，返解析后的 JSON。"""
    r = runner.invoke(app, ["gc", *args])
    assert r.exit_code == expect_exit, (
        f"gc exit {r.exit_code} (expected {expect_exit}):\n{r.output}"
    )
    return json.loads(_first_json_line(r.output))


def _first_json_line(s: str) -> str:
    """取输出里第一个像 JSON 的行（跳过 logging 噪音）。"""
    for line in s.splitlines():
        line = line.strip()
        if line.startswith("{"):
            return line
    raise AssertionError(f"未找到 JSON 行：{s!r}")


# ── env 注入：bootstrap 创建 artifacts/ + env 文件含 ORCA_ARTIFACTS_DIR ─────


def test_bootstrap_creates_artifacts_dir_and_env_var(cwd_tmp):
    """bootstrap 后 ``runs/<run_id>/artifacts/`` 存在 + ``orca_env.sh`` 含 ORCA_ARTIFACTS_DIR。"""
    wf = cwd_tmp / "wf.yaml"
    wf.write_text(textwrap.dedent("""
        name: env_inject_check
        description: env inject
        entry: a
        nodes:
          - name: a
            kind: agent
            executor: opencode
            model: deepseek/deepseek-v4-flash
            prompt: "做 A。"
            routes:
              - to: $end
    """), encoding="utf-8")

    runner = CliRunner()
    r = runner.invoke(app, ["bootstrap", str(wf), "--inputs", "{}"])
    assert r.exit_code == 0, f"bootstrap exit {r.exit_code}: {r.output}"
    reply = json.loads(_first_json_line(r.output))
    run_id = reply["run_id"]

    # 目录存在
    artifacts_dir = cwd_tmp / "runs" / run_id / "artifacts"
    assert artifacts_dir.is_dir(), f"artifacts 目录未创建：{artifacts_dir}"

    # env 文件含 ORCA_ARTIFACTS_DIR（绝对路径）
    env_path = cwd_tmp / "runs" / run_id / "orca_env.sh"
    env_content = env_path.read_text(encoding="utf-8")
    assert "export ORCA_ARTIFACTS_DIR=" in env_content
    # 取出路径值，验证指向刚创建的目录
    artifacts_line = next(
        line for line in env_content.splitlines()
        if line.startswith("export ORCA_ARTIFACTS_DIR=")
    )
    artifacts_value = artifacts_line.split("=", 1)[1].strip().strip("'\"")
    assert Path(artifacts_value).resolve() == artifacts_dir.resolve()
    assert Path(artifacts_value).is_dir()  # 真存在

    # 收尾：让 daemon 自退
    runner.invoke(app, ["next", "--run-id", run_id, "--output", "done"])
    _wait_sock_gone(chart_sock_path(run_id), timeout=8.0)


def _wait_sock_gone(sock_path: Path, *, timeout: float = 8.0) -> None:
    """等 socket 文件消失（daemon 终态自退后 unlink）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not sock_path.exists():
            return
        time.sleep(0.1)


# ── _is_run_active：marker + flock 判定 ─────────────────────────────────────


def test_is_run_active_marker_exists(cwd_tmp):
    """marker 存在 → active=True（bootstrap 写、终态/stop 清）。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-active", active=True)
    active, reason = _is_run_active("r-active", rundir, tape)
    assert active is True
    assert "marker" in reason


def test_is_run_active_no_marker_inactive(cwd_tmp):
    """无 marker → inactive（run 已终态 / 或从未来 bootstrap）。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-done")
    active, reason = _is_run_active("r-done", rundir, tape)
    assert active is False


# ── _collect_gc_candidates：keep / age / orphan ────────────────────────────


def test_collect_skips_active_runs(cwd_tmp):
    """active run（marker 存在）永不进候选（即便 age 过 cutoff）。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r-active", age_seconds=999999, active=True)
    _make_run(rundir, "r-done", age_seconds=999999)
    cands = _collect_gc_candidates(rundir, max_age_seconds=1.0, keep=None)
    run_ids = [c["run_id"] for c in cands]
    assert "r-active" not in run_ids
    assert "r-done" in run_ids


def test_collect_respects_max_age(cwd_tmp):
    """mtime 比 cutoff 新 → 跳过；老 → 命中。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r-fresh", age_seconds=10)  # 10s 老
    _make_run(rundir, "r-very-old", age_seconds=999999)
    cands = _collect_gc_candidates(rundir, max_age_seconds=3600, keep=None)
    run_ids = [c["run_id"] for c in cands if c["kind"] == "stale-run"]
    assert "r-fresh" not in run_ids  # 10s < 1h cutoff
    assert "r-very-old" in run_ids  # 老 → 命中


def test_collect_keep_newest_n(cwd_tmp):
    """``keep=2`` 保留最新 2 个 inactive run；其余进入 age 过滤。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r1", age_seconds=100)
    _make_run(rundir, "r2", age_seconds=200)
    _make_run(rundir, "r3", age_seconds=300)
    _make_run(rundir, "r4", age_seconds=400)
    cands = _collect_gc_candidates(rundir, max_age_seconds=0, keep=2)
    run_ids = {c["run_id"] for c in cands if c["kind"] == "stale-run"}
    # 留 r1/r2（最新），删 r3/r4
    assert "r1" not in run_ids
    assert "r2" not in run_ids
    assert "r3" in run_ids
    assert "r4" in run_ids


def test_collect_orphan_dir_without_tape(cwd_tmp):
    """``runs/<id>/`` 目录存在但无对应 ``.jsonl`` → orphan-dir 候选（abort/crash 残留）。"""
    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    (rundir / "orphan-1").mkdir()
    (rundir / "orphan-1" / "artifacts").mkdir()
    (rundir / "orphan-1" / "prompts").mkdir()
    cands = _collect_gc_candidates(rundir, max_age_seconds=999999, keep=None)
    orphan_dirs = [c for c in cands if c["kind"] == "orphan-dir"]
    assert any(c["run_id"] == "orphan-1" for c in orphan_dirs)


def test_collect_orphan_marker_without_tape(cwd_tmp):
    """``runs/orca-<id>.json`` marker 无对应 ``.jsonl`` → orphan-marker。"""
    from orca.iface.in_session.marker import ActivationMarker, marker_path, write_marker

    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    write_marker(marker_path(rundir, "ghost-1"), ActivationMarker(run_id="ghost-1"))
    cands = _collect_gc_candidates(rundir, max_age_seconds=999999, keep=None)
    markers = [c for c in cands if c["kind"] == "orphan-marker"]
    assert any(c["run_id"] == "ghost-1" for c in markers)


def test_collect_orphan_lock_without_tape(cwd_tmp):
    """``runs/<id>.jsonl.lock`` 无对应 ``.jsonl`` → orphan-lock。"""
    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    (rundir / "ghost-2.jsonl.lock").write_text("", encoding="utf-8")
    cands = _collect_gc_candidates(rundir, max_age_seconds=999999, keep=None)
    locks = [c for c in cands if c["kind"] == "orphan-lock"]
    assert any(c["run_id"] == "ghost-2" for c in locks)


# ── _delete_candidate：安全 + 幂等 ────────────────────────────────────────


def test_delete_removes_run_tree_and_tape(cwd_tmp):
    """删 stale-run 候选：per-run 目录 + tape + lock + marker 全清。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-del", age_seconds=999999)
    # 也造 marker（terminal run 残留 marker，非 active）
    from orca.iface.in_session.marker import ActivationMarker, marker_path, write_marker
    mpath = marker_path(rundir, "r-del")
    # marker 无对应 tape 时会被 _is_run_active 当 active（marker exists）→ 测试删除路径
    # 需要 candidate 已收集（orphan-marker）而非 stale-run。这里直接构造 candidate dict。
    cands = _collect_gc_candidates(rundir, max_age_seconds=1, keep=None)
    # r-del 没 marker（_make_run active=False）→ 应是 stale-run
    cand = next(c for c in cands if c.get("run_id") == "r-del")
    result = _delete_candidate(cand, rundir=rundir)
    assert result["ok"] is True
    assert not tape.exists()
    assert not (rundir / "r-del").exists()


def test_delete_rejects_path_escape(cwd_tmp):
    """路径逃逸（在 rundir 外、非 chart socket）→ 拒绝删除并记 errors（fail loud）。"""
    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    # 构造恶意 candidate：path 在 rundir 之外
    evil = cwd_tmp / "secret.txt"
    evil.write_text("do not delete me", encoding="utf-8")
    cand = {
        "run_id": "evil",
        "kind": "stale-run",
        "paths": [evil],
        "reason": "test",
    }
    result = _delete_candidate(cand, rundir=rundir)
    assert result["ok"] is False
    assert len(result["errors"]) == 1
    assert "escapes" in result["errors"][0]["error"]
    assert evil.exists()  # 未被删


def test_delete_idempotent_missing_paths(cwd_tmp):
    """候选中已不存在的路径算成功删除（幂等，不报错）。"""
    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    cand = {
        "run_id": "ghost",
        "kind": "stale-run",
        "paths": [rundir / "ghost.jsonl"],  # 不存在
        "reason": "test",
    }
    result = _delete_candidate(cand, rundir=rundir)
    assert result["ok"] is True
    assert str(rundir / "ghost.jsonl") in result["deleted"]


def test_delete_chart_socket_in_temp_ok(cwd_tmp, tmp_path):
    """chart socket 在 temp 根下（``/tmp/orca-*.sock``）→ 白名单允许删除。"""
    import tempfile
    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    # 在 tempfile.gettempdir() 下造一个假 socket 文件
    fake_sock = Path(tempfile.gettempdir()) / "orca-fake-test-gc.sock"
    fake_sock.write_text("", encoding="utf-8")
    try:
        cand = {
            "run_id": "fake-test-gc",
            "kind": "stale-run",
            "paths": [fake_sock],
            "reason": "test",
        }
        result = _delete_candidate(cand, rundir=rundir)
        assert result["ok"] is True, result
        assert not fake_sock.exists()
    finally:
        if fake_sock.exists():
            fake_sock.unlink(missing_ok=True)


# ── CLI 端到端：dry-run / real-delete / arg validation ─────────────────────


def test_cli_gc_requires_max_age_or_keep(cwd_tmp):
    """无 ``--max-age`` 和 ``--keep`` → BadParameter（防「全删」误操作）。"""
    runner = CliRunner()
    r = runner.invoke(app, ["gc"])
    assert r.exit_code != 0
    assert "max-age" in r.output or "keep" in r.output


def test_cli_gc_invalid_max_age_unit(cwd_tmp):
    """``--max-age 14x``（未知单位）→ BadParameter（fail loud）。"""
    runner = CliRunner()
    r = runner.invoke(app, ["gc", "--max-age", "14x"])
    assert r.exit_code != 0


def test_cli_gc_dry_run_lists_but_does_not_delete(cwd_tmp):
    """``--dry-run``：列出候选但不真删（文件保留）。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-old", age_seconds=999999)
    runner = CliRunner()
    result = _gc(runner, "--dry-run", "--max-age", "1d")
    assert result["dry_run"] is True
    assert result["deleted_count"] == 0
    # 文件仍存在
    assert tape.exists()
    assert (rundir / "r-old").exists()
    # 候选里有这个 run
    run_ids = [c.get("run_id") for c in result["candidates"]]
    assert "r-old" in run_ids


def test_cli_gc_actually_deletes(cwd_tmp):
    """``--max-age`` 真删：tape + per-run 目录消失。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-del-me", age_seconds=999999)
    runner = CliRunner()
    result = _gc(runner, "--max-age", "1d")
    assert result["dry_run"] is False
    assert result["deleted_count"] > 0
    assert not tape.exists()
    assert not (rundir / "r-del-me").exists()


def test_cli_gc_skips_active_run(cwd_tmp):
    """正在跑的 run（marker 存在）不被列、不被删。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-running", age_seconds=999999, active=True)
    runner = CliRunner()
    result = _gc(runner, "--max-age", "1d")
    run_ids = [c.get("run_id") for c in result["candidates"]]
    assert "r-running" not in run_ids
    assert tape.exists()  # 未删


def test_cli_gc_keep_n_keeps_newest(cwd_tmp):
    """``--keep 1``（不带 --max-age）保留最新 inactive run，其余全删。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r1", age_seconds=100)
    _make_run(rundir, "r2", age_seconds=999999)
    runner = CliRunner()
    result = _gc(runner, "--keep", "1")
    run_ids = {c.get("run_id") for c in result["candidates"]}
    # r1 是最新（age 100s）→ 保留；r2 → 删
    assert "r1" not in run_ids
    assert "r2" in run_ids


def test_cli_gc_no_runs_returns_empty(cwd_tmp):
    """``runs/`` 目录不存在 → 返空候选（非错误，新项目首次跑 gc）。"""
    runner = CliRunner()
    result = _gc(runner, "--max-age", "1d", "--runs-dir", str(cwd_tmp / "no-runs-yet"))
    assert result["candidates"] == []
    assert result["deleted_count"] == 0
    assert "note" in result  # 提示 runs dir not found


def test_cli_gc_combines_max_age_and_keep(cwd_tmp):
    """``--keep 1 --max-age 1d``：先留最新 1，再按 age 过滤余下。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "new", age_seconds=10)     # 最新，被 keep
    _make_run(rundir, "mid", age_seconds=999999)  # 老，删
    _make_run(rundir, "old", age_seconds=999999)  # 老，删
    runner = CliRunner()
    result = _gc(runner, "--keep", "1", "--max-age", "1d")
    run_ids = {c.get("run_id") for c in result["candidates"]}
    assert "new" not in run_ids  # 被 keep
    assert "mid" in run_ids
    assert "old" in run_ids


def test_cli_gc_max_age_zero_rejected(cwd_tmp):
    """``--max-age 0`` → 拒绝（避免误操作删全部；用 --keep 显式控制）。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r1", age_seconds=999999)
    runner = CliRunner()
    r = runner.invoke(app, ["gc", "--max-age", "0"])
    assert r.exit_code != 0
    assert "max-age" in r.output.lower() or "positive" in r.output.lower()


# ── worktree MANIFEST 列示（best-effort，P9 闭环前不删 worktree）────────────


def test_cli_gc_lists_worktree_manifest(cwd_tmp):
    """``runs/<id>/artifacts/.worktrees/MANIFEST.json`` 存在 → gc 列出但不真删 worktree。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r-with-wt", age_seconds=999999)
    manifest = rundir / "r-with-wt" / "artifacts" / ".worktrees" / "MANIFEST.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps([
        {"path": "/abs/repo/.worktrees/cand-1", "candidate": "cand-1"},
    ]), encoding="utf-8")

    runner = CliRunner()
    result = _gc(runner, "--dry-run", "--max-age", "1d")
    notes = result.get("worktree_notes", [])
    assert any(n.get("run_id") == "r-with-wt" for n in notes)
    note = next(n for n in notes if n.get("run_id") == "r-with-wt")
    assert note["worktrees"] == ["/abs/repo/.worktrees/cand-1"]
    assert "P9" in note["note"] or "NOT removed" in note["note"]


# ── code-reviewer 🔴#1 fix: MANIFEST-holding run 跳过 stale-run 候选 ─────────


def test_collect_skips_stale_run_with_manifest(cwd_tmp):
    """stale-run 持 MANIFEST → 完全跳过（不进 candidates）；保留 run_dir + MANIFEST 供 P9。

    意图：旧实现把 run_dir 整树删（含 MANIFEST），销毁 P9 闭环所需的 worktree 列表 → P9
    无法定位遗留 worktree。修复后 stale-run 持 MANIFEST 时跳过（保留），等 P9 闭环。
    """
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-with-wt", age_seconds=999999)
    manifest = rundir / "r-with-wt" / "artifacts" / ".worktrees" / "MANIFEST.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps([{"path": "/abs/repo/.worktrees/cand-1"}]), encoding="utf-8")

    cands = _collect_gc_candidates(rundir, max_age_seconds=1.0, keep=None)
    # 不出现在任何候选（stale-run / orphan-* 都不该收）
    run_ids = {c.get("run_id") for c in cands}
    assert "r-with-wt" not in run_ids


def test_gc_preserves_manifest_run_dir(cwd_tmp):
    """真删模式下，持 MANIFEST 的 run 不被删（run_dir + tape + MANIFEST 都保留）。"""
    rundir = cwd_tmp / "runs"
    tape = _make_run(rundir, "r-with-wt", age_seconds=999999)
    manifest = rundir / "r-with-wt" / "artifacts" / ".worktrees" / "MANIFEST.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps([{"path": "/x"}]), encoding="utf-8")

    runner = CliRunner()
    result = _gc(runner, "--max-age", "1d")  # 真删模式
    # run_dir + tape + MANIFEST 都保留
    assert tape.exists()
    assert (rundir / "r-with-wt").exists()
    assert manifest.exists()


# ── code-reviewer 🟡#1 fix: orphan-dir + 残留 marker 不被误判 active ──────


def test_collect_orphan_dir_with_leftover_marker(cwd_tmp):
    """crash 留下 ``<id>/`` + ``orca-<id>.json`` marker 但无 ``.jsonl`` → orphan-dir 被收集。

    意图：旧实现 ``_is_run_active`` 在 orphan 场景因 marker 残留误判 active → 跳过 orphan-dir
    → 永久孤儿。修复后 orphan-dir 仅查 flock（marker 无 tape 必 stale，不应 gate）。
    """
    from orca.iface.in_session.marker import ActivationMarker, marker_path, write_marker

    rundir = cwd_tmp / "runs"
    rundir.mkdir(parents=True)
    (rundir / "crashed-run").mkdir()
    (rundir / "crashed-run" / "artifacts").mkdir()
    # marker 残留（crash 前写、未来得及 clear）
    write_marker(marker_path(rundir, "crashed-run"), ActivationMarker(run_id="crashed-run"))

    cands = _collect_gc_candidates(rundir, max_age_seconds=999999, keep=None)
    orphan_dirs = [c for c in cands if c["kind"] == "orphan-dir"]
    assert any(c["run_id"] == "crashed-run" for c in orphan_dirs), (
        f"crashed-run 应被 orphan-dir 收集，但 candidates: {cands}"
    )


# ── code-reviewer 🟡#2 fix: gc ↔ bootstrap race 用 advisory lock serialize ──


def test_cli_gc_concurrent_lock_rejected(cwd_tmp):
    """两个 gc 并发：第二个返 ``another gc is running`` + 0 删除（advisory lock 兜底）。"""
    import fcntl

    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r1", age_seconds=999999)

    # 模拟另一个 gc 持锁
    lock_path = rundir / ".orca-gc.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    other_fd = open(lock_path, "w")
    fcntl.flock(other_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        runner = CliRunner()
        result = _gc(runner, "--max-age", "1d")  # 应撞锁返 note
        assert "another gc is running" in result.get("note", "")
        assert result["deleted_count"] == 0
    finally:
        fcntl.flock(other_fd.fileno(), fcntl.LOCK_UN)
        other_fd.close()


def test_cli_gc_releases_lock_after_completion(cwd_tmp):
    """gc 完成后释放 advisory lock（finally 块 + 后续 gc 能再拿锁）。"""
    rundir = cwd_tmp / "runs"
    _make_run(rundir, "r1", age_seconds=999999)

    runner = CliRunner()
    # 第一次 gc
    _gc(runner, "--dry-run", "--max-age", "1d")
    # 第二次 gc（同进程，第一次的锁应已释放）
    result = _gc(runner, "--dry-run", "--max-age", "1d")
    assert "another gc is running" not in result.get("note", "")
