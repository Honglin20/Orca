"""test_executor_integration.py —— ``orca executor test claude`` 真 spawn 集成测试。

标 ``@pytest.mark.integration``：CI 默认不跑（``-m "not integration"`` 跳过），本地
``pytest -m integration tests/iface/cli/test_executor_integration.py`` 可选跑。

与 ``test_executor_e2e.py`` 的区别：e2e 用伪造脚本验证编排链路；本文件真 spawn 系统
``claude`` CLI（需 ``ANTHROPIC_API_KEY``），是「真后端」烟雾测试。

前置：
  - 本机装了 claude CLI（``shutil.which("claude")`` 非 None）
  - 配了 API key（``ANTHROPIC_API_KEY``）

覆盖 plan 步骤 5 验证节：「真跑 ``orca executor test claude``，断言 exit 0 + OK」。
"""

from __future__ import annotations

import os
import shutil

import pytest
from typer.testing import CliRunner

from orca.iface.cli import config as config_mod
from orca.iface.cli.commands import app
from orca.profiles.registry import _reset_for_test

# 全模块标 integration：CI 跳过，本地显式跑（对齐 tests/iface/cli/test_integration.py）。
pytestmark = pytest.mark.integration


def _claude_and_key_available() -> bool:
    """真 spawn claude 需 claude CLI 在 PATH + ANTHROPIC_API_KEY 已配。缺则 skip。"""
    return shutil.which("claude") is not None and bool(
        os.environ.get("ANTHROPIC_API_KEY")
    )


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """隔离 config_path + env + registry（与 e2e / 单测同隔离模式）。

    集成测试不能让真实 ``~/.orca/config.json`` 的 override 干扰（如用户本地配了 ccr），
    故仍把 config_path 指到 tmp_path，让 ``ORCA_CLAUDE_CLI`` 走 profile default = ``claude``。
    """
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "config_path", lambda: cfg_file)
    pre_env = {
        k: os.environ[k]
        for k in list(os.environ)
        if k.startswith("ORCA_") and k.endswith("_CLI")
    }
    for key in list(os.environ):
        if key.startswith("ORCA_") and key.endswith("_CLI"):
            monkeypatch.delenv(key, raising=False)
    _reset_for_test()
    yield
    _reset_for_test()
    for key in list(os.environ):
        if key.startswith("ORCA_") and key.endswith("_CLI") and key not in pre_env:
            os.environ.pop(key, None)
    for key, val in pre_env.items():
        os.environ[key] = val


runner = CliRunner()


@pytest.mark.skipif(
    not _claude_and_key_available(),
    reason="需 claude CLI + ANTHROPIC_API_KEY（集成测试真 spawn）",
)
class TestExecutorTestRealClaude:
    """真 spawn ``claude -p`` 跑极简 prompt，断言 ``orca executor test`` PASS。

    不跑断言 claude 输出细节（非确定），只断言 exit 0 + 「端到端 OK」收尾消息
    （即 classify 判 PASS，说明真链路 result 行被检测到）。
    """

    def test_real_claude_passes(self):
        """``orca executor test claude`` → exit 0 + 端到端 OK（真 claude）。"""
        result = runner.invoke(app, ["executor", "test", "claude"])
        assert result.exit_code == 0, f"stdout={result.output!r}"
        # classify 的 PASS 消息（收到 result 行）
        assert "端到端 OK" in result.output or "PASS" in result.output

    def test_real_claude_default_profile_arg(self):
        """``orca executor test``（省略 profile，默认 claude）→ 同上 PASS。

        验证 ``profile: str = typer.Argument("claude")`` 默认值生效。
        """
        result = runner.invoke(app, ["executor", "test"])
        assert result.exit_code == 0, f"stdout={result.output!r}"
        assert "端到端 OK" in result.output or "PASS" in result.output
