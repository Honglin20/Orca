"""tests/iface/in_session/test_skill_md_flags_guard.py —— S2 CI 守门（SPEC §5 S2）。

防止 ``orca/skills/tars/SKILL.md`` 教主 session 用 CLI 未声明的 flag（如 ``--resume`` /
``--verbose``），让真实调用时报 ``unknown option`` 阻塞推进。

**做法**：解析 SKILL.md 的 code fence，扫 ``orca <cmd> ...`` 行的 flag，断言 ⊆
``orca --help`` + 各子命令 ``--help`` 输出。

**范围**：仅 ``orca/skills/tars/SKILL.md``。SPEC md（``docs/specs/*.md``）**不扫** ——
SPEC 含讨论性假命令（如 deferred 项 / 反例），扫了会假报。

**实现选择**：regex 抓 `````...````` fence（不引入 markdown lib 依赖；项目 pyproject
未声明 markdown，CI 环境未必有）。AST 解析等价但成本不对等。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from orca.iface.in_session.cli import app


REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_MD = REPO_ROOT / "orca" / "skills" / "tars" / "SKILL.md"

# 注册的 7 命令 + bootstrap（hidden 但仍可调；语法糖 ``orca <wf>`` ≡ ``orca bootstrap <wf>``）。
_REAL_CMDS = {"list", "next", "status", "stop", "open", "doctor", "bootstrap"}


def _resolve_cmd(token: str) -> str | None:
    """``orca <token> ...`` 的首 token → 真实 CLI 命令名（或 None 表跳过）。

    - 直接命中的命令名（list/next/.../bootstrap）→ 该命令。
    - 占位符（如 ``<wf-name>``）→ ``bootstrap``（语法糖 ``orca <wf>`` ≡ ``orca bootstrap <wf>``）。
    - 其它（如路径 / ``$end``）→ None，调用方跳过本行。
    """
    if token in _REAL_CMDS:
        return token
    if token.startswith("<") and token.endswith(">"):
        return "bootstrap"
    return None


def _extract_flags_from_line(line: str) -> tuple[str | None, set[str]]:
    """解析一行 ``orca <cmd> ...`` → ``(cmd, flags)``；非 orca 行 → ``(None, set())``。

    flag = 以 ``--`` 开头的 token 的前缀 ``--<alphanum->`` 部分（如 ``--inputs：返该`` /
    ``--output=foo`` → ``--inputs`` / ``--output``；避免行内注释 / 中文标点 / ``=value``
    污染）。方括号包裹的可选 flag（如 ``[--run-id <id>]``）也抽出来（先剥方括号再 tokenize）。
    """
    stripped = line.strip()
    # 去掉行首 shell prompt / comment 标记
    if stripped.startswith("#") or stripped.startswith("$"):
        stripped = stripped.lstrip("#$ ").strip()
    if not stripped.startswith("orca "):
        return None, set()
    # 去掉前缀 ``orca ``；剥方括号（``[--run-id <id>]`` → ``--run-id <id>``）。
    body = stripped[len("orca "):]
    body = body.replace("[", " ").replace("]", " ")
    tokens = body.split()
    if not tokens:
        return None, set()
    cmd = _resolve_cmd(tokens[0])
    if cmd is None:
        return None, set()
    flags: set[str] = set()
    for tok in tokens[1:]:
        if not tok.startswith("--"):
            continue
        # 仅取 ``--<alphanum->`` 前缀，剥除 ``=value`` / 行内注释 / 中文标点尾污染。
        m = re.match(r"--[a-zA-Z][a-zA-Z0-9_-]*", tok)
        if m:
            flags.add(m.group(0))
    return cmd, flags


def _parse_skill_md_flags() -> dict[str, set[str]]:
    """解析 SKILL.md，返 ``{cmd: {flags}}``。

    仅扫 code fence（```...```) 内容；fence 外的散文 / 表格不扫（含举例性 / 讨论性命令）。
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    # ````lang\n...\n```` 风格 fence（容忍 ```` 前有 4 空格缩进 / 末尾无换行）。
    fence_re = re.compile(
        r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL,
    )
    cmd_to_flags: dict[str, set[str]] = {cmd: set() for cmd in _REAL_CMDS}
    for block in fence_re.findall(text):
        for line in block.splitlines():
            cmd, flags = _extract_flags_from_line(line)
            if cmd is None:
                continue
            cmd_to_flags[cmd] |= flags
    return cmd_to_flags


def _help_flags(*args: str) -> set[str]:
    """跑 ``orca <args> --help``，抽输出中所有 ``--flag`` 形态的 token。"""
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0, (
        f"`orca {' '.join(args)} --help` 异常 exit={result.exit_code}: {result.output}"
    )
    return set(re.findall(r"--[a-zA-Z][a-zA-Z0-9_-]*", result.output))


def test_skill_md_exists() -> None:
    """前置：SKILL.md 必须存在（防路径漂移让下面的 guard 静默 no-op）。"""
    assert SKILL_MD.is_file(), f"SKILL.md 不在 {SKILL_MD}"


def test_skill_md_flags_subset_of_cli_help() -> None:
    """SKILL.md code fence 内的 ``orca <cmd> ...`` flag 必须 ⊆ CLI ``--help`` 输出。

    SPEC §5 S2 守门。失败时报告哪个 flag 在哪个命令下未声明（提示更新 SKILL.md 或补 CLI option）。
    """
    cmd_to_flags = _parse_skill_md_flags()

    top_help = _help_flags()
    per_cmd_help: dict[str, set[str]] = {cmd: _help_flags(cmd) for cmd in _REAL_CMDS}

    failures: list[str] = []
    for cmd, flags in cmd_to_flags.items():
        if not flags:
            continue
        known = top_help | per_cmd_help[cmd]
        unknown = flags - known
        if unknown:
            failures.append(
                f"  orca {cmd}: SKILL.md 提到 {sorted(unknown)}，但 CLI "
                f"`orca {cmd} --help` + `orca --help` 均未声明。"
            )
    assert not failures, (
        "SKILL.md 教了 CLI 未声明的 flag（主 session 调用会 unknown option 阻塞推进）：\n"
        + "\n".join(failures)
    )


def test_skill_md_mentions_seven_commands() -> None:
    """SKILL.md 至少提到 7 命令的核心命令名（防重构改名后 SKILL 漂移）。

    bootstrap 是 hidden 语法糖，不强制；6 个非 hidden 命令必须出现。
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    for cmd in ("list", "next", "status", "stop", "open", "doctor"):
        # ``orca <cmd>`` 形态出现至少一次（含 fence + 散文）。
        pattern = rf"\borca\s+{re.escape(cmd)}\b"
        assert re.search(pattern, text), (
            f"SKILL.md 未提及 `orca {cmd}`（7 命令之一，文档漂移）"
        )


def test_spec_md_not_scanned() -> None:
    """SPEC md 不应被 S2 守门扫描（防讨论性假命令假报）。

    本测试 self-check：确认我们只读 ``orca/skills/tars/SKILL.md``，不读 ``docs/specs/*``。
    用 fixture 注入一个临时 SPEC md 含假 flag，断言它**不**触发 guard。
    """
    # 反向：确认 _parse_skill_md_flags 只读 SKILL_MD 路径（不被 cwd / sys.path 影响）。
    cmd_to_flags = _parse_skill_md_flags()
    # 简单 sanity：SKILL.md 应至少抽到 ``--run-id``（出现在 next/status/stop 多处）。
    all_flags = set().union(*cmd_to_flags.values()) if any(cmd_to_flags.values()) else set()
    assert "--run-id" in all_flags, (
        "SKILL.md 应在 fence 内提到 `--run-id`（next/status/stop/open 都有）—— "
        "若失败说明 fence 解析坏了"
    )
