"""tests/iface/web/test_audit_e.py —— SPEC E（单 tape 真相源裂缝）单测。

覆盖 AC1-AC13：
  - AC1  legacy completed → status=="completed"（非 cancelled）
  - AC2  legacy crashed（无终态事件）→ status == _summary_from_tape 派生值（非 cancelled）
  - AC3  legacy cancelled（tape 含 workflow_cancelled）→ status == "cancelled"
  - AC4  legacy tape 缺失 / 相对路径 → warn + skip（fail loud）
  - AC5  legacy tape corrupt → warn + skip（含 run_id + tape path）
  - AC6  legacy 与 attached 共用 _summary_from_tape（DRY）
  - AC7  handle.status hint 语义文档化（静态 grep）
  - AC8  in-memory 与 attached 在稳态下 status 一致
  - AC8b _meta_from_handle replay 失败降级（非回归守卫）
  - AC9  三分支 dedup（attached×attached / attached×legacy / in-memory×legacy）
  - AC10 legacy meta.run_id 与 tape_path.stem 不一致 → 以 tape_stem 为准 + warn
  - AC11 不引入第二真相源（静态语义判断）
  - AC12 既有 discover 测试保持 green（grep 守门，release note 列清单）
  - AC13 release note 关键词集
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import pytest

from orca.iface.web import run_manager as rm_mod
from orca.iface.web.run_manager import (
    RunManager,
    RunMeta,
    RunSummary,
)


# ── 共享 fixtures / helpers ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """每测独立 ORCA_HOME（隔离 legacy ~/.orca/runs）。"""
    home = tmp_path / "orca-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("ORCA_HOME", str(home))
    yield home


def _make_project(parent: Path, name: str = "proj") -> Path:
    p = parent / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def _evt(seq: int, etype: str, data: dict | None = None, *, node=None, ts: float | None = None) -> str:
    return json.dumps({
        "seq": seq, "type": etype, "node": node, "session_id": None,
        "timestamp": ts if ts is not None else time.time(),
        "data": data or {},
    })


def _write_tape(
    tape_path: Path,
    *,
    run_id: str | None = None,
    workflow_name: str = "demo",
    terminal: str = "workflow_completed",
    extra_events: list[str] | None = None,
    lines: list[str] | None = None,
) -> None:
    """写合法 tape（workflow_started + node_started + 终态事件）。

    ``lines`` 优先；否则按 ``terminal`` 拼装（terminal="" → 无终态，模拟 crashed）。
    """
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    if lines is not None:
        tape_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    evts = [
        _evt(1, "workflow_started", {
            "inputs": {}, "node_count": 1, "entry": "a",
            "workflow_name": workflow_name,
            "run_id": run_id or tape_path.stem,
            "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "script"}]},
        }),
        _evt(2, "node_started", {}, node="a"),
    ]
    if terminal:
        evts.append(_evt(3, terminal, {"elapsed": 0.5, "outputs": {}}))
    if extra_events:
        evts.extend(extra_events)
    tape_path.write_text("\n".join(evts) + "\n", encoding="utf-8")


def _write_legacy_meta(
    home: Path, *, run_id: str, tape_path: str, yaml_path: str = "/x.yaml",
    started_at: float = 1.0, meta_stem: str | None = None,
) -> Path:
    """写一个 legacy BgRunMeta JSON 到 ``$ORCA_HOME/runs/<meta_stem>.json``。

    ``meta_stem``：缺省 = run_id。模拟 AC10 ``meta_file.stem`` 源（三源之一）。
    """
    root = home / "runs"
    root.mkdir(parents=True, exist_ok=True)
    meta_file = root / f"{meta_stem or run_id}.json"
    meta_file.write_text(
        json.dumps({
            "run_id": run_id, "yaml_path": yaml_path,
            "started_at": started_at, "tape_path": tape_path,
        }),
        encoding="utf-8",
    )
    return meta_file


# ── AC1：legacy completed 不再被误标 cancelled ──────────────────────────────


def test_discover_legacy_completed_not_cancelled(tmp_path, _isolated_env):
    """AC1：legacy tape 含 workflow_completed → status=="completed"（非 cancelled）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "rid-ok.jsonl"
    _write_tape(tape_path, run_id="rid-ok", terminal="workflow_completed")
    _write_legacy_meta(_isolated_env, run_id="rid-ok", tape_path=str(tape_path))

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "rid-ok" in by_id, f"legacy run 未发现：{list(by_id)}"
    s = by_id["rid-ok"]
    assert s.status == "completed", f"期望 completed 得 {s.status}"
    assert s.source == "legacy"
    assert s.event_count > 0
    # cost 来自 tape（overview.cost_usd）—— 与 _summary_from_tape 直接调用值逐字相等（防硬编码 0.0）
    direct = manager._summary_from_tape(
        tape_path, project_id=None, project_name="Legacy", source="legacy",
    )
    assert direct is not None
    assert s.cost == direct.cost, (
        f"cost 应来自 tape fold（={direct.cost}），得 {s.cost}（可能被硬编码 0.0 覆盖）"
    )
    # progress 严格匹配正则 ^\d+/\d+$（"1/1"）
    import re
    assert re.match(r"^\d+/\d+$", s.progress), f"progress 非数字格式：{s.progress}"
    assert s.progress == direct.progress, "progress 应与 _summary_from_tape 逐字相等"


def test_discover_legacy_cost_round_trip(tmp_path, _isolated_env, monkeypatch):
    """AC1 / N8：patch _summary_from_tape 返固定 cost=1.23 → discover_runs round-trip。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "rid-cost.jsonl"
    _write_tape(tape_path, run_id="rid-cost", terminal="workflow_completed")
    _write_legacy_meta(_isolated_env, run_id="rid-cost", tape_path=str(tape_path))

    fixed = RunSummary(
        run_id="rid-cost", workflow_name="w", status="completed",
        progress="1/1", cost=1.23, elapsed=0.0, event_count=3, source="legacy",
    )

    def fake(self, tp, *, project_id, project_name, source, warn_run_id=None):
        # 仅对 rid-cost tape 命中，其它走原方法（不影响其他测试并发）。
        if tp == tape_path:
            return fixed.model_copy(update={"source": source})
        return _orig_summary_from_tape(self, tp, project_id=project_id, project_name=project_name, source=source, warn_run_id=warn_run_id)

    _orig_summary_from_tape = RunManager._summary_from_tape
    monkeypatch.setattr(RunManager, "_summary_from_tape", fake)

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert by_id["rid-cost"].cost == 1.23, "cost 应 round-trip 1.23（防被覆盖为 0.0）"


# ── AC2 / E9：legacy crashed 不被误标 cancelled ──────────────────────────────


def test_discover_legacy_crashed_equals_summary_from_tape(tmp_path, _isolated_env):
    """AC2 / E9：legacy tape 无终态事件 → status == _summary_from_tape 确定返回值。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "rid-crash.jsonl"
    _write_tape(tape_path, run_id="rid-crash", terminal="")  # 无终态事件
    _write_legacy_meta(_isolated_env, run_id="rid-crash", tape_path=str(tape_path))

    # 直接调 _summary_from_tape 拿确定返回值
    direct = manager._summary_from_tape(
        tape_path, project_id=None, project_name="Legacy", source="legacy",
    )
    assert direct is not None, "crashed tape 应能派生（live-pending / running）"
    assert direct.status != "cancelled", "crashed tape 派生 status 不应误为 cancelled"

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "rid-crash" in by_id, "crashed legacy run 应出现在列表（不 skip）"
    assert by_id["rid-crash"].status == direct.status, (
        f"discover_runs status {by_id['rid-crash'].status} != _summary_from_tape {direct.status}"
    )
    assert by_id["rid-crash"].status != "cancelled"


# ── AC3：legacy cancelled from tape ─────────────────────────────────────────


def test_discover_legacy_cancelled_from_tape(tmp_path, _isolated_env):
    """AC3：legacy tape 含 workflow_cancelled → status=="cancelled"（从 tape，非字面量）。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "rid-canc.jsonl"
    _write_tape(tape_path, run_id="rid-canc", terminal="workflow_cancelled")
    _write_legacy_meta(_isolated_env, run_id="rid-canc", tape_path=str(tape_path))

    summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert by_id["rid-canc"].status == "cancelled"


# ── AC4：legacy tape 缺失 / 相对路径 fail loud ───────────────────────────────


def test_discover_legacy_tape_missing_warns_and_skips(tmp_path, _isolated_env, caplog):
    """AC4(a)：绝对路径不存在 → 该 run 不在列表 + 列表长度正确 + caplog warning。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    # 正常 legacy run
    ok_tape = tmp_path / "legacy-tapes" / "ok.jsonl"
    _write_tape(ok_tape, run_id="ok", terminal="workflow_completed")
    _write_legacy_meta(_isolated_env, run_id="ok", tape_path=str(ok_tape))
    # 问题 legacy run（tape 不存在）
    missing = tmp_path / "legacy-tapes" / "missing.jsonl"
    _write_legacy_meta(_isolated_env, run_id="missing", tape_path=str(missing))

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summaries = manager.discover_runs()
    ids = [s.run_id for s in summaries]
    assert "ok" in ids
    assert "missing" not in ids, "tape 缺失的 legacy run 应 skip"
    # caplog 至少 1 条 warning 含 missing run_id + tape path
    warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "missing" in r.getMessage()
        and str(missing) in r.getMessage()
    ]
    assert warns, f"未找到含 run_id + tape path 的 warning：{[r.getMessage() for r in caplog.records]}"


def test_discover_legacy_tape_relative_path_skips(tmp_path, _isolated_env, caplog, monkeypatch):
    """AC4(b)：相对路径字符串（"./tape.jsonl"）即使 cwd 下有同名文件 → warn + skip。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    # 在 cwd 下造同名文件（证明不是因为找不到才 skip）
    cwd_tape = Path.cwd() / "tape.jsonl"
    written_cwd = False
    if not cwd_tape.exists():
        cwd_tape.write_text("{}\n", encoding="utf-8")
        written_cwd = True
    try:
        _write_legacy_meta(_isolated_env, run_id="relpath", tape_path="./tape.jsonl")
        with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
            summaries = manager.discover_runs()
        ids = [s.run_id for s in summaries]
        assert "relpath" not in ids, "相对路径 legacy run 必须 skip（即便 cwd 下有同名文件）"
        warns = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "relpath" in r.getMessage()
        ]
        assert warns, "相对路径 legacy run skip 时应 warn"
    finally:
        if written_cwd:
            try:
                cwd_tape.unlink()
            except OSError:
                pass


# ── AC5 / N1：legacy tape corrupt → warn + skip ─────────────────────────────


def test_discover_legacy_tape_corrupt_warns(tmp_path, _isolated_env, caplog, monkeypatch):
    """AC5 / N1：_summary_from_tape 抛异常 → 内层 warn（含 run_id + tape path）+ skip。

    用 lm.run_id="abc-meta" ≠ tape_path.stem="xyz-tape" 构造 mismatch fixture，
    断言 warn 同时含 lm.run_id 与 tape_path（防 stem 巧合通过——MAJOR-1 闭环）。
    """
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "xyz-tape.jsonl"
    _write_tape(tape_path, run_id="xyz-tape", terminal="workflow_completed")
    # 故意 lm.run_id 与 tape stem 不同（覆盖 AC10 三源 + AC5 run_id 锚点）
    _write_legacy_meta(
        _isolated_env, run_id="abc-meta", tape_path=str(tape_path), meta_stem="abc-meta",
    )

    # 让 _scan_meta_overview_cached 抛异常（模拟 corrupt tape 触发内层 except）
    def boom(_self, _tp):
        raise RuntimeError("simulated corrupt tape read failure")

    monkeypatch.setattr(RunManager, "_scan_meta_overview_cached", boom)

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summaries = manager.discover_runs()
    ids = [s.run_id for s in summaries]
    assert "xyz-tape" not in ids, "corrupt tape 应 skip"
    assert "abc-meta" not in ids, "lm.run_id 不应进列表（tape corrupt 已 skip）"
    # AC5：至少 1 条 warning 同时含 lm.run_id + tape path（N1 内层 warn 嵌入 warn_run_id）
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("abc-meta" in m and str(tape_path) in m for m in msgs), (
        f"未找到含 lm.run_id + tape path 的 warning：{msgs}"
    )


# ── AC6 / DRY：legacy 与 attached 共用 _summary_from_tape ────────────────────


def test_discover_legacy_uses_summary_from_tape(tmp_path, _isolated_env, monkeypatch):
    """AC6：legacy 与 attached 分支都调 _summary_from_tape；无第二条派生路径。"""
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    # attached tape（注册项目）
    proj = _make_project(tmp_path, "proj1")
    register_project(proj)
    _write_tape(proj / "runs" / "att.jsonl", run_id="att", terminal="workflow_completed")
    # legacy tape
    legacy_tape = tmp_path / "legacy-tapes" / "leg.jsonl"
    _write_tape(legacy_tape, run_id="leg", terminal="workflow_completed")
    _write_legacy_meta(_isolated_env, run_id="leg", tape_path=str(legacy_tape))

    calls: list[str] = []
    orig = RunManager._summary_from_tape

    def spy(self, tp, *, project_id, project_name, source, warn_run_id=None):
        calls.append(source)
        return orig(self, tp, project_id=project_id, project_name=project_name, source=source, warn_run_id=warn_run_id)

    monkeypatch.setattr(RunManager, "_summary_from_tape", spy)
    manager.discover_runs()

    assert "attached" in calls, "attached 分支应调 _summary_from_tape"
    assert "legacy" in calls, "legacy 分支应调 _summary_from_tape"

    # 静态 grep：discover_runs body 无第二条 status 派生 / 无硬编码 cancelled 字面量
    import inspect
    src = inspect.getsource(RunManager.discover_runs)
    assert 'status="cancelled"' not in src, (
        "discover_runs body 禁出现硬编码 cancelled 字面量（除 _summary_from_tape 内部映射）"
    )


# ── AC7：handle.status hint 语义文档化（静态 grep）──────────────────────────


def test_handle_status_hint_documented():
    """AC7：_meta_from_handle / list_runs / discover_runs docstring + 字段行注释命中关键词。"""
    import inspect

    keywords_hint = ("hint", "tape", "权威")
    kw_transient = ("transient", "非原子")

    def _docstring_hits(fn) -> bool:
        doc = fn.__doc__ or ""
        has_hint_set = all(kw in doc for kw in keywords_hint)
        has_transient = any(kw in doc for kw in kw_transient)
        return has_hint_set and has_transient

    assert _docstring_hits(RunManager._meta_from_handle), (
        "_meta_from_handle docstring 缺关键词 {hint, tape, 权威, transient/非原子}"
    )
    assert _docstring_hits(RunManager.list_runs), (
        "list_runs docstring 缺关键词"
    )
    assert _docstring_hits(RunManager.discover_runs), (
        "discover_runs docstring 缺关键词"
    )

    # 字段行注释
    import re as _re
    src = inspect.getsource(rm_mod)
    # RunMeta.status 与 RunSummary.status 行注释 # hint: 权威在 tape
    hint_comments = _re.findall(r"# hint: 权威在 tape", src)
    assert len(hint_comments) >= 2, (
        f"RunMeta.status + RunSummary.status 应各加 `# hint: 权威在 tape` 行注释，"
        f"找到 {len(hint_comments)}"
    )


# ── AC8：in-memory 与 attached 稳态一致 ──────────────────────────────────────


def test_discover_inmemory_matches_attached_in_steady_state(tmp_path, _isolated_env):
    """AC8：run 终态后，in-memory（handle 在 _runs）vs 仅 attached（清空 _runs）status 一致。"""
    import asyncio
    from orca.runtime import register_project

    manager = RunManager(runs_dir=tmp_path / "runs")
    proj = _make_project(tmp_path, "proj")
    register_project(proj)
    tape_path = proj / "runs" / "rid-steady.jsonl"
    _write_tape(tape_path, run_id="rid-steady", terminal="workflow_completed")

    async def go():
        await manager.ensure_attached("rid-steady")
        # in-memory 路径
        in_mem = manager.discover_runs()
        by_id1 = {s.run_id: s for s in in_mem}
        status_inmem = by_id1["rid-steady"].status

        # 模拟 restart：清空 _runs（保留磁盘 tape）
        for h in list(manager._runs.values()):
            try:
                await manager._teardown_handle(h)
            except Exception:
                pass
        manager._runs.clear()
        # attached 路径
        attached = manager.discover_runs()
        by_id2 = {s.run_id: s for s in attached}
        status_attached = by_id2["rid-steady"].status

        assert status_inmem == status_attached == "completed", (
            f"in-memory={status_inmem} attached={status_attached} 应一致且都 completed"
        )

    asyncio.run(go())


# ── AC8b：_meta_from_handle replay 失败降级（非回归守卫）────────────────────


def test_meta_from_handle_replay_fail_status_consistent(tmp_path, _isolated_env, caplog, monkeypatch):
    """AC8b：replay 失败 → RunMeta.status == handle.status + warning 含 run_id + replay-degraded。"""
    import asyncio
    from orca.iface.web.run_manager import InProcessRunHandle
    from orca.events.tape import Tape

    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "t" / "r.jsonl"
    _write_tape(tape_path, run_id="r", terminal="workflow_completed")

    tape = Tape(tape_path, run_id="r-degraded")
    handle = InProcessRunHandle(run_id="r-degraded", bus=None, tape=tape)  # type: ignore[arg-type]
    handle.status = "running"
    manager._runs[handle.run_id] = handle

    def boom(_tape):
        raise RuntimeError("simulated replay failure")

    monkeypatch.setattr(rm_mod, "replay_state", boom)

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        meta = manager._meta_from_handle(handle)
    assert isinstance(meta, RunMeta)
    assert meta.status == handle.status, "replay 失败时 status 应取 handle.status（不抛、不静默）"

    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(handle.run_id in m and "replay-degraded" in m for m in msgs), (
        f"warning 缺 run_id + replay-degraded 标记：{msgs}"
    )

    # 清理避免污染其他测试
    manager._runs.pop(handle.run_id, None)
    tape.close()
    asyncio.run(manager.shutdown())


# ── AC9：三分支 dedup ────────────────────────────────────────────────────────


def test_discover_dedup_three_pair_collisions(tmp_path, _isolated_env):
    """AC9：枚举三对 run_id 撞车，各断言列表仅出现一次 + source 优先级胜出。"""
    import asyncio
    from orca.runtime import register_project

    # ----- (i) attached × attached：两注册项目各写同 stem 的 tape -----
    manager_i = RunManager(runs_dir=tmp_path / "runs-i")
    p1 = _make_project(tmp_path, "p1")
    p2 = _make_project(tmp_path, "p2")
    register_project(p1)
    register_project(p2)
    _write_tape(p1 / "runs" / "dup.jsonl", run_id="dup", terminal="workflow_completed")
    _write_tape(p2 / "runs" / "dup.jsonl", run_id="dup", terminal="workflow_completed")
    sum_i = manager_i.discover_runs()
    dup_count = sum(1 for s in sum_i if s.run_id == "dup")
    assert dup_count == 1, f"attached×attached 应去重为 1，得 {dup_count}"

    # ----- (ii) attached × legacy：注册项目 tape + legacy meta 共用 run_id -----
    manager_ii = RunManager(runs_dir=tmp_path / "runs-ii")
    proj = _make_project(tmp_path, "proj-ii")
    register_project(proj)
    _write_tape(proj / "runs" / "shared.jsonl", run_id="shared", terminal="workflow_completed")
    legacy_tape = tmp_path / "legacy-ii" / "shared.jsonl"
    _write_tape(legacy_tape, run_id="shared", terminal="workflow_failed")
    _write_legacy_meta(_isolated_env, run_id="shared", tape_path=str(legacy_tape))
    sum_ii = manager_ii.discover_runs()
    shared = [s for s in sum_ii if s.run_id == "shared"]
    assert len(shared) == 1, f"attached×legacy 应去重为 1，得 {len(shared)}"
    # 优先级：attached > legacy（attached 胜 → status=completed，legacy 是 failed）
    assert shared[0].status == "completed", "attached 应胜出（status=completed 非 failed）"
    assert shared[0].source == "attached"

    # ----- (iii) in-memory × legacy：_runs handle + legacy meta 共用 run_id -----
    manager_iii = RunManager(runs_dir=tmp_path / "runs-iii")
    proj_iii = _make_project(tmp_path, "proj-iii")
    register_project(proj_iii)
    inmem_tape = proj_iii / "runs" / "mem.jsonl"
    _write_tape(inmem_tape, run_id="mem", terminal="workflow_completed")
    legacy_tape_iii = tmp_path / "legacy-iii" / "mem.jsonl"
    _write_tape(legacy_tape_iii, run_id="mem", terminal="workflow_failed")
    _write_legacy_meta(_isolated_env, run_id="mem", tape_path=str(legacy_tape_iii))

    async def go():
        # ensure_attached 把 inmem_tape 装进 _runs（in-memory 通道）
        await manager_iii.ensure_attached("mem")

    asyncio.run(go())

    sum_iii = manager_iii.discover_runs()
    mem = [s for s in sum_iii if s.run_id == "mem"]
    assert len(mem) == 1, f"in-memory×legacy 应去重为 1，得 {len(mem)}"
    # in-memory > legacy（in-memory 胜，source ∈ {in-process, attached}）
    assert mem[0].source in ("in-process", "attached"), (
        f"in-memory 应胜出（source 应为 in-process/attached），得 {mem[0].source}"
    )


# ── AC10：legacy meta.run_id 与 tape_path.stem 不一致 ────────────────────────


def test_discover_legacy_run_id_mismatch_uses_tape_stem(tmp_path, _isolated_env, caplog):
    """AC10：lm.run_id 三源（data.run_id / meta_file.stem / fallback）≠ tape_path.stem
    时，RunSummary.run_id == tape_path.stem + warning。
    """
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "xyz.jsonl"  # stem=xyz
    _write_tape(tape_path, run_id="xyz", terminal="workflow_completed")

    # 源 1: data.run_id="abc"
    _write_legacy_meta(_isolated_env, run_id="abc", tape_path=str(tape_path), meta_stem="abc-1")
    # 源 2: meta_file.stem="def"（data.run_id 缺省 fallback 到 meta_file.stem）
    root = _isolated_env / "runs"
    (root / "def.json").write_text(
        json.dumps({"tape_path": str(tape_path)}),  # 无 run_id → fallback 到 stem
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}

    # 无论 lm.run_id 来自哪一源，run_id 都以 tape_path.stem=xyz 为准
    assert "xyz" in by_id, f"应使用 tape_path.stem=xyz，实际 ids={list(by_id)}"
    assert "abc" not in by_id and "def" not in by_id, "lm.run_id 不应进入列表（被 tape_stem 覆盖）"

    # warning 含 meta run_id + tape_stem
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("mismatch" in m and "abc" in m and "xyz" in m for m in msgs), (
        f"warning 缺 meta run_id + tape_stem mismatch 锚点：{msgs}"
    )


def test_discover_legacy_run_id_matches_tape_stem_no_warn(tmp_path, _isolated_env, caplog):
    """AC10 第三源（平凡态）：lm.run_id 来自 tape_path.stem（无 mismatch）→ 无 warn + run_id 入列表。"""
    manager = RunManager(runs_dir=tmp_path / "runs")
    tape_path = tmp_path / "legacy-tapes" / "match.jsonl"
    _write_tape(tape_path, run_id="match", terminal="workflow_completed")
    # data 缺 run_id、meta_file.stem == tape_path.stem="match" → fallback 到 stem，无 mismatch
    root = _isolated_env / "runs"
    root.mkdir(parents=True, exist_ok=True)
    (root / "match.json").write_text(
        json.dumps({"tape_path": str(tape_path)}), encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="orca.iface.web.run_manager"):
        summaries = manager.discover_runs()
    by_id = {s.run_id: s for s in summaries}
    assert "match" in by_id, "第三源（tape stem）应正常入列表"
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("mismatch" in m for m in msgs), "无 mismatch 时不应 warn"


# ── AC11：不引入第二真相源（静态语义判断）──────────────────────────────────


def test_no_status_cache_introduced():
    """AC11：RunManager 类体无 RunStatus 字段 / 无 dict[run_id→RunStatus]；
    discover_runs 无 cache 装饰；豁免 _scan_meta_overview_cached / _meta_cache。
    """
    import inspect
    import re

    src = inspect.getsource(RunManager)
    # 类体内不应有形如 ``_status_cache: dict[...]`` / ``_run_status`` / ``_statuses``
    # 的 RunStatus-typed 字段（语义判断，覆盖常见命名）。
    forbidden_patterns = [
        r"self\._\w*status\w*_cache\b\s*:",
        r"self\._run_statuses\b\s*:",
        r"self\._status_map\b\s*:",
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, src), f"RunManager 引入禁止的 status 字段：{pat}"

    # discover_runs 无 functools.cache / lru_cache 装饰
    disc_src = inspect.getsource(RunManager.discover_runs)
    # 实际装饰器列表（getsource 含 def 行前的 @...）
    assert "functools.cache" not in disc_src and "functools.lru_cache" not in disc_src, (
        "discover_runs 不应被 cache 装饰（status 仅即时计算）"
    )

    # 豁免：_scan_meta_overview_cached / _meta_cache 存在且属 perf 优化（C5）
    assert hasattr(RunManager, "_scan_meta_overview_cached"), "豁免项 _scan_meta_overview_cached 应存在"
    # _meta_cache 字段在 __init__ 初始化（结构层面校验）
    init_src = inspect.getsource(RunManager.__init__)
    assert "_meta_cache" in init_src, "_meta_cache 应在 __init__ 初始化（perf 优化，可重建）"


# ── AC13：release note 关键词 ──────────────────────────────────────────────


def test_release_note_documents_breaking_changes():
    """AC13：release note 存在 + 命中关键词集。"""
    rn = Path(__file__).resolve().parents[3] / "docs" / "releases" / "2026-08-02-audit-e.md"
    assert rn.is_file(), f"release note 不存在：{rn}"
    text = rn.read_text(encoding="utf-8")
    keywords = [
        "legacy tape 缺失",
        "skip",
        "started_at 语义",
        "workflow_started.timestamp",
        "crashed legacy",
    ]
    # live-pending 或 running 任一即可
    missing = [kw for kw in keywords if kw not in text]
    assert not missing, f"release note 缺关键词：{missing}"
    assert ("live-pending" in text) or ("running" in text), (
        "release note 应含 live-pending 或 running（E9 crashed legacy 派生 status）"
    )
