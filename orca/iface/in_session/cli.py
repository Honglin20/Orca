"""orca/iface/in_session/cli.py —— in-session shell 用户命令面（ADR v2 §6）。

三个命令，清晰友好：
  - ``orca in-session start <wf.yaml>`` —— 准备一个 run（gen run_id + tape 路径 +
    校验 wf），打印 opencode.json 的 ``mcp`` 配置块给用户贴。不 spawn daemon
    （daemon 由 opencode 经该配置 spawn，生命周期归 opencode）。
  - ``orca in-session serve --yaml --tape --run-id`` —— daemon 入口（opencode/CC
    经 mcp 配置 spawn；用户一般不直接调）。
  - ``orca in-session status [<run_id>]`` —— 读 tape 报 workflow 进度；无 run_id
    列 ``runs/`` 下全部 in-session tape。

resume 隐式：run 崩溃后用同一 run_id/tape 重连（``serve`` 以 ``Tape(resume=True)``
打开自动半写恢复），无需显式 resume 命令。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import typer

from orca.compile import load_workflow
from orca.run.lifecycle import gen_run_id


def _default_tape_path(run_id: str) -> Path:
    """lazy import 避开 orca.iface.cli 包初始化期的循环 import。"""
    from orca.iface.cli.bg_runner import default_tape_path
    return default_tape_path(run_id)

app = typer.Typer(
    name="in-session",
    help="in-session shell：宿主主 session（opencode/CC）执行 workflow，Orca daemon 独占 tape。",
    no_args_is_help=True,
)


@app.command()
def start(
    yaml: Path = typer.Argument(..., help="workflow YAML 路径", exists=True),
) -> None:
    """准备一个 in-session run，打印 opencode mcp 配置。"""
    wf = load_workflow(yaml)  # fail loud：非法 yaml 抛 ConfigurationError
    run_id = gen_run_id(wf.name)
    tape = _default_tape_path(run_id)
    yaml_abs = str(yaml.resolve())
    tape_abs = str((Path.cwd() / tape).resolve())

    serve_cmd = [
        "orca", "in-session", "serve",
        "--yaml", yaml_abs,
        "--tape", tape_abs,
        "--run-id", run_id,
    ]
    config = {
        "mcp": {
            "orca": {
                "type": "local",
                "command": serve_cmd,
                "enabled": True,
            }
        }
    }

    typer.echo(f"workflow: {wf.name}")
    typer.echo(f"run_id:   {run_id}")
    typer.echo(f"tape:     {tape_abs}")
    typer.echo("")
    typer.echo(typer.style("把以下配置贴进项目的 opencode.json：", fg=typer.colors.CYAN, bold=True))
    typer.echo(json.dumps(config, indent=2, ensure_ascii=False))
    typer.echo("")
    typer.echo(typer.style("然后：", fg=typer.colors.CYAN))
    typer.echo("  1. 在 opencode 里打开一个会话（它会按上面的 command spawn Orca daemon）。")
    typer.echo("  2. 让主 session 调用 orca_advance 工具驱动 workflow（每完成一节点调一次）。")
    typer.echo(f"  3. 跑完用 `orca in-session status {run_id}` 看结果。")


@app.command()
def serve(
    yaml: Path = typer.Option(..., "--yaml", help="workflow YAML"),
    tape: Path = typer.Option(..., "--tape", help="tape 文件路径（daemon 独占）"),
    run_id: str = typer.Option(..., "--run-id", help="run id"),
    inputs: str = typer.Option("{}", "--inputs", help="workflow inputs（JSON）"),
    opencode_url: str = typer.Option(None, "--opencode-url", help="opencode serve 的 base_url（opencode 前端）"),
    session: str = typer.Option(None, "--session", help="opencode session id（opencode 前端）"),
    model: str = typer.Option("deepseek/deepseek-v4-flash", "--model", help="provider/model（opencode 前端）"),
    opencode_auth: str = typer.Option(None, "--opencode-auth", help='opencode serve basic auth，格式 "user:password"'),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """daemon 入口（hook-driven v5）。opencode 前端：连 --opencode-url/--session 自驱动。"""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from orca.iface.in_session.daemon import InSessionDaemon

    wf = load_workflow(yaml)
    try:
        inp = json.loads(inputs) if inputs else {}
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--inputs 不是合法 JSON：{e}") from e
    daemon = InSessionDaemon(wf, Path(tape), run_id, inp)
    if opencode_url and session:
        provider, _, mid = model.partition("/")
        auth = None
        if opencode_auth and ":" in opencode_auth:
            u, _, p = opencode_auth.partition(":")
            auth = (u, p)
        daemon.run_opencode(opencode_url, session, {"providerID": provider, "modelID": mid}, auth=auth)
    else:
        raise typer.BadParameter(
            "v5 hook-driven：opencode 前端需 --opencode-url + --session；"
            "CC 前端（Unix socket）待 phase SPEC 落实"
        )


@app.command(name="status")
def status(
    run_id: str = typer.Argument(None, help="run id（省略则列 runs/ 下全部 run tape）"),
) -> None:
    """查看 in-session run 的 workflow 进度（读 tape replay_state）。"""
    from orca.events.replay import replay_state
    from orca.events.tape import Tape

    if run_id is None:
        runs_dir = Path("runs")
        if not runs_dir.exists():
            typer.echo("(无 runs/ 目录)")
            return
        tapes = sorted(runs_dir.glob("*.jsonl"))
        if not tapes:
            typer.echo("(无 run tape)")
            return
        for tp in tapes:
            typer.echo(f"- {tp.stem}")
        typer.echo("\n用 `orca in-session status <run_id>` 看详情。")
        return

    tape = _default_tape_path(run_id)
    if not tape.exists():
        typer.echo(typer.style(f"run {run_id!r} 无 tape", fg=typer.colors.RED))
        raise typer.Exit(1)
    state = replay_state(Tape(tape, run_id=run_id))
    typer.echo(f"run {run_id}")
    typer.echo(f"  status:      {state.status}")
    typer.echo(f"  current_node: {state.current_node}")
    typer.echo(f"  node_status: {dict(state.node_status)}")
    done = sum(1 for s in state.node_status.values() if s == "done")
    typer.echo(f"  progress:    {done}/{len(state.node_status)} done")
