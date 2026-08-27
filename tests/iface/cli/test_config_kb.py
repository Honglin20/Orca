"""test_config_kb.py —— plan sprightly-questing-donut §1.2/§1.4 KB 解析 + 预检单测。

覆盖 code-reviewer 标出的关键契约（Rule 9：deterministic 分支逻辑）：
- resolve_kb_dir 优先级：env > config > ~/.orca/knowledge_base > cwd/knowledge_base（first-existing）。
- 显式来源（env/config）权威：设了但不存在 → ""（不静默回退到隐式来源）。
- apply_kb_requirement：无 requires → no-op（不写 env）；requires+[knowledge_base]+KB 在 → 写
  os.environ['ORCA_KB_DIR']；requires+[knowledge_base]+KB 缺 → ConfigurationError（含指引）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from orca.compile import ConfigurationError
from orca.iface.cli.config import (
    _INJECTED_KB_ENV,
    apply_kb_requirement,
    resolve_kb_dir,
)


class _WF:
    """轻量 workflow 替身（.requires + .workflows_root——per-wf KB 来源解析用）。"""
    def __init__(self, requires: list[str], workflows_root=None):
        self.requires = requires
        self.workflows_root = workflows_root


@pytest.fixture(autouse=True)
def _clean_kb_env(monkeypatch):
    """每个测试前后清 ORCA_KB_DIR + 注入标记集，防 shell / 跨测试残留污染
    （teardown 清集合：apply 直写 os.environ 不经 monkeypatch，标记集会跨测试存活）。"""
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)
    _INJECTED_KB_ENV.clear()
    yield
    _INJECTED_KB_ENV.clear()


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


# ── per-wf 来源（plan 2026-08-27 批 C，SPEC 步骤 2.3 R4'）────────────────────


def _make_per_wf_kb(wf_root) -> Path:
    """造 per-wf KB fixture：``wf_root/knowledge_base/index.json``（判据含 index.json）。"""
    kb = Path(wf_root) / "knowledge_base"
    kb.mkdir(parents=True)
    (kb / "index.json").write_text("{}", encoding="utf-8")
    return kb


def test_resolve_per_wf_kb_over_home_install_point(monkeypatch, tmp_path):
    """R4' ①：wf 带 per-wf KB → 优先于 ~/.orca/knowledge_base（隐式 install 部署点）。"""
    home = tmp_path / "fake_home"
    (home / ".orca" / "knowledge_base").mkdir(parents=True)  # home 部署点同时存在
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    wf_root = tmp_path / "wfs" / "struct-exploration"
    per_wf = _make_per_wf_kb(wf_root)

    out = resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf_root))

    assert out == str(per_wf.resolve())


def test_resolve_per_wf_kb_requires_index_json(monkeypatch, tmp_path):
    """per-wf 判据含 index.json：目录在但无 index.json → 不当 KB（防任意同名目录误命中）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.chdir(tmp_path)  # cwd 无 knowledge_base → 全 miss
    wf_root = tmp_path / "wfs" / "struct-exploration"
    (wf_root / "knowledge_base").mkdir(parents=True)  # 无 index.json

    assert resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf_root)) == ""


def test_resolve_explicit_env_not_overridden_by_per_wf(monkeypatch, tmp_path):
    """R4' ②：用户显式 env ORCA_KB_DIR（目录存在）→ 权威，不被 per-wf KB 覆盖。"""
    explicit = tmp_path / "explicit_kb"
    explicit.mkdir()
    (explicit / "index.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("ORCA_KB_DIR", str(explicit))
    wf_root = tmp_path / "wfs" / "struct-exploration"
    _make_per_wf_kb(wf_root)

    out = resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf_root))

    assert out == str(explicit.resolve())


def test_resolve_explicit_env_bad_path_still_empty_with_per_wf(monkeypatch, tmp_path):
    """显式 env 坏路径 + per-wf KB 存在 → 仍返 ""（显式来源权威、fail loud，
    不静默回退到 per-wf 隐式来源——与「config/env 显式坏路径不回退」契约同款）。"""
    monkeypatch.setenv("ORCA_KB_DIR", str(tmp_path / "does_not_exist"))
    wf_root = tmp_path / "wfs" / "struct-exploration"
    _make_per_wf_kb(wf_root)

    out = resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf_root))

    assert out == ""


def test_resolve_config_overrides_per_wf(monkeypatch, tmp_path):
    """config knowledge_base_dir > per-wf KB（探测序钉死：per-wf 在 config 之后——
    用户显式 config 覆盖随 workflow 走的 KB）。"""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "fake_home")
    monkeypatch.chdir(tmp_path)  # 项目级 config = tmp_path/.orca/config.json
    cfg_kb = tmp_path / "cfg_kb"
    cfg_kb.mkdir()
    cfg_dir = tmp_path / ".orca"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"knowledge_base_dir": str(cfg_kb)}), encoding="utf-8"
    )
    wf_root = tmp_path / "wfs" / "struct-exploration"
    _make_per_wf_kb(wf_root)

    out = resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf_root))

    assert out == str(cfg_kb.resolve())


def test_apply_then_second_wf_resolves_own_per_wf_kb(monkeypatch, tmp_path):
    """R4' ③：同进程串行两个 wf——① 无 per-wf KB（命中 ~/.orca 触发 env 注入），
    ② 有 per-wf KB → 解析到自己的 per-wf KB，而非注入残留被误判为用户显式 env。

    这是 ``_INJECTED_KB_ENV`` 防护的行为锁：若读 env 时不忽略注入串，第二个 resolve
    会返回 home 部署点（注入残留以最高优先遮蔽 per-wf 来源）。
    """
    home = tmp_path / "fake_home"
    home_kb = home / ".orca" / "knowledge_base"
    home_kb.mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.chdir(tmp_path)  # cwd 无 knowledge_base
    monkeypatch.delenv("ORCA_KB_DIR", raising=False)

    # wf1：无 per-wf KB → apply 命中 ~/.orca 并注入 os.environ
    wf1 = _WF(["knowledge_base"], workflows_root=tmp_path / "wfs" / "wf-no-kb")
    apply_kb_requirement(wf1)
    assert os.environ["ORCA_KB_DIR"] == str(home_kb.resolve())
    assert str(home_kb.resolve()) in _INJECTED_KB_ENV  # 注入已标记

    # wf2：有 per-wf KB → resolve 忽略注入残留，解析到自己的 per-wf KB
    wf2_root = tmp_path / "wfs" / "wf-with-kb"
    per_wf2 = _make_per_wf_kb(wf2_root)
    out = resolve_kb_dir(_WF(["knowledge_base"], workflows_root=wf2_root))
    assert out == str(per_wf2.resolve())
