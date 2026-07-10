"""test_exit_codes.py —— ExitCode 5 档 + exit_for_terminal_status 派生 + grep 守门。

phase-11-process-lifecycle §3 / ADR §4.6 / §8.1。

verify intent (Rule 9)：
  - 测试不是「返回 0」，而是「**CI 能据退出码判断 workflow 结果**」——
    模拟 CI 调用 ``orca run`` 后据 ``$?`` 判成功/失败/取消。
  - grep 守门不是「sys.exit 出现 N 次」，而是「**除 iface/exit_codes.py + __main__.py
    外无新增裸 sys.exit / raise SystemExit**」——SPEC §3.3 契约，违反即返工。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orca.iface.exit_codes import ExitCode, exit_for_terminal_status

REPO_ROOT = Path(__file__).resolve().parents[1]
ORCA_DIR = REPO_ROOT / "orca"


# ── ExitCode 枚举契约 ─────────────────────────────────────────────────────


def test_exit_code_values_are_the_5_tier_contract():
    """5 档 0/1/2/3/130 是 CI 契约（SPEC §3.1），不可漂移。"""
    assert int(ExitCode.SUCCESS) == 0
    assert int(ExitCode.CONFIG_ERROR) == 1
    assert int(ExitCode.BUSINESS_FAILURE) == 2
    assert int(ExitCode.CANCELLED) == 3
    assert int(ExitCode.SIGINT) == 130
    # 恰好 5 个，不多不少（防止后续随手加档而不更新 SPEC）
    assert len(list(ExitCode)) == 5


def test_exit_code_is_int_enum_for_sys_exit_compatibility():
    """``sys.exit(ExitCode.X)`` 必须等价于 ``sys.exit(int_value)``——IntEnum 契约。"""
    assert int(ExitCode.CANCELLED) == 3
    # sys.exit 接受 ExitCode（IntEnum 是 int 子类）
    assert ExitCode.SUCCESS == 0


# ── exit_for_terminal_status 派生 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected",
    [
        ("completed", ExitCode.SUCCESS),
        ("failed", ExitCode.BUSINESS_FAILURE),
        ("cancelled", ExitCode.CANCELLED),
    ],
)
def test_terminal_status_maps_to_contract_exit_code(status, expected):
    """SPEC §3.1：workflow 终态 → 退出码 5 档映射（CI 据此判结果）。"""
    assert exit_for_terminal_status(status) == int(expected)


def test_unknown_status_raises_key_error_fail_loud():
    """未知 status 必须 fail loud（铁律 12）——CI 不该见到未契约化的退出码。"""
    with pytest.raises(KeyError):
        exit_for_terminal_status("running")  # 非终态
    with pytest.raises(KeyError):
        exit_for_terminal_status("totally-bogus")


def test_returns_int_not_exit_code_object():
    """返回 int（``sys.exit`` 接受 int；返回 ExitCode 也行但 int 是稳定契约）。"""
    assert isinstance(exit_for_terminal_status("completed"), int)


# ── grep 守门（SPEC §3.3 / ADR §8.1）────────────────────────────────────────


def test_no_bare_sys_exit_or_raise_system_exit_outside_allowed_paths():
    """守门（SPEC §3.3）：除 ``iface/exit_codes.py`` + 任意 ``__main__.py`` 外，
    orca/ 下不许裸 ``sys.exit(...)`` / ``raise SystemExit``。

    违反 = 新增了未契约化的退出路径，CI 会见到非 5 档退出码 → 返工。

    已知遗留（批 4 follow-up，本测试 allowlist 暂时容忍）：
      - ``orca/gates/hook_script.py`` ``sys.exit(main())``：hook 脚本入口，
        其退出码 0/2 是 git pre-commit / pre-push 协议（非 Orca workflow 退出码），
        与 SPEC §3.1 5 档语义不同；批 4 决定是否经 ExitCode 派生（语义待议）。
    """
    import re

    # 命中行起始的 ``sys.exit(`` 或 ``raise SystemExit``（容忍前导空白）。
    pattern = re.compile(r"^\s*(sys\.exit\(|raise\s+SystemExit)")

    # 允许的路径模式：路径包含 ``__main__`` 或 ``iface/exit_codes``
    def is_allowed(py_path: Path) -> bool:
        s = str(py_path)
        return ("__main__" in s) or ("iface" in s and "exit_codes" in s)

    # 已知遗留 allowlist（SPEC §3.3 文档化，批 4 follow-up）
    legacy_allowlist_rel = {
        "gates/hook_script.py",  # hook 脚本入口，git 协议退出码（非 workflow）
    }

    violations = []
    for py in ORCA_DIR.rglob("*.py"):
        rel = py.relative_to(ORCA_DIR)
        if is_allowed(py) or str(rel) in legacy_allowlist_rel:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.match(line):
                violations.append(f"{py}:{lineno}:{line.strip()}")

    assert not violations, (
        "SPEC §3.3 守门违反：在 ``orca/`` 下发现裸 ``sys.exit`` / ``raise SystemExit``，"
        "位置不在 ``iface/exit_codes.py`` 也非 ``__main__.py``：\n  "
        + "\n  ".join(violations)
        + "\n请改用 ``orca/iface/exit_codes.py`` 的 ExitCode + exit_for_terminal_status。"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
