"""tests/iface/in_session/test_opencode_permission_bridge.py —— opencode 权限审批桥静态守门。

守两块（SPEC 2026-08-11-opencode-permission-bridge）：
  - Part A：opencode profile ``flags`` 含 ``--auto``（堵 DEFECT-1 headless 挂死，§2）。
  - Part B：``orca.ts`` 含 ``tool.execute.before`` 审批桥 + 关键契约锚点（§3/§4/§5）。

**为什么静态守门**：``orca.ts`` 是 TypeScript（跑在 opencode bun 进程），pytest 无法直接执行。
行为表的执行级单测在同目录 ``bridge.test.ts``（node:test + node 24 type stripping，零依赖）。
本文件是 **CI 守门**（pytest 必跑）：锚定关键源码结构，防误删 / 误改 fail 语义分支。
两文件互补：本文件守「结构在」，``bridge.test.ts`` 守「行为对」。

**cc/cac 非回归（AC5）**：本测试不触碰 ``approval_broker.py`` / ``orca-permission-hook.py`` /
``routes/approval.py`` / claude,ccr profile / ``_install_cc_nudge``——它们由各自的既有测试守护
（``test_orca_permission_hook.py`` / ``test_approval_broker.py`` / ``test_install_permission_hook.py``）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orca.profiles.registry import get_profile

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCA_TS = (
    REPO_ROOT / "orca" / "iface" / "in_session" / "templates" / "opencode" / "orca.ts"
)


@pytest.fixture(scope="module")
def orca_ts_source() -> str:
    """读 ``orca.ts`` 全文（模块级缓存）。"""
    return ORCA_TS.read_text(encoding="utf-8")


# ── Part A：opencode profile --auto flag（§2，AC1 静态部分）────────────────────


def test_opencode_profile_flags_contain_auto():
    """opencode profile ``flags`` 含 ``--auto``（SPEC §2）。

    ``--auto`` 兜底 opencode 原生权限 ask（``external_directory`` / ``doom_loop``），
    堵 DEFECT-1 headless 挂死。固化进 default 仅救未设 ``ORCA_OPENCODE_FLAGS`` 的用户
    （``resolve_flags`` REPLACES 整个 tuple，见 docstring B4 限制）。
    """
    p = get_profile("opencode")
    assert "--auto" in p.flags, "opencode profile flags 必须含 --auto（DEFECT-1 修复）"
    # --dangerously-skip-permissions 仍在（--auto 是叠加，非替换）
    assert "--dangerously-skip-permissions" in p.flags


def test_opencode_flags_env_replaces_entire_tuple(monkeypatch):
    """``ORCA_OPENCODE_FLAGS`` REPLACE 语义回归（SPEC §2 B4 闭环）。

    ``resolve_flags``（``profiles/base.py:112``）在 env 设定时**整体替换** flags（非追加）。
    故固化 ``--auto`` 进 default 仅救未设该 env 的用户；设了 env 的用户（如 DEFECT-1 报告人）
    须自行在自定义 flags 里补 ``--auto`` + ``--dangerously-skip-permissions``。锁住此限制防
    误以为「固化进 default 就万事大吉」。
    """
    p = get_profile("opencode")
    # 默认（无 env）含 --auto + --dangerously-skip-permissions
    assert "--auto" in p.resolve_flags()
    # env 设定 → 整体替换：自定义 flags 不含 --auto（实证 REPLACE 语义）
    monkeypatch.setenv("ORCA_OPENCODE_FLAGS", "run --format json")
    resolved = p.resolve_flags()
    assert resolved == ("run", "--format", "json")
    assert "--auto" not in resolved
    assert "--dangerously-skip-permissions" not in resolved


@pytest.mark.parametrize("profile_name", ["claude", "ccr"])
def test_cc_cac_profiles_do_not_have_auto(profile_name: str):
    """cc/cac profile 零改动（SPEC I-3 / AC6 守门）：``--auto`` 只进 opencode profile。

    ``--auto`` 是 opencode 二进制 flag，与 claude/ccr 协议无关；claude/ccr 不碰。
    """
    p = get_profile(profile_name)
    assert "--auto" not in p.flags, f"{profile_name} profile 不应含 --auto（cc/cac 零改动）"


# ── Part B：orca.ts tool.execute.before 桥静态守门（§3/§4/§5，AC2 静态部分）────


def test_orca_ts_has_tool_execute_before_hook(orca_ts_source: str):
    """``orca.ts`` 含 ``tool.execute.before`` hook（SPEC §3，AC2 静态门）。"""
    assert '"tool.execute.before"' in orca_ts_source, (
        "orca.ts 必须含 tool.execute.before hook（审批桥入口）"
    )


def test_orca_ts_session_id_dual_key_resolution(orca_ts_source: str):
    """session 解析用双键 ``||``（SPEC §3 B1 闭环）。

    ORCA_SESSION_ID（executor 注入 == tape node session_id；headless 命中 broker node 键）
    || input.sessionID（opencode 内部 id；交互命中 host 键）。缺任一 = 死桥。
    """
    assert "ORCA_SESSION_ID" in orca_ts_source
    assert "sessionID" in orca_ts_source
    # 纯函数 _resolveApprovalSessionId 必须存在（两键合一的载体）
    assert "_resolveApprovalSessionId" in orca_ts_source


def test_orca_ts_deny_throws_with_no_retry_message(orca_ts_source: str):
    """deny = throw，文案含「不要重试」缓解 agent 循环（SPEC §3 / R5）。"""
    assert "被审批拒绝" in orca_ts_source
    assert "不要重试" in orca_ts_source


def test_orca_ts_fail_open_branches_present(orca_ts_source: str):
    """SPEC §4 失败语义六分支决策表齐全（结构守门）。

    不可达 / 异常 → fail-open 放行（headless 防挂死）；HTTP 错 / 坏响应 → fail loud throw；
    timeout → policy；deny → throw。决策在纯函数 ``_decide``（switch 六 case）。
    """
    # 六种 BrokerOutcome kind 必须都在（决策表完备性）
    for kind in ('"behavior"', '"unreachable"', '"http-error"',
                 '"bad-response"', '"timeout"', '"exception"'):
        assert kind in orca_ts_source, f"orca.ts 决策表缺 {kind} 分支"
    # _decide 纯函数 + _askBroker IO 函数都在
    assert "_decide" in orca_ts_source
    assert "_askBroker" in orca_ts_source


def test_orca_ts_broker_endpoint_and_defaults(orca_ts_source: str):
    """broker 端点 POST /approval + 默认 127.0.0.1:7428（SPEC §5 / I-1）。"""
    assert "/approval" in orca_ts_source
    assert "127.0.0.1" in orca_ts_source
    assert "7428" in orca_ts_source
    # env 覆盖通道（SPEC §5）：ORCA_HOST / ORCA_PORT / ORCA_APPROVAL_TIMEOUT[_POLICY]
    assert "ORCA_HOST" in orca_ts_source
    assert "ORCA_PORT" in orca_ts_source
    assert "ORCA_APPROVAL_TIMEOUT" in orca_ts_source


def test_orca_ts_post_body_shape_matches_cc_hook(orca_ts_source: str):
    """POST body 形状复用 CC hook（SPEC §3，I-1 非回归契约）。

    ``{session_id, tool, tool_input, hook_event: "PermissionRequest"}``。
    broker 决策路径不读 hook_event（I-1）—— 它是标签。键名取自 ``_askBroker`` 的
    ``JSON.stringify({...})`` 块（JS 对象字面量，键无引号）。
    """
    # POST body 字段（JS 对象字面量键 = ``key: value`` 形态）
    assert "session_id: sid" in orca_ts_source
    assert "tool_input:" in orca_ts_source
    assert 'hook_event: "PermissionRequest"' in orca_ts_source
    # tool 字段：POST body 用 ``tool: tool || "<unknown>"``（|| 与 _decide 兜底一致）
    assert 'tool: tool || "<unknown>"' in orca_ts_source


def test_orca_ts_uses_fetch_not_http_lib(orca_ts_source: str):
    """用 bun-native ``fetch``（SPEC §5 load-bearing detail #5），不引第三方 http 库。"""
    assert "fetch(" in orca_ts_source
    # 不应出现 http 库 import（opencode plugin 进程内 fetch 足够）
    assert "import http" not in orca_ts_source
    assert "require(" not in orca_ts_source


def test_orca_ts_tool_args_on_output(orca_ts_source: str):
    """工具 args 在 ``output.args``（spike 实证，非 input.args；SPEC §3 load-bearing #2）。"""
    assert "output?.args" in orca_ts_source or "output.args" in orca_ts_source
