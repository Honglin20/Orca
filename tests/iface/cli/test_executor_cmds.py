"""test_executor_cmds.py —— ``orca executor`` 子命令组 + config 模块单测（不真起进程）。

覆盖 plan 步骤 5：
  - ``config`` 模块：missing→{}、corrupt→warn+{}、atomic write、``apply_config_env``
    未知 profile warn+skip、``setdefault`` 尊重既有 env（env>config）
  - ``executor set/show/unset/list``：CliRunner + monkeypatch ``config_path`` 到 tmp_path、
    exit code、stdout
  - ``classify`` 纯函数全分支
  - ``executor test``：monkeypatch ``CLIRunner`` / ``create_subprocess_exec`` 模拟
    FileNotFoundError→FAIL、模拟 result 行→PASS（不真 spawn）
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from orca.iface.cli import config as config_mod
from orca.iface.cli.commands import app
from orca.iface.cli.executor_cmds import classify
from orca.profiles.registry import _reset_for_test


# ── fixture：隔离 config_path + registry ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试把 config_path 指到 tmp_path、清掉相关 env、重置 registry。

    避免污染真实 ``~/.orca/config.json``，且隔离 ``apply_config_env`` 写的 env。
    ``apply_config_env`` 用 ``os.environ.setdefault`` 直接写 env（非 monkeypatch.setenv），
    故 teardown 显式清理被写入的 ``ORCA_*_CLI``，防止污染后续测试（如
    ``test_registry.test_resolve_cli_path_env_overrides_default``）。
    """
    import os

    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: cfg_file)
    # 记录测试前已存在的 ORCA_*_CLI env（teardown 只清新增的，保留既有的原值）
    pre_env = {
        k: os.environ[k] for k in list(os.environ) if k.startswith("ORCA_") and k.endswith("_CLI")
    }
    # 清掉可能影响 resolve_cli_path 的 env（确保 default 生效）
    for key in list(os.environ):
        if key.startswith("ORCA_") and key.endswith("_CLI"):
            monkeypatch.delenv(key, raising=False)
    _reset_for_test()
    yield
    _reset_for_test()
    # teardown：清掉测试期间新增的 ORCA_*_CLI，还原既有值
    for key in list(os.environ):
        if key.startswith("ORCA_") and key.endswith("_CLI") and key not in pre_env:
            os.environ.pop(key, None)
    for key, val in pre_env.items():
        os.environ[key] = val


runner = CliRunner()


# ── config 模块 ───────────────────────────────────────────────────────────────


class TestConfigModule:
    """config_path / load_config / save_config / apply_config_env / bootstrap_config。"""

    def test_load_config_missing_returns_empty(self):
        """文件不存在 → {}。"""
        assert config_mod.load_config() == {}

    def test_load_config_corrupt_returns_empty_with_warning(self, caplog):
        """JSON 损坏 → warn + {}（不崩，降级为空配置）。"""
        config_mod.config_path().write_text("{ not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = config_mod.load_config()
        assert result == {}
        assert "损坏" in caplog.text

    def test_load_config_top_level_not_object_returns_empty(self, caplog):
        """顶层非 object（如 list）→ warn + {}。"""
        config_mod.config_path().write_text("[1, 2, 3]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = config_mod.load_config()
        assert result == {}

    def test_save_config_atomic_write(self):
        """save_config 写入 + 可读回（原子写 tmp+replace）。"""
        cfg = {"binaries": {"claude": "ccr code"}}
        config_mod.save_config(cfg)
        # 文件存在且内容正确
        raw = config_mod.config_path().read_text(encoding="utf-8")
        assert json.loads(raw) == cfg
        # tmp 文件已清理（os.replace 后 tmp 不残留）
        assert not config_mod.config_path().with_suffix(".json.tmp").exists()

    def test_save_config_creates_parent_dir(self, tmp_path: Path):
        """parent 目录不存在时 save_config 自动创建（mkdir parents）。"""
        nested = tmp_path / "deep" / "nest" / "config.json"
        # 用 patch 把 config_path 指到嵌套路径（覆盖 fixture 的 cfg_file）
        import orca.iface.cli.config as cm

        original = cm.config_path
        cm.config_path = lambda: nested
        try:
            cm.save_config({"binaries": {"claude": "x"}})
            assert nested.is_file()
        finally:
            cm.config_path = original

    def test_apply_config_env_unknown_profile_warns_and_skips(self, caplog):
        """config.binaries 含未知 profile → warn + skip（不阻断，对齐 disable 风格）。"""
        cfg = {"binaries": {"nonexistent_profile": "some-binary"}}
        with caplog.at_level("WARNING"):
            config_mod.apply_config_env(cfg)
        assert "nonexistent_profile" in caplog.text
        # 未写任何 ORCA_*_CLI env
        import os

        assert all(not k.startswith("ORCA_") for k in os.environ)

    def test_apply_config_env_sets_known_profile_env(self):
        """config.binaries 含已知 profile → setdefault 到对应 env var。"""
        cfg = {"binaries": {"claude": "ccr code"}}
        config_mod.apply_config_env(cfg)
        import os

        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"

    def test_apply_config_env_respects_existing_env(self, monkeypatch):
        """env > config：已存在的 env 不被 config 覆盖（setdefault 语义）。"""
        monkeypatch.setenv("ORCA_CLAUDE_CLI", "env-binary")
        cfg = {"binaries": {"claude": "config-binary"}}
        config_mod.apply_config_env(cfg)
        import os

        assert os.environ["ORCA_CLAUDE_CLI"] == "env-binary"

    def test_apply_config_env_skips_non_string_entries(self, caplog):
        """非字符串项（如 int）→ warn + skip（防御性）。"""
        cfg = {"binaries": {"claude": 123}}  # type: ignore[dict-item]
        with caplog.at_level("WARNING"):
            config_mod.apply_config_env(cfg)
        assert "非字符串" in caplog.text
        import os

        assert os.environ.get("ORCA_CLAUDE_CLI") is None

    def test_apply_config_env_no_binaries_key_is_noop(self):
        """无 binaries 键 → no-op（不抛）。"""
        config_mod.apply_config_env({"other": "x"})  # 不应抛
        config_mod.apply_config_env({})  # 空也不抛

    def test_bootstrap_config_loads_then_applies(self):
        """bootstrap_config = apply_config_env(load_config())，幂等。"""
        config_mod.save_config({"binaries": {"claude": "ccr code"}})
        config_mod.bootstrap_config()
        import os

        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"
        # 再调一次幂等（已 setdefault，不改变）
        config_mod.bootstrap_config()
        assert os.environ.get("ORCA_CLAUDE_CLI") == "ccr code"


# ── classify 纯函数（全分支）──────────────────────────────────────────────────


class TestClassify:
    """``classify`` 纯函数：覆盖 5 个判定分支。"""

    def test_timed_out_fail(self):
        ok, msg = classify(set(), False, -1, True, "")
        assert ok is False
        assert "超时" in msg

    def test_no_stream_events_fail_with_stderr(self):
        """无 stream-json 事件 → FAIL「非 stream-json」（附 stderr 片段）。"""
        ok, msg = classify(set(), False, 0, False, "some error output")
        assert ok is False
        assert "非 stream-json" in msg
        assert "some error output" in msg

    def test_no_stream_events_fail_no_stderr(self):
        ok, msg = classify(set(), False, 0, False, "")
        assert ok is False
        assert "无 stderr 输出" in msg

    def test_stream_events_nonzero_exit_no_result_fail(self):
        """有事件、exit!=0、无 result → FAIL「退出码」。"""
        ok, msg = classify({"stream_event"}, False, 1, False, "")
        assert ok is False
        assert "退出码 1" in msg

    def test_stream_events_with_result_nonzero_exit_pass(self):
        """有 result 行即使 exit!=0 也判 PASS（result 行说明端到端跑通）。"""
        ok, msg = classify({"result"}, True, 1, False, "")
        # saw_result 优先于 exit_code 判定（result 行 = 协议跑通）
        assert ok is True
        assert "端到端 OK" in msg

    def test_saw_result_pass(self):
        ok, msg = classify({"result", "stream_event"}, True, 0, False, "")
        assert ok is True
        assert "端到端 OK" in msg

    def test_stream_events_no_result_exit_zero_pass_warn(self):
        """有事件、exit=0、无 result → PASS + warn「未收到 result 行」。"""
        ok, msg = classify({"stream_event"}, False, 0, False, "")
        assert ok is True
        assert "未收到 result 行" in msg

    def test_stderr_snippet_truncated_to_500(self):
        """stderr 超 500 字符时只取前 500（防喷爆）。"""
        long_stderr = "x" * 600
        ok, msg = classify(set(), False, 0, False, long_stderr)
        assert ok is False
        # 截断后的内容长度（stderr 部分）
        assert "x" * 500 in msg
        assert "x" * 600 not in msg


# ── executor 子命令（CliRunner + monkeypatch）─────────────────────────────────


class TestExecutorSetUnsetShowList:
    """set / unset / show / list 命令：exit code + stdout + config 写入。"""

    def test_set_unknown_profile_exits_two(self):
        """未知 profile → exit 2（fail loud）。"""
        result = runner.invoke(app, ["executor", "set", "nonexistent", "x"])
        assert result.exit_code == 2
        assert "错误" in result.output

    def test_set_writes_config_and_exits_zero(self):
        """合法 profile → 写 config + exit 0 + 提示跑 test。"""
        result = runner.invoke(app, ["executor", "set", "claude", "ccr code"])
        assert result.exit_code == 0
        assert "已设置 claude" in result.output
        assert "test" in result.output.lower()
        # config 已写入
        cfg = json.loads(config_mod.config_path().read_text(encoding="utf-8"))
        assert cfg["binaries"]["claude"] == "ccr code"

    def test_unset_existing_override(self):
        """有 override 时 unset → 清除 + exit 0。"""
        config_mod.save_config({"binaries": {"claude": "ccr code"}})
        result = runner.invoke(app, ["executor", "unset", "claude"])
        assert result.exit_code == 0
        assert "已清除" in result.output
        cfg = json.loads(config_mod.config_path().read_text(encoding="utf-8"))
        assert "claude" not in cfg.get("binaries", {})

    def test_unset_no_override_is_noop(self):
        """无 override 时 unset → 友好提示 + exit 0（非错误）。"""
        result = runner.invoke(app, ["executor", "unset", "claude"])
        assert result.exit_code == 0
        assert "无 config override" in result.output

    def test_set_warns_on_non_dict_binaries(self, caplog):
        """config.binaries 非 dict（用户手改坏）→ warn + 重置为 dict（fail loud 不静默吞）。"""
        config_mod.save_config({"binaries": ["not", "a", "dict"]})
        with caplog.at_level("WARNING"):
            result = runner.invoke(app, ["executor", "set", "claude", "ccr code"])
        assert result.exit_code == 0
        assert "非 object" in caplog.text
        # 写回的 config 已清理为合法 dict
        cfg = json.loads(config_mod.config_path().read_text(encoding="utf-8"))
        assert cfg["binaries"] == {"claude": "ccr code"}

    def test_unset_warns_on_non_dict_binaries(self, caplog):
        """unset 同样对非 dict binaries warn（与 set / load_config 行为一致）。"""
        config_mod.save_config({"binaries": "oops"})
        with caplog.at_level("WARNING"):
            result = runner.invoke(app, ["executor", "unset", "claude"])
        assert result.exit_code == 0
        assert "非 object" in caplog.text

    def test_show_empty_config(self):
        """空 config → 显示（空）+ profile 列表。"""
        result = runner.invoke(app, ["executor", "show"])
        assert result.exit_code == 0
        assert "（空）" in result.output
        assert "claude" in result.output  # builtin profile 列出

    def test_show_with_override(self):
        """有 override → show 显示 effective + override 标记。"""
        config_mod.save_config({"binaries": {"claude": "ccr code"}})
        result = runner.invoke(app, ["executor", "show"])
        assert result.exit_code == 0
        assert "ccr code" in result.output
        assert "config override" in result.output

    def test_list_shows_profiles(self):
        """list → 列出可用 profile + env 名。"""
        result = runner.invoke(app, ["executor", "list"])
        assert result.exit_code == 0
        assert "claude" in result.output
        assert "ORCA_CLAUDE_CLI" in result.output

    def test_list_marks_overrides(self):
        """list → 被 override 的 profile 标 *。"""
        config_mod.save_config({"binaries": {"claude": "ccr code"}})
        result = runner.invoke(app, ["executor", "list"])
        assert result.exit_code == 0
        # claude 行带 * 标记
        lines = [l for l in result.output.splitlines() if "claude" in l]
        assert any("*" in l for l in lines)


# ── executor test（不真起进程，monkeypatch）────────────────────────────────────


class TestExecutorTest:
    """``orca executor test``：monkeypatch CLIRunner / create_subprocess_exec。"""

    def test_unknown_profile_exits_two(self):
        """未知 profile → exit 2。"""
        result = runner.invoke(app, ["executor", "test", "nonexistent"])
        assert result.exit_code == 2

    def test_binary_not_found_exits_one(self, monkeypatch):
        """模拟 FileNotFoundError（二进制不存在）→ FAIL exit 1（gotcha G5）。"""

        async def fake_create_subprocess_exec(*args, **kwargs):
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {args[0]}")

        # CLIRunner.stream 内部调 create_subprocess_exec；patch 它。
        monkeypatch.setattr(
            "orca.exec.runner.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        )
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "二进制无法启动" in result.output

    def test_test_passes_with_result_line(self, monkeypatch):
        """模拟 CLIRunner 吐 result 行 → PASS exit 0。"""
        # 构造假 stream：吐一行 result JSON + 一行 stream_event JSON，正常结束。
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
                # 模拟吐两行后正常 EOF
                for line in [stream_line, result_line]:
                    yield line
                # 触发 on_result（模拟 CLIRunner._maybe_fire_on_result）
                if self.on_result:
                    self.on_result("OK", {}, 0.0, False, None)

        # CLIRunner 在 test 命令内 ``from orca.exec.runner import CLIRunner`` 延迟 import，
        # 故 patch 源模块 ``orca.exec.runner.CLIRunner`` 即可拦截。
        import orca.exec.runner as runner_mod

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 0
        assert "端到端 OK" in result.output

    def test_test_fails_on_non_stream_json(self, monkeypatch):
        """模拟 CLIRunner 吐非 JSON 行 + 非零退出 → FAIL「非 stream-json」。"""

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = 1
                self.stderr = "some protocol error"
                self.timed_out = False

            async def stream(self):
                # 吐一行非 JSON（不收集 type）
                yield "this is not json"
                if self.on_result:
                    pass  # 无 result

        import orca.exec.runner as runner_mod

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "非 stream-json" in result.output

    def test_test_internal_timeout_reported_as_timeout(self, monkeypatch):
        """🔴-1 回归：CLIRunner 内部逐行超时（SpawnConfig.timeout=30 触发）走「正常结束
        生成器 + 标记 timed_out=True」路径，不抛异常。test 命令必须读 runner.timed_out
        属性，否则会误判为「退出码 -1」而非「超时」。
        """
        import orca.exec.runner as runner_mod
        import orca.iface.cli.executor_cmds as ec

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = -1  # 超时强杀后 returncode 未知
                self.stderr = ""
                self.timed_out = True  # 关键：模拟 CLIRunner._handle_timeout 已置

            async def stream(self):
                # 模拟卡死：吐一两行后内部超时，stream() 正常 return（不抛）
                yield json.dumps({"type": "stream_event", "event": {"type": "x"}})
                # CLIRunner 内部超时后 stream() return，不 yield 更多

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        # 必须是「超时」诊断，而非「退出码 -1」或「非 stream-json」
        assert "超时" in result.output
        assert "退出码" not in result.output

    def test_test_wall_clock_timeout_triggers_fail(self, monkeypatch):
        """gotcha G4：外层 ``asyncio.wait_for(60s)`` 兜底——子进程永不退出但持续吐行
        （绕过逐行 timeout=30）。把 ``_TEST_WALL_CLOCK_TIMEOUT`` 改小 + FakeRunner 永不
        结束 stream，模拟 wall-clock 触发。
        """
        import orca.exec.runner as runner_mod
        import orca.iface.cli.executor_cmds as ec

        class FakeRunner:
            def __init__(self, cfg, on_result=None):
                self.cfg = cfg
                self.on_result = on_result
                self.exit_code = -1
                self.stderr = ""
                self.timed_out = False  # 内部未超时（持续吐行绕过逐行 timeout）

            async def stream(self):
                # 模拟永续流：不停吐行，永不 EOF（绕过逐行 timeout 假设每行间隔 < timeout）
                import asyncio as _aio

                while True:
                    yield json.dumps({"type": "stream_event", "event": {"type": "x"}})
                    await _aio.sleep(0.01)

        monkeypatch.setattr(runner_mod, "CLIRunner", FakeRunner)
        # 把 wall-clock 上限改到 0.2s，让测试快速触发
        monkeypatch.setattr(ec, "_TEST_WALL_CLOCK_TIMEOUT", 0.2)
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "超时" in result.output
