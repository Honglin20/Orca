"""tests/iface/in_session/test_resolve_artifacts_dir_integration.py —— 端到端集成测试。

回答「project-scoped artifacts 的 in-session bootstrap 接线**真**的工作吗？」——不只是
helper ``_resolve_artifacts_dir`` 自洽（已由 ``test_resolve_artifacts_dir.py`` 15 个单测覆盖），
而是真实 ``orca bootstrap`` CLI 路径**真的**把 ``$ORCA_ARTIFACTS_DIR`` 写进 ``runs/<run_id>/orca_env.sh``，
且磁盘上**真的** ``mkdir`` 了对应目录（SPEC 2026-08-06 §2.1 三条契约）。

**Mode A：真实执行 through public surface**（非"跑既有单测"）：
  - surface = typer ``CliRunner`` 驱动真实 ``orca bootstrap`` 命令（用户视角的入口）。
  - 真实 ``advance_step`` / ``Tape`` / ``EventBus`` / ``write_marker`` 全跑；真实 ``disk mkdir``
    + 真实 ``env file`` 写入；真实 stdout JSON 契约。
  - 仅在 **subprocess 边界** stub（chart/sidechain/open_web detached spawn + socket wait）——
    这些是合法 outermost-edge mock：它们 spawn 真子进程（脱离测试 lifecycle），与 ARTIFACTS_DIR
    接线零相关；ARTIFACTS_DIR 接线本身的代码（``_resolve_artifacts_dir`` / ``_write_orca_env`` /
    ``artifacts_dir.mkdir``）**全跑真实代码**，未 mock。

覆盖三条契约：
  1. **project-scoped 正路径**：workflow inputs 含绝对 ``project_root`` →
     ``<proj>/artifacts/<wf>/`` **真在磁盘 mkdir** + env 文件**真写**绝对路径。
  2. **相对路径 fail loud**：``project_root="rel/path"`` → exit 1 + 结构化错误信封
     (``error_kind=invalid_inputs``) + 不留 orphan marker（``clear_marker`` 真生效）。
  3. **per-run 回落**：workflow 无 ``project_root`` input → env 文件写 per-run 路径
     (``runs/<run_id>/artifacts/``)，回归未破。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session import cli as cli_mod
from orca.iface.in_session.cli import app

# ── 最小 workflow fixtures ───────────────────────────────────────────────────
#
# 真实 ``nas-supernet.yaml`` 有 4 个 required inputs（model_path / target_latency_ms 等），
# 满足全部 input 校验拖累测试聚焦点。这里造最小 wf：仅 ``project_root`` 一个 required input
# （或零 input），其余字段最小可启动（1 agent 节点 + 路由 $end）。compile 层 ``load_workflow``
# 全跑真 schema 校验， wf 形态合法 = ``orca <wf>`` 真实启动路径。

_WF_WITH_PROJECT_ROOT_YAML = """\
name: {name}
description: 最小 workflow，含 project_root input（project-scoped artifacts 接线测试用）。
entry: a
inputs:
  project_root:
    type: string
    description: "[ask] 用户项目根绝对路径。project-scoped artifacts 锚点（非绝对 → bootstrap fail loud）。"
    required: true
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 A。"
    routes:
      - to: $end
"""

_WF_NO_INPUTS_YAML = """\
name: {name}
description: 最小 workflow，零 input（per-run artifacts 回落测试用）。
entry: a
nodes:
  - name: a
    kind: agent
    executor: opencode
    model: deepseek/deepseek-v4-flash
    prompt: "产出 A。"
    routes:
      - to: $end
"""


@pytest.fixture
def cwd_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """bootstrap 在 CWD 下建 ``runs/``；chdir 到 tmp_path 隔离磁盘副作用。

    bootstrap 的 tape 路径是 ``runs/<run_id>.jsonl``（相对 CWD，见 ``default_tape_path``）；
    env 文件 = ``runs/<run_id>/orca_env.sh``；marker = ``runs/orca-<run_id>.json``。chdir 到
    tmp_path 后所有这些落点都在测试隔离目录里，测试结束 pytest 自动清。
    """
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def wf_project_root(tmp_path: Path) -> Path:
    """带 ``project_root`` input 的最小 wf（project-scoped 正路径 / 相对 fail loud 共用）。"""
    p = tmp_path / "wf_project_root.yaml"
    p.write_text(
        _WF_WITH_PROJECT_ROOT_YAML.format(name="proj-scoped-wf"),
        encoding="utf-8",
    )
    return p


@pytest.fixture
def wf_no_inputs(tmp_path: Path) -> Path:
    """零 input 的最小 wf（per-run 回落测试用）。"""
    p = tmp_path / "wf_no_inputs.yaml"
    p.write_text(
        _WF_NO_INPUTS_YAML.format(name="per-run-wf"),
        encoding="utf-8",
    )
    return p


def _stub_daemon_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """stub **subprocess 边界**的 daemon spawn（合法 outermost-edge mock）。

    被测对象是 ``_resolve_artifacts_dir`` 在 bootstrap 调用点的真实输出（env 文件内容 +
    磁盘 mkdir），其代码路径**零 subprocess**。stub 仅为隔离 detached 守护进程脱离测试 lifecycle
    + 加快测试（与 ``test_bootstrap_open_web.py`` 同模式，DRY）。

    未 stub 的真实代码路径（测试**真验证**这些）：
      - ``_resolve_artifacts_dir``（被测核心，从 tape 读 wf_name + inputs 派生路径）。
      - ``artifacts_dir.mkdir(parents=True, exist_ok=True)``（真 disk mkdir）。
      - ``_write_orca_env``（真写 env 文件 + 真拼 ``export ORCA_ARTIFACTS_DIR=`` 字面值）。
      - ``_advance_and_emit`` / ``Tape`` / ``EventBus`` / ``write_marker`` / ``clear_marker``
        （bootstrap 全真跑，tape 真有 ``workflow_started`` 事件）。
    """
    monkeypatch.setattr(cli_mod, "_spawn_chart_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_spawn_sidechain_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_wait_for_sock", lambda *a, **kw: True)
    monkeypatch.setattr(cli_mod, "_spawn_open_web", lambda *a, **kw: None)
    monkeypatch.setattr(cli_mod, "_WEB_READY_TIMEOUT", 0.01)


def _parse_stdout_json(result) -> dict:
    """bootstrap stdout 末行是 JSON 信封（前可能有 stderr 行 / log）。"""
    assert result.exit_code is not None, "CliRunner 未执行"
    last_line = result.output.splitlines()[-1]
    return json.loads(last_line)


def _env_file_path(run_id: str) -> Path:
    """``runs/<run_id>/orca_env.sh``（bootstrap 真实落点，CWD 相对）。"""
    return Path("runs") / run_id / "orca_env.sh"


# ── 契约 1：project-scoped 正路径 ─────────────────────────────────────────────


def test_project_scoped_bootstrap_mkdirs_and_writes_env(
    cwd_tmp: Path, wf_project_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """绝对 ``project_root`` → ``<proj>/artifacts/<wf>/`` 真磁盘 mkdir + env 文件真写绝对路径。

    断言三层（每层都是 user-observable 的真实结果，非内部 mock 断言）：
      (a) bootstrap exit 0 + stdout JSON schema 含 ``run_id``（bootstrap 成功）。
      (b) ``<project_root>/artifacts/<wf_name>/`` **真在磁盘存在**（``Path.is_dir()`` 真 check）。
      (c) ``runs/<run_id>/orca_env.sh`` **真存在** 且内容含字面
          ``export ORCA_ARTIFACTS_DIR=<abs_proj>/artifacts/<wf_name>/``。
    """
    _stub_daemon_subprocesses(monkeypatch)

    # project_root 用 tmp_path 下的子目录（绝对路径，bootstrap 真会去 mkdir）。
    proj_root = cwd_tmp / "user-pytorch-project"
    proj_root.mkdir()
    assert proj_root.is_absolute()  # invariant：tmp_path 是 pytest 给的绝对路径

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", str(wf_project_root),
         "--inputs", json.dumps({"project_root": str(proj_root)})],
    )
    assert result.exit_code == 0, f"bootstrap 应成功，stdout={result.output}"

    reply = _parse_stdout_json(result)
    assert "run_id" in reply, f"stdout JSON 缺 run_id：{reply}"
    run_id = reply["run_id"]

    # (b) 磁盘：project-scoped 目录真的 mkdir 了。
    expected_artifacts_dir = proj_root / "artifacts" / "proj-scoped-wf"
    assert expected_artifacts_dir.is_dir(), (
        f"project-scoped artifacts 目录未在磁盘创建：{expected_artifacts_dir}"
        f"（proj_root={proj_root}, run_id={run_id}）"
    )

    # (c) env 文件内容：真的写了 export ORCA_ARTIFACTS_DIR=<abs>。
    env_path = _env_file_path(run_id)
    assert env_path.is_file(), f"env 文件未真写到磁盘：{env_path}"
    env_content = env_path.read_text(encoding="utf-8")

    # 字面契约：字面值 = 解析后的绝对路径（与 _resolve_artifacts_dir 返回值一致）。
    expected_literal = (
        f"export ORCA_ARTIFACTS_DIR={expected_artifacts_dir.resolve()}"
    )
    assert expected_literal in env_content, (
        f"env 文件未含预期字面值：\n  期望：{expected_literal}\n  实际内容：\n{env_content}"
    )

    # 强约束：ORCA_ARTIFACTS_DIR 不能落到 per-run 路径（防接线回归到 per-run）。
    per_run_pattern = f"runs/{run_id}/artifacts"
    artifacts_lines = [
        line for line in env_content.splitlines()
        if line.startswith("export ORCA_ARTIFACTS_DIR=")
    ]
    assert len(artifacts_lines) == 1, f"env 文件应恰好一行 ORCA_ARTIFACTS_DIR：\n{env_content}"
    assert per_run_pattern not in artifacts_lines[0], (
        f"project-scoped 路径下 ORCA_ARTIFACTS_DIR 误落 per-run：{artifacts_lines[0]}"
    )


def test_project_scoped_env_value_is_absolute_resolved_path(
    cwd_tmp: Path, wf_project_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """ORCA_ARTIFACTS_DIR 字面值是 ``Path.resolve()`` 后的绝对路径（防 ``../`` 残留 / 相对漂移）。

    SPEC 2026-08-06 §2.1：``_resolve_artifacts_dir`` 返回 ``(p / 'artifacts' / wf_name).resolve()``。
    env 文件由 ``_write_orca_env`` 用 ``shlex.quote(str(artifacts_dir))`` 字面写入。
    本测试钉死：字面值必须以 ``/`` 开头（POSIX 绝对）且与 resolve() 后路径逐字相等。
    """
    _stub_daemon_subprocesses(monkeypatch)
    proj_root = cwd_tmp / "another-proj"
    proj_root.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", str(wf_project_root),
         "--inputs", json.dumps({"project_root": str(proj_root)})],
    )
    assert result.exit_code == 0, result.output
    run_id = _parse_stdout_json(result)["run_id"]

    env_content = _env_file_path(run_id).read_text(encoding="utf-8")
    m = re.search(r"^export ORCA_ARTIFACTS_DIR=(.+)$", env_content, re.MULTILINE)
    assert m, f"找不到 ORCA_ARTIFACTS_DIR 行：\n{env_content}"
    literal_value = m.group(1).strip().strip("'\"")

    # 绝对路径（POSIX ``/`` 起或 Windows drive letter；resolve 后必为绝对）。
    assert Path(literal_value).is_absolute(), (
        f"ORCA_ARTIFACTS_DIR 字面值非绝对路径：{literal_value!r}"
    )
    # 与 resolve() 后预期路径逐字相等（防 ``foo/../bar`` 残留）。
    expected = (proj_root / "artifacts" / "proj-scoped-wf").resolve()
    assert Path(literal_value) == expected, (
        f"ORCA_ARTIFACTS_DIR 字面值与预期 resolve() 路径不符："
        f"\n  实际：{literal_value}\n  预期：{expected}"
    )


# ── 契约 2：相对路径 fail loud ────────────────────────────────────────────────


def test_relative_project_root_fails_loud_with_structured_envelope(
    cwd_tmp: Path, wf_project_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """``project_root="rel/path"`` → bootstrap exit 1 + 结构化错误信封 (invalid_inputs)。

    SPEC §2.1 + 闭环 1cb377f：``_resolve_artifacts_dir`` 在 marker 已写、bootstrap_lock 已释放
    之后调；``project_root`` 非绝对 raise ``ValueError``。bootstrap 接住 → ``clear_marker`` +
    emit ``workflow_failed`` (kind=invalid_inputs) + JSON 错误信封 + ``Exit(1)``。

    断言：
      (a) exit_code == 1（非 0）。
      (b) stdout 末行是 JSON，含 ``error_kind == "invalid_inputs"`` + ``reason == "invalid-inputs"``。
      (c) 不留 orphan marker：``runs/orca-*.json`` 不存在（``clear_marker`` 真生效）。
      (d) tape 已写 ``workflow_started``（fail loud 发生在 ws 之后，marker 已写之后）—— 故 tape 文件存在。
    """
    _stub_daemon_subprocesses(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", str(wf_project_root),
         "--inputs", json.dumps({"project_root": "relative/proj/path"})],
    )

    # (a) 非 0 退出。
    assert result.exit_code == 1, (
        f"相对 project_root 应 fail loud exit 1，实际 exit={result.exit_code}"
        f"，stdout={result.output}"
    )

    # (b) 结构化错误信封：error_kind=invalid_inputs + reason=invalid-inputs。
    body = _parse_stdout_json(result)
    assert body.get("error_kind") == "invalid_inputs", (
        f"error_kind 应为 invalid_inputs，实际：{body}"
    )
    assert body.get("reason") == "invalid-inputs", (
        f"reason 应为 invalid-inputs，实际：{body}"
    )
    # hint 含「project_root 必须绝对路径」（用户能据 hint 修复）。
    hint = body.get("hint", "")
    assert "绝对路径" in hint, (
        f"hint 应含修复指引「绝对路径」，实际 hint={hint!r}"
    )

    # (c) 不留 orphan marker：``runs/orca-*.json`` 应被 clear_marker 清掉。
    runs_dir = cwd_tmp / "runs"
    if runs_dir.is_dir():
        orphan_markers = list(runs_dir.glob("orca-*.json"))
        assert orphan_markers == [], (
            f"相对路径失败应 clear_marker 不留 orphan，但发现：{orphan_markers}"
        )

    # (d) tape 文件存在（workflow_started 已落 tape，fail 不删 tape）。
    tape_files = list(runs_dir.glob("*.jsonl")) if runs_dir.is_dir() else []
    assert len(tape_files) == 1, (
        f"应恰好有 1 个 tape 文件（fail 不删 tape），实际：{tape_files}"
    )
    tape_text = tape_files[0].read_text(encoding="utf-8")
    assert "workflow_started" in tape_text, "tape 应含 workflow_started"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "已知 BUG（2026-08-06 端到端集成测试发现，未修）：bootstrap ``_resolve_artifacts_dir`` "
        "ValueError 路径在 ``bus`` 已被 inner finally 关闭后调 ``_emit_workflow_failed(bus, ...)`` → "
        "``RuntimeError: Tape 已 close``，emit 静默失败（被 ``_emit_workflow_failed`` 的 except 吞）。"
        "用户可见 stdout 信封仍正确（独立 typer.echo），marker 也正确清，但 tape **缺 ``workflow_failed`` "
        "终态事件** → 违反「tape 唯一真相源」契约（raw tape / 直接读 tape 的消费者看不到终态）。"
        "复现：见 cli.py:1252-1256 ``finally: bus.close()`` vs cli.py:1328-1332 ``_emit_workflow_failed(bus, ...)`` "
        "用同一已关闭 bus。对比 line 1268-1276 ``write_marker`` 失败路径正确开了新 ``tape2/bus2``。"
        "修法（留给 coder）：ValueError 分支仿 marker 失败路径，新开 ``Tape(resume=True) + EventBus`` 再 emit。"
    ),
)
def test_relative_project_root_known_bug_tape_missing_workflow_failed(
    cwd_tmp: Path, wf_project_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """xfail：钉死已知 bug——相对路径 fail loud 时 tape 缺 ``workflow_failed`` 终态事件。

    本测试**期望失败**（xfail strict）——若 coder 修了 bug，本测试开始通过 → xfail strict 会让
    pytest 失败（``XPASS(strict)``），提醒删除 xfail 标记。这是「bug 修复回归测试」的常用 pattern。

    断言（bug 存在时为 False → xfail 满足）：
      tape 含 ``workflow_failed`` 终态事件。bug 路径下 emit 静默失败 → 不含 → 断言失败 → xfail。
    """
    _stub_daemon_subprocesses(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", str(wf_project_root),
         "--inputs", json.dumps({"project_root": "relative/proj/path"})],
    )
    assert result.exit_code == 1  # 用户可见 fail loud 仍正确

    runs_dir = cwd_tmp / "runs"
    tape_files = list(runs_dir.glob("*.jsonl"))
    assert len(tape_files) == 1
    tape_text = tape_files[0].read_text(encoding="utf-8")

    # bug 存在时：workflow_failed 不在 tape（emit 静默失败）→ 断言 False → xfail 满足。
    # bug 修复后：workflow_failed 真落 tape → 断言 True → xfail strict 触发 XPASS 失败。
    assert "workflow_failed" in tape_text, (
        f"已知 bug 复现：tape 缺 workflow_failed 终态事件。tape 内容：\n{tape_text}"
    )


# ── 契约 3：per-run 回落（无 project_root input）────────────────────────────


def test_no_project_root_input_falls_back_to_per_run_env(
    cwd_tmp: Path, wf_no_inputs: Path, monkeypatch: pytest.MonkeyPatch,
):
    """workflow 无 ``project_root`` input → ``$ORCA_ARTIFACTS_DIR`` 落 per-run 路径。

    SPEC 2026-08-06 §2.1「向后兼容」分支：无 project_root → ``runs/<run_id>/artifacts/``
    （既有 per-run 行为不变，旧 workflow 零回归）。env 文件字面值钉死 per-run 落点。

    断言：
      (a) bootstrap exit 0。
      (b) ``runs/<run_id>/orca_env.sh`` 真存在且字面含 ``runs/<run_id>/artifacts``
          （per-run 路径，而非 project-scoped ``<proj>/artifacts/<wf>/``）。
      (c) per-run artifacts 目录真在磁盘 mkdir。
      (d) 反向：cwd 下不应出现顶层 ``artifacts/`` 目录（project-scoped 路径未触发）。
    """
    _stub_daemon_subprocesses(monkeypatch)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["bootstrap", str(wf_no_inputs), "--inputs", "{}"],
    )
    assert result.exit_code == 0, f"bootstrap 应成功：{result.output}"

    reply = _parse_stdout_json(result)
    run_id = reply["run_id"]

    # (b) env 文件真写了 per-run 字面路径。
    env_path = _env_file_path(run_id)
    assert env_path.is_file(), f"env 文件未真写到磁盘：{env_path}"
    env_content = env_path.read_text(encoding="utf-8")

    artifacts_lines = [
        line for line in env_content.splitlines()
        if line.startswith("export ORCA_ARTIFACTS_DIR=")
    ]
    assert len(artifacts_lines) == 1, f"env 应恰好一行 ORCA_ARTIFACTS_DIR：\n{env_content}"

    # per-run 路径字面：``runs/<run_id>/artifacts``（``_resolve_artifacts_dir`` 走
    # ``artifacts_dir_for_run`` 分支，落到 ``<rundir>/<run_id>/artifacts/``）。
    per_run_literal = f"runs/{run_id}/artifacts"
    assert per_run_literal in artifacts_lines[0], (
        f"per-run 字面路径未出现在 env 文件：\n  期望含：{per_run_literal}"
        f"\n  实际 ORCA_ARTIFACTS_DIR 行：{artifacts_lines[0]}"
    )

    # ORCA_ARTIFACTS_DIR 字面值是绝对路径（per-run 也走 resolve()）。
    m = re.search(r"^export ORCA_ARTIFACTS_DIR=(.+)$", env_content, re.MULTILINE)
    assert m, f"找不到 ORCA_ARTIFACTS_DIR 行：\n{env_content}"
    literal_value = m.group(1).strip().strip("'\"")
    assert Path(literal_value).is_absolute(), (
        f"per-run ORCA_ARTIFACTS_DIR 字面值也应是 resolve() 后绝对路径：{literal_value!r}"
    )

    # (c) per-run artifacts 目录真在磁盘 mkdir。
    per_run_dir = cwd_tmp / "runs" / run_id / "artifacts"
    assert per_run_dir.is_dir(), (
        f"per-run artifacts 目录未真创建：{per_run_dir}"
    )

    # (d) 反向守门：cwd 下不应有 project-scoped 顶层 ``artifacts/`` 目录。
    assert not (cwd_tmp / "artifacts").is_dir(), (
        f"无 project_root 不应触发 project-scoped 路径，但 cwd 下出现了 artifacts/："
        f"{cwd_tmp / 'artifacts'}"
    )
