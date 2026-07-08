"""skill_cmds.py —— ``orca skill`` 子命令组（**已弃用 → ``orca install``**）。

历史职责：``orca skill install`` 把打包在 ``orca/skills/`` 下的 skill 拷到两边的用户 skill
目录（CC ``~/.claude/skills/``、opencode ``~/.config/opencode/skills/``，后者可被
``OPENCODE_CONFIG_DIR`` 覆盖）。

**现状（2026-07-08 统一安装收口）**：skill 安装 + in-session 安装已合并进 ``orca install``
（``orca/iface/cli/install_cmds.py``）。``orca skill install`` 降为**向后兼容的弃用别名**——
打印 ``⚠`` 警告后委托 ``run_install(target, "user")`` 执行（行为升级：opencode target 现在
额外装 plugin / command / opencode.json 声明）。详见 ``docs/plans/2026-07-08-unified-install.md``。

**保留的纯函数**：``install_targets``（host × scope 的 skill 目标目录解析）仍作为可测 seam
保留（``test_skill_cmds`` 覆盖），其 opencode 全局目录解析逻辑（``OPENCODE_CONFIG_DIR``）
与 ``install_cmds.resolve_roots`` 同源。

依赖单向：本模块只 import 标准库 + typer + ``orca.iface.cli.install_cmds``（委托）。**不**反向依赖。
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

# 随包 skill 名（``orca/skills/<SKILL_NAME>/``）。多 skill 时改这里 + 打包配置。
SKILL_NAME = "create-workflow"

app = typer.Typer(
    name="skill",
    no_args_is_help=True,
    help="安装/管理随包分发的 Orca skill（create-workflow）。",
)


# ── 可测性 seam：目标解析抽纯函数（gotcha G2）───────────────────────────────


def opencode_global_root(home: Path) -> Path:
    """opencode 全局 config 根目录：``OPENCODE_CONFIG_DIR`` 或 ``<home>/.config/opencode``。

    单一真相源（``install_cmds.resolve_roots`` 复用本函数）。``expanduser`` 兜底用户在
    env 里写 ``~`` 的情况——避免与 install_cmds 解析漂移（review 🟡#1 闭环）。
    """
    raw = os.environ.get("OPENCODE_CONFIG_DIR") or str(home / ".config" / "opencode")
    return Path(raw).expanduser()


def install_targets(target: str, *, home: Path | None = None) -> list[tuple[str, Path]]:
    """返回 ``[(label, dir), ...]``：按 ``--target`` 选定的 skill 安装目标目录。

    - CC：``<home>/.claude/skills/<SKILL_NAME>``
    - opencode：``<OPENCODE_CONFIG_DIR 或 <home>/.config/opencode>/skills/<SKILL_NAME>``

    纯函数（``home`` 可注入），单测 monkeypatch ``Path.home`` 时传 ``home=`` 即可。
    未知 ``target`` → ``typer.BadParameter``（fail loud）。
    """
    if home is None:
        home = Path.home()
    cc = home / ".claude" / "skills" / SKILL_NAME
    opencode_root = opencode_global_root(home)
    oc = opencode_root / "skills" / SKILL_NAME

    valid = {"claude": [("claude", cc)], "opencode": [("opencode", oc)], "all": [("claude", cc), ("opencode", oc)]}
    if target not in valid:
        raise typer.BadParameter(f"未知 target {target!r}，可选：claude / opencode / all")
    return valid[target]


@app.command(name="install")
def install(
    target: str = typer.Option(
        "all",
        "--target",
        "-t",
        help="装到哪边：claude / opencode / all（默认 all，两边都装）",
    ),
) -> None:
    """[已弃用 → ``orca install``] 把 skill 装到 Claude Code / opencode。

    ``orca skill install`` 已被 ``orca install`` 收口（后者同时装 skill + in-session 集成）。
    本命令保留为向后兼容的弃用别名：打印 ``⚠`` 警告后委托 ``orca install --target <target>
    --scope user`` 执行。**行为升级**：opencode target 现在额外装 plugin / command / opencode.json
    声明（不再是 skill-only）。

    幂等（由 ``run_install`` 保证）。
    """
    typer.echo(
        "⚠ `orca skill install` 已弃用 → 改用 `orca install`（收口 skill + in-session）。"
        "本次按等价语义委托执行（全局 skill；opencode target 额外装 in-session）。",
        err=True,
    )
    # 延迟 import 避免循环（install_cmds 顶层 import skill_cmds.SKILL_NAME）。
    from orca.iface.cli.install_cmds import run_install
    failed = run_install(target, "user")  # skill install 原语义 = 全局 skill
    if failed:
        raise typer.Exit(1)
