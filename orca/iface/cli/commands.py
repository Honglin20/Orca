"""commands.py —— ``orca run/validate/list`` 命令绑定 + 参数解析（SPEC §5）。

回答「用户终端怎么跑一个 workflow？」：``typer`` 子命令绑定 + 纯函数参数解析
（``parse_inputs`` / ``parse_run_args``），退出码 0/1/2（SPEC §5.3 决策 8）。

纯逻辑层（不启动 TUI）：
  - ``parse_inputs(args)``：``-i key=value`` 类型推断（bool/null/JSON/int/float/str）。
  - ``RunConfig``：run 命令解析结果（wf path / inputs / task / max_iter）。
  - ``parse_run_args(...)``：组装 inputs（含 task 注入）+ 优先级裁决（见 ``_resolve_max_iter``）。

入口（``main``）：用 typer 编排 run/validate/list；run 实际启动 TUI 由 ``app.run_in_terminal``
桥接（避免阻塞 typer 的事件循环，见 ``app.py``）。退出码：
  - workflow completed → 0
  - workflow failed / runtime 错误 → 1
  - 参数错误 / 校验失败 → 2（typer ``Exit(code=2)``）

依赖单向：本模块只 import ``orca.{compile, schema, run}``（``run`` 仅用 RunState 类型）+
typer + stdlib。**不 import textual**（textual 在 app.py 才用），让单测不依赖 TUI。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer

from orca.compile import ConfigurationError, load_workflow

logger = logging.getLogger(__name__)

# 退出码（SPEC §5.3 决策 8）。
EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_ARG_OR_VALIDATE = 2

# typer app（子命令绑定）。``no_args_is_help`` 让裸 ``orca`` 显示帮助。
app = typer.Typer(
    name="orca",
    help="Orca — vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面。",
    no_args_is_help=True,
    add_completion=False,
)


# ── 参数解析（纯函数，单测友好）──────────────────────────────────────────────


def parse_inputs(args: list[str]) -> dict[str, Any]:
    """解析 ``-i key=value`` 重复参数 → dict，带类型推断（SPEC §5.1）。

    类型推断顺序（第一个匹配胜出）：
      1. ``true`` / ``false``（任意大小写）→ bool
      2. ``null`` / ``none`` → None
      3. ``[...]`` / ``{...}`` → JSON parse（失败回退 str）
      4. 纯整数 → int
      5. 纯浮点 → float
      6. 其他 → str（原样）

    格式错（不含 ``=`` / 空 key）→ ``typer.BadParameter``（exit 2，fail loud）。
    """
    result: dict[str, Any] = {}
    for raw in args:
        if "=" not in raw:
            raise typer.BadParameter(
                f"-i 参数需为 key=value 形式，收到：{raw!r}"
            )
        key, _, value = raw.partition("=")
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"-i 参数 key 不能为空：{raw!r}")
        result[key] = _infer_type(value)
    return result


def _infer_type(value: str) -> Any:
    """单个 ``-i`` value 的类型推断（SPEC §5.1）。"""
    low = value.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    # JSON（list/dict，覆盖 [...] / {...}）。失败回退 str（不 fail loud：用户可能
    # 真的想传 "[1,2]" 这种字符串字面量；用 ``-i 'x="[1,2]"'`` 可强制 str）。
    if value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    # int / float：仅纯数字串（避免 "1a" 被当数字）；带前导 + / - 也认。
    if _looks_like_int(value):
        return int(value)
    if _looks_like_float(value):
        return float(value)
    return value


def _looks_like_int(value: str) -> bool:
    """``[+-]?digits`` 才认 int（避免 ``"1_000"`` / ``"0x1"`` 等 Python literal 边界）。"""
    if not value:
        return False
    body = value[1:] if value[0] in "+-" else value
    return body.isdigit() and body != ""


def _looks_like_float(value: str) -> bool:
    """``[+-]?digits.digits``（至少一个 ``.``）认 float。"""
    if "." not in value:
        return False
    try:
        float(value)
    except ValueError:
        return False
    # 排除 inf/nan（float() 接受 "inf"/"nan"，但这些更像字符串字面量）
    low = value.lower().lstrip("+-")
    return low not in ("inf", "infinity", "nan")


@dataclass
class RunConfig:
    """``orca run`` 命令解析结果（纯数据）。

    ``inputs`` 已含 task 注入（若 positional task 给了）；``max_iter`` 已按优先级裁决。
    """

    yaml_path: Path
    inputs: dict[str, Any] = field(default_factory=dict)
    task: str | None = None
    max_iter: int | None = None


def parse_run_args(
    yaml_path: Path,
    positional_task: str | None,
    i_args: list[str],
    max_iter: int | None,
) -> RunConfig:
    """组装 run 命令的最终参数（SPEC §5.1）。

    - task 位置参数 → ``inputs.task``（语法糖，决策 7）。若 workflow 未声明 task
      input → 仍注入（不阻断，由 wf 层决定要不要用）；过度 warn 反而吵，故静默注入。
    - ``-i key=value`` 经 ``parse_inputs`` 类型推断；与 task 合并时 ``-i`` 显式覆盖 task。
    - ``max_iter`` 优先级裁决（``--max-iter`` > ``-i iterations``）延迟到 orchestrator
      的 ``resolve_max_iter``；此处只把 CLI 覆盖（``--max-iter``）透传过去（None=未给）。
    """
    inputs = parse_inputs(i_args)
    if positional_task is not None:
        # task 语法糖：注入 inputs.task，但 -i task="..." 显式覆盖（优先级 -i > positional）。
        inputs.setdefault("task", positional_task)
    return RunConfig(
        yaml_path=yaml_path,
        inputs=inputs,
        task=positional_task,
        max_iter=max_iter,
    )


# ── typer 子命令 ─────────────────────────────────────────────────────────────


@app.command()
def run(
    yaml: Path = typer.Argument(..., help="workflow YAML 文件路径"),
    task: str | None = typer.Argument(
        None, help="可选位置参数 = -i task=\"...\" 语法糖（注入 inputs.task）"
    ),
    inputs: list[str] = typer.Option(
        [], "-i", "--input",
        help="覆盖 inputs，格式 key=value，带类型推断（true/null/JSON/int/float/str）",
        metavar="KEY=VALUE",
    ),
    max_iter: int | None = typer.Option(
        None, "--max-iter",
        help="覆盖 max_iterations（优先级：--max-iter > -i iterations > yaml default > 100）",
    ),
) -> None:
    """跑一个 workflow（启动 Textual TUI，看 DAG 进度 / 日志 / 答 gate）。"""
    config = parse_run_args(yaml, task, inputs, max_iter)
    # 启动 TUI 在独立函数（延迟 import textual，让 import orca.iface.cli.commands 不拉 textual）。
    raise typer.Exit(_run_workflow(config))


@app.command()
def validate(
    yaml: Path = typer.Argument(..., help="workflow YAML 文件路径"),
) -> None:
    """校验 workflow（不跑，只做结构 + 语义校验，报告 errors/warnings）。"""
    try:
        load_workflow(yaml)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
    except FileNotFoundError:
        typer.echo(f"文件不存在：{yaml}", err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
    typer.echo(f"✓ {yaml} 校验通过")


@app.command(name="list")
def list_workflows(
    examples_dir: Path = typer.Option(
        Path("examples"), "--dir", help="扫描的 workflow 目录（默认 ./examples）",
    ),
) -> None:
    """列出目录下的 workflow yaml 文件。"""
    if not examples_dir.is_dir():
        typer.echo(f"目录不存在：{examples_dir}", err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
    yamls = sorted(p for p in examples_dir.glob("*.yaml"))
    if not yamls:
        typer.echo(f"（{examples_dir} 下无 .yaml 文件）")
        return
    for p in yamls:
        typer.echo(f"  {p.name}")


# ── 入口 ─────────────────────────────────────────────────────────────────────


def _run_workflow(config: RunConfig) -> int:
    """启动 Textual TUI 跑 workflow，返回退出码（0/1）。

    校验失败（yaml 不存在 / ConfigurationError）→ exit 2。
    workflow 终态 completed → 0；failed → 1。运行期异常 → 1（fail loud，stderr 打 stack）。
    """
    # 校验前置（启动 TUI 前，避免 TUI 起来才发现 yaml 错）。
    try:
        wf = load_workflow(config.yaml_path)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        return EXIT_ARG_OR_VALIDATE
    except FileNotFoundError:
        typer.echo(f"workflow 文件不存在：{config.yaml_path}", err=True)
        return EXIT_ARG_OR_VALIDATE

    # 延迟 import：textual 仅在此路径才需要（让 ``--help`` / validate / list 不拉 textual）。
    from orca.iface.cli.app import OrcaApp

    tui = OrcaApp(
        wf=wf,
        inputs=config.inputs,
        task=config.task,
        max_iter=config.max_iter,
    )
    # 起编排 worker + gate HTTP 桥（必须在 run 前调：run 阻塞，worker 需在 run 的 loop 内起）。
    tui.kickoff()
    try:
        tui.run()
    except Exception:  # noqa: BLE001 —— 顶层兜底，任何异常 → exit 1
        logger.exception("Orca TUI 运行异常")
        return EXIT_RUN_FAILED

    state = tui.terminal_state
    if state is None:
        # TUI 未跑到终态（用户中途 q 退出）→ 视为未完成，exit 1（fail loud）。
        return EXIT_RUN_FAILED
    return EXIT_OK if state.status == "completed" else EXIT_RUN_FAILED


def main() -> None:
    """console_scripts 入口（pyproject ``[project.scripts] orca``）。"""
    # 子进程默认不 buffered，让 TUI 内的 print/echo 立即可见。
    app()


if __name__ == "__main__":
    main()
