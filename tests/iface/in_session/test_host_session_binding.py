"""tests/iface/in_session/test_host_session_binding.py —— host_session 绑定防串台（SPEC-A v2）。

覆盖 SPEC §5 验收：
  - ``_host_session_from_env`` 优先级（ORCA_HOST_SESSION_ID > CLAUDE_CODE_SESSION_ID > None）。
  - ``make_workflow_started`` 写入 ``data["host_session"]``（tape 钉值，键恒写）。
  - ``advance_step`` 仅 pending 首节点分支透传 host_session（next 不重发 ws）。
  - cli bootstrap：env → tape workflow_started.data.host_session（端到端钉值）。
  - cc_nudge.sh 过滤逻辑：host_session 等/不等/None、per-session 限流分键、current=None+活跃
    marker→warn、tape 首行非 workflow_started/读失败→None。

铁律：host_session 只存 tape（marker 零改动，test_marker_only_three_fields 仍绿——在
test_marker.py 守门，本文件不复测）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.iface.cli import install_cmds
from orca.iface.in_session.cli import _host_session_from_env, app
from orca.run.lifecycle import make_workflow_started
from orca.run.step import advance_step
from orca.schema import AgentNode, Route, Workflow

# ── fixtures ────────────────────────────────────────────────────────────────

_AGENT_WF_YAML = """\
name: hsb_test_wf
description: 2-agent 线性 workflow（host_session 绑定测试用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 step A 的输出。"
    routes:
      - to: b
  - name: b
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "基于 {{ a.output }} 总结。"
    routes:
      - to: $end
"""


@pytest.fixture
def wf_path(tmp_path: Path) -> Path:
    p = tmp_path / "wf.yaml"
    p.write_text(_AGENT_WF_YAML, encoding="utf-8")
    return p


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _wf_single() -> Workflow:
    """单节点 agent workflow（advance_step 直调用，不走 CLI）。entry a → $end。"""
    return Workflow(
        name="hsb_unit",
        entry="a",
        nodes=[AgentNode(
            name="a", executor="opencode", model="d/d", prompt="do A",
            routes=[Route(to="$end")],
        )],
    )


# ── _host_session_from_env 优先级（SPEC §4.2）────────────────────────────────


def test_host_session_env_orca_wins_over_claude():
    """ORCA_HOST_SESSION_ID 优先于 CLAUDE_CODE_SESSION_ID。"""
    with mock.patch.dict(os.environ, {
        "ORCA_HOST_SESSION_ID": "orca-sess-1",
        "CLAUDE_CODE_SESSION_ID": "cc-sess-1",
    }):
        assert _host_session_from_env() == "orca-sess-1"


def test_host_session_env_fallback_to_claude():
    """无 ORCA_HOST_SESSION_ID → fallback CLAUDE_CODE_SESSION_ID（CC 零配置路径）。"""
    with mock.patch.dict(os.environ, {
        "CLAUDE_CODE_SESSION_ID": "cc-sess-2",
        "ORCA_HOST_SESSION_ID": "",
    }):
        assert _host_session_from_env() == "cc-sess-2"


def test_host_session_env_neither_returns_none():
    """两个 env 都无 → None（手 CLI / 未注入）。"""
    with mock.patch.dict(os.environ, {}, clear=True):
        assert _host_session_from_env() is None


def test_host_session_env_empty_orca_falls_through():
    """ORCA_HOST_SESSION_ID 空串（falsy）→ short-circuit 到 CLAUDE_CODE_SESSION_ID。

    防御：``or`` 的 falsy 语义让空串等同未设（用户误 export ORCA_HOST_SESSION_ID= 时不静默丢。
    """
    with mock.patch.dict(os.environ, {
        "ORCA_HOST_SESSION_ID": "",
        "CLAUDE_CODE_SESSION_ID": "cc-sess-3",
    }):
        assert _host_session_from_env() == "cc-sess-3"


# ── make_workflow_started 写入 host_session（SPEC §4.1 tape 钉值）─────────────


def _simple_wf() -> Workflow:
    from orca.schema import ScriptNode
    return Workflow(
        name="demo", entry="a",
        nodes=[ScriptNode(name="a", command="echo", routes=[])],
    )


def test_make_workflow_started_writes_host_session_value():
    """host_session 非空 → data['host_session'] == 传入值（tape 钉值，§5.3）。"""
    t, data = make_workflow_started("r1", _simple_wf(), {}, host_session="sess-xyz")
    assert t == "workflow_started"
    assert data["host_session"] == "sess-xyz"


def test_make_workflow_started_host_session_none_key_present():
    """host_session=None → 键恒写入且值为 None（str|null 契约，区别于 yaml_path 条件写）。

    区别于 yaml_path（None 时键省略）：host_session 是 nudge 过滤核心字段，显式 None
    比「键缺失」更清晰（reader 对两者都返 None，但 tape schema 明确）。
    """
    t, data = make_workflow_started("r1", _simple_wf(), {}, host_session=None)
    assert "host_session" in data, "host_session 键必须恒写入（str|null 契约）"
    assert data["host_session"] is None


def test_make_workflow_started_host_session_default_none():
    """不传 host_session（默认）→ 键仍在 + None（向后兼容老调用方）。"""
    t, data = make_workflow_started("r1", _simple_wf(), {})
    assert "host_session" in data
    assert data["host_session"] is None


# ── advance_step 透传 host_session（SPEC §4.1 emit 真链）─────────────────────


def test_advance_step_pending_passes_host_session_in_emit(tmp_path: Path):
    """首节点（pending）→ advance_step 的 emits 含 workflow_started 且 data.host_session 钉值。

    advance_step 是纯决策（不写 tape；§step.py docstring）；daemon/cli 调 apply_step_result
    才真写 tape。本测试守门 emits 内容（emit 真链第一点：lifecycle ← step 透传）。
    """
    from orca.events.tape import Tape
    tape = Tape(tmp_path / "t.jsonl", run_id="r1", resume=True)
    result = advance_step(tape, _wf_single(), run_id="r1", prompts_dir=None, host_session="sess-boot")
    # emits[0] = workflow_started（advance_step 在 pending 分支先 emit ws，再 emit node_started）
    ws_emits = [e for e in result.emits if e.type == "workflow_started"]
    assert len(ws_emits) == 1
    assert ws_emits[0].data["host_session"] == "sess-boot"


def test_advance_step_next_path_does_not_re_emit_workflow_started(tmp_path: Path):
    """next 路径（非 pending）不重发 workflow_started → host_session 不再需要（§4.1）。

    预置 tape（已有 workflow_started + node_started，模拟 bootstrap 后的状态），再调
    advance_step(output=...) → emits 应含 node_completed/route_taken/workflow_completed，
    **不应**含 workflow_started。
    """
    from orca.events.tape import Tape
    tape_path = tmp_path / "t.jsonl"
    # 手工预置 bootstrap 后的 tape（单节点 wf，entry=a 已 started）。
    _seed_tape(tape_path, run_id="r1", host_session="sess-boot")
    tape = Tape(tape_path, run_id="r1", resume=True)
    result = advance_step(
        tape, _wf_single(), output="done A", run_id="r1", prompts_dir=None,
        host_session="should-be-ignored",  # 即便传也不应产生新 ws
    )
    ws_emits = [e for e in result.emits if e.type == "workflow_started"]
    assert ws_emits == [], "next 路径不应重发 workflow_started（host_session 只在 bootstrap 写一次）"
    assert result.done, "单节点 wf output 后应 done"


def _seed_tape(tape_path: Path, *, run_id: str, host_session: str | None) -> None:
    """手工写 tape 前 2 条事件（workflow_started + node_started），模拟 bootstrap 后状态。

    replay_state 据此判定 status=running（pending→running），advance_step 走 next 路径。
    seq/timestamp 字段保持最小可用（replay 不强校验这些）。
    """
    import time as _t
    ts = _t.time()
    events = [
        {"seq": 1, "type": "workflow_started", "timestamp": ts,
         "data": {"inputs": {}, "node_count": 1, "entry": "a", "workflow_name": "hsb_unit",
                  "topology": {"entry": "a", "nodes": [{"name": "a", "kind": "agent"}],
                               "routes": [], "parallel": []},
                  "host_session": host_session}, "node": None, "session_id": None},
        {"seq": 2, "type": "node_started", "timestamp": ts,
         "data": {"node": "a"}, "node": "a", "session_id": None},
    ]
    tape_path.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8",
    )


# ── cli bootstrap 端到端：env → tape（SPEC §5.3 钉值）────────────────────────


def _bootstrap(runner: CliRunner, wf_path: Path) -> dict:
    result = runner.invoke(app, ["bootstrap", str(wf_path), "--inputs", "{}"])
    assert result.exit_code == 0, f"bootstrap failed: {result.output}"
    return json.loads(result.output.splitlines()[-1])


def test_cli_bootstrap_env_written_to_tape(cwd_tmp, wf_path, monkeypatch):
    """bootstrap 时 CLAUDE_CODE_SESSION_ID → tape workflow_started.data.host_session 钉值。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-e2e-sess")
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    runner = CliRunner()
    reply = _bootstrap(runner, wf_path)
    tape_path = Path(reply["tape"])
    ws = json.loads(tape_path.read_text(encoding="utf-8").splitlines()[0])
    assert ws["type"] == "workflow_started"
    assert ws["data"]["host_session"] == "cc-e2e-sess", (
        "tape 必须钉值启动时的 CLAUDE_CODE_SESSION_ID（非硬编码/非 None，§5.3）"
    )


def test_cli_bootstrap_no_env_writes_none(cwd_tmp, wf_path, monkeypatch):
    """无 env（手 CLI）→ tape host_session 为 None（fail-safe，§2.5）。"""
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    runner = CliRunner()
    reply = _bootstrap(runner, wf_path)
    tape_path = Path(reply["tape"])
    ws = json.loads(tape_path.read_text(encoding="utf-8").splitlines()[0])
    assert ws["data"]["host_session"] is None


def test_cli_bootstrap_orca_env_wins(cwd_tmp, wf_path, monkeypatch):
    """ORCA_HOST_SESSION_ID 优先于 CLAUDE_CODE_SESSION_ID（opencode plugin 注入路径）。"""
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "opencode-sess")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-sess")
    runner = CliRunner()
    reply = _bootstrap(runner, wf_path)
    tape_path = Path(reply["tape"])
    ws = json.loads(tape_path.read_text(encoding="utf-8").splitlines()[0])
    assert ws["data"]["host_session"] == "opencode-sess"


# ── cc_nudge.sh 过滤逻辑（SPEC §4.4 / §2.5 边界）─────────────────────────────

_BASH = shutil.which("bash")
_PYTHON3 = shutil.which("python3")
_NUDGE_OK = bool(_BASH) and bool(_PYTHON3)
_pytestmark_nudge = pytest.mark.skipif(
    not _NUDGE_OK,
    reason="跑 cc_nudge.sh 需要 bash + python3（Windows 原生缺；WSL / Linux / macOS 有）",
)


def _nudge_script_src() -> Path:
    """cc_nudge.sh 模板源路径（随包 templates/cc_nudge.sh）。"""
    return install_cmds._cc_nudge_script_src()


def _write_nudge(dst_dir: Path) -> Path:
    dst = dst_dir / "orca-nudge.sh"
    dst.write_text(_nudge_script_src().read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def _env(session: str | None) -> dict:
    """subprocess env：默认只设 CLAUDE_CODE_SESSION_ID（模拟 CC Stop-hook）。

    显式清 ORCA_HOST_SESSION_ID 防测试进程 env 泄漏（若 pytest 在 opencode shell.env
    注入后跑，ORCA 会抢占 CLAUDE → current 漂移，🟡 review#4）。
    """
    e = dict(os.environ)
    # 恒清 ORCA_HOST_SESSION_ID：本组测试用 CLAUDE_CODE_SESSION_ID 模拟 CC session。
    e.pop("ORCA_HOST_SESSION_ID", None)
    if session is not None:
        e["CLAUDE_CODE_SESSION_ID"] = session
    else:
        e.pop("CLAUDE_CODE_SESSION_ID", None)
    return e


def _env_with_orca(orca_session: str, claude_session: str | None = None) -> dict:
    """subprocess env：显式设 ORCA_HOST_SESSION_ID（测 cc_nudge.sh 的 ORCA > CLAUDE 优先级）。"""
    e = _env(claude_session)
    e["ORCA_HOST_SESSION_ID"] = orca_session
    return e


def _write_marker(runs: Path, run_id: str, model: str = "deepseek/d") -> None:
    (runs / f"orca-{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "model": model, "no_output_count": 0}),
        encoding="utf-8",
    )


def _write_tape(runs: Path, run_id: str, host_session: str | None,
                first_type: str = "workflow_started") -> None:
    """写 tape 首行（默认 workflow_started；可改为非 ws 测试异常 tape）。"""
    data = {"host_session": host_session}
    if first_type != "workflow_started":
        data = {"node": "x"}  # 非 workflow_started 的内容
    (runs / f"{run_id}.jsonl").write_text(
        json.dumps({"type": first_type, "data": data}) + "\n", encoding="utf-8",
    )


@_pytestmark_nudge
def test_cc_nudge_blocks_when_host_session_matches(tmp_path: Path):
    """host_session == current → block（本 session 的 run）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-mine")
    _write_tape(runs, "r-mine", host_session="sess-A")
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["decision"] == "block"
    assert "r-mine" in payload["reason"]


@_pytestmark_nudge
def test_cc_nudge_skips_when_host_session_differs(tmp_path: Path):
    """host_session != current → 跳过（别的 session 的 run，不 block，§2.5）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-theirs")
    _write_tape(runs, "r-theirs", host_session="sess-B")
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    # 不 block（r-theirs 属于 sess-B，不属于 sess-A）→ 无 stdout
    assert proc.stdout.strip() == "", "别的 session 的 run 不应触发 block"


@_pytestmark_nudge
def test_cc_nudge_skips_when_host_session_none_in_tape(tmp_path: Path):
    """tape host_session 为 None（手 CLI 起）→ 跳过（无法证明归属，§2.5）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-manual")
    _write_tape(runs, "r-manual", host_session=None)
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "手 CLI 起 run（host_session=null）不应被任何 session nudge"


@_pytestmark_nudge
def test_cc_nudge_tape_first_line_not_workflow_started(tmp_path: Path):
    """tape 首行非 workflow_started（异常 tape）→ host_session None → 跳过（§6.4 fail-safe）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-weird")
    _write_tape(runs, "r-weird", host_session="sess-A", first_type="node_started")
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    # 首行非 ws → _host_session_from_tape 返 None → 不等 current → 跳过
    assert proc.stdout.strip() == ""


@_pytestmark_nudge
def test_cc_nudge_tape_missing_returns_none(tmp_path: Path):
    """tape 文件不存在（marker 孤儿）→ _host_session_from_tape fail-safe None → 跳过。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-orphan")  # 有 marker 无 tape
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "孤儿 marker（无 tape）不应 block"


@_pytestmark_nudge
def test_cc_nudge_per_session_throttle_separate_keys(tmp_path: Path):
    """per-session 限流分键：A nudge 后 B 仍被提醒自己的 run（§2.4 / 评审 C1）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    # A 和 B 各有一个 run（归属各自的 session）
    _write_marker(runs, "r-a")
    _write_tape(runs, "r-a", host_session="sess-A")
    _write_marker(runs, "r-b")
    _write_tape(runs, "r-b", host_session="sess-B")
    script = _write_nudge(tmp_path)

    # A nudge（写 sess-A 的 throttle 文件）
    proc_a = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc_a.returncode == 0
    assert json.loads(proc_a.stdout.strip())["decision"] == "block"
    # A 的 throttle 文件存在
    assert (runs / ".orca-nudge-cc-sess-A").is_file(), "per-session 限流文件按 session 分键"

    # B 紧接着 nudge（A 的 throttle 不应抑制 B）
    proc_b = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-B"),
    )
    assert proc_b.returncode == 0
    payload_b = json.loads(proc_b.stdout.strip())
    assert payload_b["decision"] == "block", "B 不应被 A 的限流抑制（per-session 分键，§2.4）"
    assert "r-b" in payload_b["reason"]
    # B 的 throttle 文件独立于 A
    assert (runs / ".orca-nudge-cc-sess-B").is_file()


@_pytestmark_nudge
def test_cc_nudge_no_env_with_active_marker_warns(tmp_path: Path):
    """current=None + 有活跃 marker → stderr warn（区分手 CLI 与 env 注入 bug，评审 C10）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-x")
    _write_tape(runs, "r-x", host_session="sess-some")
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env(None),
    )
    assert proc.returncode == 0, "无 env 不 fail（手 CLI 是合法用法）"
    assert proc.stdout.strip() == "", "无 env 不 block"
    assert proc.stderr, "无 env + 活跃 marker 必须 warn 到 stderr"
    assert "host session" in proc.stderr.lower() or "session" in proc.stderr.lower()


@_pytestmark_nudge
def test_cc_nudge_no_env_no_marker_silent(tmp_path: Path):
    """current=None + 无 marker → 静默放行（无 warn，无 block）。"""
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env(None),
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert proc.stderr.strip() == "", "无 marker 时不应 warn"


@_pytestmark_nudge
def test_cc_nudge_mixed_sessions_only_nudges_own(tmp_path: Path):
    """多 session 共存：A 的 run + B 的 run 同时活跃，sess-A idle 只 block A 的（§5.1）。"""
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-a")
    _write_tape(runs, "r-a", host_session="sess-A")
    _write_marker(runs, "r-b")
    _write_tape(runs, "r-b", host_session="sess-B")
    script = _write_nudge(tmp_path)

    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["decision"] == "block"
    assert "r-a" in payload["reason"]
    assert "r-b" not in payload["reason"], "sess-A 的 nudge 不应提及 sess-B 的 run（防串台）"


@_pytestmark_nudge
def test_cc_nudge_orca_env_priority_over_claude(tmp_path: Path):
    """cc_nudge.sh 的 _host_session_from_env：ORCA_HOST_SESSION_ID 优先于 CLAUDE_CODE_SESSION_ID。

    两份 _host_session_from_env（cli.py / cc_nudge.sh）DRY 漂移闸门——改一忘另一会被此测抓
    （🟡 coverage review#3）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-orca")
    _write_tape(runs, "r-orca", host_session="orca-sess")
    script = _write_nudge(tmp_path)
    # 同时设 ORCA + CLAUDE，tape 钉 ORCA → 应 block（证明 cc_nudge.sh 取 ORCA 而非 CLAUDE）
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
        env=_env_with_orca("orca-sess", claude_session="cc-sess"),
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["decision"] == "block", "cc_nudge.sh 应取 ORCA_HOST_SESSION_ID（高优先）"
    assert "r-orca" in payload["reason"]


@_pytestmark_nudge
def test_cc_nudge_tape_first_line_corrupt_json(tmp_path: Path):
    """tape 首行是损坏 JSON（非合法）→ _host_session_from_tape 的 except JSONDecodeError → None → 跳过。

    区别于 test_cc_nudge_tape_first_line_not_workflow_started（首行合法 JSON 但 type 错，走 break）：
    本测走 except 分支（🟢 coverage review#3 / impl review🟢#1）。
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_marker(runs, "r-corrupt")
    (runs / "r-corrupt.jsonl").write_text("{not valid json at all\n", encoding="utf-8")
    script = _write_nudge(tmp_path)
    proc = subprocess.run(
        ["bash", str(script)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10, env=_env("sess-A"),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", "tape 首行损坏 JSON → host_session None → 跳过"


# ── §5.6 作用域回归：next 不改写 host_session（coverage review 🟡#2）──────────


def test_cli_next_does_not_rewrite_host_session(cwd_tmp, wf_path, monkeypatch):
    """SPEC §5.6：next/status/open/stop 行为不变——host_session 仅作用 nudge，不改写。

    bootstrap 写 host_session → 跑 next（带 output）→ tape 中 workflow_started 条数仍为 1，
    且 host_session 值不变（next 路径不重发 ws，§4.1 emit 真链）。
    锁「next 不传 host_session 给 advance_step」的代码结构契约（防有人误加参数）。
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cc-scope-sess")
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    runner = CliRunner()
    boot = _bootstrap(runner, wf_path)
    run_id, tape = boot["run_id"], boot["tape"]

    # 跑 next（推进到节点 b）
    nxt = runner.invoke(app, ["next", "--run-id", run_id, "--output", "out_a"])
    assert nxt.exit_code == 0, nxt.output

    lines = Path(tape).read_text(encoding="utf-8").strip().split("\n")
    ws_events = [json.loads(ln) for ln in lines if json.loads(ln)["type"] == "workflow_started"]
    assert len(ws_events) == 1, "next 不应新增 workflow_started（host_session 只在 bootstrap 写一次）"
    assert ws_events[0]["data"]["host_session"] == "cc-scope-sess", (
        "next 路径不应改写 host_session（§5.6 作用域：host_session 仅作用 nudge）"
    )


# ── orca.ts 结构守门（coverage review 🟡#1：项目无 TS 测基础设施，结构 grep 兜底）──


def test_orca_ts_has_host_session_binding_hooks():
    """orca.ts 结构守门：host_session 绑定的关键元素存在（shell.env 注入 + tape 过滤 + per-session 限流）。

    项目无 jest/vitest（纯 Python pytest），orca.ts 行为零测是 known gap（SPEC §5.8 非阻塞）。
    本测只锁结构（grep 关键符号存在），防重构误删；行为正确性靠 cc_nudge.sh 同源逻辑的 21 测 +
    test-agent E2E 兜底。
    """
    plugin = Path(__file__).resolve().parents[3] / "orca/iface/in_session/templates/opencode/orca.ts"
    text = plugin.read_text(encoding="utf-8")
    # shell.env 钩子注入 ORCA_HOST_SESSION_ID（§4.5 注入可行性）
    assert '"shell.env"' in text, "shell.env 钩子必须存在（注入 ORCA_HOST_SESSION_ID）"
    assert "ORCA_HOST_SESSION_ID" in text
    # tape 首行读 host_session（§4.5 tape-only 派生）
    assert "function hostSessionOfRun" in text
    assert "workflow_started" in text
    # listActiveRuns 按 hostSession 过滤 + fail-open 回退（C5 静默死防护）
    assert "function listActiveRuns(hostSession: string)" in text
    assert "fail-open" in text.lower() or "hasAnyReal" in text, "fail-open 回退逻辑必须存在（C5 防护）"
    # per-session 限流分键（§2.4）
    assert "function nudgeFile(sessionID: string)" in text
    assert "${sessionID}" in text
    # Marker interface 不加 host_session（tape-only 铁律，§4.5）
    marker_block = text[text.index("interface Marker"):text.index("interface Marker") + 200]
    assert "host_session" not in marker_block, "Marker interface 不应加 host_session（tape-only）"
