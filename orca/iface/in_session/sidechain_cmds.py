"""sidechain_cmds.py —— ``orca sidechain`` 子命令组：``sidechain.family`` 配置入口。

回答「用户怎么不用手编 ``~/.orca/config.json`` 就能切子 agent 过程读取的 dotdir？」：
``orca sidechain family <cc|cac|opencode|nga>`` 一条命令写 config（项目或用户级）；
``orca sidechain family`` 查看当前生效 family + resolved 路径 + source；``--unset`` 清除回探测。

**配置语义**（详见 ``orca.iface.cli.config`` 的 ``sidechain`` 字段说明 + ``_family.resolve_*``）：
``sidechain.family`` 是**路径解析维度**，独立于 ``executor`` 管的 spawn 三维度
（binaries/flags/prompt_channel）。它决定 sidechain 守护（``sidechain_daemon``）从哪个 dotdir
读子 agent 过程——CC 家族 ``cc`` → ``~/.claude``、``cac`` → ``~/.cac``；opencode 家族
``opencode`` / ``nga`` → 对应 sqlite DB。daemon 启动时经 argv ``--family`` 透传给 adapter resolver
（``cli._spawn_sidechain_daemon``）。

**与 ``executor set`` 同款 config 写入模式**：``--scope project|user`` 选层 →
``load_config(target)`` → ``cfg.setdefault("sidechain", {})["family"] = value`` → ``save_config`` →
回打生效值。合法家族值取 ``CC_FAMILY_DOTDIR | OPENCODE_FAMILY_DOTDIR`` 的 keys（加新前端自动同步）。

**依赖方向（无环）**：只 import ``orca.iface.cli.config``（iface→iface）+
``orca.events.adapters._family``（events→iface，与 ``_check_sidechain_backend`` 同方向）+ stdlib。
**严禁** import ``orca.iface.in_session.cli``——本模块的 ``app`` 被 cli 模块级 ``add_typer`` 挂载，
反向 import 会成环。读 family 用 ``config.sidechain_family``（与 ``cli._read_sidechain_family_from_config``
共享，DRY）；读 host_session env 本地内联（仅展示用，3 行，不值得跨层共享）。

依赖单向：iface 层（依赖 iface.cli.config + events.adapters._family + stdlib）。
"""

from __future__ import annotations

import os

import typer

from orca.iface.cli import config as config_mod
from orca.iface.cli.config import (
    load_config,
    sidechain_family,
    save_config,
)
from orca.events.adapters._family import (
    CC_FAMILY_DOTDIR,
    OPENCODE_FAMILY_DOTDIR,
    resolve_cc_sidechain_root,
    resolve_opencode_db,
)

app = typer.Typer(
    name="sidechain",
    no_args_is_help=True,
    help="配置 sidechain.family（子 agent 过程读取的 dotdir：cc/.claude | cac/.cac | opencode | nga）。",
)

# 全部合法家族值（CC + opencode 两族 keys 并集）；加新前端时两 dict 同步即自动收录。
_VALID_FAMILIES: set[str] = set(CC_FAMILY_DOTDIR) | set(OPENCODE_FAMILY_DOTDIR)


def _host_session_from_env() -> str:
    """读宿主 session id（仅 ``_print_effective`` 展示用；与 ``cli._host_session_from_env`` 同语义）。

    本地内联不共享：cli 那份挂在 ``cli`` 模块（import 即成环），而此处仅需展示诊断，3 行复制
    低于 DRY 阈值。CC ``CLAUDE_CODE_SESSION_ID`` 开箱注入；opencode 经 plugin 注入 ``ORCA_HOST_SESSION_ID``。
    """
    return (
        os.environ.get("CLAUDE_CODE_SESSION_ID")
        or os.environ.get("ORCA_HOST_SESSION_ID")
        or ""
    )


def _print_effective() -> None:
    """回显当前生效 ``sidechain.family`` + resolved 路径/DB + source（``family`` 查看/unset 后调）。

    家族选择据 env（与 ``_check_sidechain_backend`` 同源）：有 ``CLAUDE_CODE_SESSION_ID`` → CC 家族
    （``resolve_cc_sidechain_root``）；有 ``ORCA_HOST_SESSION_ID`` → opencode 家族
    （``resolve_opencode_db``）；都无 → 只显示 config 值（非 in-session，无法 resolve）。
    """
    from orca.iface.cli.config import load_merged_config

    fam = sidechain_family(load_merged_config())
    fam_disp = fam if fam is not None else "(未设 → 探测)"

    has_cc = bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    has_opc = bool(os.environ.get("ORCA_HOST_SESSION_ID"))

    if has_cc:
        host = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
        try:
            root, src = resolve_cc_sidechain_root(host, family=fam, cwd=os.getcwd())
        except ValueError as e:
            typer.echo(f"  family={fam_disp}；CC 家族解析失败：{e}", err=True)
            return
        typer.echo(f"  family={fam_disp}（source={src}）")
        typer.echo(f"  resolved_root={root}")
        typer.echo(f"  root_exists={root.exists()}")
    elif has_opc:
        try:
            db, src = resolve_opencode_db(family=fam)
        except ValueError as e:
            typer.echo(f"  family={fam_disp}；opencode 家族解析失败：{e}", err=True)
            return
        typer.echo(f"  family={fam_disp}（source={src}）")
        typer.echo(f"  resolved_db={db}")
        typer.echo(f"  db_exists={db.is_file()}")
    else:
        typer.echo(f"  family={fam_disp}")
        typer.echo("  （未检测到 CC/opencode env，无法 resolve 路径；in-session run 内才有）")


@app.command(name="family")
def family(
    value: str = typer.Argument(
        None,
        help="cc | cac | opencode | nga；省略=查看当前生效",
    ),
    scope: str = typer.Option(
        "project", "--scope", "-s",
        help="写到哪层 config：project（默认，.orca/config.json）| user（~/.orca/config.json）",
    ),
    unset: bool = typer.Option(
        False, "--unset", help="清除 sidechain.family（回探测）",
    ),
) -> None:
    """设置 / 查看 / 清除 ``sidechain.family``。

    \b
    - ``orca sidechain family cac``：设为 cac（写 sidechain.family=cac）。
    - ``orca sidechain family``：查看当前生效 family + resolved 路径 + source。
    - ``orca sidechain family --unset``：清除，回探测。
    - ``--scope user``：写用户级 ``~/.orca/config.json``（默认 project）。

    合法值取 CC + opencode 两族并集：``cc`` / ``cac``（CC 后端换皮）/ ``opencode`` / ``nga``
    （opencode 后端换皮）。设错 resolver 会报 fail；本命令提前校验给清晰错误。
    """
    if scope not in ("project", "user"):
        typer.echo(f"错误：--scope 必须 project|user（got {scope!r}）", err=True)
        raise typer.Exit(code=2)

    target = (
        config_mod.project_config_path() if scope == "project" else config_mod.config_path()
    )

    if unset:
        cfg = load_config(target)
        sidechain = cfg.get("sidechain")
        if isinstance(sidechain, dict) and "family" in sidechain:
            sidechain.pop("family", None)
            if not sidechain:  # sidechain dict 空了 → 整键删掉，不留 {"sidechain": {}} 残留。
                del cfg["sidechain"]
            save_config(cfg, target)
            typer.echo(f"✓ 已从 {target} 清除 sidechain.family（回探测）")
        else:
            typer.echo(f"{target} 无 sidechain.family（已是探测模式）")
        typer.echo("")
        _print_effective()
        return

    if value is not None:
        if value not in _VALID_FAMILIES:
            typer.echo(
                f"错误：family 必须 {' | '.join(sorted(_VALID_FAMILIES))}（got {value!r}）",
                err=True,
            )
            raise typer.Exit(code=2)
        cfg = load_config(target)
        cfg.setdefault("sidechain", {})["family"] = value
        save_config(cfg, target)
        typer.echo(f"✓ 已写入 {target}（scope={scope}）：sidechain.family={value}")
        typer.echo("")
        _print_effective()
        return

    # 无 value 且无 --unset：查看。
    _print_effective()
