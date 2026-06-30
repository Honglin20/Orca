"""test_context_registry.py —— session_id → (run_id, node) 映射（SPEC §6 / 计划 G3.2）。"""

from __future__ import annotations

from orca.gates.context_registry import SessionContextRegistry


def test_register_and_lookup():
    reg = SessionContextRegistry()
    reg.register("sess-1", "run-1", "node-1")
    ctx = reg.lookup("sess-1")
    assert ctx is not None
    assert ctx.run_id == "run-1"
    assert ctx.node == "node-1"


def test_lookup_unknown_returns_none():
    reg = SessionContextRegistry()
    assert reg.lookup("never-registered") is None


def test_unregister_then_lookup_none():
    reg = SessionContextRegistry()
    reg.register("sess-1", "run-1", "node-1")
    reg.unregister("sess-1")
    assert reg.lookup("sess-1") is None


def test_unregister_unknown_is_idempotent():
    """未注册的 unregister 静默忽略（幂等，方便 node 完成路径统一调用）。"""
    reg = SessionContextRegistry()
    reg.unregister("never-registered")  # 不抛


def test_reregister_overwrites():
    """同 session_id 重复 register → last-writer-wins（claude 重连场景）。"""
    reg = SessionContextRegistry()
    reg.register("sess-1", "run-1", "node-1")
    reg.register("sess-1", "run-2", "node-2")  # 覆盖
    ctx = reg.lookup("sess-1")
    assert ctx is not None
    assert ctx.run_id == "run-2"
    assert ctx.node == "node-2"
