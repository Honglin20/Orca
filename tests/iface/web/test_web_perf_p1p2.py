"""test_web_perf_p1p2.py —— web-perf P1/P2（plan ``docs/plans/2026-09-08-web-runs-list-perf.md``）。

覆盖意图（非仅行为）：
  - **P1 稳定排序**：scope=all 返回 ``(started_at, run_id)`` desc **全序**——这是 keyset
    游标不重不漏的前提（顺序不稳定则游标翻页必漏）。
  - **P1 默认分页**：不传 ``limit`` → 截到 ``DEFAULT_SCOPE_ALL_LIMIT``；显式 ``?limit=``
    覆盖；``limit<0`` 不限制。契约是「默认截断」而非「永远全量」。
  - **P1 keyset 游标**：``before_ts`` / ``before_id`` 逐页行走无重复无丢失（含同
    started_at tie-break 按 run_id desc 的边界）。offset 翻页在删除场景跳行，游标是
    替代方案——本组测试钉死其正确性。
  - **P2 粗判快速路径**：二次 discovery 目录指纹命中 → **不进慢路径**
    （``_iter_runs_dir_tapes`` raising stub 证伪）；**非 terminal 例外**：tape 追加
    （名字集合不变、粗判看不见）仍被验证路径感知——这是粗判正确性的核心；新增 tape →
    名单变化 → 指纹失效 → 慢路径可见。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orca.iface.web.run_manager import RunManager
from orca.runtime import register_project


# ── helpers ──────────────────────────────────────────────────────────────


def _event(seq: int, etype: str, timestamp: float, data: dict, node=None) -> dict:
    return {
        "seq": seq,
        "type": etype,
        "timestamp": timestamp,
        "node": node,
        "session_id": None,
        "data": data,
    }


def _write_tape(
    path: Path,
    *,
    run_id: str,
    wf: str = "wf",
    started: float = 100.0,
    ended: float | None = 110.0,
) -> None:
    """写合法 tape；``ended=None`` → 只有 workflow_started（run_status=running）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        _event(
            1, "workflow_started", started,
            {"inputs": {}, "node_count": 1, "entry": "n1",
             "workflow_name": wf, "run_id": run_id,
             "topology": {"nodes": [{"name": "n1"}]}},
        ),
    ]
    if ended is not None:
        events.append(
            _event(2, "workflow_completed", ended, {"elapsed": 1.0, "outputs": {}}),
        )
    lines = [json.dumps(e, ensure_ascii=False) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup_project(tmp_path, monkeypatch, tapes: dict[str, tuple[float, float | None]]):
    """注册隔离项目 + 写 tapes（name → (started, ended)）。返回 (proj, runs_dir)。"""
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / ".orca_home"))
    proj = tmp_path / "proj"
    (proj / "workflows").mkdir(parents=True)
    register_project(proj, require_marker=False)
    runs_dir = proj / "runs"
    for name, (started, ended) in tapes.items():
        _write_tape(runs_dir / f"{name}.jsonl", run_id=name, started=started, ended=ended)
    return proj, runs_dir


def _make_client(manager: RunManager):
    """真 ASGI TestClient 打真路由（非 mock）——P1 排序/分页/游标走完整 FastAPI 栈。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orca.iface.web.routes import build_runs_router

    app = FastAPI()
    app.include_router(build_runs_router(manager))
    return TestClient(app)


# ── P1：稳定排序 ─────────────────────────────────────────────────────────


def test_scope_all_sorted_by_started_desc(tmp_path, monkeypatch):
    """P1：返回按 (started_at, run_id) desc 稳定排序——keyset 游标的全序前提。"""
    _, _ = _setup_project(
        tmp_path, monkeypatch,
        tapes={
            "rid-a": (100.0, 101.0),
            "rid-b": (200.0, 201.0),
            "rid-c": (150.0, 151.0),
            "rid-d": (50.0, 51.0),
        },
    )
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    client = _make_client(manager)

    r = client.get("/api/runs", params={"scope": "all", "limit": -1})
    assert r.status_code == 200
    ids = [x["run_id"] for x in r.json()]
    assert ids == ["rid-b", "rid-c", "rid-a", "rid-d"]  # 200 > 150 > 100 > 50


def test_scope_all_same_started_tiebreak_by_run_id_desc(tmp_path, monkeypatch):
    """P1：同 started_at → run_id desc tie-break（元组全序，游标边界确定）。"""
    _, _ = _setup_project(
        tmp_path, monkeypatch,
        tapes={
            "rid-1": (100.0, 101.0),
            "rid-2": (100.0, 101.0),
            "rid-3": (100.0, 101.0),
        },
    )
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    client = _make_client(manager)

    r = client.get("/api/runs", params={"scope": "all", "limit": -1})
    ids = [x["run_id"] for x in r.json()]
    assert ids == ["rid-3", "rid-2", "rid-1"]


# ── P1：默认分页 ─────────────────────────────────────────────────────────


def test_scope_all_default_limit_truncates(tmp_path, monkeypatch):
    """P1：不传 limit → 截到 DEFAULT_SCOPE_ALL_LIMIT；显式 limit 覆盖；<0 不限制。"""
    from orca.iface.web.routes.runs import DEFAULT_SCOPE_ALL_LIMIT

    n = DEFAULT_SCOPE_ALL_LIMIT + 5
    tapes = {f"rid-{i:03d}": (float(i), float(i) + 1) for i in range(n)}
    _, _ = _setup_project(tmp_path, monkeypatch, tapes=tapes)
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    client = _make_client(manager)

    # 默认：截到 DEFAULT_SCOPE_ALL_LIMIT，且是**最新的** 200 个（排序后头部）
    r = client.get("/api/runs", params={"scope": "all"})
    body = r.json()
    assert len(body) == DEFAULT_SCOPE_ALL_LIMIT
    assert body[0]["run_id"] == f"rid-{n - 1:03d}"  # 最新在前

    # 显式覆盖
    r2 = client.get("/api/runs", params={"scope": "all", "limit": 3})
    assert len(r2.json()) == 3

    # <0 逃生门：不限制
    r3 = client.get("/api/runs", params={"scope": "all", "limit": -1})
    assert len(r3.json()) == n


# ── P1：keyset 游标翻页 ──────────────────────────────────────────────────


def test_scope_all_keyset_cursor_walks_all_without_dup_or_loss(tmp_path, monkeypatch):
    """P1：游标逐页行走覆盖全部 run，无重复无丢失（offset+删除会跳行，游标不会）。"""
    tapes = {f"rid-{i}": (float(i * 10), float(i * 10) + 1) for i in range(7)}
    _, _ = _setup_project(tmp_path, monkeypatch, tapes=tapes)
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    client = _make_client(manager)

    seen: list[str] = []
    before_ts = None
    before_id = None
    for _ in range(10):  # 上限兜底（防死循环），3 页应走完
        params: dict = {"scope": "all", "limit": 3}
        if before_ts is not None:
            params.update({"before_ts": before_ts, "before_id": before_id})
        page = client.get("/api/runs", params=params).json()
        if not page:
            break
        seen.extend(x["run_id"] for x in page)
        last = page[-1]
        before_ts = last["started_at"]
        before_id = last["run_id"]

    assert len(seen) == 7
    assert len(set(seen)) == 7  # 无重复
    assert set(seen) == {f"rid-{i}" for i in range(7)}  # 无丢失


def test_scope_all_cursor_straddles_tie_break_correctly(tmp_path, monkeypatch):
    """P1：游标落在同 started_at 组中间时按 (ts, run_id) 元组比较——不重不漏。"""
    _, _ = _setup_project(
        tmp_path, monkeypatch,
        tapes={f"rid-{i}": (100.0, 101.0) for i in range(4)},  # 全同 ts
    )
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    client = _make_client(manager)

    page1 = client.get("/api/runs", params={"scope": "all", "limit": 2}).json()
    assert [x["run_id"] for x in page1] == ["rid-3", "rid-2"]
    last = page1[-1]
    page2 = client.get(
        "/api/runs",
        params={
            "scope": "all", "limit": 2,
            "before_ts": last["started_at"], "before_id": last["run_id"],
        },
    ).json()
    assert [x["run_id"] for x in page2] == ["rid-1", "rid-0"]


# ── P2：目录指纹粗判快速路径 ─────────────────────────────────────────────


def test_discover_coarse_hit_skips_slow_path(tmp_path, monkeypatch):
    """P2：首次 discovery 落指纹；二次 discovery 指纹命中 → 不进慢路径、结果全等。"""
    _, runs_dir = _setup_project(
        tmp_path, monkeypatch,
        tapes={"run-1": (100.0, 110.0), "run-2": (200.0, 205.0)},
    )
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")

    summaries1 = manager.discover_runs()
    by1 = {s.run_id: s for s in summaries1}

    # 指纹已落盘（随 defer flush）
    disk = json.loads(
        (runs_dir / ".orca-meta-cache.json").read_text(encoding="utf-8")
    )
    assert isinstance(disk.get("dir_fingerprint"), str)

    # 二次 discovery：慢路径被 patch 成 raising stub——若粗判失效会 raise → 测试失败
    def _boom(self, runs_dir_):
        raise AssertionError("指纹命中不应进慢路径 _iter_runs_dir_tapes")

    monkeypatch.setattr(RunManager, "_iter_runs_dir_tapes", _boom)
    summaries2 = manager.discover_runs()
    by2 = {s.run_id: s for s in summaries2}
    assert set(by2) == {"run-1", "run-2"}
    for rid, s1 in by1.items():
        assert by2[rid].status == s1.status
        assert by2[rid].workflow_name == s1.workflow_name
        assert by2[rid].started_at == s1.started_at
        assert by2[rid].event_count == s1.event_count


def test_discover_non_terminal_tape_refreshed_under_coarse_hit(tmp_path, monkeypatch):
    """P2 核心：粗判看不见 tape 内追加（名字集合不变）→ **非 terminal 必须仍被验证
    路径感知**。running tape 追加 workflow_completed 后，二次 discovery（粗判命中）
    须反映 completed——若粗判把非 terminal 也直构缓存则此处失败。"""
    _, runs_dir = _setup_project(
        tmp_path, monkeypatch,
        tapes={"run-done": (100.0, 110.0), "run-live": (200.0, None)},  # live=running
    )
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    s1 = {s.run_id: s.status for s in manager.discover_runs()}
    assert s1["run-live"] == "running"

    # live tape 追加终态事件（重写文件：mtime/size 变，名字集合不变）
    _write_tape(runs_dir / "run-live.jsonl", run_id="run-live", started=200.0, ended=210.0)

    # 二次 discovery：粗判命中（慢路径 raising stub 证伪），非 terminal 走验证路径
    def _boom(self, runs_dir_):
        raise AssertionError("指纹命中不应进慢路径（非 terminal 走 _discover_one_tape）")

    monkeypatch.setattr(RunManager, "_iter_runs_dir_tapes", _boom)
    s2 = {s.run_id: s.status for s in manager.discover_runs()}
    assert s2["run-live"] == "completed"  # 追加被感知
    assert s2["run-done"] == "completed"  # terminal 直构不受影响


def test_discover_new_tape_invalidates_fingerprint(tmp_path, monkeypatch):
    """P2：新增 tape → 名字集合变化 → 指纹失效 → 慢路径可见新 run；三次 discovery
    指纹重建后恢复快速路径（慢路径 stub 不触发）。"""
    _, runs_dir = _setup_project(tmp_path, monkeypatch, tapes={"run-1": (100.0, 110.0)})
    manager = RunManager(runs_dir=tmp_path / "mgr_runs")
    manager.discover_runs()

    # 新增 tape → 指纹失效 → 慢路径可见
    _write_tape(runs_dir / "run-2.jsonl", run_id="run-2", started=200.0, ended=205.0)
    ids2 = {s.run_id for s in manager.discover_runs()}
    assert ids2 == {"run-1", "run-2"}

    # 三次 discovery：指纹已重建 → 快速路径（慢路径 stub 不触发）
    def _boom(self, runs_dir_):
        raise AssertionError("指纹重建后不应再进慢路径")

    monkeypatch.setattr(RunManager, "_iter_runs_dir_tapes", _boom)
    ids3 = {s.run_id for s in manager.discover_runs()}
    assert ids3 == {"run-1", "run-2"}
