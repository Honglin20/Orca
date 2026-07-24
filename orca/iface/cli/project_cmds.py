"""project_cmds.py —— ``tars project`` 子命令组（SPEC §13.3 P1 + P3）。

**职责**：注册表运维操作（operator 面）。当前子命令：
  - ``tars project rebuild`` —— 注册表损坏时重建（扫已知项目根，重新 register）。
  - ``tars project list`` —— 列注册项（含 stale 标识，给运维 grep）。

依赖单向：本模块只 import stdlib + typer + ``orca.runtime``（中立层）。**禁止** import web。

SPEC §13.3 P1：``~/.orca/projects.json`` 损坏时重建路径；SPEC §13.3 P3：stale 注册项可见性。
"""

from __future__ import annotations

import json
from typing import Any

import typer

from orca.runtime import (
    RegistryCorruptError,
    list_registered,
    list_stale_projects,
    rebuild_registry,
)

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="注册表运维（rebuild/list），SPEC §13.3 P1/P3。",
)


@app.command(name="rebuild")
def rebuild_cmd(
    extra: list[str] = typer.Argument(  # noqa: B008
        None,
        help="额外候选项目根（已注册表里的 path 之外再扫；可多次传）",
    ),
) -> None:
    """注册表损坏 / 部分丢失时重建（SPEC §13.3 P1 / §8）。

    扫旧注册表残留 path + 显式传入 extra + 当前 detect 到的 project root，**重置**注册表
    后逐个 ``register_project``。结果打印 ``{scanned, registered, skipped}``。

    语义：**重建（rebuild）非追加**——会先清空再注册（确保坏 entry 被剔除）。
    """
    result = rebuild_registry(extra_paths=extra or [])
    payload: dict[str, Any] = {
        "ok": True,
        "scanned": result["scanned"],
        "registered": result["registered"],
        "skipped": result["skipped"],
    }
    if result.get("rolled_back"):
        payload["rolled_back"] = True
    typer.echo(json.dumps(payload, ensure_ascii=False))
    if result["registered"] == 0:
        if result.get("rolled_back"):
            typer.echo(
                "⚠ 未注册任何项目，已回滚到 rebuild 前 registry（pre-rebuild 快照在 "
                "~/.orca/projects.json.pre-rebuild.bak）。请确认候选路径后重试。",
                err=True,
            )
        else:
            typer.echo(
                "⚠ 未注册任何项目：候选均不合法（无 workflows/ 或 .orca/config.json）。"
                "确认在项目根下运行或传 extra 路径。",
                err=True,
            )
        raise typer.Exit(1)


@app.command(name="list")
def list_cmd(
    include_stale: bool = typer.Option(  # noqa: B008
        True,
        "--stale/--no-stale",
        help="是否含 stale（path 失效）注册项",
    ),
) -> None:
    """列注册表（运维 grep 用）。结构：``{projects: [...], stale: [...]}``。"""
    try:
        registered = list_registered()
    except RegistryCorruptError as e:
        typer.echo(f"❌ 注册表损坏：{e}", err=True)
        raise typer.Exit(2)
    projects: list[dict[str, Any]] = [
        {"project_id": pid, **meta} for pid, meta in registered.items()
    ]
    payload: dict[str, Any] = {"projects": projects}
    if include_stale:
        payload["stale"] = list_stale_projects()
    typer.echo(json.dumps(payload, ensure_ascii=False))
