"""tests/iface/in_session/test_v3_step1.py —— SPEC v3 §8 step 1 验收守门。

覆盖 §11 验收相关项：
  - **保留字黑名单**（§2.2 MS1）：wf 取 ``status``/``next``/``teams`` 等 → compile fail loud。
  - **``orca <wf>`` 语法糖**（§2.1）：未注册的首 token = wf 名 → bootstrap 派发。
  - **``orca --help``**（§2.4）：含 7 命令（list/next/status/stop/open/doctor + <wf> epilog），
    不含 teams 命令名（run/serve/ps/...）。
  - **驱动协议 B1**（§8）：prompt 含 ``orca next``、不含 ``orca in-session``。
  - **marker 3 字段**（§7.2）：grep dataclass 无 tape_path/yaml/session_id/owner。
  - **catalog 单一实现**（§3.1 / coordinator 铁律）：orca list 委托 commands.run_list。
  - **teams 命令名变量化**（§3.2）：ORCA_BACKEND_CMD env 控制 backend_cmd_name 显示。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from orca.compile import ConfigurationError, load_workflow
from orca.iface.in_session import marker as marker_mod
from orca.iface.in_session.cli import _drive_protocol, app


# ── §2.2 保留字黑名单（MS1）─────────────────────────────────────────────────


RESERVED_WF_YAML_TMPL = """\
name: {name}
description: wf 取保留名（应被 compile 拒）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "x"
    routes:
      - to: $end
"""


@pytest.mark.parametrize("reserved", [
    "status", "next", "list", "stop", "open", "doctor",  # orca 7 命令
    "run", "serve", "ps", "logs", "wait", "resume",      # teams 后端命令
    "install", "validate", "mcp", "executor",
    "teams",  # ORCA_BACKEND_CMD 默认值
    "bootstrap",
])
def test_reserved_wf_name_rejected_at_compile(tmp_path, reserved):
    """§2.2：wf.name 取保留字 → ConfigurationError（compile fail loud）。"""
    p = tmp_path / "wf.yaml"
    p.write_text(RESERVED_WF_YAML_TMPL.format(name=reserved), encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc:
        load_workflow(p)
    assert "保留字" in str(exc.value)
    assert reserved in str(exc.value)


def test_non_reserved_wf_name_accepted(tmp_path):
    """非保留字 wf 名正常通过 compile。"""
    p = tmp_path / "wf.yaml"
    p.write_text(RESERVED_WF_YAML_TMPL.format(name="my_writer_wf"), encoding="utf-8")
    wf = load_workflow(p)
    assert wf.name == "my_writer_wf"


# ── §2.1 ``orca <wf>`` 语法糖 ────────────────────────────────────────────────


SIMPLE_WF_YAML = """\
name: sugar_test_wf
description: 语法糖测试 wf。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "node A"
    routes:
      - to: $end
"""


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_wf_sugar_dispatches_to_bootstrap(cwd_tmp):
    """``orca <wf-path>`` ≡ ``orca bootstrap <wf-path>``（首 token 非注册命令 → 重写）。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()

    # 裸 wf 路径（语法糖）
    sugar = runner.invoke(app, [str(p)])
    assert sugar.exit_code == 0, sugar.output
    sugar_reply = json.loads(sugar.output.splitlines()[-1])

    # 显式 bootstrap
    explicit = runner.invoke(app, ["bootstrap", str(p)])
    # 第二次 bootstrap 同 wf → duplicate-active-run fail loud（§7.3 m12）
    assert explicit.exit_code == 1
    dup = json.loads(explicit.output.splitlines()[-1])
    assert dup["reason"] == "duplicate-active-run"
    assert dup["run_id"] == sugar_reply["run_id"]


def test_wf_sugar_preserves_inputs_option(cwd_tmp):
    """``orca <wf> --inputs '{...}'`` 把 --inputs 透传到 bootstrap。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, [str(p), "--inputs", '{"x":1}'])
    assert result.exit_code == 0, result.output
    reply = json.loads(result.output.splitlines()[-1])
    assert reply["done"] is False
    assert reply["node"] == "a"


def test_reserved_command_name_not_treated_as_wf(cwd_tmp):
    """``orca status`` 走 status 命令（注册命令优先），不当 wf 名 bootstrap。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    runner.invoke(app, [str(p)])  # bootstrap 一个 run
    # status 是注册命令 → 走 status（列 run），不重写为 bootstrap
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    # status 列表输出含 tape stem（run_id）
    assert ".jsonl" not in result.output


# ── §2.4 ``orca --help`` 含 7 命令、不含 teams ─────────────────────────────


def test_orca_help_lists_seven_commands_no_teams():
    """§2.4：--help 含 list/next/status/stop/open/doctor + <wf> epilog；不含 teams 命令名。"""
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = result.output

    # 7 命令（注册的 6 + <wf> epilog 提及）
    for cmd in ("list", "next", "status", "stop", "open", "doctor"):
        assert cmd in help_text, f"--help 缺命令 {cmd!r}"
    # <wf> 语法糖在 epilog 提及
    assert "wf" in help_text.lower()

    # 不含 teams 后端命令名（§2.4 守门）
    for teams_cmd in ("run", "serve", "ps", "logs", "wait", "resume",
                       "install", "validate", "mcp", "executor"):
        # 命令名作独立 token 出现（避免 substring 误判，如 "run" in "runtimes"）
        # typer help 列命令名缩进，用 "  run" / "\nrun" 作粗略独立行匹配
        assert f"\n{teams_cmd}" not in help_text and f"  {teams_cmd} " not in help_text, (
            f"--help 不应含 teams 命令 {teams_cmd!r}（§2.4 守门）"
        )


# ── §8 B1：驱动协议 + marker 3 字段 + catalog 单一 ────────────────────────────


def test_drive_protocol_uses_top_level_next_command():
    """B1：驱动协议含 ``orca next``，不含旧 ``orca in-session next`` namespace。"""
    text = _drive_protocol("r-test-123")
    assert "orca next" in text
    assert "orca in-session" not in text
    assert "--run-id r-test-123" in text  # run_id 注入


def test_drive_protocol_documents_single_quote_escaping():
    """§5.2（M7）：驱动协议教模型 ``'\''`` 转义撇号（单个撇号就破 quoting）。"""
    text = _drive_protocol("r-x")
    assert "\\'\\''" in text or "it's" in text  # 转义示例存在


def test_activation_marker_exactly_three_fields():
    """§7.2：ActivationMarker dataclass 字段集 = {run_id, model, no_output_count}。

    coordinator 铁律：marker 必须只 3 字段（无 tape_path/yaml/session_id/owner）。
    """
    fields = set(marker_mod.ActivationMarker.__dataclass_fields__.keys())
    assert fields == {"run_id", "model", "no_output_count"}, (
        f"marker 字段漂移：实得 {fields}"
    )


def test_marker_module_has_no_find_scan():
    """§7.2：删 ``find_marker_by_run_id`` 扫描（改 ``marker_path`` O(1) 直定位）。"""
    assert not hasattr(marker_mod, "find_marker_by_run_id"), (
        "marker 模块不应再有 find_marker_by_run_id（v3 §7.2 删扫描）"
    )


def test_orca_list_delegates_to_single_catalog_impl(cwd_tmp, monkeypatch):
    """§3.1 / coordinator 铁律：orca list 委托 commands.run_list（单一 list 逻辑）。"""
    p = cwd_tmp / "workflows" / "w.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    with mock.patch("orca.iface.cli.commands.run_list") as m:
        m.return_value = None
        result = runner.invoke(app, ["list"])
    assert result.exit_code == 0
    m.assert_called_once()  # orca list 走 commands.run_list（单一实现）


# ── §3.2 teams 命令名变量化（env ORCA_BACKEND_CMD）──────────────────────────


def test_backend_cmd_name_reads_env():
    """§3.2：``ORCA_BACKEND_CMD`` env 控制 backend 命令名（默认 teams）。"""
    from orca.iface.cli.commands import DEFAULT_BACKEND_CMD, backend_cmd_name

    assert DEFAULT_BACKEND_CMD == "teams"
    # 默认（env 未设）
    monkeypatch_env = {"ORCA_BACKEND_CMD": "conductor"}
    with mock.patch.dict("os.environ", monkeypatch_env, clear=False):
        assert backend_cmd_name() == "conductor"
    # env 未设 → 默认 teams
    with mock.patch.dict("os.environ", {}, clear=True):
        assert backend_cmd_name() == "teams"


# ── review 补缺：并发 bootstrap / yaml_path 恢复 / open 默认 / state_corrupt ─────


def test_bootstrap_uses_well_known_serialize_lock(cwd_tmp):
    """review B1：bootstrap 锁用 well-known 路径（``.orca-bootstrap.lock``），NOT per-run_id。

    per-run_id 锁无法 serialize 同 wf 并发（两进程各 gen 不同 run_id → 各锁不同文件 →
    都过 dupe check → 孤儿）。本测试验证 well-known 锁文件落盘（独立于 run_id），保
    TOCTOU 闭环的契约：锁资源选对。
    """
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p)])
    assert result.exit_code == 0, result.output

    # well-known bootstrap serialize lock 必须落盘（rundir = cwd/runs/）
    lock_file = cwd_tmp / "runs" / ".orca-bootstrap.lock"
    assert lock_file.is_file(), (
        "bootstrap 必须用 well-known `.orca-bootstrap.lock` serialize（review B1），"
        "per-run_id 锁无法防同 wf 并发 TOCTOU"
    )
    # 不应残留 per-run_id 的 marker-level .flock（旧错误设计）
    bad_locks = list((cwd_tmp / "runs").glob("orca-*.json.flock"))
    assert not bad_locks, (
        f"不应有 per-run_id `.json.flock`（TOCTOU 漏洞设计）：{bad_locks}"
    )


def test_next_recovers_wf_from_tape_yaml_path(cwd_tmp, tmp_path):
    """review §7.2：``next`` 从 tape.workflow_started.data.yaml_path 恢复 wf（marker 不存 yaml）。

    bootstrap 把 yaml_path 记入 tape；next 读 tape 反查 → load_workflow。验证非 catalog
    路径（tmp_path 下的 yaml）也能 next 推进。
    """
    p = tmp_path / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    boot = runner.invoke(app, ["bootstrap", str(p)])
    assert boot.exit_code == 0
    reply = json.loads(boot.output.splitlines()[-1])
    run_id, tape = reply["run_id"], reply["tape"]

    # tape workflow_started.data.yaml_path 应记入（canonical realpath）
    import json as _json
    ws = _json.loads(Path(tape).read_text(encoding="utf-8").splitlines()[0])
    assert ws["type"] == "workflow_started"
    assert ws["data"]["yaml_path"]

    # next 推进（wf 从 tape yaml_path 恢复，非 catalog）
    nxt = runner.invoke(app, ["next", "--run-id", run_id, "--output", "out_a"])
    assert nxt.exit_code == 0
    nxt_reply = _json.loads(nxt.output.splitlines()[-1])
    assert nxt_reply["done"] is True  # 单节点 wf，output 后即 completed


def test_default_active_run_id_no_active_fails_loud(cwd_tmp):
    """review：``orca open`` 无活跃 run → fail loud（exit 1）。cwd_tmp 确保 runs/ 为空。"""
    import typer
    from orca.iface.in_session.cli import _default_active_run_id

    with pytest.raises(typer.Exit) as exc:
        _default_active_run_id()
    assert exc.value.exit_code == 1


def test_default_active_run_id_multiple_active_fails_loud(cwd_tmp):
    """review：多个活跃 run → fail loud（提示指定 run_id）。"""
    import typer
    from orca.iface.in_session.cli import _default_active_run_id, _default_rundir
    from orca.iface.in_session.marker import marker_path, write_marker, ActivationMarker

    rundir = _default_rundir()
    rundir.mkdir(parents=True, exist_ok=True)
    for rid in ("r-a", "r-b"):
        write_marker(marker_path(rundir, rid), ActivationMarker(run_id=rid))
    try:
        with pytest.raises(typer.Exit) as exc:
            _default_active_run_id()
        assert exc.value.exit_code == 1
    finally:
        # 清理：删测试 marker，不污染其他测试。
        for rid in ("r-a", "r-b"):
            (rundir / f"orca-{rid}.json").unlink(missing_ok=True)


def test_default_active_run_id_single_returns_it(cwd_tmp):
    """review：恰好一个活跃 run → 返它（``orca open`` 默认目标）。"""
    from orca.iface.in_session.cli import _default_active_run_id, _default_rundir
    from orca.iface.in_session.marker import marker_path, write_marker, ActivationMarker

    rundir = _default_rundir()
    rundir.mkdir(parents=True, exist_ok=True)
    write_marker(marker_path(rundir, "r-only"), ActivationMarker(run_id="r-only"))
    try:
        assert _default_active_run_id() == "r-only"
    finally:
        (rundir / "orca-r-only.json").unlink(missing_ok=True)


def test_load_wf_for_run_state_corrupt_on_missing_workflow_started(cwd_tmp, tmp_path):
    """review：tape 无 workflow_started → _load_wf_for_run raise state_corrupt。"""
    from orca.iface.in_session.cli import _load_wf_for_run
    from orca.run.step import InSessionError, ERR_STATE_CORRUPT
    from orca.events.tape import Tape

    # 空 tape（无 workflow_started）
    empty_tape = tmp_path / "empty.jsonl"
    empty_tape.write_text("", encoding="utf-8")
    tape = Tape(empty_tape, run_id="r-x", resume=True)
    with pytest.raises(InSessionError) as exc:
        _load_wf_for_run("r-x", tape)
    assert exc.value.error_kind == ERR_STATE_CORRUPT


def test_start_deprecation_warning_to_stderr(cwd_tmp):
    """review：``start`` 标 deprecated，warn 出现（v3 §8 step 1 不删，step 2b 删）。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["start", str(p)])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower()


def test_bootstrap_bad_inputs_fails_loud(cwd_tmp):
    """review：``--inputs`` 非 JSON → BadParameter exit 2（fail loud）。"""
    p = cwd_tmp / "wf.yaml"
    p.write_text(SIMPLE_WF_YAML, encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", str(p), "--inputs", "{not json"])
    assert result.exit_code == 2


def test_bootstrap_unresolvable_wf_name_fails_loud(cwd_tmp):
    """review：wf 名既非路径也不在 catalog → BadParameter exit 2。"""
    runner = CliRunner()
    result = runner.invoke(app, ["bootstrap", "no_such_wf_anywhere_xyz"])
    assert result.exit_code == 2

