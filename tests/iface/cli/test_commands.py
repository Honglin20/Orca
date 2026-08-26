"""test_commands.py —— orca CLI 命令绑定 + 参数解析单测（SPEC §6.1 / 计划 C1.2）。

纯函数测试（不启动 TUI），覆盖：
  - ``parse_inputs`` 类型推断（bool/null/JSON/int/float/str）
  - 格式错 fail loud（不含 ``=`` / 空 key → BadParameter）
  - task 位置参数 → inputs.task（``-i task=...`` 显式覆盖 positional）
  - RunConfig 字段透传
  - ``validate`` 命令：合法 yaml / 校验失败 exit 2 / 文件不存在 exit 2
  - ``list`` 命令：列目录
  - 退出码常量
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer import BadParameter
from typer.testing import CliRunner

from orca.iface.cli.commands import (
    EXIT_ARG_OR_VALIDATE,
    EXIT_OK,
    EXIT_RUN_FAILED,
    app,
    parse_inputs,
    parse_run_args,
)


# ── parse_inputs 类型推断（SPEC §5.1）───────────────────────────────────────


class TestParseInputsTypeInference:
    """``-i key=value`` 类型推断：覆盖 6 种类型 + 大小写 + 边界。"""

    def test_int_inferred(self):
        assert parse_inputs(["x=1"]) == {"x": 1}

    def test_negative_int_inferred(self):
        assert parse_inputs(["x=-42"]) == {"x": -42}

    def test_float_inferred(self):
        assert parse_inputs(["x=3.14"]) == {"x": 3.14}

    def test_bool_true_case_insensitive(self):
        assert parse_inputs(["x=true", "y=TRUE", "z=False"]) == {
            "x": True, "y": True, "z": False,
        }

    def test_null_aliases(self):
        assert parse_inputs(["x=null", "y=none"]) == {"x": None, "y": None}

    def test_json_list_inferred(self):
        assert parse_inputs(["x=[1,2,3]"]) == {"x": [1, 2, 3]}

    def test_json_dict_inferred(self):
        assert parse_inputs(['x={"a":1,"b":2}']) == {"x": {"a": 1, "b": 2}}

    def test_json_malformed_falls_back_to_str(self):
        # 不合法 JSON（[1,2 但没闭合）→ 不 fail loud，回退 str（用户可能真想传字面量）
        assert parse_inputs(["x=[1,2"]) == {"x": "[1,2"}

    def test_plain_str_inferred(self):
        assert parse_inputs(["x=hello"]) == {"x": "hello"}

    def test_str_with_spaces_preserved(self):
        assert parse_inputs(["x=hello world"]) == {"x": "hello world"}

    def test_inf_nan_not_treated_as_float(self):
        # inf/nan 虽然 float() 接受，但更像字符串字面量 → 保持 str
        assert parse_inputs(["x=inf"]) == {"x": "inf"}
        assert parse_inputs(["x=nan"]) == {"x": "nan"}

    def test_multiple_keys_merged(self):
        result = parse_inputs(["a=1", "b=true", "c=hi"])
        assert result == {"a": 1, "b": True, "c": "hi"}


# ── parse_inputs 格式错（fail loud, SPEC §6.0 铁律 4）───────────────────────


class TestParseInputsFormatErrors:
    """格式错必须 fail loud（exit 2），不能静默吞。"""

    def test_missing_equals_raises(self):
        with pytest.raises(BadParameter):
            parse_inputs(["no_key_value"])

    def test_empty_key_raises(self):
        with pytest.raises(BadParameter):
            parse_inputs(["=value"])

    def test_empty_value_is_empty_string(self):
        # ``key=`` 是合法的空字符串值（不是格式错）
        assert parse_inputs(["x="]) == {"x": ""}

    def test_whitespace_key_stripped(self):
        assert parse_inputs(["  x  =1"]) == {"x": 1}


# ── parse_run_args / RunConfig（SPEC §5.1 决策 7：task 语法糖）─────────────


class TestParseRunArgs:
    """task 位置参数 = ``-i task="..."`` 语法糖；``-i task=...`` 显式覆盖 positional。"""

    def test_task_positional_injected_into_inputs(self):
        cfg = parse_run_args(Path("wf.yaml"), "测试任务", [], None)
        assert cfg.inputs["task"] == "测试任务"
        assert cfg.task == "测试任务"

    def test_explicit_i_task_overrides_positional(self):
        # -i task="..." 优先级 > positional task（显式声明覆盖语法糖）
        cfg = parse_run_args(Path("wf.yaml"), "positional", ["task=explicit"], None)
        assert cfg.inputs["task"] == "explicit"

    def test_no_task_leaves_inputs_without_task_key(self):
        cfg = parse_run_args(Path("wf.yaml"), None, ["x=1"], None)
        assert "task" not in cfg.inputs
        assert cfg.task is None

    def test_max_iter_passed_through(self):
        cfg = parse_run_args(Path("wf.yaml"), None, [], 42)
        assert cfg.max_iter == 42

    def test_max_iter_none_when_not_given(self):
        cfg = parse_run_args(Path("wf.yaml"), None, [], None)
        assert cfg.max_iter is None

    def test_i_args_type_inferred_in_run_config(self):
        cfg = parse_run_args(Path("wf.yaml"), None, ["count=5", "flag=true"], None)
        assert cfg.inputs == {"count": 5, "flag": True}


# ── typer 命令绑定（CliRunner）───────────────────────────────────────────────


runner = CliRunner()


def _write_yaml(tmp_path: Path, name: str, content: dict) -> Path:
    """写一个最小合法 workflow yaml 到 tmp_path。"""
    p = tmp_path / f"{name}.yaml"
    p.write_text(yaml.safe_dump(content), encoding="utf-8")
    return p


def _linear_wf() -> dict:
    """最小线性 workflow（a→$end，全 script，零依赖）。"""
    return {
        "name": "t",
        "entry": "a",
        "nodes": [
            {"name": "a", "kind": "script", "command": "echo hi",
             "routes": [{"to": "$end"}]},
        ],
    }


def _invalid_wf() -> dict:
    """结构非法 workflow（entry 指向不存在的 node）。"""
    return {"name": "t", "entry": "missing", "nodes": []}


class TestValidateCommand:
    """``orca validate`` 子命令：合法 / 校验失败 / 文件不存在。"""

    def test_validate_ok_exits_zero(self, tmp_path):
        wf = _write_yaml(tmp_path, "ok", _linear_wf())
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == EXIT_OK
        assert "校验通过" in result.stdout

    def test_validate_failure_exits_two(self, tmp_path):
        wf = _write_yaml(tmp_path, "bad", _invalid_wf())
        result = runner.invoke(app, ["validate", str(wf)])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        # 校验错误打到 stderr（typer CliRunner 默认 mix stderr 进 output）
        assert "校验失败" in result.output

    def test_validate_missing_file_exits_two(self, tmp_path):
        result = runner.invoke(app, ["validate", str(tmp_path / "nope.yaml")])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE


class TestListCommand:
    """``orca list`` 子命令：与 MCP ``list_workflows`` 同源（catalog 驱动，按 name）。

    monkeypatch ``catalog._workflow_dirs`` 指向 tmp_path 隔离，避免读到真实
    ``./workflows`` / ``~/.orca/workflows``。
    """

    SIMPLE = (
        "name: simple\n"
        "description: 简单 workflow\n"
        "entry: a\n"
        "nodes:\n"
        "  - name: a\n"
        "    kind: script\n"
        '    command: "echo hi"\n'
        "    routes:\n"
        "      - to: $end\n"
    )

    def test_list_shows_workflow_names(self, tmp_path, monkeypatch):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        (wf_dir / "simple.yaml").write_text(self.SIMPLE, encoding="utf-8")
        monkeypatch.setattr(
            "orca.compile.catalog._workflow_dirs", lambda: [wf_dir]
        )

        result = runner.invoke(app, ["list"])

        assert result.exit_code == EXIT_OK
        assert "simple" in result.stdout
        assert "简单 workflow" in result.stdout
        # 按 name 列出，不输出文件名
        assert "simple.yaml" not in result.stdout

    def test_list_empty_note(self, tmp_path, monkeypatch):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()
        monkeypatch.setattr(
            "orca.compile.catalog._workflow_dirs", lambda: [wf_dir]
        )

        result = runner.invoke(app, ["list"])

        assert result.exit_code == EXIT_OK
        assert "无可用 workflow" in result.stdout


# ── run 命令：退出码边界（不真跑 TUI，只验校验前置）────────────────────────


class TestRunExitCodes:
    """``orca run`` 启动前的校验前置：校验失败 → exit 2（不进入 TUI）。

    真正跑 TUI 的退出码（completed→0 / failed→1）由 test_app.py + test_integration.py
    覆盖（那里用 run_test pilot 或真 demo workflow）。
    """

    def test_run_invalid_yaml_exits_two_before_tui(self, tmp_path, monkeypatch):
        # 校验失败 → 直接 exit 2，绝不进入 TUI（关键：fail fast 避免黑屏）。
        # 保险地把 OrcaApp.run 替换成 fail（若校验前置漏了，TUI 起来才报错 → 此测试会 fail）。
        # app 模块在 C3 才存在；此处用 sys.modules 占位避免 import error，验证 run 不被调。
        import sys
        import types

        fake_mod = types.ModuleType("orca.iface.cli.app")

        class _Bomb:  # noqa: D401 - test stub
            def __init__(self, *a, **kw):
                raise AssertionError("OrcaApp should not be constructed on invalid yaml")

            def run(self):
                raise AssertionError("TUI should not start on invalid yaml")

        fake_mod.OrcaApp = _Bomb  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "orca.iface.cli.app", fake_mod)

        wf = _write_yaml(tmp_path, "bad", _invalid_wf())
        result = runner.invoke(app, ["run", str(wf)])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE

    def test_run_missing_file_exits_two(self, tmp_path):
        result = runner.invoke(app, ["run", str(tmp_path / "nope.yaml")])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE


# ── resume 命令（phase 11 §7 Checkpoint Resume）─────────────────────────────


class TestResumeCommand:
    """``orca resume`` 失败模式 → exit code 映射（SPEC §7.3）。

    不真跑续跑（那需要完整 tape + workflow，由 tests/run/test_resume.py 覆盖 from_tape
    核心逻辑）；此处覆盖 **CLI 层** 的参数解析 + typed exception → exit code。
    """

    def test_resume_missing_file_exits_two(self, tmp_path):
        """Tape 文件不存在 → exit 2（SPEC §7.3）。"""
        result = runner.invoke(
            app, ["resume", str(tmp_path / "nonexistent.jsonl")],
        )
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "Tape 不存在" in result.output

    def test_resume_missing_file_as_run_id_exits_two(self, tmp_path, monkeypatch):
        """参数视为 run_id，查 runs/<run_id>.jsonl 不存在 → exit 2。

        用 monkeypatch 把 cwd 切到 tmp_path（避免读到真实 ./runs/）。
        """
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["resume", "fake-run-id-12345"])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        # 解析为 runs/fake-run-id-12345.jsonl。
        assert "runs/fake-run-id-12345.jsonl" in result.output

    def test_resume_run_id_resolution_finds_file(self, tmp_path, monkeypatch):
        """run_id 解析：runs/<run_id>.jsonl 存在 → 走到 yaml 解析步骤（验证路径解析正确）。

        构造一个空 tape（触发 EmptyTapeError → exit 2），验证 run_id 被正确拼成
        runs/<run_id>.jsonl 并打开（而非报「Tape 不存在」）。
        """
        monkeypatch.chdir(tmp_path)
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        tape_path = runs_dir / "my-run-123.jsonl"
        tape_path.write_text("", encoding="utf-8")  # 空 tape

        result = runner.invoke(app, ["resume", "my-run-123"])
        # 空 tape → EmptyTapeError → exit 2（但不是「Tape 不存在」——证明 run_id 解析成功）。
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "Tape 不存在" not in result.output

    def test_resume_empty_tape_exits_two(self, tmp_path):
        """空 tape（0 字节）→ exit 2（EmptyTapeError）。

        传 --yaml 跳过 yaml 解析前置（空 tape 无 workflow_started 推断不出 yaml），
        直达 from_tape 的 EmptyTapeError。
        """
        tape_path = tmp_path / "empty.jsonl"
        tape_path.write_text("", encoding="utf-8")
        yaml_path = _write_yaml(tmp_path, "wf", _linear_wf())
        result = runner.invoke(
            app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
        )
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "无状态可恢复" in result.output or "为空" in result.output

    def test_resume_completed_tape_exits_zero(self, tmp_path, monkeypatch):
        """Tape 以 workflow_completed 结尾 → exit 0（「已完成，无需 resume」）。

        需要可解析的 yaml：在 tmp_path 写 yaml + 用 --yaml 指定。
        """
        # 构造一个完成的 tape（跑真实 workflow）。
        import asyncio

        from orca.events.bus import EventBus
        from orca.events.tape import Tape
        from orca.run.orchestrator import Orchestrator
        from orca.schema import Workflow, ScriptNode, Route

        wf = Workflow(
            name="resume_cli_test",
            entry="a",
            nodes=[ScriptNode(name="a", command="echo hi", routes=[Route(to="$end")])],
        )
        tape_path = tmp_path / "done.jsonl"
        tape = Tape(tape_path, run_id="done")
        bus = EventBus(tape)
        orch = Orchestrator(wf, bus, inputs={}, run_id="done")
        state = asyncio.run(orch.run())
        assert state.status == "completed"

        # 写一份 yaml 供 resume 重建 Workflow。
        yaml_path = _write_yaml(
            tmp_path, "wf",
            {"name": "resume_cli_test", "entry": "a",
             "nodes": [{"name": "a", "kind": "script", "command": "echo hi",
                        "routes": [{"to": "$end"}]}]},
        )

        result = runner.invoke(
            app, ["resume", str(tape_path), "--yaml", str(yaml_path)],
        )
        assert result.exit_code == EXIT_OK
        assert "已完成" in result.output

    def test_resume_no_yaml_and_unresolvable_exits_two(self, tmp_path, monkeypatch):
        """无 --yaml 且 tape 的 workflow_name 在 examples/ 找不到 → exit 2（fail loud）。"""
        # 构造一个含 workflow_started 的 tape（任意 workflow_name）。
        import json as _json

        tape_path = tmp_path / "x.jsonl"
        line = _json.dumps({
            "seq": 1, "type": "workflow_started", "timestamp": 1.0,
            "node": None, "session_id": None,
            "data": {"inputs": {}, "node_count": 1, "entry": "a",
                     "workflow_name": "totally_unique_unresolvable", "topology": {}},
        })
        tape_path.write_text(line + "\n", encoding="utf-8")

        monkeypatch.chdir(tmp_path)  # 无 examples/ → 推断失败
        result = runner.invoke(app, ["resume", str(tape_path)])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "--yaml" in result.output


class TestExitCodeConstants:
    """退出码常量值锁定（SPEC §5.3）。"""

    def test_constants(self):
        assert (EXIT_OK, EXIT_RUN_FAILED, EXIT_ARG_OR_VALIDATE) == (0, 1, 2)


# ── daemon --background + ps/logs/wait（phase 11 §8 P3.2）──────────────────────


class TestBackgroundRun:
    """``orca run --background`` + ``ps`` / ``logs`` / ``wait`` 三件套（SPEC §10.2 item10/11）。

    测试隔离：monkeypatch ``ORCA_RUNS_DIR`` 指到 tmp_path（不污染用户 ~/.orca）。
    不真 fork detached（CI flaky），spy ``daemonize`` 验证调用 + exit 0 不阻塞。
    """

    @pytest.fixture(autouse=True)
    def _isolated_runs_dir(self, tmp_path, monkeypatch):
        """把 ORCA_RUNS_DIR 指到 tmp_path/runs，daemon metadata 不污染 ~/.orca。"""
        fake = tmp_path / "orca_runs"
        monkeypatch.setattr("orca.iface.cli.bg_runner.ORCA_RUNS_DIR", fake)
        return fake

    def _seed_meta(self, runs_dir, **overrides):
        """写一份 background run metadata 到 runs_dir（供 ps/logs/wait 读）。"""
        from orca.iface.cli.bg_runner import BgRunMeta, write_meta

        defaults = dict(
            run_id="demo-20260701-120000-abc123",
            pid=99999,  # 一个几乎不可能存活的 pid（让 effective_status 走 crashed 测试用）
            yaml_path="/abs/examples/demo.yaml",
            started_at=1000.0,
            log_path=str(runs_dir / "demo-20260701-120000-abc123" / "log"),
            tape_path="runs/demo-20260701-120000-abc123.jsonl",
            status="completed",
        )
        defaults.update(overrides)
        meta = BgRunMeta(**defaults)
        write_meta(meta)
        return meta

    # ── run --background ──────────────────────────────────────────────────────

    def test_run_background_flag_invokes_daemonize_and_exits_zero(
        self, tmp_path, monkeypatch,
    ):
        """``--background`` → spy daemonize 被调用 + 打印 run_id/pid/logs + exit 0，不跑 workflow。

        INTENT：``--background`` 必须**立即返回**（SPEC §10.2 item10），不启动 TUI 阻塞终端。
        验证：(1) daemonize 被调，参数含正确 yaml + extra_argv；(2) workflow 主体**没在
        当前进程跑**（_run_workflow 没被调，没有 textual TUI）；(3) 输出含 run_id + pid。
        """
        yaml_path = _write_yaml(tmp_path, "wf", _linear_wf())
        called = {}

        def fake_daemonize(yaml, run_id, extra_argv, **kwargs):
            called["yaml"] = yaml
            called["run_id"] = run_id
            called["extra_argv"] = extra_argv
            return 12345  # 假 pid

        monkeypatch.setattr("orca.iface.cli.bg_runner.daemonize", fake_daemonize)

        result = runner.invoke(
            app, ["run", str(yaml_path), "--background", "-i", "k=v"],
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "Started background run" in result.output
        assert "PID: 12345" in result.output
        assert "logs:" in result.output
        # extra_argv 透传 -i k=v（用户参数透传给 detached child）。
        assert "-i" in called["extra_argv"] and "k=v" in called["extra_argv"]
        # yaml_path 原样透传。
        assert called["yaml"] == yaml_path

    def test_run_background_yaml_missing_exits_two(self, tmp_path):
        """``--background`` + yaml 不存在 → exit 2（与 foreground 同前置校验，fail loud）。"""
        result = runner.invoke(
            app, ["run", str(tmp_path / "nope.yaml"), "--background"],
        )
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "不存在" in result.output

    def test_run_background_passes_max_iter_and_task(self, tmp_path, monkeypatch):
        """``--background`` 把 ``--max-iter`` + positional task 透传给 child argv。"""
        yaml_path = _write_yaml(tmp_path, "wf", _linear_wf())
        captured = {}

        def fake_daemonize(yaml, run_id, extra_argv, **kwargs):
            captured["extra_argv"] = extra_argv
            return 1

        monkeypatch.setattr("orca.iface.cli.bg_runner.daemonize", fake_daemonize)

        result = runner.invoke(
            app, ["run", str(yaml_path), "do thing", "--background", "--max-iter", "5"],
        )
        assert result.exit_code == EXIT_OK
        # positional task 透传 + --max-iter 透传。
        assert "do thing" in captured["extra_argv"]
        assert "--max-iter" in captured["extra_argv"]
        assert "5" in captured["extra_argv"]

    # ── ps ────────────────────────────────────────────────────────────────────

    def test_ps_lists_runs_from_metadata(self, _isolated_runs_dir):
        """``ps`` 读 ``~/.orca/runs/*.json`` 列全部 run（表头 + 行）。"""
        # 用一个存活 pid（当前进程）让 effective_status 显示 running。
        import os

        self._seed_meta(
            _isolated_runs_dir,
            run_id="run-alpha", pid=os.getpid(), status="running",
        )
        self._seed_meta(
            _isolated_runs_dir,
            run_id="run-beta", pid=os.getpid(), status="completed",
        )

        result = runner.invoke(app, ["ps"])
        assert result.exit_code == EXIT_OK
        assert "RUN_ID" in result.output
        assert "run-alpha" in result.output
        assert "run-beta" in result.output
        assert "running" in result.output
        assert "completed" in result.output

    def test_ps_empty_message_when_no_runs(self, _isolated_runs_dir):
        """无 background run → 友好提示（不报错，不空行）。"""
        result = runner.invoke(app, ["ps"])
        assert result.exit_code == EXIT_OK
        assert "无 background run" in result.output

    def test_ps_marks_dead_pid_as_crashed(self, _isolated_runs_dir):
        """INTENT（fail loud）：status=running 但 pid 已死 → ``ps`` 必须标 crashed。

        SPEC §10.2 item11 硬约束——child 崩未及更新 metadata 时，不能静默显示 running 误导用户。
        """
        # pid=99999999 几乎不可能存活（远超 pid_max）。
        self._seed_meta(
            _isolated_runs_dir,
            run_id="run-crash", pid=99999999, status="running",
        )
        result = runner.invoke(app, ["ps"])
        assert result.exit_code == EXIT_OK
        assert "run-crash" in result.output
        assert "crashed" in result.output

    # ── logs ──────────────────────────────────────────────────────────────────

    def test_logs_reads_run_log(self, _isolated_runs_dir):
        """``logs <id>`` 打印 metadata.log_path 指向的日志文件最后 N 行。"""
        meta = self._seed_meta(
            _isolated_runs_dir, run_id="run-log", status="completed",
        )
        # 写日志文件（metadata.log_path 指的位置）。
        log_file = Path(meta.log_path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = runner.invoke(app, ["logs", "run-log", "-n", "2"])
        assert result.exit_code == EXIT_OK
        assert "line2" in result.output
        assert "line3" in result.output
        # 只显示最后 2 行（-n 2）。
        assert "line1" not in result.output

    def test_logs_unknown_run_id_exits_two(self, _isolated_runs_dir):
        """``logs`` 找不到 run_id 的 metadata → exit 2（fail loud，提示 ``orca ps``）。"""
        result = runner.invoke(app, ["logs", "nonexistent"])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "未找到" in result.output or "orca ps" in result.output

    def test_logs_missing_log_file_exits_two(self, _isolated_runs_dir):
        """metadata 存在但日志文件不存在 → exit 2（run 还没写日志）。"""
        self._seed_meta(_isolated_runs_dir, run_id="run-nolog", status="running")
        result = runner.invoke(app, ["logs", "run-nolog"])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "日志文件不存在" in result.output

    # ── wait ──────────────────────────────────────────────────────────────────

    def test_wait_returns_immediately_if_completed(self, _isolated_runs_dir):
        """run 已 completed → ``wait`` 立即返回 exit 0。"""
        self._seed_meta(
            _isolated_runs_dir, run_id="run-done", status="completed",
        )
        result = runner.invoke(app, ["wait", "run-done"])
        assert result.exit_code == EXIT_OK
        assert "终态" in result.output and "completed" in result.output

    def test_wait_returns_exit_one_if_failed(self, _isolated_runs_dir):
        """run failed → ``wait`` exit 1（fail loud）。"""
        self._seed_meta(
            _isolated_runs_dir, run_id="run-fail", status="failed",
        )
        result = runner.invoke(app, ["wait", "run-fail"])
        assert result.exit_code == EXIT_RUN_FAILED

    def test_wait_returns_exit_one_if_crashed(self, _isolated_runs_dir):
        """run status=running 但 pid 死 → effective_status=crashed → ``wait`` exit 1。"""
        self._seed_meta(
            _isolated_runs_dir, run_id="run-crash-wait",
            pid=99999999, status="running",  # pid 死 → crashed
        )
        result = runner.invoke(app, ["wait", "run-crash-wait"])
        assert result.exit_code == EXIT_RUN_FAILED
        assert "crashed" in result.output

    def test_wait_not_found_exit_two(self, _isolated_runs_dir):
        """``wait`` 找不到 run_id → exit 2（SPEC：not-found exit 2）。"""
        result = runner.invoke(app, ["wait", "nonexistent"])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE
        assert "未找到" in result.output


class TestRunWorkflowHeadless:
    """``_run_workflow_headless`` —— detached daemon child 的 headless 执行路径单测。

    INTENT（SPEC §8.2 / review 🟡 #4）：detached child 无 TTY 不能跑 TUI，走 headless
    Orchestrator。三条路径必须各自标对 metadata：(1) 配置错（ValueError）→ failed；
    (2) 运行期异常 → failed；(3) 正常完成 → completed。
    不真跑 workflow（mock Orchestrator），专注验证 ``mark_terminal_status`` 调用契约。
    """

    @pytest.fixture(autouse=True)
    def _isolated_runs_dir(self, tmp_path, monkeypatch):
        """隔离 metadata 到 tmp_path（不污染 ~/.orca）。"""
        fake = tmp_path / "orca_runs"
        monkeypatch.setattr("orca.iface.cli.bg_runner.ORCA_RUNS_DIR", fake)
        return fake

    def _seed_running_meta(self, runs_dir, run_id):
        """写一份 status=running 的 metadata（daemonize 父进程已写的状态）。"""
        from orca.iface.cli.bg_runner import BgRunMeta, write_meta

        write_meta(BgRunMeta(
            run_id=run_id, pid=12345, yaml_path="/x.yaml", started_at=1.0,
            log_path=str(runs_dir / run_id / "log"),
            tape_path=f"runs/{run_id}.jsonl", status="running",
        ))

    def test_headless_orchestrator_config_error_marks_failed(
        self, _isolated_runs_dir, monkeypatch,
    ):
        """Orchestrator 构造抛 ValueError（必填 input 缺失）→ mark failed + exit 1。"""
        from orca.iface.cli import commands as cmds
        from orca.schema import Route, ScriptNode, Workflow

        self._seed_running_meta(_isolated_runs_dir, "r-cfg")
        config = cmds.RunConfig(yaml_path=Path("/x.yaml"), inputs={})
        wf = Workflow(
            name="t", entry="a",
            nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        )

        # mock Orchestrator.__init__ 抛 ValueError（模拟必填 input 缺失）。
        def boom(self, *a, **kw):
            raise ValueError("missing required input")

        monkeypatch.setattr("orca.run.orchestrator.Orchestrator.__init__", boom)

        exit_code = cmds._run_workflow_headless(config, wf, "r-cfg")
        assert exit_code == cmds.EXIT_RUN_FAILED
        # metadata 被标成 failed（mark_terminal_status 调了）。
        from orca.iface.cli.bg_runner import read_meta
        meta = read_meta("r-cfg")
        assert meta.status == "failed"

    def test_headless_orchestrator_runtime_exception_marks_failed(
        self, _isolated_runs_dir, monkeypatch,
    ):
        """orch.run() 抛 Exception → mark failed + exit 1（fail loud）。"""
        from orca.iface.cli import commands as cmds
        from orca.schema import Route, ScriptNode, Workflow

        self._seed_running_meta(_isolated_runs_dir, "r-run")
        config = cmds.RunConfig(yaml_path=Path("/x.yaml"), inputs={})
        wf = Workflow(
            name="t", entry="a",
            nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        )

        # mock Orchestrator 构造 OK，但 run() 抛 Exception。
        class FakeOrch:
            def __init__(self, *a, **kw):
                pass

            def run(self):
                raise RuntimeError("boom in drive loop")

        monkeypatch.setattr("orca.iface.cli.commands.Orchestrator", FakeOrch, raising=False)
        # _run_workflow_headless 内延迟 import，patch commands 不够 —— patch 真源头。
        monkeypatch.setattr("orca.run.orchestrator.Orchestrator", FakeOrch)

        exit_code = cmds._run_workflow_headless(config, wf, "r-run")
        assert exit_code == cmds.EXIT_RUN_FAILED
        from orca.iface.cli.bg_runner import read_meta
        assert read_meta("r-run").status == "failed"

    def test_headless_completed_marks_completed(
        self, _isolated_runs_dir, monkeypatch,
    ):
        """orch.run() 返回 completed RunState → mark completed + exit 0。"""
        from orca.iface.cli import commands as cmds
        from orca.schema import Route, ScriptNode, Workflow

        self._seed_running_meta(_isolated_runs_dir, "r-ok")
        config = cmds.RunConfig(yaml_path=Path("/x.yaml"), inputs={})
        wf = Workflow(
            name="t", entry="a",
            nodes=[ScriptNode(name="a", command="echo", routes=[Route(to="$end")])],
        )

        class FakeState:
            status = "completed"

        # asyncio.run 要协程，故 run 用 async def（模拟真 Orchestrator.run 签名）。
        class FakeOrch:
            def __init__(self, *a, **kw):
                pass

            async def run(self):
                return FakeState()

        monkeypatch.setattr("orca.iface.cli.commands.Orchestrator", FakeOrch, raising=False)
        monkeypatch.setattr("orca.run.orchestrator.Orchestrator", FakeOrch)

        exit_code = cmds._run_workflow_headless(config, wf, "r-ok")
        assert exit_code == cmds.EXIT_OK
        from orca.iface.cli.bg_runner import read_meta
        assert read_meta("r-ok").status == "completed"


def _linear_wf() -> dict:
    """最小合法 workflow（1 个 script node，供 run --background 测试用）。"""
    return {
        "name": "wf",
        "entry": "a",
        "nodes": [{"name": "a", "kind": "script", "command": "echo hi",
                   "routes": [{"to": "$end"}]}],
    }
