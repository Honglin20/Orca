"""tests/exec/test_env.py —— build_env_overlay 单元（DRY 抽出的共享 env overlay，SPEC §2.6）。

覆盖：
  - 前缀匹配的 env 变量被透传（如 ANTHROPIC_API_KEY / CLAUDE_*）
  - 不匹配的不透传
  - 空 prefixes → 空 overlay
  - 多前缀任一命中即透传

抽这层的动机（review 裁定 Rule 6）：原三处内联重复（exec/claude/executor + exec/validator +
gates/dialog），第三处出现即触发 DRY 抽象。
"""

from __future__ import annotations

from orca.exec.env import build_env_overlay


def test_build_env_overlay_includes_matching_prefix(monkeypatch):
    """ANTHROPIC_ 前缀的 env 变量被透传。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://ccr.example.com")
    overlay = build_env_overlay(("ANTHROPIC_",))
    assert overlay["ANTHROPIC_API_KEY"] == "sk-test-123"
    assert overlay["ANTHROPIC_BASE_URL"] == "https://ccr.example.com"


def test_build_env_overlay_excludes_non_matching(monkeypatch):
    """不匹配前缀的 env 变量不透传（防泄漏无关变量给子进程）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HOME", "/Users/someone")  # 不应透传
    monkeypatch.setenv("PATH", "/usr/bin:/bin")   # 不应透传
    overlay = build_env_overlay(("ANTHROPIC_",))
    assert "ANTHROPIC_API_KEY" in overlay
    assert "HOME" not in overlay
    assert "PATH" not in overlay


def test_build_env_overlay_empty_prefixes_returns_empty():
    """空 prefixes → 空 overlay（不透传任何变量）。"""
    assert build_env_overlay(()) == {}


def test_build_env_overlay_multiple_prefixes_any_match(monkeypatch):
    """多前缀：任一命中即透传（ANTHROPIC_ 或 CLAUDE_）。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-1")
    monkeypatch.setenv("CLAUDE_CODE_TIMEOUT", "30")
    monkeypatch.setenv("UNRELATED_VAR", "x")
    overlay = build_env_overlay(("ANTHROPIC_", "CLAUDE_"))
    assert overlay["ANTHROPIC_API_KEY"] == "sk-1"
    assert overlay["CLAUDE_CODE_TIMEOUT"] == "30"
    assert "UNRELATED_VAR" not in overlay
