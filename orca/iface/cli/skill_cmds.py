"""skill_cmds.py —— ``teams skill`` 子命令组（**已弃用 → ``teams install``**）。

历史职责：``teams skill install`` 把打包在 ``orca/skills/`` 下的 skill 拷到宿主 skill 目录。

**现状（v5 §4.3 统一安装）**：skill 安装已合并进 ``teams install``（``orca/iface/cli/install_cmds.py``），
支持四前端（cc/opencode/cac/nga）。``teams skill install`` 降为**向后兼容的弃用别名**——
打印 ``⚠`` 警告后委托 ``run_install(target, "user")`` 执行。

**保留的纯函数**：``install_targets``（host 的 skill 目录解析）仍作为可测 seam 保留
（``test_skill_cmds`` 覆盖），其 opencode 全局目录解析逻辑（``OPENCODE_CONFIG_DIR``）
与 ``install_cmds.resolve_roots`` 同源。

依赖单向：本模块只 import 标准库 + typer + ``orca.iface.cli.install_cmds``（委托）。**不**反向依赖。
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

# 随包 skill 名（``orca/skills/<SKILL_NAME>/``）。create-workflow = authoring skill；
# v5 新增 in-session 入口 skill（TARS 品牌：用户面 = TARS，底层 orca CLI 引擎），install
# 一并拷两个（见 install_cmds）。本常量保留给测试作 create-workflow 的稳定引用。
SKILL_NAME = "create-workflow"

# in-session 入口 skill 名（用户面 = TARS；目录 ``orca/skills/tars/``）。
# **单一真相源**：doctor 的 ``skill_install`` 检查（``in_session.cli._scan_skill_install``）
# + install 落地目录 + 测试断言都据本常量，防目录名与 doctor check 漂移（rename 时改一处）。
# 注意：skill 名是 tars，但 skill body 里调的命令仍是 ``orca``（CLI 引擎不改）。
ENTRY_SKILL_NAME = "tars"

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


# v5 §4.3：四前端 skill 落点的**单一真相源**（install_cmds / in_session.cli._scan_skill_install
# 都 import 这两个常量，避免三处副本漂移——review DRY/OCP 闭环）。
SKILL_TARGETS: tuple[str, ...] = ("cc", "opencode", "cac", "nga", "all")
# 各宿主的项目级 dotdir（opencode 的 user-scope 特殊：走 opencode_global_root，见下）。
HOST_DOTDIR: dict[str, str] = {
    "cc": ".claude", "opencode": ".opencode", "cac": ".cac", "nga": ".nga",
}
# 实际前端列表（不含 ``all`` 聚合项）。
SKILL_HOSTS: tuple[str, ...] = ("cc", "opencode", "cac", "nga")


def install_targets(target: str, *, home: Path | None = None) -> list[tuple[str, Path]]:
    """返回 ``[(label, dir), ...]``：按 ``--target`` 选定的 skill **base 目录**（v5 §4.3）。

    每项的 ``dir`` 是该宿主的 ``skills/`` 目录（随包所有 skill 落到其下子目录）：
      - cc：``<home>/.claude/skills``
      - opencode：``<OPENCODE_CONFIG_DIR 或 <home>/.config/opencode>/skills``
      - cac：``<home>/.cac/skills``
      - nga：``<home>/.nga/skills``
      - all：上列四个

    纯函数（``home`` 可注入），单测 monkeypatch ``Path.home`` 时传 ``home=`` 即可。
    未知 ``target`` → ``typer.BadParameter``（fail loud）。
    """
    if home is None:
        home = Path.home()
    if target not in SKILL_TARGETS:
        raise typer.BadParameter(
            f"未知 target {target!r}，可选：{' / '.join(SKILL_TARGETS)}"
        )

    hosts = list(SKILL_HOSTS) if target == "all" else [target]
    out: list[tuple[str, Path]] = []
    for host in hosts:
        if host == "opencode":
            base = opencode_global_root(home)
        else:
            base = home / HOST_DOTDIR[host]
        out.append((host, base / "skills"))
    return out


@app.command(name="install")
def install(
    target: str = typer.Option(
        "all",
        "--target",
        "-t",
        help="装到哪个前端：cc / opencode / cac / nga / all（默认 all）",
    ),
) -> None:
    """[已弃用 → ``teams install``] 把随包 skill 装到前端宿主。

    ``teams skill install`` 已被 ``teams install`` 收口。本命令保留为向后兼容的弃用别名：
    打印 ``⚠`` 警告后委托 ``teams install --target <target> --scope user`` 执行。

    幂等（由 ``run_install`` 保证）。
    """
    typer.echo(
        "⚠ `teams skill install` 已弃用 → 改用 `teams install`（四前端统一 skill 落点）。"
        "本次按等价语义委托执行（全局 skill）。",
        err=True,
    )
    # 延迟 import 避免循环（install_cmds 顶层 import skill_cmds.opencode_global_root）。
    from orca.iface.cli.install_cmds import run_install
    failed = run_install(target, "user")  # skill install 原语义 = 全局 skill
    if failed:
        raise typer.Exit(1)
