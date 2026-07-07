"""test_executor_e2e.py —— ``orca executor`` 全链路端到端测试（无外部依赖）。

与 ``test_executor_cmds.py`` 单测的区别：单测 monkeypatch ``CLIRunner`` 为 ``FakeRunner``，
绕过了真 spawn 链路；本文件用**伪造的可执行脚本**当 backend binary，端到端驱动整条
``orca executor test`` 编排::

    resolve_cli_path → SpawnConfig → shlex.split → CLIRunner.stream()
        → create_subprocess_exec → stdin pump → _readlines → _maybe_fire_on_result
        → _record_type → classify → typer.Exit

**不 mock CLIRunner**——这条链路在单测里完全没人验证过（FakeRunner 跳过了 argv 拼装、
stdin pump、真 readline、result 行检测）。本文件锁住 CLI ↔ exec 层的契约。

设计要点：
  - 伪造脚本是真实可执行文件（``os.chmod 0o755``），往 stdout 吐符合 claude stream-json
    格式的 JSON 行后正常 exit 0；坏脚本吐非 JSON / exit 1；卡死脚本 sleep 不输出。
  - 用 ``sys.executable`` 跑 python（跨 python 环境稳，比 sh 脚本移植性好）。
  - 隔离 config：monkeypatch ``config_path`` 到 tmp_path + 清 ``ORCA_*_CLI`` env
    （复用单测的隔离模式，避免污染真实 ``~/.orca/config.json``）。
  - wall-clock timeout 用 ``executor_cmds._TEST_WALL_CLOCK_TIMEOUT`` seam 缩短
    （卡死脚本不能让测试等 60s）。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.cli import config as config_mod
from orca.iface.cli.commands import app
from orca.profiles.registry import _reset_for_test

# ── 伪造 backend 脚本模板 ─────────────────────────────────────────────────────
# 脚本读 stdin（claude profile prompt_channel=stdin，CLIRunner pump 后 close；脚本必须
# 读完或忽略 stdin EOF）。脚本吐出的 stdout 行需 ``rstrip("\\n")`` 后是合法 JSON 且顶层
# ``type`` ∈ stream-json 已知集合（``stream_event`` / ``result`` 等）。
#
# 用 ``sys.executable`` 而非 ``sh``：项目 Unix-only 但 python 解释器路径稳定，且能
# 复用同一 python 环境（避免 sh 脚本里 ``echo`` 的转义坑）。

_GOOD_SCRIPT = """\
import sys
# 读完 stdin（CLIRunner pump 后 close，read() 返回 ""）
sys.stdin.read()
# 吐一行 stream_event + 一行 result（subtype=success），模拟 claude stream-json
print('{"type":"stream_event","event":{"type":"content_block_delta"}}', flush=True)
print('{"type":"result","subtype":"success","result":"OK","is_error":false}', flush=True)
sys.exit(0)
"""

_BAD_JSON_SCRIPT = """\
import sys
sys.stdin.read()
# 非 JSON 行：classify 会判「非 stream-json / 协议不兼容」
sys.stderr.write("totally not stream-json protocol\\n")
print("garbage line that is not json", flush=True)
sys.exit(1)
"""

_NONZERO_EXIT_SCRIPT = """\
import sys
sys.stdin.read()
# 吐 stream 事件但 exit 1 且无 result 行 → classify 判「退出码 1」
print('{"type":"stream_event","event":{"type":"x"}}', flush=True)
sys.exit(1)
"""

# 卡死脚本：不停吐行绕过逐行 timeout（SpawnConfig.timeout=30），但永不 EOF → 触发外层
# wall-clock timeout（_TEST_WALL_CLOCK_TIMEOUT）。
_STUCK_SCRIPT = """\
import sys, time
sys.stdin.read()
while True:
    print('{"type":"stream_event","event":{"type":"x"}}', flush=True)
    time.sleep(0.05)
"""


def _write_exec_script(tmp_path: Path, name: str, source: str) -> Path:
    """写一个 python 脚本到 tmp_path 并 chmod 0o755，返回路径。

    脚本内容用 ``#!{sys.executable}`` shebang，``os.chmod`` 让 ``create_subprocess_exec``
    能直接 exec（非 shell）。
    """
    script = tmp_path / name
    script.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    os.chmod(script, 0o755)
    return script


# ── fixture：隔离 config_path + env + registry（复用单测的隔离模式）─────────────


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """每个测试把 config_path / project_config_path 指到 tmp_path、清 ``ORCA_*`` env、重置 registry。

    ``apply_config_env`` 用 ``os.environ.setdefault`` 直接写 env（非 monkeypatch.setenv），
    故 teardown 显式清理被写入的 ``ORCA_*``，防污染后续测试。重置 ``_shell_env_snapshot``
    让 show 来源判定每测试重抓。两层 config 都隔离，避免 ``set --scope project``（默认）
    污染真实仓库 ``./.orca/config.json``。
    """
    user_cfg = tmp_path / "user_config.json"
    proj_cfg = tmp_path / "proj" / ".orca" / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: user_cfg)
    monkeypatch.setattr(config_mod, "project_config_path", lambda: proj_cfg)
    pre_env = {k: os.environ[k] for k in list(os.environ) if k.startswith("ORCA_")}
    for key in list(os.environ):
        if key.startswith("ORCA_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_mod, "_shell_env_snapshot", None)
    _reset_for_test()
    yield
    _reset_for_test()
    for key in list(os.environ):
        if key.startswith("ORCA_") and key not in pre_env:
            os.environ.pop(key, None)
    for key, val in pre_env.items():
        os.environ[key] = val


runner = CliRunner()


# ── 全链路 PASS：伪造好脚本，端到端跑通 ─────────────────────────────────────────


class TestExecutorTestFullSpawnChain:
    """``orca executor test`` 走真 ``CLIRunner`` spawn 链路（不 mock）。

    每个测试用 ``ORCA_CLAUDE_CLI=<script>`` 让 ``resolve_cli_path`` 返回脚本路径，
    profile flags 经 ``shlex.split`` + ``create_subprocess_exec`` 真起子进程。
    """

    def test_good_script_passes_end_to_end(self, tmp_path: Path, monkeypatch):
        """好脚本（吐 stream_event + result）→ exit 0 + 端到端 OK。

        验证全链路：resolve_cli_path → SpawnConfig → shlex.split → CLIRunner.stream()
        → stdin pump → readline → _maybe_fire_on_result(置 saw_result) → _record_type
        (收集 type) → classify(seen={stream_event,result}, saw_result=True) → PASS。
        """
        script = _write_exec_script(tmp_path, "good_backend.py", _GOOD_SCRIPT)
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(script))

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 0, f"stdout={result.output!r}"
        assert "端到端 OK" in result.output

    def test_bad_json_script_fails_protocol_incompatible(
        self, tmp_path: Path, monkeypatch
    ):
        """坏脚本（吐非 JSON + exit 1）→ exit 1 + 非 stream-json 诊断。

        classify(seen=set(), saw_result=False, exit=1) → 走「无 stream-json 事件」分支
        （stderr 片段进诊断消息）。
        """
        script = _write_exec_script(tmp_path, "bad_json_backend.py", _BAD_JSON_SCRIPT)
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(script))

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1, f"stdout={result.output!r}"
        assert "非 stream-json" in result.output
        # stderr 片段被纳入诊断（截断到 500 字符内）
        assert "totally not stream-json protocol" in result.output

    def test_nonzero_exit_with_stream_events_fails_on_exit_code(
        self, tmp_path: Path, monkeypatch
    ):
        """吐 stream 事件但 exit 1 且无 result → classify 判「退出码 1」。

        classify(seen={stream_event}, saw_result=False, exit=1, timed_out=False) →
        走「有事件 + exit!=0 + 无 result」分支（区别于「非 stream-json」分支）。
        """
        script = _write_exec_script(
            tmp_path, "nonzero_backend.py", _NONZERO_EXIT_SCRIPT
        )
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(script))

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1, f"stdout={result.output!r}"
        assert "退出码 1" in result.output

    def test_stuck_script_triggers_wall_clock_timeout(
        self, tmp_path: Path, monkeypatch
    ):
        """卡死脚本（持续吐行绕过逐行 timeout=30，永不 EOF）→ 外层 wall-clock 触发。

        用 ``_TEST_WALL_CLOCK_TIMEOUT`` seam 缩到 0.5s，让测试快速触发。这条路径走
        ``asyncio.wait_for`` 的 ``TimeoutError`` 分支（区别于 CLIRunner 内部逐行超时）。
        """
        script = _write_exec_script(tmp_path, "stuck_backend.py", _STUCK_SCRIPT)
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(script))
        # 缩短 wall-clock 上限（seam 在 executor_cmds 模块级常量）。
        import orca.iface.cli.executor_cmds as ec

        monkeypatch.setattr(ec, "_TEST_WALL_CLOCK_TIMEOUT", 0.5)

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1, f"stdout={result.output!r}"
        assert "超时" in result.output

    def test_binary_not_found_exits_one(self, monkeypatch):
        """``ORCA_CLAUDE_CLI`` 指向不存在路径 → spawn 前 FileNotFoundError → exit 1。

        走 ``test_binary`` 的 ``except (FileNotFoundError, PermissionError, OSError)``
        分支（gotcha G5）。这条路径单测也覆盖（monkeypatch create_subprocess_exec），
        但此处走真 ``create_subprocess_exec``，验证 OS 真的抛 FileNotFoundError 而非
        被某层吞掉。
        """
        monkeypatch.setenv("ORCA_CLAUDE_CLI", "/definitely/does/not/exist/claude")

        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 1
        assert "二进制无法启动" in result.output


# ── 配置生命周期：set → show → unset（端到端经 CLI）─────────────────────────────


class TestExecutorConfigLifecycle:
    """``set``/``show``/``unset`` 的端到端往返：config 文件真写、真读、真生效。

    单测分别测了 set 写 config / show 读 config，但没测 **往返**：set 后 show 显示
    effective = 写入值；unset 后 show 回 default。本类锁住配置 round-trip。
    """

    def test_set_then_show_displays_effective_binary(
        self, tmp_path: Path, monkeypatch
    ):
        """set 写 config → show 显示 effective = 写入值 + 来源标注。"""
        script = _write_exec_script(tmp_path, "good_backend.py", _GOOD_SCRIPT)
        set_result = runner.invoke(
            app, ["executor", "set", "claude", "--binary", str(script)]
        )
        assert set_result.exit_code == 0
        assert "已写入" in set_result.output

        # show 读回：生效命令含脚本路径（项目 config override 生效）
        show_result = runner.invoke(app, ["executor", "show", "claude"])
        assert show_result.exit_code == 0
        assert str(script) in show_result.output
        assert "← 项目" in show_result.output  # 来源标注

        # config 文件确实写入（默认 scope=project）
        cfg = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert cfg["binaries"]["claude"] == str(script)

    def test_unset_restores_default_binary(self, tmp_path: Path):
        """set 后 unset → show 回 default（``claude``）。"""
        script = _write_exec_script(tmp_path, "lifecycle_backend.py", _GOOD_SCRIPT)
        runner.invoke(app, ["executor", "set", "claude", "--binary", str(script)])

        unset_result = runner.invoke(app, ["executor", "unset", "claude"])
        assert unset_result.exit_code == 0
        assert "清除" in unset_result.output

        # show 应回到 default
        show_result = runner.invoke(app, ["executor", "show", "claude"])
        assert show_result.exit_code == 0
        assert "← default" in show_result.output
        assert str(script) not in show_result.output
        # config 文件中 claude 已移除
        cfg = json.loads(config_mod.project_config_path().read_text(encoding="utf-8"))
        assert "claude" not in cfg.get("binaries", {})

    def test_set_multi_token_binary_then_test_uses_it(
        self, tmp_path: Path, monkeypatch
    ):
        """set 多 token binary（如 ``ccr code``）→ ``shlex.split`` 拆 argv 真起。

        构造一个「包装脚本」模拟 ``ccr code``：用 ``ORCA_CLAUDE_CLI`` 不好模拟多 token
        （env var 是单值），但 ``set`` 命令的 binary 参数可以是多 token 串。set 写
        config 后 ``test`` 读 config → ``resolve_cli_path`` 返 ``"ccr code"`` →
        ``shlex.split`` → argv[0]=ccr argv[1]=code。

        为让 ``create_subprocess_exec`` 真能 exec ``ccr``，我们把脚本命名为 ``ccr``
        放进 tmp_path，再把 tmp_path prepend 到 PATH（monkeypatch）。脚本的 argv[1]
        是 ``code``（被忽略），脚本本身吐 stream-json。
        """
        # 伪造 ccr 可执行文件（无扩展名，放 tmp_path/bin）
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        ccr_script = bin_dir / "ccr"
        ccr_script.write_text(
            f"#!{sys.executable}\n{_GOOD_SCRIPT}", encoding="utf-8"
        )
        os.chmod(ccr_script, 0o755)
        # prepend tmp_path/bin 到 PATH（让 create_subprocess_exec 找到 ccr）
        monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

        # set 多 token binary → 写 config
        set_result = runner.invoke(
            app, ["executor", "set", "claude", "--binary", "ccr code"]
        )
        assert set_result.exit_code == 0

        # test 读 config → shlex.split("ccr code") → argv=["ccr","code"] → 真起 ccr 脚本
        test_result = runner.invoke(app, ["executor", "test", "claude"])
        assert test_result.exit_code == 0, f"stdout={test_result.output!r}"
        assert "端到端 OK" in test_result.output

    def test_env_override_takes_precedence_over_config(
        self, tmp_path: Path, monkeypatch
    ):
        """env > config 优先级：config 写脚本 A，env 设脚本 B → test 走 env 的 B。

        这是 ``apply_config_env`` 用 ``os.environ.setdefault``（非 ``=``）的核心契约：
        显式 export 永远赢。单测在 ``apply_config_env`` 层验证过，此处验证 ``test``
        命令端到端也遵守（``resolve_cli_path`` 读 env 而非 config）。
        """
        # config 写脚本 A
        script_a = _write_exec_script(tmp_path, "backend_a.py", _GOOD_SCRIPT)
        runner.invoke(app, ["executor", "set", "claude", "--binary", str(script_a)])

        # env 设脚本 B（也吐 result，内容不同便于区分）
        script_b = _write_exec_script(
            tmp_path,
            "backend_b.py",
            _GOOD_SCRIPT.replace('"result":"OK"', '"result":"FROM_B"'),
        )
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(script_b))

        # test 应走 env 的 B（仍 PASS，但用的是 B 而非 config 的 A）
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 0
        assert "端到端 OK" in result.output

        # 反证：若走 config 的 A 也 PASS，无法区分；故再写一个 env 指向坏脚本，
        # config 指向好脚本，断言走 env 的坏脚本 → FAIL（证 env 赢）。
        bad_script = _write_exec_script(
            tmp_path, "backend_bad.py", _BAD_JSON_SCRIPT
        )
        # config 已是 script_a（好）；env 改指向 bad
        monkeypatch.setenv("ORCA_CLAUDE_CLI", str(bad_script))
        result2 = runner.invoke(app, ["executor", "test", "claude"])
        assert result2.exit_code == 1
        assert "非 stream-json" in result2.output
