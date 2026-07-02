"""tests/profiles/test_registry.py —— registry：builtin/project 覆盖/disable/env 覆盖。

覆盖 SPEC §6.5 / §6.6：
  - load_builtin_profiles 自动发现 builtin/*.py
  - get_profile('claude') / get_profile('ccr') 返回 CliProfile
  - get_profile('nonexistent') 抛 ValueError（fail loud）
  - project 覆盖 builtin（.orca/profiles/*.py）
  - 损坏 profile 文件 → disable_profile + get_profile 抛清晰错误
  - env 覆盖：resolve_cli_path() 读 env > default
  - HARNESS_DISABLE_PROJECT_PROFILES=1 禁用 project 加载
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

import orca.profiles.registry as reg
from orca.profiles import (
    CliProfile,
    ProviderCapabilities,
    available_profiles,
    disable_profile,
    get_profile,
    register,
)
from orca.profiles.registry import (
    load_builtin_profiles,
    load_project_profiles,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """每个测试前重置注册表（隔离全局状态，避免测试间污染）。"""
    reg._reset_for_test()
    yield
    reg._reset_for_test()


# ── helpers ──────────────────────────────────────────────────────────────────


def _profile(name: str, **cap_overrides) -> CliProfile:
    cap_kw = dict(
        mcp_tools=True, streaming_events=True, structured_output="native",
        interrupt=True, checkpoint_resume=True, usage_tracking=True,
        concurrent_safe=True,
    )
    cap_kw.update(cap_overrides)
    return CliProfile(
        name=name,
        capabilities=ProviderCapabilities(**cap_kw),
        cli_path_env=f"ORCA_{name.upper()}_CLI",
        default_cli_path=name,
        flags=(),
        prompt_channel="stdin",
        mcp_flag_template=None,
        env_overlay_prefixes=(),
        stream_format="text",
        translator=lambda l, s: [],
        result_extractor=lambda r: r,
    )


# ── builtin 自动发现 ─────────────────────────────────────────────────────────


def test_load_builtin_discovers_claude_and_ccr():
    load_builtin_profiles()
    names = available_profiles()
    assert "claude" in names
    assert "ccr" in names


def test_load_builtin_is_idempotent():
    load_builtin_profiles()
    load_builtin_profiles()  # 重复调用不重复加载
    assert available_profiles().count("claude") == 1


def test_get_profile_lazy_loads_builtin():
    """get_profile 首次调用惰性触发 load_builtin（不需显式 load）。"""
    p = get_profile("claude")
    assert isinstance(p, CliProfile)
    assert p.name == "claude"


def test_get_profile_ccr_returns_cli_profile():
    p = get_profile("ccr")
    assert p.default_cli_path == "ccr code"
    assert p.capabilities.mcp_tools is False  # ccr 不透传 mcp


def test_ccr_translator_reuses_claude_translator():
    """ccr 协议兼容 claude stream-json，translator 应复用 claude_translator（非 dummy）。

    锁住 builtin/ccr.py 的修复：之前用 ``_dummy_translator``（返回 ``[]``），导致
    ``executor: ccr`` 跑得起来但事件全丢。复用 claude_translator 后事件映射正常。
    """
    from orca.profiles.translators import claude_translator

    assert get_profile("ccr").translator is claude_translator


# ── 不存在 → ValueError（fail loud）──────────────────────────────────────────


def test_get_profile_unknown_raises_value_error():
    with pytest.raises(ValueError, match="未知 executor"):
        get_profile("nonexistent")


def test_get_profile_disabled_raises_with_reason():
    register(_profile("temp"))
    disable_profile("temp", "测试原因：模拟损坏")
    with pytest.raises(ValueError) as exc:
        get_profile("temp")
    assert "测试原因" in str(exc.value)


# ── project 覆盖 builtin ─────────────────────────────────────────────────────


def test_project_profile_overrides_builtin(tmp_path):
    """./.orca/profiles/claude.py 覆盖 builtin claude（SPEC §4.6 / §6.5）。"""
    proj_dir = tmp_path / ".orca" / "profiles"
    proj_dir.mkdir(parents=True)
    (proj_dir / "claude.py").write_text(textwrap.dedent("""
        from orca.profiles.base import CliProfile
        from orca.profiles.capabilities import ProviderCapabilities

        PROFILE = CliProfile(
            name="claude",
            capabilities=ProviderCapabilities(
                mcp_tools=False, streaming_events=True, structured_output="none",
                interrupt=False, checkpoint_resume=False, usage_tracking=False,
                concurrent_safe=False,
            ),
            cli_path_env="ORCA_CLAUDE_CLI",
            default_cli_path="claude-overridden",
            flags=(),
            prompt_channel="stdin",
            mcp_flag_template=None,
            env_overlay_prefixes=(),
            stream_format="text",
            translator=lambda l, s: [],
            result_extractor=lambda r: r,
        )
    """), encoding="utf-8")

    load_builtin_profiles()
    load_project_profiles(tmp_path)
    p = get_profile("claude")
    assert p.default_cli_path == "claude-overridden"  # project 覆盖
    assert p.capabilities.concurrent_safe is False


def test_disable_project_profiles_env(tmp_path, monkeypatch):
    """HARNESS_DISABLE_PROJECT_PROFILES=1 时 project 不加载（SPEC §4.7）。"""
    proj_dir = tmp_path / ".orca" / "profiles"
    proj_dir.mkdir(parents=True)
    (proj_dir / "claude.py").write_text(
        "PROFILE = object()  # 占位（不会加载）", encoding="utf-8"
    )
    monkeypatch.setenv("HARNESS_DISABLE_PROJECT_PROFILES", "1")
    load_builtin_profiles()
    load_project_profiles(tmp_path)
    # builtin claude 仍在（project 未覆盖）
    assert get_profile("claude").default_cli_path == "claude"


# ── 损坏 profile → disable + fail loud ───────────────────────────────────────


def test_corrupt_project_profile_disables_and_fails_loud(tmp_path):
    """损坏 profile 文件（缺 PROFILE）→ disable + get_profile 抛清晰错误（SPEC §4.7 / §6.6）。

    fail loud：不静默丢，get_profile 抛带原因的 ValueError。
    """
    proj_dir = tmp_path / ".orca" / "profiles"
    proj_dir.mkdir(parents=True)
    (proj_dir / "broken.py").write_text("# no PROFILE here\nx = 1\n", encoding="utf-8")

    load_builtin_profiles()
    load_project_profiles(tmp_path)
    with pytest.raises(ValueError) as exc:
        get_profile("broken")
    assert "加载失败" in str(exc.value)


def test_syntax_error_project_profile_disables(tmp_path):
    """语法错 → disable（不抛到外层，get_profile 抛清晰错误）。"""
    proj_dir = tmp_path / ".orca" / "profiles"
    proj_dir.mkdir(parents=True)
    (proj_dir / "syntaxerr.py").write_text("def :( \n", encoding="utf-8")

    load_builtin_profiles()
    load_project_profiles(tmp_path)  # 不抛
    with pytest.raises(ValueError, match="加载失败"):
        get_profile("syntaxerr")


def test_wrong_type_profile_disables(tmp_path):
    """PROFILE 存在但类型错 → disable。"""
    proj_dir = tmp_path / ".orca" / "profiles"
    proj_dir.mkdir(parents=True)
    (proj_dir / "wrongtype.py").write_text("PROFILE = 'not a profile'\n", encoding="utf-8")

    load_builtin_profiles()
    load_project_profiles(tmp_path)
    with pytest.raises(ValueError, match="加载失败"):
        get_profile("wrongtype")


# ── env 覆盖：resolve_cli_path ───────────────────────────────────────────────


def test_resolve_cli_path_env_overrides_default(monkeypatch):
    """ORCA_CLAUDE_CLI=claude-ds-flash 时 resolve_cli_path() 返回覆盖值（SPEC §4.6 / §6.5）。

    二进制替换零代码改动（canary 切换无需重启）。
    """
    p = get_profile("claude")
    assert p.resolve_cli_path() == "claude"  # 默认
    monkeypatch.setenv("ORCA_CLAUDE_CLI", "claude-ds-flash")
    assert p.resolve_cli_path() == "claude-ds-flash"  # env 覆盖


def test_resolve_cli_path_runtime_read(monkeypatch):
    """resolve_cli_path 运行时读 env（canary 切换无需重启，SPEC §4.3）。"""
    p = get_profile("claude")
    monkeypatch.setenv("ORCA_CLAUDE_CLI", "v1")
    assert p.resolve_cli_path() == "v1"
    monkeypatch.setenv("ORCA_CLAUDE_CLI", "v2")
    assert p.resolve_cli_path() == "v2"  # 运行时读，不缓存


# ── register / disable_profile / available_profiles ──────────────────────────


def test_register_and_available_profiles():
    register(_profile("custom-a"))
    register(_profile("custom-b"))
    names = available_profiles()
    # builtin 也惰性加载了（available_profiles 内部 _ensure_loaded）
    assert "custom-a" in names
    assert "custom-b" in names


def test_register_recovers_from_disable():
    """被 disable 的 name 重新 register 成功即恢复。"""
    register(_profile("temp"))
    disable_profile("temp", "x")
    with pytest.raises(ValueError):
        get_profile("temp")
    register(_profile("temp"))
    assert get_profile("temp").name == "temp"
