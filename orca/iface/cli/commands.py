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

import asyncio
import json
import logging
import os
import time
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
    background: bool = typer.Option(
        False, "--background", "-b",
        help="后台跑（fork detached，立即返回 run_id，不占终端；用 ``orca ps/logs/wait`` 管理）",
    ),
) -> None:
    """跑一个 workflow（启动 Textual TUI，看 DAG 进度 / 日志 / 答 gate）。

    ``--background``：fork 出脱离终端的子进程跑 workflow，父进程立即返回 run_id + pid
    （SPEC §8 P3.2 daemon）。配合 ``orca ps`` / ``orca logs <id>`` / ``orca wait <id>``。
    子进程跑的就是普通 foreground ``orca run``，只是 parentless + stdio 落
    ``~/.orca/runs/<run_id>/log``。读 ``ORCA_BG_RUN_ID`` env 复用父进程生成的 run_id
    （保 tape / metadata 三者一致）。
    """
    if background:
        # 后台模式：校验 yaml → gen run_id → daemonize → 打印 run_id/pid/logs → 立即 exit 0。
        # 不走 _run_workflow（那个会起 TUI 阻塞终端）。
        raise typer.Exit(_start_background(yaml, task, inputs, max_iter))

    config = parse_run_args(yaml, task, inputs, max_iter)
    # 启动 TUI 在独立函数（延迟 import textual，让 import orca.iface.cli.commands 不拉 textual）。
    raise typer.Exit(_run_workflow(config))


def _start_background(
    yaml: Path,
    positional_task: str | None,
    i_args: list[str],
    max_iter: int | None,
) -> int:
    """``--background`` 入口：校验 yaml → gen run_id → daemonize fork → 打印信息。

    立即返回 exit 0（不阻塞终端）。daemonize 的子进程跑的是无 ``--background`` 的
    ``orca run``（foreground），故此函数不跑 workflow 主体。

    失败模式：
      - yaml 不存在 / ConfigurationError → exit 2（与 foreground run 同前置校验）。
      - 非 Unix（无 ``os.fork``）→ exit 1（``daemonize`` 内部 raise RuntimeError）。
    """
    # 校验前置（fork 前就发现 yaml 错，避免子进程起不来还看不到错）。
    try:
        wf = load_workflow(yaml)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        return EXIT_ARG_OR_VALIDATE
    except FileNotFoundError:
        typer.echo(f"workflow 文件不存在：{yaml}", err=True)
        return EXIT_ARG_OR_VALIDATE

    # run_id：父进程 gen 一次，经 env 传子进程复用（DRY：复用 gen_run_id，与 OrcaApp 同算法）。
    from orca.iface.cli.bg_runner import daemonize, log_path
    from orca.run.lifecycle import gen_run_id

    run_id = gen_run_id(wf.name)

    # 透传给 detached child 的 argv：剥掉 ``--background`` / ``-b``（子进程不再 detach），
    # 其余 flag（``-i`` / ``--max-iter`` / positional task）原样保留。
    extra_argv: list[str] = []
    if positional_task is not None:
        extra_argv.append(positional_task)
    for kv in i_args:
        extra_argv.extend(["-i", kv])
    if max_iter is not None:
        extra_argv.extend(["--max-iter", str(max_iter)])

    try:
        pid = daemonize(yaml, run_id, extra_argv)
    except RuntimeError as e:
        # 非 Unix 平台 / execv 失败 → exit 1（fail loud，stderr 打错）。
        typer.echo(f"后台启动失败：{e}", err=True)
        return EXIT_RUN_FAILED

    typer.echo(f"Started background run: {run_id}")
    typer.echo(f"PID: {pid}")
    typer.echo(f"logs: {log_path(run_id)}")
    return EXIT_OK


@app.command()
def validate(
    yaml: Path = typer.Argument(..., help="workflow YAML 文件路径"),
) -> None:
    """校验 workflow（不跑，只做结构 + 语义校验，报告 errors/warnings）。"""
    import warnings

    try:
        # phase-14：捕获 compile 期 DeprecationWarning（旧约定 prompt=None + name 匹配），
        # 展示到 stderr（不阻断，exit 0）。simplefilter("always") 确保每次都捕获（默认
        # DeprecationWarning 在 Python 3.2+ 只显示一次且非 main 时静默）。
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load_workflow(yaml)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
    except FileNotFoundError:
        typer.echo(f"文件不存在：{yaml}", err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
    for w in caught:
        if issubclass(w.category, DeprecationWarning):
            typer.echo(f"⚠️  {w.message}", err=True)
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


# ── executor 子命令组（后端二进制配置与健康检查）──────────────────────────────
# sub-Typer：executor show/set/unset/list/test 共享名词，比扁平 executor-show UX 好。
# 注：此处非函数内延迟 import——Typer app 装配必须在模块级（add_typer 修改模块级 ``app``
# 装配状态），无法延迟。executor_cmds 模块自身导入无重副作用（只 import typer + profiles +
# config），与 textual 那种重运行时不同。
from orca.iface.cli.executor_cmds import app as executor_app

app.add_typer(executor_app, name="executor", help="配置/测试 agent 后端二进制")


# ── ps / logs / wait 子命令（phase 11 §8 P3.2 daemon）─────────────────────────


@app.command()
def ps() -> None:
    """列出全部 background run（从 ``~/.orca/runs/*.json`` 读 metadata）。

    列：RUN_ID / WORKFLOW（yaml 文件名）/ STATUS / ELAPSED / PID。

    STATUS 由 ``effective_status`` 判：metadata.status 已 terminal → 原样；
    metadata.status=running 但 pid 已死 → ``crashed``（fail loud，子进程崩未及更新 metadata）。
    """
    from orca.iface.cli.bg_runner import TERMINAL_STATUSES, effective_status, list_all_meta

    runs = list_all_meta()
    if not runs:
        typer.echo("（无 background run；用 ``orca run <yaml> --background`` 启动）")
        return

    # 表头 + 行。ELAPSED：terminal status（completed/failed/crashed）→ finished_at - started_at
    # （固定，不再随墙钟增长，避免「已完成的 run elapsed 还在涨」误导）；running → now - started_at
    # （实时增长）。老 metadata 无 finished_at（None）→ fallback 到 now - started_at（向后兼容）。
    now = time.time()
    header = f"{'RUN_ID':<40} {'WORKFLOW':<24} {'STATUS':<12} {'ELAPSED':<10} {'PID':<8}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for meta in runs:
        status = effective_status(meta)
        if status in TERMINAL_STATUSES and meta.finished_at is not None:
            elapsed = meta.finished_at - meta.started_at
        else:
            elapsed = now - meta.started_at
        # workflow 名取 yaml 文件名（去扩展名），过长截断。
        wf_name = Path(meta.yaml_path).stem[:24]
        typer.echo(
            f"{meta.run_id[:40]:<40} {wf_name:<24} {status:<12} "
            f"{_format_elapsed(elapsed):<10} {meta.pid:<8}"
        )


def _format_elapsed(seconds: float) -> str:
    """秒 → ``1m30s`` / ``2h5m`` / ``45s`` 人类可读（``ps`` ELAPSED 列用）。"""
    if seconds < 0:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s}s" if s else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


@app.command()
def logs(
    run_id: str = typer.Argument(..., help="background run id（``ps`` 列出的 RUN_ID）"),
    follow: bool = typer.Option(
        False, "-f", "--follow", help="持续 tail（``tail -f``），Ctrl-C 退出",
    ),
    lines: int = typer.Option(
        50, "-n", "--lines", help="初始显示最后 N 行（``--follow`` 时也先显示这些）",
    ),
) -> None:
    """tail background run 的日志文件（``~/.orca/runs/<run_id>/log``）。

    ``--follow`` 持续 tail 新行（``tail -f`` 语义），Ctrl-C 退出。无 ``--follow``
    则打印最后 N 行后退出。

    失败模式：
      - run_id 无对应 metadata → exit 2（fail loud，提示先 ``orca ps`` 看合法 id）。
      - 日志文件不存在（run 还没写日志 / metadata 损坏）→ exit 2。
    """
    from orca.iface.cli.bg_runner import read_meta

    meta = read_meta(run_id)
    if meta is None:
        typer.echo(
            f"未找到 run_id {run_id!r} 的 metadata（用 ``orca ps`` 列全部 background run）",
            err=True,
        )
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)

    log_file = Path(meta.log_path)
    if not log_file.is_file():
        typer.echo(f"日志文件不存在：{log_file}（run 可能还未开始写日志）", err=True)
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)

    # 先打印最后 N 行（``tail -n``）。
    _tail_print(log_file, lines)

    if not follow:
        return

    # ``--follow``：持续 tail 新行。用 seek + read 循环（不依赖 inotify，跨平台 Unix OK）。
    # Ctrl-C（KeyboardInterrupt）→ 正常退出 0（用户主动结束 follow）。
    try:
        _follow(log_file)
    except KeyboardInterrupt:
        typer.echo("\n（停止 follow）")


def _tail_print(path: Path, n: int) -> None:
    """打印文件最后 n 行（``tail -n`` 语义）。n<=0 → 不打印。"""
    if n <= 0:
        return
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
    except OSError as e:
        typer.echo(f"读日志失败：{e}", err=True)
        return
    tail = all_lines[-n:] if len(all_lines) > n else all_lines
    for line in tail:
        _emit_log_line(line)


def _emit_log_line(line: str) -> None:
    """打印一行日志，自动补换行（文件末行可能缺 ``\\n``）。

    用 ``print``（非 ``typer.echo``）—— typer.echo 不支持 ``end=``，而我们要控制换行
    （日志行已有 ``\\n`` 时不重复加）。print 是 stdlib，签名稳定。
    """
    # 行已有 trailing \n → 原样输出（nl=False 等价）；否则补一个。
    print(line, end="" if line.endswith("\n") else "\n")


def _follow(path: Path) -> None:
    """``tail -f``：从文件末尾持续读新行，阻塞到进程被杀（KeyboardInterrupt）。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)  # 跳到文件末尾（只读后续新行，与 ``tail -f`` 一致）
        while True:
            line = f.readline()
            if line:
                _emit_log_line(line)
            else:
                time.sleep(0.3)


@app.command()
def wait(
    run_id: str = typer.Argument(..., help="background run id"),
    timeout: float | None = typer.Option(
        None, "--timeout", help="最长等 N 秒（默认无限等）；超时 exit 3",
    ),
) -> None:
    """阻塞直到 background run 进入终态（completed/failed/crashed）或超时。

    退出码：
      - 0：completed
      - 1：failed 或 crashed（子进程崩未及更新 metadata，``effective_status`` 检测出）
      - 2：run_id 不存在（metadata 找不到）
      - 3：超时（``--timeout`` 到了仍在 running）

    典型用法：``orca run x.yaml --background`` 后 ``orca wait <id>`` 阻塞到完成。
    """
    from orca.iface.cli.bg_runner import (
        TERMINAL_STATUSES,
        read_meta,
        wait_for_terminal,
    )

    meta = read_meta(run_id)
    if meta is None:
        typer.echo(
            f"未找到 run_id {run_id!r} 的 metadata（用 ``orca ps`` 列全部 background run）",
            err=True,
        )
        raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)

    status, final_meta = wait_for_terminal(run_id, timeout=timeout)
    final_meta = final_meta or meta

    if status not in TERMINAL_STATUSES:
        # 仍在 running 且未到 terminal —— 只可能是 timeout 触发。
        typer.echo(f"超时：run {run_id} 仍在 {status}（elapsed 见 ``orca ps``）", err=True)
        raise typer.Exit(code=3)

    typer.echo(f"run {run_id} 终态：{status}")
    if status == "completed":
        raise typer.Exit(code=0)
    # failed / crashed → exit 1（fail loud）。
    raise typer.Exit(code=1)





def _resolve_tape_path(tape_or_run_id: str) -> Path:
    """把 CLI 参数解析成 Tape 文件路径。

    - 参数是已存在的文件路径 → 直接用。
    - 否则视为 run_id，查默认 ``runs/<run_id>.jsonl``。

    run_id → tape_path 的拼法复用 ``bg_runner.default_tape_path``（DRY，单一真相源 ——
    resume / daemon / OrcaApp 三处约定一致，不各写一遍）。
    """
    from orca.iface.cli.bg_runner import default_tape_path

    p = Path(tape_or_run_id)
    if p.is_file():
        return p
    # 当作 run_id：用 bg_runner 的路径约定（与 daemon metadata.tape_path 同源）。
    return default_tape_path(tape_or_run_id)


def _resolve_workflow_yaml(
    tape_path: Path, yaml_override: Path | None
) -> Path | None:
    """定位 workflow YAML（resume 需要重建 Workflow 对象）。

    优先级：
      1. ``--yaml`` 显式覆盖（最高，fail loud 校验存在）。
      2. 从 Tape 的 ``workflow_started.data.workflow_name`` 推断，扫 ``examples/`` 匹配
         ``name:`` 字段等于 workflow_name 的 yaml（覆盖最常见的 examples 用法）。
      3. 找不到 → 返回 None（CLI 层 fail loud 提示用户传 ``--yaml``）。
    """
    if yaml_override is not None:
        if not yaml_override.is_file():
            typer.echo(f"--yaml 指定的文件不存在：{yaml_override}", err=True)
            raise typer.Exit(code=EXIT_ARG_OR_VALIDATE)
        return yaml_override
    # 从 tape 读 workflow_name。
    wf_name: str | None = None
    try:
        for line in tape_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "workflow_started":
                wf_name = obj.get("data", {}).get("workflow_name")
                break
    except FileNotFoundError:
        return None
    if wf_name is None:
        return None
    # 扫 examples/ 找 name 匹配的 yaml。
    examples_dir = Path("examples")
    if not examples_dir.is_dir():
        return None
    import yaml as _yaml

    for candidate in sorted(examples_dir.glob("*.yaml")):
        try:
            doc = _yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — 跳过非 yaml / 损坏文件
            continue
        if isinstance(doc, dict) and doc.get("name") == wf_name:
            return candidate
    return None


@app.command()
def resume(
    tape_or_run_id: str = typer.Argument(..., help="Tape 文件路径或 run_id"),
    yaml_path: Path | None = typer.Option(
        None, "--yaml", help="workflow YAML 路径（默认从 tape 的 workflow_name 推断）",
    ),
) -> None:
    """从 Tape 重放恢复 workflow，从崩溃点继续跑（headless，不启动 TUI）。

    Tape 是 Orca 的唯一 checkpoint（append-only JSONL）。本命令读 Tape 重放到崩溃前
    状态，emit ``workflow_resumed`` 后从崩溃点续跑。失败模式（SPEC §7.3）：

      - Tape 不存在 / 空 / 中段损坏 → exit 2
      - 末尾残行（崩溃写一半）→ fail-soft 截断 + 继续（不 exit 2）
      - Tape 已是 workflow_completed → exit 0（「已完成，无需 resume」）
      - 崩溃在 parallel 组中间 → exit 1（不支持 mid-group resume）
      - 续跑失败 → exit 1；续跑完成 → exit 0

    需要重建 Workflow 对象：用 ``--yaml`` 显式指定，或从 tape 的 workflow_name 在
    ``examples/`` 自动推断。
    """
    raise typer.Exit(_resume_workflow(tape_or_run_id, yaml_path))


def _resume_workflow(tape_or_run_id: str, yaml_override: Path | None) -> int:
    """resume 命令核心（headless 跑，返回退出码）。

    校验 / 失败模式映射 SPEC §7.3。typed exception → 明确 exit code（fail loud）。
    """
    from orca.events.bus import EventBus
    from orca.events.tape import Tape
    from orca.run.orchestrator import Orchestrator
    from orca.run.resume import (
        AlreadyCompletedError,
        EmptyTapeError,
        MidFileCorruptError,
        ParallelGroupMidCrashError,
        TapeNotFoundError,
    )

    # 1) 解析 tape 路径。
    tape_path = _resolve_tape_path(tape_or_run_id)
    if not tape_path.is_file():
        typer.echo(f"Tape 不存在：{tape_path}", err=True)
        return EXIT_ARG_OR_VALIDATE

    # 2) 定位 workflow yaml（resume 需要重建 Workflow）。
    resolved_yaml = _resolve_workflow_yaml(tape_path, yaml_override)
    if resolved_yaml is None:
        typer.echo(
            "无法定位 workflow YAML：请用 --yaml 显式指定（resume 需要重建 Workflow）",
            err=True,
        )
        return EXIT_ARG_OR_VALIDATE
    try:
        wf = load_workflow(resolved_yaml)
    except ConfigurationError as e:
        typer.echo(str(e), err=True)
        return EXIT_ARG_OR_VALIDATE

    # 3) 用 resume=True 打开 Tape（截断末尾残行，fail-soft，SPEC §7.3）。
    #    先读 run_id（从 tape 的 workflow_started 拿，保 bus.tape 连续性）。
    run_id = _read_run_id(tape_path) or "resumed"
    tape = Tape(tape_path, run_id=run_id, resume=True)
    bus = EventBus(tape)

    # 4) from_tape 校验 + 构造（typed exceptions → exit code）。
    try:
        orch = Orchestrator.from_tape(tape_path, bus, wf)
    except AlreadyCompletedError as e:
        # 非错误：workflow 已完成，exit 0。
        typer.echo(f"✓ {e}")
        bus.close()
        return EXIT_OK
    except EmptyTapeError as e:
        typer.echo(str(e), err=True)
        bus.close()
        return EXIT_ARG_OR_VALIDATE
    except MidFileCorruptError as e:
        typer.echo(str(e), err=True)
        bus.close()
        return EXIT_ARG_OR_VALIDATE
    except ParallelGroupMidCrashError as e:
        typer.echo(str(e), err=True)
        bus.close()
        return EXIT_RUN_FAILED

    # 5) run_from_state 续跑。
    try:
        state = asyncio.run(orch.run_from_state())
    except Exception:  # noqa: BLE001 — 顶层兜底
        logger.exception("resume 运行异常")
        return EXIT_RUN_FAILED
    return EXIT_OK if state.status == "completed" else EXIT_RUN_FAILED


def _read_run_id(tape_path: Path) -> str | None:
    """从 Tape 的 ``workflow_started`` 事件读 run_id（用顶层 run_id，保连续性）。

    Tape 每行不带 run_id（run_id 在 Tape 对象上，非 Event 字段）；但从
    ``workflow_started.data.inputs`` 与 tape 文件名（``<run_id>.jsonl``）可推。优先用
    文件名（与 OrcaApp 的 ``runs/<run_id>.jsonl`` 约定一致）。
    """
    name = tape_path.stem
    if name:
        return name
    return None


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
    port: int = typer.Option(7428, "--port", help="监听端口（浏览器访问 http://<host>:<port>）"),
    max_concurrent: int = typer.Option(
        3, "--max-concurrent", help="最大并发 run 数（超过排队）"
    ),
) -> None:
    """启动 Web UI（FastAPI + WebSocket，多 run 管理 + DAG + gate + tape replay + chart）。

    首次使用需先构建前端（一次性）::

        cd orca/iface/web/frontend && npm install && npm run build

    然后执行本命令，浏览器打开 http://127.0.0.1:7428 。hook 桥默认同端口（ORCA_PORT 可覆盖）。
    """
    import asyncio

    # 延迟 import：fastapi/uvicorn 仅 serve 路径需要（让 run/validate/list/--help 不拉 web 栈）。
    from orca.iface.web import RunManager, run_server

    manager = RunManager(max_concurrent=max_concurrent)
    typer.echo(f"Orca Web UI → http://{host}:{port}  (Ctrl-C 退出)")
    asyncio.run(run_server(manager, host=host, port=port))


@app.command()
def mcp(
    with_web: bool = typer.Option(
        False, "--with-web", help="同进程额外挂 Web UI（stdin EOF 后转 daemon）"
    ),
    web_port: int = typer.Option(
        7428, "--web-port", help="--with-web 模式 Web 监听端口"
    ),
    max_concurrent: int = typer.Option(
        3, "--max-concurrent", help="最大并发 run 数（超过排队）"
    ),
    idle_timeout: int = typer.Option(
        30,
        "--idle-timeout",
        help="--with-web 模式下，无活跃 run 持续 N 分钟后退出（仅 daemon 生效）",
    ),
    runs_dir: str | None = typer.Option(
        None,
        "--runs-dir",
        help="tape 落盘目录（默认 ./runs）。测试隔离用，业务无需配置",
    ),
) -> None:
    """启动 MCP server（stdio JSON-RPC），供 Claude Code / opencode / Cursor 接入。

    CC 拉起本命令后通过 stdin/stdout 调四件套工具（start_workflow / get_task_status /
    resolve_gate / cancel_task）。无 --with-web 时随 CC session 生灭（stdin EOF 退出）；
    --with-web 时同进程挂 Web UI，stdin EOF 后转 daemon 继续监控（idle_timeout 分钟无活跃 run 退出）。
    """
    import asyncio

    # 延迟 import：mcp SDK 仅 mcp 命令需要（让 run/validate/list/serve/--help 不拉 mcp 栈）。
    from orca.iface.mcp import run_mcp_server

    asyncio.run(
        run_mcp_server(
            with_web=with_web,
            web_port=web_port,
            max_concurrent=max_concurrent,
            idle_timeout=idle_timeout,
            runs_dir=runs_dir,
        )
    )


# ── 入口 ─────────────────────────────────────────────────────────────────────


def _run_workflow(config: RunConfig) -> int:
    """启动 Textual TUI 跑 workflow，返回退出码（0/1）。

    校验失败（yaml 不存在 / ConfigurationError）→ exit 2。
    workflow 终态 completed → 0；failed → 1。运行期异常 → 1（fail loud，stderr 打 stack）。

    phase 11 §8 P3.2 daemon：若进程是 detached child（``ORCA_BG_RUN_ID`` env 存在），
    **不启动 TUI**（detached 进程无 TTY，Textual 会 hang / 崩），改走 headless 路径
    （直接 ``Orchestrator.run()``，与 resume 同 pattern）。跑完时调 ``mark_terminal_status``
    更新 ``~/.orca/runs/<run_id>.json`` 的 status（让 ``ps``/``wait`` 看到 completed/failed，
    而非靠 pid 死检测成 crashed）。
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

    from orca.iface.cli.bg_runner import ENV_BG_RUN_ID, mark_terminal_status

    bg_run_id = os.environ.get(ENV_BG_RUN_ID)

    # daemon detached child：headless 跑（无 TTY，TUI 会崩）。
    if bg_run_id is not None:
        return _run_workflow_headless(config, wf, bg_run_id)

    # 延迟 import：textual 仅在此路径才需要（让 ``--help`` / validate / list 不拉 textual）。
    from orca.iface.cli.app import OrcaApp

    tui = OrcaApp(
        wf=wf,
        inputs=config.inputs,
        task=config.task,
        max_iter=config.max_iter,
    )
    # 不在此处调 kickoff：``@work`` decorator 需要 Textual event loop running，
    # 而 ``tui.run()`` 是阻塞起 loop 的入口——run() 之前 loop 还没起，调 kickoff 会撞
    # ``no running event loop`` RuntimeError。kickoff 由 ``OrcaApp.on_mount`` 自动调
    # （那时 loop 已 running，与 ``_consume_events`` 同 pattern）。
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


def _run_workflow_headless(
    config: RunConfig, wf: "Workflow", bg_run_id: str,
) -> int:
    """detached daemon child 的 headless 执行路径（无 Textual TUI）。

    daemon 子进程脱离了 controlling terminal（``setsid``），无 TTY —— Textual TUI 在无
    TTY 下会 hang / 崩（init 序列写不出）。故 background child 不走 TUI，直接调
    ``Orchestrator.run()``（与 resume 的 ``run_from_state`` 同 headless pattern）。

    run_id 从 ``ORCA_BG_RUN_ID`` env 拿（父进程已 gen，保 tape/metadata 一致）。
    跑完调 ``mark_terminal_status`` 让 ``ps``/``wait`` 立刻看到终态。
    """
    from orca.events.bus import EventBus
    from orca.events.tape import Tape
    from orca.iface.cli.bg_runner import default_tape_path, mark_terminal_status
    from orca.run.orchestrator import Orchestrator

    # tape_path：与父进程 metadata 记录的 tape_path 一致（default_tape_path，runs/<id>.jsonl）。
    tape = Tape(default_tape_path(bg_run_id), run_id=bg_run_id)
    bus = EventBus(tape)
    try:
        orch = Orchestrator(
            wf, bus, inputs=config.inputs,
            task=config.task, max_iter=config.max_iter, run_id=bg_run_id,
        )
    except ValueError:
        # 配置错误（必填 input 缺失等）→ workflow_failed（Orchestrator.__init__ 抛）。
        logger.exception("headless Orchestrator 构造失败（配置错误）")
        mark_terminal_status(bg_run_id, "failed")
        return EXIT_RUN_FAILED
    except BaseException:
        # KeyboardInterrupt / SystemExit（SIGTERM 默认）也要标 failed —— detached daemon 被
        # kill 时 metadata 不能停在 running 误导用户。effective_status 的 pid-death 检测会
        # 把遗漏的标 crashed，但显式更新更准确（failed vs crashed 语义不同）。
        mark_terminal_status(bg_run_id, "failed")
        raise

    try:
        state = asyncio.run(orch.run())
    except Exception:  # noqa: BLE001 —— 业务异常顶层兜底
        logger.exception("headless workflow 运行异常")
        mark_terminal_status(bg_run_id, "failed")
        return EXIT_RUN_FAILED
    except BaseException:
        # KeyboardInterrupt / SystemExit / SIGTERM：标 failed 让 metadata 不停在 running，
        # 然后 re-raise（不吞 KeyboardInterrupt，保 Ctrl-C 语义；detached child 收 SIGTERM
        # 也走此路径，正常退出且 metadata 已更新）。
        mark_terminal_status(bg_run_id, "failed")
        raise

    exit_code = EXIT_OK if state.status == "completed" else EXIT_RUN_FAILED
    mark_terminal_status(
        bg_run_id, "completed" if state.status == "completed" else "failed"
    )
    return exit_code


def main() -> None:
    """console_scripts 入口（pyproject ``[project.scripts] orca``）。"""
    # 函数内 import（保模块导入零副作用，对齐 commands.py:17-18 的 textual 延迟 import 纪律）：
    # 把 ~/.orca/config.json 的 binary override 注入对应 env var，之后所有 orca run 生效。
    from orca.iface.cli.config import bootstrap_config

    bootstrap_config()
    # 子进程默认不 buffered，让 TUI 内的 print/echo 立即可见。
    app()


if __name__ == "__main__":
    main()
