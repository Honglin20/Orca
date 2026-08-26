"""test_active_runs.py —— active-run 兜底 resolver 单测（SPEC 2026-08-07 T7–T15b）。

覆盖（SPEC §4）：
  - T7：host_session 匹配命中（tape 首行 ``data.host_session``）。
  - T8：节点顶层 ``session_id`` 匹配命中（宿主≠子代理双键）。
  - T9：无 marker / stale marker+终态末行 / orphan marker（tape 缺失）→ 不命中。
  - T10：marker 存在但 tape 无双键匹配 → None。
  - T11：tape 坏行 / 首行截断 / data 非 dict / host_session=null → fail-soft。
  - T12：多 run 命中 → 取 marker mtime 最新 + warn；mtime 平局 → run_id 字典序最小。
  - T13：多 runs dir 枚举（registered projects，调用期枚举）。
  - T14：缓存键含 marker 状态——marker 增删后失效重新扫描；tape 追加后重扫。
  - T15b：注册表损坏 → resolver catch → None（不炸，不传播到 create_app）。
  - AC4：结构化 import 守门——active_runs 不 import run/tape*/exec/events.bus/gates.handler。

约定：纯 pytest（无 pytest-asyncio）；fixture 用 tmp_path + monkeypatch 隔离
``ORCA_HOME``（注册表）与 ``ORCA_PROJECT_ROOT``（runs dir 解析）。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from orca.iface.in_session.marker import ActivationMarker, marker_path, write_marker
from orca.iface.web.active_runs import (
    build_active_run_resolver,
    resolve_session_to_active_run,
)
from orca.runtime import register_project


def _event(
    seq: int,
    event_type: str,
    *,
    session_id: str | None = None,
    data: dict | None = None,
) -> dict:
    """构造 tape 事件信封（对齐 events/tape.py 的 seq/type/timestamp/node/session_id/data）。"""
    return {
        "seq": seq,
        "type": event_type,
        "timestamp": 1.0,
        "node": None,
        "session_id": session_id,
        "data": data if data is not None else {},
    }


def _write_tape(runs_dir: Path, run_id: str, events: list[dict]) -> Path:
    """写 ``<runs_dir>/<run_id>.jsonl``（末行非终态，视为活跃）。"""
    path = runs_dir / f"{run_id}.jsonl"
    lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
    path.write_text(lines, encoding="utf-8")
    return path


def _write_marker(runs_dir: Path, run_id: str, mtime_ns: int | None = None) -> Path:
    """写 ``orca-<run_id>.json`` marker；可选固定 mtime_ns（多 run 命中测试）。"""
    path = marker_path(runs_dir, run_id)
    write_marker(path, ActivationMarker(run_id=run_id))
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _make_runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    return runs


def _isolate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离注册表与 runs 解析：ORCA_HOME / ORCA_PROJECT_ROOT → tmp 空项目。"""
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / "home"))
    proj = tmp_path / "projC"
    (proj / "workflows").mkdir(parents=True)
    monkeypatch.setenv("ORCA_PROJECT_ROOT", str(proj))
    return proj


# ── T7 / T8：双键匹配 ────────────────────────────────────────────────────────


def test_host_session_match(tmp_path):
    """T7：首行 data.host_session == session_id → 命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-host")
    _write_tape(runs, "run-host", [
        _event(1, "workflow_started", data={"host_session": "ses-host"}),
        _event(2, "node_started", session_id="ses-child"),
    ])
    assert resolve_session_to_active_run("ses-host", [runs]) == "run-host"


def test_node_session_match_when_host_differs(tmp_path):
    """T8：宿主≠子代理双键——顶层 session_id 命中；host 键同样可命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-node")
    _write_tape(runs, "run-node", [
        _event(1, "workflow_started", data={"host_session": "ses-host"}),
        _event(2, "node_started", session_id="ses-child"),
    ])
    assert resolve_session_to_active_run("ses-child", [runs]) == "run-node"
    assert resolve_session_to_active_run("ses-host", [runs]) == "run-node"


# ── T9 / T10：活跃判定 ───────────────────────────────────────────────────────


def test_no_marker_returns_none(tmp_path):
    """T9a：无 marker → 不命中（tape 存在但非 in-session 活跃 run）。"""
    runs = _make_runs(tmp_path)
    _write_tape(runs, "run-x", [_event(1, "workflow_started", data={"host_session": "ses"})])
    assert resolve_session_to_active_run("ses", [runs]) is None


def test_terminal_last_event_inactive(tmp_path):
    """T9b：stale marker + tape 末行终态事件 → 不命中（防 kill -9 残留 marker）。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-dead")
    _write_tape(runs, "run-dead", [
        _event(1, "workflow_started", data={"host_session": "ses"}),
        _event(2, "workflow_completed"),
    ])
    assert resolve_session_to_active_run("ses", [runs]) is None


def test_orphan_marker_tape_missing_inactive(tmp_path):
    """T9c：orphan marker（tape 缺失）→ 不命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-orphan")
    assert resolve_session_to_active_run("ses", [runs]) is None


def test_marker_exists_no_key_match_returns_none(tmp_path):
    """T10：marker 存在 + tape 非终态，但无双键匹配 → None。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-other")
    _write_tape(runs, "run-other", [
        _event(1, "workflow_started", data={"host_session": "ses-a"}),
        _event(2, "node_started", session_id="ses-b"),
    ])
    assert resolve_session_to_active_run("ses-z", [runs]) is None


# ── T11：fail-soft 坏数据 ────────────────────────────────────────────────────


def test_corrupt_middle_line_fail_soft_still_matches(tmp_path):
    """T11a：坏行跳过 + warn，host/node 双键仍可命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-mixed")
    tape = runs / "run-mixed.jsonl"
    lines = [
        json.dumps(_event(1, "workflow_started", data={"host_session": "ses-host"})),
        "{this is not json",
        json.dumps(_event(3, "node_started", session_id="ses-child")),
    ]
    tape.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert resolve_session_to_active_run("ses-host", [runs]) == "run-mixed"
    assert resolve_session_to_active_run("ses-child", [runs]) == "run-mixed"


def test_truncated_first_line_fail_soft_still_node_match(tmp_path):
    """T11b：首行截断 → host 键缺失，node 扫描仍命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-trunc")
    tape = runs / "run-trunc.jsonl"
    tape.write_text(
        '{"seq": 1, "type": "workflow_started", "data": {"host_sess\n'
        + json.dumps(_event(2, "node_started", session_id="ses-child"))
        + "\n",
        encoding="utf-8",
    )
    assert resolve_session_to_active_run("ses-child", [runs]) == "run-trunc"
    assert resolve_session_to_active_run("ses-host", [runs]) is None


def test_data_non_dict_host_skipped_still_node_scan(tmp_path):
    """T11c：data 非 dict → host 键跳过，node 扫描仍命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-bad-data")
    _write_tape(runs, "run-bad-data", [
        _event(1, "workflow_started", data="not-a-dict"),
        _event(2, "node_started", session_id="ses-child"),
    ])
    assert resolve_session_to_active_run("ses-child", [runs]) == "run-bad-data"


def test_host_session_null_still_node_scan(tmp_path):
    """T11d：host_session=null → 不参与 host 键，node 扫描仍命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-null-host")
    _write_tape(runs, "run-null-host", [
        _event(1, "workflow_started", data={"host_session": None}),
        _event(2, "node_started", session_id="ses-child"),
    ])
    assert resolve_session_to_active_run("ses-child", [runs]) == "run-null-host"
    assert resolve_session_to_active_run("ses-host", [runs]) is None


# ── T12：多 run 命中 ─────────────────────────────────────────────────────────


def test_multi_run_hit_latest_marker_mtime(tmp_path, caplog):
    """T12a：多 run 命中 → 取 marker mtime 最新者 + warning（fail loud）。"""
    runs = _make_runs(tmp_path)
    base = 1_700_000_000_000_000_000
    _write_marker(runs, "run-A", mtime_ns=base)
    _write_marker(runs, "run-B", mtime_ns=base + 1_000_000)
    for run_id in ("run-A", "run-B"):
        _write_tape(runs, run_id, [
            _event(1, "workflow_started", data={"host_session": "ses-shared"}),
        ])
    with caplog.at_level("WARNING", logger="orca.iface.web.active_runs"):
        hit = resolve_session_to_active_run("ses-shared", [runs])
    assert hit == "run-B"  # mtime 最新。
    assert any("多 run 命中" in r.message for r in caplog.records)


def test_multi_run_hit_mtime_tie_min_run_id(tmp_path, caplog):
    """T12b：mtime 平局 → run_id 字典序最小（确定性）。"""
    runs = _make_runs(tmp_path)
    ns = 1_700_000_000_000_000_000
    _write_marker(runs, "run-B", mtime_ns=ns)
    _write_marker(runs, "run-A", mtime_ns=ns)
    # 断言两 marker 的 mtime 确实一致（文件系统粒度防护）。
    mtimes = {p.stat().st_mtime_ns for p in runs.glob("orca-*.json")}
    assert len(mtimes) == 1
    for run_id in ("run-A", "run-B"):
        _write_tape(runs, run_id, [
            _event(1, "workflow_started", data={"host_session": "ses-shared"}),
        ])
    with caplog.at_level("WARNING", logger="orca.iface.web.active_runs"):
        hit = resolve_session_to_active_run("ses-shared", [runs])
    assert hit == "run-A"  # 字典序最小。
    assert any("多 run 命中" in r.message for r in caplog.records)


# ── T13：多 runs dir 枚举（registered projects） ─────────────────────────────


def test_multi_runs_dir_enumeration_via_registry(tmp_path, monkeypatch):
    """T13：registered 多项目 runs dir 全扫，调用期枚举命中任一项目。"""
    _isolate_env(tmp_path, monkeypatch)
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    for proj in (proj_a, proj_b):
        (proj / "workflows").mkdir(parents=True)
        register_project(proj)
    runs_a = proj_a / "runs"
    runs_b = proj_b / "runs"
    runs_a.mkdir()
    runs_b.mkdir()
    _write_marker(runs_a, "run-a1")
    _write_tape(runs_a, "run-a1", [
        _event(1, "workflow_started", data={"host_session": "ses-host"}),
    ])
    _write_marker(runs_b, "run-b1")
    _write_tape(runs_b, "run-b1", [
        _event(1, "workflow_started", data={"host_session": "ses-other"}),
        _event(2, "node_started", session_id="ses-child"),
    ])
    # 工厂无参：调用期枚举 resolve_runs_dir() + list_registered() 全项目。
    resolver = build_active_run_resolver()
    assert resolver("ses-host") == "run-a1"
    assert resolver("ses-child") == "run-b1"
    # 显式注入 runs_dirs 同样生效（纯函数路径）。
    assert resolve_session_to_active_run("ses-host", [runs_a]) == "run-a1"


# ── T14：缓存键含 marker 状态 + tape 追加失效 ────────────────────────────────


def test_marker_add_delete_invalidates_scan(tmp_path):
    """T14a：marker 增删后缓存失效——删除 marker → 不再命中。"""
    runs = _make_runs(tmp_path)
    _write_tape(runs, "run-cache", [
        _event(1, "workflow_started", data={"host_session": "ses"}),
    ])
    assert resolve_session_to_active_run("ses", [runs]) is None  # 无 marker。
    marker = _write_marker(runs, "run-cache")
    assert resolve_session_to_active_run("ses", [runs]) == "run-cache"  # 命中并建缓存。
    # 缓存键含 marker 存在性维度（SPEC §2.3）：键第 4 元素 = True（仅对存在 marker 的 run 建索引）。
    import orca.iface.web.active_runs as active_runs_mod
    cache_keys = [
        k for k in active_runs_mod._tape_cache
        if k[0].endswith("run-cache.jsonl")
    ]
    assert cache_keys and all(k[3] is True for k in cache_keys)
    marker.unlink()
    assert resolve_session_to_active_run("ses", [runs]) is None  # marker 删除 → 失效。


def test_tape_append_invalidates_cache(tmp_path):
    """T14b：tape 追加（mtime/size 变）→ 缓存失效重新扫描，新 session 可命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-cache")
    tape = _write_tape(runs, "run-cache", [
        _event(1, "workflow_started", data={"host_session": "ses-1"}),
    ])
    assert resolve_session_to_active_run("ses-1", [runs]) == "run-cache"  # 建缓存。
    # 追加事件（写文件使 mtime/size 变化）。
    with tape.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_event(2, "node_started", session_id="ses-2")) + "\n")
    assert resolve_session_to_active_run("ses-2", [runs]) == "run-cache"


def test_invalid_utf8_tape_skips_only_that_run(tmp_path):
    """非法 UTF-8 tape → 仅跳过该 run（per-tape fail-soft），其它 run 仍可命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-bad")
    bad = runs / "run-bad.jsonl"
    bad.write_bytes(
        b'{"seq": 1, "type": "workflow_started", '
        b'"data": {"host_session": "ses"}}\xff\xfe\n'
    )
    _write_marker(runs, "run-good")
    _write_tape(runs, "run-good", [
        _event(1, "workflow_started", data={"host_session": "ses-good"}),
    ])
    # 坏 tape 不参与匹配、不中断整轮扫描；好 run 正常命中。
    assert resolve_session_to_active_run("ses-good", [runs]) == "run-good"
    assert resolve_session_to_active_run("ses", [runs]) is None


def test_invalid_utf8_marker_skips_only_that_marker(tmp_path):
    """非法 UTF-8 marker → 仅跳过该 marker（per-marker fail-soft），其它 run 仍可命中。"""
    runs = _make_runs(tmp_path)
    bad = runs / "orca-run-bad.json"
    bad.write_bytes(b'{"run_id": "run-bad"}\xff\xfe')
    # 仅坏 marker：不炸，未命中。
    assert resolve_session_to_active_run("ses-bad", [runs]) is None
    # 好 marker 并存：好 run 正常命中（坏 marker 不中断整轮扫描）。
    _write_marker(runs, "run-good")
    _write_tape(runs, "run-good", [
        _event(1, "workflow_started", data={"host_session": "ses-good"}),
    ])
    assert resolve_session_to_active_run("ses-good", [runs]) == "run-good"


def test_huge_single_line_tape_reads_last_line(tmp_path):
    """超长单行（>64KB，跨向后 seek 多块）→ 末行解析正确并命中。"""
    runs = _make_runs(tmp_path)
    _write_marker(runs, "run-huge")
    _write_tape(runs, "run-huge", [
        _event(1, "workflow_started", data={
            "host_session": "ses-huge",
            "blob": "x" * (80 * 1024),
        }),
    ])
    assert resolve_session_to_active_run("ses-huge", [runs]) == "run-huge"


# ── T15b：注册表损坏 fail-soft ───────────────────────────────────────────────


def test_corrupt_registry_resolver_fails_soft(tmp_path, monkeypatch):
    """T15b：注册表损坏 → resolver catch → None（不炸，不传播到 create_app 装配）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ORCA_HOME", str(home))
    # 主文件 + .bak 均损坏 → list_registered 抛 RegistryCorruptError。
    (home / "projects.json").write_text("{not json", encoding="utf-8")
    (home / "projects.json.bak").write_text("{also broken", encoding="utf-8")
    proj = _isolate_env(tmp_path, monkeypatch)
    # 工厂创建零 IO：即使注册表损坏也不抛（create_app 装配安全）。
    resolver = build_active_run_resolver()
    # 调用期枚举：注册表来源 warn + 忽略，cwd/env runs 目录为空 → 未命中。
    assert resolver("ses-x") is None
    # 有 active run 时注册表损坏不影响 env 路径扫描。
    runs = proj / "runs"
    runs.mkdir()
    _write_marker(runs, "run-local")
    _write_tape(runs, "run-local", [
        _event(1, "workflow_started", data={"host_session": "ses-local"}),
    ])
    assert resolver("ses-local") == "run-local"


# ── AC4：结构化 import 守门 ──────────────────────────────────────────────────


def test_active_runs_no_forbidden_imports():
    """SPEC AC4：active_runs 不 import run/tape*/exec/events.bus/gates.handler。

    结构化 AST 检查（非裸 grep）：``orca.runtime`` 允许（public re-export），
    但 ``orca.run`` / ``orca.tape`` 等前缀需按模块边界精确匹配。
    """
    import orca.iface.web.active_runs as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = (
        "orca.run",
        "orca.tape",
        "orca.exec",
        "orca.events.bus",
        "orca.gates.handler",
    )
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    for mod_name in modules:
        assert not any(
            mod_name == prefix or mod_name.startswith(prefix + ".")
            for prefix in forbidden
        ), f"active_runs 不应 import {mod_name!r}"
