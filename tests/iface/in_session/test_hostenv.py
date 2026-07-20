"""test_hostenv.py —— ``_hostenv`` 宿主身份探测单测。

覆盖 ``detect_family_from_env``（family 决策的 env 真相源）各分支 + ``detect_backend_from_env``
（认 cac）+ ``cac_session_id_from_pid`` 边界。

这是 family 误判 bug 修复的核心守门：当前后端的 family 由 **env/进程身份**决定，**不由
dotdir 存在性**——真 CC（``CLAUDE_CODE_SESSION_ID`` 在）+ ``~/.cac`` 存在（install 副作用）
→ 必须 family=cc（读 ``.claude``），而非误判 cac。
"""
from __future__ import annotations

from pathlib import Path

from orca.iface.in_session._hostenv import (
    cac_session_id_from_pid,
    detect_backend_from_env,
    detect_family_from_env,
)


# ── detect_family_from_env（family 决策的 env 真相源）────────────────────────────


def test_family_cc_when_claude_code_session_id(monkeypatch):
    """真 Claude Code：``CLAUDE_CODE_SESSION_ID`` 在 → cc（读 ``.claude``）。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.delenv("CODEAGENT", raising=False)
    assert detect_family_from_env() == "cc"


def test_family_cac_when_codeagent_and_pid_hit(monkeypatch):
    """CAC（CC 换皮）：``CODEAGENT=1`` + PID 回溯命中 → cac（读 ``.cac``）。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid",
        lambda: "cac-session-456",
    )
    assert detect_family_from_env() == "cac"


def test_family_none_when_codeagent_but_pid_miss(monkeypatch):
    """``CODEAGENT=1`` 但 PID 回溯未命中（非真 cac 进程，仅 env 残留）→ None（回退 config/probe）。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.cac_session_id_from_pid", lambda: None,
    )
    assert detect_family_from_env() is None


def test_family_none_when_opencode_env(monkeypatch):
    """opencode 家族（``ORCA_HOST_SESSION_ID``）→ CC 子家族无意义 → None。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEAGENT", raising=False)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "ses-opc")
    assert detect_family_from_env() is None


def test_family_none_when_no_env(monkeypatch):
    """全空 env（非 in-session）→ None。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEAGENT", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    assert detect_family_from_env() is None


def test_family_cc_beats_cac_when_both_env(monkeypatch):
    """边界：``CLAUDE_CODE_SESSION_ID`` + ``CODEAGENT`` 同在 → cc 优先（CC env 是真 CC 硬信号）。"""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("CODEAGENT", "1")
    assert detect_family_from_env() == "cc"


# ── detect_backend_from_env（认 cac：CODEAGENT+PID 也算 CC 家族）─────────────────


def test_backend_cc_when_claude_code_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEAGENT", raising=False)
    assert detect_backend_from_env() == "cc"


def test_backend_opencode_when_host_session_id(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("ORCA_HOST_SESSION_ID", "ses")
    assert detect_backend_from_env() == "opencode"


def test_backend_cc_when_codeagent_and_pid_hit(monkeypatch):
    """CAC 走 CC backend（CCJsonlAdapter），靠 PID 回溯拿 session id。"""
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("ORCA_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("CODEAGENT", "1")
    monkeypatch.setattr(
        "orca.iface.in_session._hostenv.host_session_from_env", lambda: "cac-sid",
    )
    assert detect_backend_from_env() == "cc"


def test_backend_none_when_no_env(monkeypatch):
    for k in ("CLAUDE_CODE_SESSION_ID", "ORCA_HOST_SESSION_ID", "CODEAGENT"):
        monkeypatch.delenv(k, raising=False)
    assert detect_backend_from_env() is None


# ── cac_session_id_from_pid 边界（fail-safe）──────────────────────────────────────


def test_cac_pid_none_when_no_sessions_dir(monkeypatch, tmp_path: Path):
    """``~/.cac/sessions`` 不存在 → None（不崩，fail-safe）。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert cac_session_id_from_pid() is None
