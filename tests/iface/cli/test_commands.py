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
    """``orca list`` 子命令：列目录 / 空目录 / 不存在目录。"""

    def test_list_lists_yaml_files(self, tmp_path):
        (tmp_path / "a.yaml").write_text("name: a\n", encoding="utf-8")
        (tmp_path / "b.yaml").write_text("name: b\n", encoding="utf-8")
        (tmp_path / "not_yaml.txt").write_text("x", encoding="utf-8")
        result = runner.invoke(app, ["list", "--dir", str(tmp_path)])
        assert result.exit_code == EXIT_OK
        assert "a.yaml" in result.stdout
        assert "b.yaml" in result.stdout
        assert "not_yaml.txt" not in result.stdout

    def test_list_empty_dir_note(self, tmp_path):
        result = runner.invoke(app, ["list", "--dir", str(tmp_path)])
        assert result.exit_code == EXIT_OK
        assert "无" in result.stdout  # 「（X 下无 .yaml 文件）」

    def test_list_nonexistent_dir_exits_two(self, tmp_path):
        result = runner.invoke(app, ["list", "--dir", str(tmp_path / "nope")])
        assert result.exit_code == EXIT_ARG_OR_VALIDATE


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
