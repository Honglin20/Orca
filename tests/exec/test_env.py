"""tests/exec/test_env.py —— build_env_overlay 单元（DRY 抽出的共享 env overlay，SPEC §2.6）。

覆盖：
  - 前缀匹配的 env 变量被透传（如 ANTHROPIC_API_KEY / CLAUDE_*）
  - 不匹配的不透传
  - 空 prefixes → 空 overlay
  - 多前缀任一命中即透传
  - phase-13 §2：4 个 ORCA_* keyword 注入（chart 路由）+ 缺省不注（backward compat）

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


# ── phase-13 §2：ORCA_* chart 路由注入（缺省不注 → backward compat）────────


def test_build_env_overlay_no_chart_kwargs_no_inject():
    """旧调用方式 ``build_env_overlay(prefixes)`` 不注任何 ORCA_*（backward compat）。

    意图：phase-13 引入 4 个 keyword 之前，既有调用方（validator / dialog / 旧 executor
    测试）不传任何 keyword → env overlay 必须不含 ORCA_*，行为与重构前完全一致。
    """
    overlay = build_env_overlay(("ANTHROPIC_",))
    assert "ORCA_RUN_ID" not in overlay
    assert "ORCA_NODE" not in overlay
    assert "ORCA_SESSION_ID" not in overlay
    assert "ORCA_CHART_SOCK" not in overlay


def test_build_env_overlay_injects_all_four_orca_chart_vars(monkeypatch):
    """4 个 keyword 全传 → overlay 含全部 ORCA_* 路由变量。

    意图：ClaudeExecutor spawn 时显式传 4 个 keyword（phase-13 §2.2），子进程 env 必含
    全部 4 个 ORCA_*（script 端 render_chart 据此路由 chart 事件）。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-1")  # 确保 prefix 透传仍工作
    overlay = build_env_overlay(
        ("ANTHROPIC_",),
        run_id="demo-abc123",
        node="train",
        session_id="sess-xyz",
        chart_sock="/tmp/orca-runs/demo-abc123.sock",
    )
    assert overlay["ORCA_RUN_ID"] == "demo-abc123"
    assert overlay["ORCA_NODE"] == "train"
    assert overlay["ORCA_SESSION_ID"] == "sess-xyz"
    assert overlay["ORCA_CHART_SOCK"] == "/tmp/orca-runs/demo-abc123.sock"
    # prefix 透传仍工作（ORCA_* 与 ANTHROPIC_* 共存）
    assert overlay["ANTHROPIC_API_KEY"] == "sk-1"


def test_build_env_overlay_partial_kwargs_partial_inject():
    """仅传 run_id/node → 仅注 2 个 ORCA_*，其余 2 个不出现在 overlay。

    意图：keyword 缺省 = 空串 = 不注（不是注空串值）。这对后续 executor 渐进迁移重要
    （如先接 run_id/node，session_id/chart_sock 后接，过渡期不会污染 env）。
    """
    overlay = build_env_overlay(
        (),
        run_id="demo-1",
        node="train",
        # session_id / chart_sock 故意缺省
    )
    assert overlay["ORCA_RUN_ID"] == "demo-1"
    assert overlay["ORCA_NODE"] == "train"
    assert "ORCA_SESSION_ID" not in overlay
    assert "ORCA_CHART_SOCK" not in overlay


def test_build_env_overlay_empty_string_kwarg_not_injected():
    """显式传空串 keyword → 不注（与缺省等价，防 ``overlay[k]="")`` 污染 env）。

    意图：调用方某些场景可能显式传 ``chart_sock=""``（如运行时尚未决定 sock 路径），
    应等价于缺省——不注 ORCA_CHART_SOCK，而非注入空值（空值会让 script 端 §7.1 fail
    loud 信息更难定位）。
    """
    overlay = build_env_overlay(
        (),
        run_id="demo-1",
        node="",
        session_id="",
        chart_sock="",
    )
    assert overlay["ORCA_RUN_ID"] == "demo-1"
    assert "ORCA_NODE" not in overlay
    assert "ORCA_SESSION_ID" not in overlay
    assert "ORCA_CHART_SOCK" not in overlay


# ── P8（plan 2026-07-21 §Phase 4-A）：ORCA_ARTIFACTS_DIR 注入 ────────────────


def test_build_env_overlay_injects_artifacts_dir():
    """``artifacts_dir`` 非空 → overlay 含 ``ORCA_ARTIFACTS_DIR``（绝对路径透传）。

    意图：workflow 脚本据 ``$ORCA_ARTIFACTS_DIR`` 写产物（替代 workflow 自建
    ``llm_artifacts/<model>/...``）。ClaudeExecutor / ScriptExecutor spawn 时显式传入，
    沿 env 链继承到 script。
    """
    overlay = build_env_overlay(
        (),
        artifacts_dir="/abs/path/runs/r-1/artifacts",
    )
    assert overlay["ORCA_ARTIFACTS_DIR"] == "/abs/path/runs/r-1/artifacts"


def test_build_env_overlay_artifacts_dir_default_not_injected():
    """``artifacts_dir`` 缺省 → 不注 ``ORCA_ARTIFACTS_DIR``（向后兼容，旧调用方零回归）。

    意图：P8 引入此 keyword 前，既有调用方（validator / dialog / executor 旧测试）不传
    此参 → env overlay 必须不含 ``ORCA_ARTIFACTS_DIR``（不破坏现有 spawn 契约）。
    """
    overlay = build_env_overlay(("ANTHROPIC_",))
    assert "ORCA_ARTIFACTS_DIR" not in overlay


def test_build_env_overlay_empty_artifacts_dir_not_injected():
    """显式传 ``artifacts_dir=""`` → 等价于缺省（不注），与 chart_sock 空串语义一致。

    意图：执行器在 ``runs_dir is None`` 路径下显式传空串（见 ``_resolve_artifacts_dir``），
    应等价于缺省——而非注入空值（空值会让 workflow 脚本读到 ``$ORCA_ARTIFACTS_DIR=""``
    误以为是当前目录）。
    """
    overlay = build_env_overlay(
        (),
        run_id="r-1",
        artifacts_dir="",
    )
    assert overlay["ORCA_RUN_ID"] == "r-1"
    assert "ORCA_ARTIFACTS_DIR" not in overlay


def test_build_env_overlay_artifacts_dir_coexists_with_chart_vars(monkeypatch):
    """``artifacts_dir`` + chart 路由 4 件套同时注入（ClaudeExecutor 生产路径）。

    意图：ClaudeExecutor spawn 时同时传 ``run_id`` / ``node`` / ``session_id`` / ``chart_sock``
    / ``agent_resources`` / ``artifacts_dir`` 6 个 keyword → 6 个 ORCA_* 共存于 overlay，
    不互相覆盖。
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-1")
    overlay = build_env_overlay(
        ("ANTHROPIC_",),
        run_id="r-1",
        node="train",
        session_id="s-1",
        chart_sock="/tmp/orca-abc.sock",
        agent_resources="/abs/agents/worker",
        artifacts_dir="/abs/runs/r-1/artifacts",
    )
    assert overlay["ORCA_RUN_ID"] == "r-1"
    assert overlay["ORCA_NODE"] == "train"
    assert overlay["ORCA_SESSION_ID"] == "s-1"
    assert overlay["ORCA_CHART_SOCK"] == "/tmp/orca-abc.sock"
    assert overlay["ORCA_AGENT_RESOURCES"] == "/abs/agents/worker"
    assert overlay["ORCA_ARTIFACTS_DIR"] == "/abs/runs/r-1/artifacts"
    # prefix 透传仍工作
    assert overlay["ANTHROPIC_API_KEY"] == "sk-1"


# ── plan sprightly-questing-donut §1.2：kb_dir（KB 根，同 artifacts_dir 形态）──

def test_build_env_overlay_injects_kb_dir():
    """``kb_dir`` 非空 → overlay 含 ``ORCA_KB_DIR``（KB 根透传，executor/script spawn 注入）。"""
    overlay = build_env_overlay((), kb_dir="/abs/knowledge_base")
    assert overlay["ORCA_KB_DIR"] == "/abs/knowledge_base"


def test_build_env_overlay_kb_dir_default_not_injected():
    """``kb_dir`` 缺省 → 不注 ``ORCA_KB_DIR``（向后兼容，不碰 KB 的 workflow 零回归）。"""
    overlay = build_env_overlay(("ANTHROPIC_",))
    assert "ORCA_KB_DIR" not in overlay


def test_build_env_overlay_kb_dir_coexists_with_artifacts():
    """kb_dir 与 artifacts_dir 同注（spawn 一次性透传两个权威目录），互不覆盖。"""
    overlay = build_env_overlay(
        (),
        artifacts_dir="/abs/runs/r-1/artifacts",
        kb_dir="/abs/knowledge_base",
    )
    assert overlay["ORCA_ARTIFACTS_DIR"] == "/abs/runs/r-1/artifacts"
    assert overlay["ORCA_KB_DIR"] == "/abs/knowledge_base"
