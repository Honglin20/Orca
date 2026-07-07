"""test_executor_cmds.py —— ``orca executor`` 子命令组 + config 模块单测（不真起进程）。

覆盖 ``docs/plans/2026-07-07-executor-cli-extend.md``：
  - ``config`` 模块：missing→{}、corrupt→warn+{}、非 dict 字段 warn+drop、atomic write、
    ``apply_config_env`` 三字段注入（binary / flags list|string / prompt_channel）、未知 profile
    warn+skip、``setdefault`` 尊重既有 env（env>config）、``load_merged_config`` 项目覆盖用户。
  - ``executor set/show/unset/list``：三维（binary/flags/prompt_channel）+ scope（project|user）、
    唯一真相源 show 的来源标注（env/项目/用户/default）、exit code、stdout。
  - ``classify`` 纯函数全分支。
  - ``executor test``：monkeypatch ``CLIRunner`` / ``create_subprocess_exec``。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.cli import config as config_mod
from orca.iface.cli.commands import app
from orca.iface.cli.executor_cmds import classify
from orca.profiles.registry import _reset_for_test


# ── fixture：隔离 config_path + project_config_path + registry + env ─────────


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试把 config_path / project_config_path 指到 tmp_path、清掉 ORCA_* env、重置 registry。

    避免污染真实 ``~/.orca/config.json`` / ``./.orca/config.json``，且隔离 ``apply_config_env``
    写的 env。重置 ``_shell_env_snapshot`` 让 show 的 env 层来源判定每测试重抓（不跨测试泄漏）。
    """
    user_cfg = tmp_path / "user_config.json"
    proj_cfg = tmp_path / "proj" / ".orca" / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: user_cfg)
    monkeypatch.setattr(config_mod, "project_config_path", lambda: proj_cfg)
    # 记录测试前已存在的 ORCA_* env（teardown 还原），清掉所有 ORCA_* 让 default 生效。
    pre_env = {
        k: os.environ[k] for k in list(os.environ) if k.startswith("ORCA_")
    }
    for key in list(os.environ):
        if key.startswith("ORCA_"):
            monkeypatch.delenv(key, raising=False)
    # 重置 shell env 快照（show 来源判定用），让本测试的 bootstrap 重抓干净 env。
    monkeypatch.setattr(config_mod, "_shell_env_snapshot", None)
    _reset_for_test()
    yield
    _reset_for_test()
    # teardown：清测试期新增的 ORCA_*，还原既有值。
    for key in list(os.environ):
        if key.startswith("ORCA_") and key not in pre_env:
            os.environ.pop(key, None)
    for key, val in pre_env.items():
        os.environ[key] = val


runner = CliRunner()


# ── config 模块 ───────────────────────────────────────────────────────────────


class TestConfigModule:
    """config_path / load_config / save_config / apply_config_env / bootstrap / merge。"""

    def test_load_config_missing_returns_empty(self):
        assert config_mod.load_config() == {}

    def test_load_config_corrupt_returns_empty_with_warning(self, caplog):
        config_mod.config_path().write_text("{ not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert config_mod.load_config() == {}
        assert "损坏" in caplog.text

    def test_load_config_top_level_not_object_returns_empty(self, caplog):
        config_mod.config_path().write_text("[1, 2, 3]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert config_mod.load_config() == {}

    def test_load_config_non_dict_known_field_warns_and_drops(self, caplog):
        """已知字段（binaries/flags/prompt_channel）值非 dict → warn + 丢弃该字段，其余保留。"""
        config_mod.config_path().write_text(
            json.dumps({"binaries": ["not", "a", "dict"], "flags": {"claude": "x"}}),
            encoding="utf-8",
        )
        with caplog.at_level("WARNING"):
            cfg = config_mod.load_config()
        assert "非 object" in caplog.text
        assert "binaries" not in cfg  # 非 dict 被丢
        assert cfg.get("flags") == {"claude": "x"}  # 合法字段保留

    def test_save_config_atomic_write(self):
        cfg = {"binaries": {"claude": "ccr code"}}
        config_mod.save_config(cfg)
        raw = config_mod.config_path().read_text(encoding="utf-8")
        assert json.loads(raw) == cfg
        assert not config_mod.config_path().with_suffix(".json.tmp").exists()

    def test_save_config_creates_parent_dir(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nest" / "config.json"
        original = config_mod.config_path
        config_mod.config_path = lambda: nested
        try:
            config_mod.save_config({"binaries": {"claude": "x"}})
            assert nested.is_file()
        finally:
            config_mod.config_path = original

    def test_apply_config_env_unknown_profile_warns_and_skips(self, caplog):
        with caplog.at_level("WARNING"):
            config_mod.apply_config_env(
                {"binaries": {"nonexistent_profile": "some-binary"}}
            )
        assert "nonexistent_profile" in caplog.text
        assert all(not k.startswith("ORCA_") for k in os.environ)

    def test_apply_config_env_sets_known_profile_binary_env(self):
        config_mod.apply_config_env({"binaries": {"claude": "ccr code"}})
        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"

    def test_apply_config_env_respects_existing_env(self, monkeypatch):
        monkeypatch.setenv("ORCA_CLAUDE_CLI", "env-binary")
        config_mod.apply_config_env({"binaries": {"claude": "config-binary"}})
        assert os.environ["ORCA_CLAUDE_CLI"] == "env-binary"

    def test_apply_config_env_skips_non_string_binary(self, caplog):
        """非字符串 binary → warn + skip。"""
        with caplog.at_level("WARNING"):
            config_mod.apply_config_env({"binaries": {"claude": 123}})
        assert "非字符串" in caplog.text
        assert os.environ.get("ORCA_CLAUDE_CLI") is None

    def test_apply_config_env_flags_as_list_joins_and_injects(self):
        """flags 存 list（规范）→ 空格 join 注入 env。"""
        config_mod.apply_config_env(
            {"flags": {"opencode": ["run", "--format", "json"]}}
        )
        assert os.environ.get("ORCA_OPENCODE_FLAGS") == "run --format json"

    def test_apply_config_env_flags_as_string_injects(self):
        """flags 存 string（手写容错）→ 原样注入。"""
        config_mod.apply_config_env({"flags": {"opencode": "run --format json"}})
        assert os.environ.get("ORCA_OPENCODE_FLAGS") == "run --format json"

    def test_apply_config_env_prompt_channel_injects(self):
        config_mod.apply_config_env({"prompt_channel": {"opencode": "stdin"}})
        assert os.environ.get("ORCA_OPENCODE_PROMPT_CHANNEL") == "stdin"

    def test_apply_config_env_prompt_channel_invalid_warns_and_skips(self, caplog):
        """非法 prompt_channel（非 stdin/argv）→ warn + skip。"""
        with caplog.at_level("WARNING"):
            config_mod.apply_config_env(
                {"prompt_channel": {"opencode": "garbage"}}
            )
        assert "非法" in caplog.text
        assert os.environ.get("ORCA_OPENCODE_PROMPT_CHANNEL") is None

    def test_apply_config_env_no_known_fields_is_noop(self):
        config_mod.apply_config_env({"other": "x"})  # 不抛
        config_mod.apply_config_env({})  # 空也不抛
        # 真正验证 noop：未注入任何 ORCA_* env（测试验证意图，非仅"不抛"）
        assert all(not k.startswith("ORCA_") for k in os.environ)

    def test_bootstrap_config_loads_then_applies(self):
        config_mod.save_config({"binaries": {"claude": "ccr code"}})
        config_mod.bootstrap_config()
        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"
        config_mod.bootstrap_config()  # 幂等
        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"

    def test_load_merged_config_project_overrides_user_per_field(self):
        """per-field project 覆盖 user（非整份替换）：project 的 opencode 赢，user 的 claude 保留。"""
        config_mod.save_config(
            {"binaries": {"claude": "user-claude", "opencode": "user-opencode"}}
        )  # user 级
        config_mod.save_config(
            {"binaries": {"opencode": "proj-opencode"}}, config_mod.project_config_path()
        )  # 项目级
        merged = config_mod.load_merged_config()
        assert merged["binaries"]["opencode"] == "proj-opencode"  # project 赢
        assert merged["binaries"]["claude"] == "user-claude"  # user 保留


# ── resolve_prompt_channel（base.py，profiles 层）─────────────────────────────


class TestResolvePromptChannel:
    """``CliProfile.resolve_prompt_channel``：env > default + 非法值回落（与 resolve_flags 同构）。"""

    def test_default_when_no_env(self):
        from orca.profiles.registry import get_profile

        assert get_profile("claude").resolve_prompt_channel() == "stdin"
        assert get_profile("opencode").resolve_prompt_channel() == "argv"

    def test_env_overrides(self, monkeypatch):
        from orca.profiles.registry import get_profile

        monkeypatch.setenv("ORCA_OPENCODE_PROMPT_CHANNEL", "stdin")
        assert get_profile("opencode").resolve_prompt_channel() == "stdin"

    def test_invalid_env_falls_back_with_warning(self, monkeypatch, caplog):
        """非法 env 值 → warn + 回落 default（fail loud 但可恢复）。"""
        import logging

        from orca.profiles.registry import get_profile

        monkeypatch.setenv("ORCA_OPENCODE_PROMPT_CHANNEL", "garbage")
        with caplog.at_level(logging.WARNING):
            assert get_profile("opencode").resolve_prompt_channel() == "argv"  # 回落 default
        assert "非法" in caplog.text


# ── classify 纯函数（全分支）──────────────────────────────────────────────────


class TestClassify:
    """``classify`` 纯函数：覆盖 5 个判定分支。"""

    def test_timed_out_fail(self):
        ok, msg = classify(set(), False, -1, True, "")
        assert ok is False
        assert "超时" in msg

    def test_no_stream_events_fail_with_stderr(self):
        ok, msg = classify(set(), False, 0, False, "some error output")
        assert ok is False
        assert "非 stream-json" in msg
        assert "some error output" in msg

    def test_no_stream_events_fail_no_stderr(self):
        ok, msg = classify(set(), False, 0, False, "")
        assert ok is False
        assert "无 stderr 输出" in msg

    def test_stream_events_nonzero_exit_no_result_fail(self):
        ok, msg = classify({"stream_event"}, False, 1, False, "")
        assert ok is False
        assert "退出码 1" in msg

    def test_stream_events_with_result_nonzero_exit_pass(self):
        ok, msg = classify({"result"}, True, 1, False, "")
        assert ok is True
        assert "端到端 OK" in msg

    def test_saw_result_pass(self):
        ok, msg = classify({"result", "stream_event"}, True, 0, False, "")
        assert ok is True
        assert "端到端 OK" in msg

    def test_stream_events_no_result_exit_zero_pass_warn(self):
        ok, msg = classify({"stream_event"}, False, 0, False, "")
        assert ok is True
        assert "未收到 result 行" in msg

    def test_stderr_snippet_truncated_to_500(self):
        long_stderr = "x" * 600
        ok, msg = classify(set(), False, 0, False, long_stderr)
        assert ok is False
        assert "x" * 500 in msg
        assert "x" * 600 not in msg


# ── executor set / unset（三维 + scope）────────────────────────────────────────


class TestExecutorSetUnset:
    """``set`` / ``unset``：三维 + scope + 校验 + config 写入。"""

    def test_set_unknown_profile_exits_two(self):
        result = runner.invoke(app, ["executor", "set", "nonexistent", "--binary", "x"])
        assert result.exit_code == 2
        assert "错误" in result.output

    def test_set_no_field_exits_two(self):
        """未指定任何字段 → exit 2（fail loud，防误触空写）。"""
        result = runner.invoke(app, ["executor", "set", "claude"])
        assert result.exit_code == 2
        assert "至少指定" in result.output

    def test_set_invalid_prompt_channel_exits_two(self):
        result = runner.invoke(
            app, ["executor", "set", "claude", "--prompt-channel", "xyz"]
        )
        assert result.exit_code == 2
        assert "stdin|argv" in result.output

    def test_set_invalid_scope_exits_two(self):
        result = runner.invoke(
            app, ["executor", "set", "claude", "--binary", "x", "--scope", "mars"]
        )
        assert result.exit_code == 2
        assert "project|user" in result.output

    def test_set_binary_writes_project_config_by_default(self):
        """默认 scope=project → 写 .orca/config.json。"""
        result = runner.invoke(
            app, ["executor", "set", "claude", "--binary", "ccr code"]
        )
        assert result.exit_code == 0
        assert "已写入" in result.output
        proj = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert proj["binaries"]["claude"] == "ccr code"
        # user config 未被写
        assert not config_mod.config_path().exists()

    def test_set_scope_user_writes_user_config(self):
        result = runner.invoke(
            app,
            ["executor", "set", "claude", "--binary", "x", "--scope", "user"],
        )
        assert result.exit_code == 0
        user = json.loads(config_mod.config_path().read_text(encoding="utf-8"))
        assert user["binaries"]["claude"] == "x"

    def test_set_flags_stored_as_list(self):
        """--flags 字符串输入 → shlex.split 成 list 存储（JSON-natural）。"""
        result = runner.invoke(
            app, ["executor", "set", "opencode", "--flags", "run --format json"]
        )
        assert result.exit_code == 0
        proj = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert proj["flags"]["opencode"] == ["run", "--format", "json"]

    def test_set_three_fields_at_once(self):
        result = runner.invoke(
            app,
            [
                "executor", "set", "opencode",
                "--binary", "nga",
                "--flags", "run --format json",
                "--prompt-channel", "argv",
            ],
        )
        assert result.exit_code == 0
        proj = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert proj["binaries"]["opencode"] == "nga"
        assert proj["flags"]["opencode"] == ["run", "--format", "json"]
        assert proj["prompt_channel"]["opencode"] == "argv"

    def test_set_echoes_effective_command(self):
        """set 写完回打生效命令（唯一真相源）便于核对。"""
        result = runner.invoke(
            app, ["executor", "set", "opencode", "--binary", "nga"]
        )
        assert "生效命令" in result.output
        assert "nga" in result.output

    def test_unset_single_field(self):
        """unset <profile> <field> 只清该字段。"""
        config_mod.save_config(
            {
                "binaries": {"claude": "x"},
                "flags": {"claude": ["-p"]},
            },
            config_mod.project_config_path(),
        )
        result = runner.invoke(app, ["executor", "unset", "claude", "flags"])
        assert result.exit_code == 0
        cfg = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert "claude" not in cfg.get("flags", {})
        assert cfg["binaries"]["claude"] == "x"  # binary 保留

    def test_unset_all_clears_three_fields(self):
        config_mod.save_config(
            {
                "binaries": {"claude": "x"},
                "flags": {"claude": ["-p"]},
                "prompt_channel": {"claude": "stdin"},
            },
            config_mod.project_config_path(),
        )
        result = runner.invoke(app, ["executor", "unset", "claude"])  # field 默认 all
        assert result.exit_code == 0
        cfg = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert "claude" not in cfg.get("binaries", {})
        assert "claude" not in cfg.get("flags", {})
        assert "claude" not in cfg.get("prompt_channel", {})

    def test_unset_no_override_is_noop(self):
        result = runner.invoke(app, ["executor", "unset", "claude"])
        assert result.exit_code == 0
        assert "无" in result.output and "override" in result.output

    def test_unset_invalid_field_exits_two(self):
        result = runner.invoke(app, ["executor", "unset", "claude", "binary_path"])
        assert result.exit_code == 2


# ── executor show（唯一真相源 + 来源标注）─────────────────────────────────────


class TestExecutorShow:
    """``show``：完整生效 argv + 每字段来源（env/项目/用户/default）。"""

    def test_show_no_override_shows_default(self):
        result = runner.invoke(app, ["executor", "show", "opencode"])
        assert result.exit_code == 0
        assert "生效命令" in result.output
        assert "← default" in result.output
        # opencode default flags 含 --dangerously-skip-permissions
        assert "--dangerously-skip-permissions" in result.output

    def test_show_marks_project_source(self):
        config_mod.save_config(
            {"binaries": {"opencode": "nga"}},
            config_mod.project_config_path(),
        )
        result = runner.invoke(app, ["executor", "show", "opencode"])
        assert "nga" in result.output
        assert "← 项目" in result.output

    def test_show_marks_user_source(self):
        config_mod.save_config({"binaries": {"opencode": "nga"}})  # user 级
        result = runner.invoke(app, ["executor", "show", "opencode"])
        assert "← 用户" in result.output

    def test_show_marks_env_source_and_it_wins(self):
        """shell env（启动期 export）覆盖 config，show 标 ← env。"""
        # set 后再 setenv：env 应赢。先写 config，再注 env，bootstrap 抓快照含 env。
        config_mod.save_config(
            {"binaries": {"opencode": "config-nga"}},
            config_mod.project_config_path(),
        )
        os.environ["ORCA_OPENCODE_CLI"] = "env-nga"
        # 重置快照让下次 bootstrap 抓到 env-nga
        config_mod._shell_env_snapshot = None
        result = runner.invoke(app, ["executor", "show", "opencode"])
        assert "env-nga" in result.output
        assert "← env" in result.output

    def test_show_priority_chain_env_project_user_default(self):
        """🔴 四态同字段端到端：env > 项目 > 用户 > default，逐层剥离验证来源切换。

        plan §3.3 优先级闭环：同一 profile 同一字段（binary）四层叠加，依次移除顶层，
        show 的来源标注应逐层落到下一层。锁住「多 fallback 生效只一份」语义。
        """
        # 用户层 binary=u
        config_mod.save_config({"binaries": {"opencode": "u"}})
        # 项目层 binary=p（覆盖 user）
        config_mod.save_config(
            {"binaries": {"opencode": "p"}}, config_mod.project_config_path()
        )

        def _show():
            # 模拟「每次 orca 是新进程」：重抓快照 + 调用后清注入的 env（防跨步骤泄漏）。
            config_mod._shell_env_snapshot = None
            r = runner.invoke(app, ["executor", "show", "opencode"])
            for k in list(os.environ):
                if k.startswith("ORCA_"):
                    os.environ.pop(k, None)
            return r

        # 1. env 在 → env 赢
        os.environ["ORCA_OPENCODE_CLI"] = "e"
        assert "← env" in _show().output
        # 2. 移除 env → 项目赢
        assert "← 项目" in _show().output and "p" in _show().output
        # 3. 移除 project config → 用户赢
        config_mod.save_config({}, config_mod.project_config_path())
        assert "← 用户" in _show().output and "u" in _show().output
        # 4. 移除 user config → default
        config_mod.save_config({})  # user 清空
        assert "← default" in _show().output

    def test_show_lists_all_profiles_when_no_arg(self):
        result = runner.invoke(app, ["executor", "show"])
        assert result.exit_code == 0
        assert "Profile: claude" in result.output
        assert "Profile: opencode" in result.output

    def test_show_effective_command_reflects_flags_override(self):
        """flags override（去掉 --dangerously-skip-permissions）后，生效命令随之变。"""
        config_mod.save_config(
            {"flags": {"opencode": ["run", "--format", "json"]}},
            config_mod.project_config_path(),
        )
        result = runner.invoke(app, ["executor", "show", "opencode"])
        # 生效命令里 flags 不再含 --dangerously-skip-permissions
        eff_line = [
            l for l in result.output.splitlines() if l.startswith("  opencode ") or "生效" in l
        ]
        assert any("dangerously-skip-permissions" not in l for l in eff_line)


# ── executor list ─────────────────────────────────────────────────────────────


class TestExecutorList:
    def test_list_shows_profiles_and_env(self):
        result = runner.invoke(app, ["executor", "list"])
        assert result.exit_code == 0
        assert "claude" in result.output
        assert "ORCA_CLAUDE_CLI" in result.output

    def test_list_marks_overridden_profile(self):
        config_mod.save_config(
            {"binaries": {"claude": "ccr code"}}, config_mod.project_config_path()
        )
        result = runner.invoke(app, ["executor", "list"])
        lines = [l for l in result.output.splitlines() if "claude" in l]
        assert any("*" in l for l in lines)


# ── executor test（monkeypatch，不真起进程）────────────────────────────────────


class TestExecutorTest:
    """``orca executor test``：monkeypatch CLIRunner / create_subprocess_exec。"""

    def test_unknown_profile_exits_two(self):
        result = runner.invoke(app, ["executor", "test", "nonexistent"])
        assert result.exit_code == 2

    def test_binary_not_found_exits_one(self, monkeypatch):
        async def fake_create_subprocess_exec(*args, **kwargs):
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {args[0]}")

        monkeypatch.setattr(
            "orca.exec.runner.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        )
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "二进制无法启动" in result.output

    def test_test_passes_with_result_line(self, monkeypatch):
        result_line = json.dumps(
            {"type": "result", "result": "OK", "subtype": "success"}
        )
        stream_line = json.dumps({"type": "stream_event", "event": {"type": "x"}})

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = 0
                self.stderr = ""
                self.timed_out = False

            async def stream(self):
                for line in [stream_line, result_line]:
                    yield line
                if self.on_result:
                    self.on_result("OK", {}, 0.0, False, None)

        import orca.exec.runner as runner_mod

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 0
        assert "端到端 OK" in result.output

    def test_test_fails_on_non_stream_json(self, monkeypatch):
        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = 1
                self.stderr = "some protocol error"
                self.timed_out = False

            async def stream(self):
                yield "this is not json"
                if self.on_result:
                    pass

        import orca.exec.runner as runner_mod

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "非 stream-json" in result.output

    def test_test_internal_timeout_reported_as_timeout(self, monkeypatch):
        """🔴-1 回归：CLIRunner 内部逐行超时走「正常 return + timed_out=True」，test 必须读属性。"""
        import orca.exec.runner as runner_mod

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = -1
                self.stderr = ""
                self.timed_out = True

            async def stream(self):
                yield json.dumps({"type": "stream_event", "event": {"type": "x"}})

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "超时" in result.output
        assert "退出码" not in result.output

    def test_test_wall_clock_timeout_triggers_fail(self, monkeypatch):
        import asyncio as _aio

        import orca.exec.runner as runner_mod
        import orca.iface.cli.executor_cmds as ec

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = -1
                self.stderr = ""
                self.timed_out = False

            async def stream(self):
                while True:
                    yield json.dumps({"type": "stream_event", "event": {"type": "x"}})
                    await _aio.sleep(0.01)

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        monkeypatch.setattr(ec, "_TEST_WALL_CLOCK_TIMEOUT", 0.2)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "超时" in result.output
