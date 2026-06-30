"""tests/exec/test_factory.py —— make_executor 分派（SPEC §7.8 / 计划 E.4）。

覆盖：
  - AgentNode → ClaudeExecutor（用 builtin claude profile）
  - ScriptNode → ScriptExecutor
  - SetNode → SetExecutor
  - ForeachNode → NotImplementedError
  - AgentNode executor="nonexistent" → 透传 get_profile 的 ValueError（fail loud）
"""

from __future__ import annotations

import pytest

from orca.exec import make_executor
from orca.exec.claude.executor import ClaudeExecutor
from orca.exec.script import ScriptExecutor
from orca.exec.set_node import SetExecutor
from orca.schema import AgentNode, ForeachNode, ScriptNode, SetNode

# profiles 注册表重置由 tests/exec/conftest.py 的 autouse fixture 负责。


# ── 分派正确 ─────────────────────────────────────────────────────────────────


def test_dispatch_agent_to_claude_executor():
    exe = make_executor(AgentNode(name="a", executor="claude"))
    assert isinstance(exe, ClaudeExecutor)
    assert exe.profile.name == "claude"  # 用 builtin claude profile


def test_dispatch_agent_default_executor_is_claude():
    """AgentNode.executor 默认 "claude"（SPEC §7.8）。"""
    exe = make_executor(AgentNode(name="a"))
    assert isinstance(exe, ClaudeExecutor)


def test_dispatch_script_to_script_executor():
    exe = make_executor(ScriptNode(name="s", command="echo hi"))
    assert isinstance(exe, ScriptExecutor)


def test_dispatch_set_to_set_executor():
    exe = make_executor(SetNode(name="st", values={"a": "1"}))
    assert isinstance(exe, SetExecutor)


def test_dispatch_foreach_raises_not_implemented():
    """ForeachNode 归 phase 5 编排层（SPEC §7.8 / §5 边界）。"""
    node = ForeachNode(name="fe", source="x.body", body=AgentNode(name="b"))
    with pytest.raises(NotImplementedError, match="phase 5"):
        make_executor(node)


# ── fail loud：未知 executor 透传 get_profile 的 ValueError ──────────────────


def test_unknown_executor_propagates_value_error():
    """AgentNode.executor="nonexistent" → get_profile ValueError 透传（fail loud，SPEC §7.8）。

    不静默兜底成 claude；错误带「未知 executor」+ available 列表。
    """
    with pytest.raises(ValueError, match="未知 executor"):
        make_executor(AgentNode(name="a", executor="nonexistent-backend"))


def test_disabled_executor_propagates_value_error():
    """被 disable 的 executor → get_profile ValueError 透传（附 disable 原因）。

    顺序：先 load builtin（注册 claude）→ disable（标记 _DISABLED）→ make_executor
    触发 get_profile 抛错。registry 的 load 是幂等的，disable 后不会因 _ensure_loaded 重新注册
    （_BUILTIN_LOADED 标记保留）。
    """
    from orca.profiles import disable_profile, load_builtin_profiles

    load_builtin_profiles()  # 先注册 claude（_BUILTIN_LOADED=True，后续不再重载）
    disable_profile("claude", "测试：模拟 disable")
    with pytest.raises(ValueError) as exc:
        make_executor(AgentNode(name="a", executor="claude"))
    assert "测试：模拟 disable" in str(exc.value)
