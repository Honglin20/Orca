"""test_config_kb.py —— plan sprightly-questing-donut §1.2/§1.4 KB 解析 + 预检单测。

覆盖 code-reviewer 标出的关键契约（Rule 9：deterministic 分支逻辑）：
- resolve_kb_dir 优先级：env > config > ~/.orca/knowledge_base > cwd/knowledge_base（first-existing）。
- 显式来源（env/config）权威：设了但不存在 → ""（不静默回退到隐式来源）。
- apply_kb_requirement：无 requires → no-op（不写 env）；requires+[knowledge_base]+KB 在 → 写
  os.environ['ORCA_KB_DIR']；requires+[knowledge_base]+KB 缺 → ConfigurationError（含指引）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orca.compile import ConfigurationError
from orca.iface.cli.config import apply_kb_requirement, resolve_kb_dir


class _WF:
    """轻量 workflow 替身（只需 .requires 属性）。"""
    def __init__(self, requires: list[str]):
        self.requires = requires


@pytest.fixture(autouse=True)
def _clean_kb_env(monkeypatch):
    """每个测试前清 ORCA_KB_DIR，防 shell 残留污染。"""
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)


# ── resolve_kb_dir ────────────────────────────────────────────

def test_resolve_prefers_env_over_implicit(monkeypatch, tmp_path):
    """env ORCA_KB_DIR 显式且存在 → 用它（优先于 ~/.orca / cwd）。"""
    kb = tmp_path / "my_kb"
    kb.mkdir()
    (kb / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ORCA_KB_DIR", str(kb))
    assert resolve_kb_dir() == str(kb.resolve())


def test_resolve_explicit_env_missing_returns_empty(monkeypatch, tmp_path):
    """env 显式但目录不存在 → ""（不静默回退到隐式来源——fail-loud 暴露错路径）。"""
    monkeypatch.setenv("ORCA_KB_DIR", str(tmp_path / "does_not_exist"))
    # 同时屏蔽隐式来源，确保不是因为回退
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.chdir(tmp_path)  # tmp_path 下无 knowledge_base/
    assert resolve_kb_dir() == ""


def test_resolve_implicit_cwd_knowledge_base(monkeypatch, tmp_path):
    """无 env/config → 回退 cwd/knowledge_base（仓库根 fallback）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")  # 屏蔽 ~/.orca
    # 用真实仓库根（tests/iface/cli/ 下 3 级 → parents[3] = repo root，有 knowledge_base/）
    monkeypatch.chdir(Path(__file__).resolve().parents[3])
    kb = resolve_kb_dir()
    assert kb.endswith("knowledge_base")


# ── apply_kb_requirement ──────────────────────────────────────

def test_apply_no_requires_is_noop(monkeypatch):
    """无 knowledge_base 依赖 → no-op（不抛、不写 env）。"""
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)
    apply_kb_requirement(_WF([]))
    assert "ORCA_KB_DIR" not in os.environ


def test_apply_requires_with_kb_writes_env(monkeypatch, tmp_path):
    """requires=[knowledge_base] + KB 存在 → 写 os.environ['ORCA_KB_DIR']（exec transport）。"""
    kb = tmp_path / "kb"
    kb.mkdir()
    monkeypatch.setenv("ORCA_KB_DIR", str(kb))
    apply_kb_requirement(_WF(["knowledge_base"]))
    assert os.environ["ORCA_KB_DIR"] == str(kb.resolve())


def test_apply_requires_kb_missing_raises(monkeypatch, tmp_path):
    """requires=[knowledge_base] + KB 解析不到 → ConfigurationError（含 searched 路径 + 修复指引）。"""
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")  # ~/.orca/knowledge_base 不存在
    monkeypatch.chdir(tmp_path)  # cwd 下无 knowledge_base/
    with pytest.raises(ConfigurationError) as ei:
        apply_kb_requirement(_WF(["knowledge_base"]))
    msg = str(ei.value)
    assert "knowledge_base" in msg
    assert "orca install" in msg  # 修复指引
    assert "ORCA_KB_DIR" in msg  # searched 路径


def test_apply_unknown_requires_token_passes(monkeypatch):
    """requires 含未知 token → apply_kb_requirement 只认 'knowledge_base'，其他 no-op（白名单校验在 schema 层）。"""
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)
    apply_kb_requirement(_WF(["something_else"]))  # 不抛、不写 env
    assert "ORCA_KB_DIR" not in os.environ
