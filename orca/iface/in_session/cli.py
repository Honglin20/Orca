"""orca/iface/in_session/cli.py —— in-session shell 用户命令面（SPEC v7 / ADR v3 §6）。

**薄 CLI = 唯一大脑 + 唯一 tape 写者**（D-v7-1）。宿主（opencode plugin / CC hook
脚本）是**哑传输**：spawn CLI + 读 stdout JSON 顶层字段。Orca 业务逻辑（advance_step
决策 / marker RMW / 合规计数 / 失败 taxonomy）全在此模块，可单测。

子命令（按 SPEC §13 迁移清单）：
  - ``bootstrap <wf>`` —— 首 step：gen run_id + tape 路径 + 写激活 marker + per-call
    flock emit_batch(workflow_started + node_started(entry)) → stdout entry prompt JSON。
    **幂等**（N1）：同 owner + 同 ``realpath(yaml)`` → 复用 run_id 不重发。
  - ``next --tape --run-id [--output] [--inputs]`` —— 推进一步：per-call flock
    (LOCK_NB → busy) + ``--output`` 空串 normalize None（B2）+ advance_step + **单次
    write 原子 ``emit_batch``**（B1）+ marker RMW 在 flock 临界区内（N2）+ 合规计数
    (F11) + 失败 taxonomy (F6) → stdout JSON。
  - ``status [<run_id>]`` —— 读 tape replay_state 报进度（沿用 v5）。
  - ``stop <run_id>`` —— 清激活 marker + per-call flock emit ``workflow_cancelled``。
  - ``start <wf>`` —— CC 路：生成 settings.json Stop/PostToolUse hook 片段 + 写 marker
    + 打印接入指引。
  - ``serve`` —— 无头 CI daemon 入口（I3.3a，主 UX 不用；保留 v5 自驱动循环）。

**铁律 1**：跨进程 sanctioned 写者（per-call CLI 形态，I3.3b）；每次 hook 触发短命
open(resume=True) → flock → emit_batch → close，flock 随进程退出释放（无孤儿锁反向）。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import typer

from orca.compile import load_workflow
from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session.marker import (
    ActivationMarker,
    clear_marker,
    find_marker_by_run_id,
    marker_path,
    read_marker,
    write_marker,
)
from orca.run.lifecycle import (
    gen_run_id,
    make_workflow_failed,
    now_monotonic,
)
from orca.run.step import InSessionError, advance_step

logger = logging.getLogger(__name__)

# subagent 合规超限阈值（D-v7-6：连续 N 次 next 无 output → workflow_failed）。
_COMPLIANCE_LIMIT = 3

# compact prompt 交付（SPEC §2.1 / §2.6.2，2026-07-08）：bootstrap + next 不再把
# 整段渲染后的 prompt 经 .prompt 注入主 session（长 prompt 会全量进主 session 上下文且
# 永久驻留对话历史）。改为 Orca 把渲染后 prompt 落盘到 <prompts_dir>/<node>.md，主 session
# 只收一句 host-facing **指针**（"派子代理读 <path> 执行"），子代理从文件读完整指令。
# 两种 agent 形态（agent:<name> 引用 md / inline prompt）渲染时无差别（compile 已扁平化）。
# plugin 仍读 reply.prompt（现是指针文本），零改动。


def _prompts_dir_for(tape_path: Path, run_id: str) -> Path:
    """compact prompt 文件目录：``<rundir>/<run_id>/prompts/``（rundir = tape 同目录）。"""
    return Path(tape_path).parent / run_id / "prompts"


def _build_pointer(result: Any) -> str:
    """把 StepResult(prompt_file, resources_root) 拼成 host-facing 指针文本。

    主 session 收到这句即知：派 task 子代理、读哪个文件、可选资源目录。子代理读文件执行，
    其输出即本节点输出（仍经 plugin 的 ToolPart.state.output 提取 → next --output）。
    """
    lines = [
        "【Orca 节点执行】请用 task 工具派一个子代理执行本节点，不要自己直接回答。",
        f"完整节点指令已写入：{result.prompt_file}",
        "请子代理先 Read 该文件，按其要求执行；子代理的输出即本节点的输出。",
    ]
    if result.resources_root:
        lines.append(f"附资源目录（脚本/参考，按需 Read）：{result.resources_root}")
    return "\n".join(lines) + "\n"


def _reply_prompt(result: Any) -> str | None:
    """compact：``prompt_file`` 给定 → 返指针；否则 inline 回退全量 ``prompt``。

    inline 回退仅在 advance_step 未传 prompts_dir 时出现（daemon 形态 / 直调单测）；
    生产路径（bootstrap/next）恒传 prompts_dir → 恒走指针。
    """
    if result.prompt_file:
        return _build_pointer(result)
    return result.prompt


def _default_tape_path(run_id: str) -> Path:
    """lazy import 避开 orca.iface.cli 包初始化期的循环 import。"""
    from orca.iface.cli.bg_runner import default_tape_path
    return default_tape_path(run_id)


def _default_rundir() -> Path:
    """rundir = tape 文件所在目录（runs/）。所有 in-session run 共享同一 rundir。"""
    return _default_tape_path("__probe__").parent


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ── 跨进程 flock helpers（I3.3b）─────────────────────────────────────────────


def _flock_path(tape_path: Path) -> Path:
    """tape 专属 flock 文件（与 tape 同目录，加 ``.lock`` 后缀）。"""
    return Path(str(tape_path) + ".lock")


def _try_acquire_flock(tape_path: Path) -> tuple[Any, Path] | None:
    """LOCK_EX | LOCK_NB；拿不到返 None（busy，0 退出语义）。

    flock 文件随 tape 同目录（同 run 作用域隔离）。fd 由调用方 ``try/finally`` close。
    本地 FS 假设由 ADR I3.3 守（NFS / 网络盘 flock 语义不保证，CLI 不专门检测——
    主 UX 在用户 repo 根目录跑，非本地 FS 由调用方规避）。
    """
    tape_path = Path(tape_path)
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _flock_path(tape_path)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # busy（F5 闭环）：另一 CLI 持锁，本调用方下一轮 idle 重试。
        fd.close()
        return None
    return fd, lock_path


def _release_flock(fd: Any) -> None:
    """释放 flock + close fd（try/finally 兜底；进程退出 OS 也会回收）。"""
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
    finally:
        fd.close()


# 注：ADR I3.3「仅本地 FS」不在 CLI 启动期主动检测——``fcntl.flock`` 在 NFS / 网络盘
# 上会显式失败或行为异常，运行时会暴露（不靠启发式检测给读者虚假安全感）。本地 FS
# 假设由 SPEC §0 明示（用户在 repo 根目录跑 ./runs/）。


# ── 失败 taxonomy（SPEC §2.5 表，F6 闭环）─────────────────────────────────────


def _classify_in_session_error(exc: InSessionError) -> str:
    """读 ``exc.error_kind``（SPEC §2.5 ``error_type``），取代脆弱的消息子串匹配。

    分类由 step.py 各 raise 处显式传 ``error_kind=ERR_*`` 携带（类型安全：加新 kind = 加
    常量 + raise 处传，不必维护本函数的关键词表）。``error_kind`` 缺省 → ``internal_error``
    （兜底，fail loud 不静默）。
    """
    return getattr(exc, "error_kind", None) or "internal_error"


async def _emit_workflow_failed(
    bus: EventBus, error_type: str, message: str, node: str | None = None,
) -> None:
    """落 ``workflow_failed`` 终态（单真相源），吞错仅 log（tape 可能已坏，仍要返信封）。"""
    logger.exception("emit workflow_failed (error_type=%s)", error_type)
    try:
        t, d = make_workflow_failed(error_type, message, node=node)
        await bus.emit(t, d, node=node)
    except Exception:
        logger.exception("emit workflow_failed 也失败（tape 可能已坏）")


def _emits_to_event_datas(emits: list) -> list[dict]:
    """``advance_step`` 返的 ``list[Emit]`` → ``emit_batch`` 入参形态。"""
    return [
        {
            "type": e.type,
            "data": e.data,
            "node": e.node,
            "timestamp": time.time(),
        }
        for e in emits
    ]


# ── Typer app ────────────────────────────────────────────────────────────────


app = typer.Typer(
    name="in-session",
    help="in-session shell：宿主主 session 执行 workflow，薄 CLI 独占 tape、确定性算下一步。",
    no_args_is_help=True,
)


@app.command()
def bootstrap(
    yaml: Path = typer.Argument(..., help="workflow YAML 路径", exists=True),
    inputs: str = typer.Option("{}", "--inputs", help="workflow inputs（JSON）"),
    owner: str = typer.Option(
        None, "--owner", help="激活 marker 的 owner key（opencode=sessionID / CC=run_id）",
    ),
    model: str = typer.Option(
        "deepseek/deepseek-v4-flash", "--model", help="provider/model（记入 marker）",
    ),
    session_id: str = typer.Option(
        None, "--session-id", help="主 session ID（记入 marker，opencode 子 session 过滤用）",
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """起一个 in-session run 的首步（gen run_id + tape + marker + emit ws+ns → entry prompt）。"""
    _setup_logging(log_level)
    wf = load_workflow(yaml)  # fail loud：非法 yaml 抛 ConfigurationError
    try:
        inp = json.loads(inputs) if inputs else {}
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--inputs 不是合法 JSON：{e}") from e

    run_id = gen_run_id(wf.name)
    tape_path = _default_tape_path(run_id)
    owner_key = owner or run_id   # 默认 CC 路用 run_id（无 sessionID 概念）
    rundir = tape_path.parent
    mpath = marker_path(rundir, owner_key)
    yaml_real = os.path.realpath(yaml)

    # 幂等去重（N1/F14）：advisory lock marker 文件贯穿整个 bootstrap（check → emit →
    # marker write），否则两并发 bootstrap 同 owner+yaml 会各自 gen run_id、emit 两次
    # workflow_started。锁覆盖 check-write 整体。
    rundir.mkdir(parents=True, exist_ok=True)
    mlock_fd = open(mpath.with_suffix(mpath.suffix + ".flock"), "w")
    try:
        fcntl.flock(mlock_fd.fileno(), fcntl.LOCK_EX)
        existing = read_marker(mpath)
        if (
            existing is not None
            and existing.owner == owner_key
            and existing.yaml == yaml_real
        ):
            # 复用 run_id 不重发 workflow_started（N1）。
            typer.echo(json.dumps({
                "run_id": existing.run_id,
                "tape": existing.tape_path,
                "done": False,
                "node": None,
                "prompt": None,
                "reused": True,
            }, ensure_ascii=False))
            return

        # 新 run：per-call flock tape → advance_step(无 output, bootstrap 分支) → emit_batch。
        tape = Tape(tape_path, run_id=run_id, resume=True)
        bus = EventBus(tape)
        acquired = _try_acquire_flock(tape_path)
        if acquired is None:
            # bootstrap 撞锁：同 run 已有 in-flight CLI（罕见，bootstrap 通常首调）。
            typer.echo(json.dumps({"done": False, "reason": "busy"}))
            return
        fd, _ = acquired
        try:
            result = asyncio.run(_advance_and_emit(bus, wf, tape, output=None, inputs=inp,
                                                   run_id=run_id, elapsed=0.0,
                                                   prompts_dir=_prompts_dir_for(tape_path, run_id)))
        except InSessionError as e:
            # bootstrap 失败 = 配置坏（如 entry 不是 agent 节点），fail loud。
            asyncio.run(_emit_workflow_failed(
                bus, _classify_in_session_error(e), str(e),
            ))
            bus.close()
            typer.echo(json.dumps({"done": True, "reason": f"failed: {e}"}))
            raise typer.Exit(1)
        finally:
            try:
                _release_flock(fd)
            finally:
                bus.close()

        # 写激活 marker（仍在 advisory lock 内；B-5 闭环：包 try，失败 emit workflow_failed
        # 不留「tape 有 ws+ns 但无 marker」的不可恢复态）。
        try:
            write_marker(mpath, ActivationMarker(
                run_id=run_id,
                tape_path=os.path.realpath(tape_path),
                yaml=yaml_real,
                owner=owner_key,
                model=model,
                session_id=session_id,
                no_output_count=0,
            ))
        except OSError as e:
            # marker 写失败（磁盘满 / 权限）→ tape 已 emit ws+ns 但 next 无 marker → 不可恢复。
            # fail loud：另开 tape 写 workflow_failed（best effort）+ 非 0 退出。
            logger.exception("bootstrap 写激活 marker 失败")
            try:
                tape2 = Tape(tape_path, run_id=run_id, resume=True)
                bus2 = EventBus(tape2)
                try:
                    asyncio.run(_emit_workflow_failed(
                        bus2, "internal_error", f"write_marker failed: {e}",
                    ))
                finally:
                    bus2.close()
            except Exception:
                logger.exception("marker 失败后 workflow_failed 也失败")
            typer.echo(json.dumps({"done": True, "reason": f"failed: write_marker: {e}"}))
            raise typer.Exit(1)

        reply: dict[str, Any] = {
            "run_id": run_id,
            "tape": str(tape_path),
            "done": result.done,
        }
        if result.node:
            reply["node"] = result.node
        prompt_text = _reply_prompt(result)
        if prompt_text:
            reply["prompt"] = prompt_text
        if result.prompt_file:
            reply["prompt_file"] = result.prompt_file
        typer.echo(json.dumps(reply, ensure_ascii=False))
    finally:
        try:
            fcntl.flock(mlock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            mlock_fd.close()


@app.command()
def next(
    tape: Path = typer.Option(..., "--tape", help="tape 文件路径"),
    run_id: str = typer.Option(..., "--run-id", help="run id"),
    output: str = typer.Option(
        None, "--output",
        help="宿主 subagent 输出（PostToolUse/tool_result 提取）。空串 ≡ 缺失（B2 normalize None）",
    ),
    inputs: str = typer.Option("{}", "--inputs", help="workflow inputs（JSON）"),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """推进一步：flock + advance_step + emit_batch（B1 单次 write 原子）+ marker RMW (N2)。"""
    _setup_logging(log_level)
    # --output 空串 normalize 为 None（B2 闭环）：CLI 入口规约，使 hook 可放心传 ""。
    normalized_output: str | None = output if output else None

    try:
        inp = json.loads(inputs) if inputs else {}
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--inputs 不是合法 JSON：{e}") from e

    tape_path = Path(tape)

    # per-call flock（LOCK_NB → busy 0 退出，F5）。
    acquired = _try_acquire_flock(tape_path)
    if acquired is None:
        typer.echo(json.dumps({"done": False, "reason": "busy"}))
        return
    fd, _ = acquired

    # 定位激活 marker（next 契约只含 --tape --run-id；按 run_id 扫描，N2 RMW 用）。
    rundir = tape_path.parent
    mpath = find_marker_by_run_id(rundir, run_id)

    # 在 flock 临界区内 open + advance + emit + marker RMW（N2：marker RMW 被 flock 串行化）。
    tape_obj = Tape(tape_path, run_id=run_id, resume=True)   # 半写恢复（I3.4）
    bus = EventBus(tape_obj)
    start_ts = now_monotonic()
    try:
        result, compliance_failed = asyncio.run(_next_in_critical_section(
            bus, tape_obj, run_id, normalized_output, inp, start_ts, mpath,
            _prompts_dir_for(tape_path, run_id),
        ))
    except InSessionError as e:
        error_type = _classify_in_session_error(e)
        asyncio.run(_emit_workflow_failed(bus, error_type, str(e)))
        # 终态后清 marker（workflow_failed 已落 tape，marker 不再需要）。
        if mpath is not None:
            clear_marker(mpath)
        typer.echo(json.dumps({"done": True, "reason": f"failed: {e}"}))
        raise typer.Exit(1)
    finally:
        try:
            _release_flock(fd)
        finally:
            bus.close()

    reply: dict[str, Any] = {"done": result.done or compliance_failed}
    if result.node:
        reply["node"] = result.node
    prompt_text = _reply_prompt(result)
    if prompt_text:
        reply["prompt"] = prompt_text
    if result.prompt_file:
        reply["prompt_file"] = result.prompt_file
    if result.reason:
        reply["reason"] = result.reason
    typer.echo(json.dumps(reply, ensure_ascii=False))
    # 合规超限与其他失败 taxonomy 对齐（fail loud 非 0 退出；SPEC §2.5 失败统一语义）。
    # 其他失败（output_schema_mismatch / unsupported_node_kind / state_corrupt）经
    # InSessionError 路径走 typer.Exit(1)；compliance_failed 经 marker 计数路径也走 exit 1。
    if compliance_failed:
        raise typer.Exit(1)


async def _advance_and_emit(
    bus: EventBus, wf, tape: Tape, *, output: str | None,
    inputs: dict, run_id: str, elapsed: float, prompts_dir: Path | None = None,
):
    """调 advance_step + emit_batch（单次 write 原子化，B1）。"""
    result = advance_step(
        tape, wf, output=output, inputs=inputs, run_id=run_id, elapsed=elapsed,
        prompts_dir=prompts_dir,
    )
    if result.emits:
        await bus.emit_batch(_emits_to_event_datas(result.emits))
    return result


async def _next_in_critical_section(
    bus: EventBus, tape: Tape, run_id: str, output: str | None,
    inputs: dict, start_ts: float, mpath: Path | None,
    prompts_dir: Path | None = None,
):
    """flock 临界区内的 next 主体：advance + emit_batch + marker RMW（N2）+ 合规计数 (F11)。

    返回 ``(result, compliance_failed)``。``compliance_failed=True`` 时已 emit
    workflow_failed，调用方据 ``done=True`` 停注入。
    """
    from orca.compile import load_workflow  # 局部，避免顶层依赖循环
    # wf 从 marker 取 yaml 路径（next 不收 --yaml）。marker 缺 → 退化 fail loud。
    if mpath is None:
        # 无 marker：调用方未 bootstrap 或 marker 已清；幂等吞 + warn（不 raise）。
        logger.warning("next 找不到 %s 的激活 marker，无法推进（需先 bootstrap）", run_id)
        from orca.run.step import StepResult
        return StepResult(done=False, reason="no-marker"), False
    marker = read_marker(mpath)
    if marker is None:
        from orca.run.step import StepResult
        return StepResult(done=False, reason="no-marker"), False

    wf = load_workflow(marker.yaml)
    result = advance_step(
        tape, wf, output=output, inputs=inputs, run_id=run_id,
        elapsed=now_monotonic() - start_ts, prompts_dir=prompts_dir,
    )
    if result.emits:
        # B1 单次 write 原子化整批 [nc, rt, ns] / [nc, rt, workflow_completed]。
        await bus.emit_batch(_emits_to_event_datas(result.emits))

    # 合规计数（D-v7-6 / F11）：无 output 且无 emits（branch 4 idempotent-replay）→ +1；
    # 有 output → 清零。≥3 → CLI 主动 emit workflow_failed(subagent_compliance)。
    compliance_failed = False
    if output is not None:
        marker.no_output_count = 0
    elif result.emits == [] and not result.done and result.node is not None:
        marker.no_output_count += 1
        if marker.no_output_count >= _COMPLIANCE_LIMIT:
            await _emit_workflow_failed(
                bus, "subagent_compliance",
                f"subagent 连续 {marker.no_output_count} 次未派 Task/产出 output",
                node=result.node,
            )
            compliance_failed = True

    # marker RMW（N2）：flock 临界区内回写。终态 → 清 marker（不复用）。
    if result.done or compliance_failed:
        clear_marker(mpath)
    else:
        write_marker(mpath, marker)
    return result, compliance_failed


@app.command(name="status")
def status(
    run_id: str = typer.Argument(None, help="run id（省略则列 runs/ 下全部 run tape）"),
    json_output: bool = typer.Option(
        False, "--json",
        help="输出 JSON（plugin messages.transform 入口用，SPEC §2.6.2 改写契约）",
    ),
) -> None:
    """查看 in-session run 的 workflow 进度（读 tape replay_state）。

    SPEC §2.6.2：当 plugin 入口（``/orca status`` → ``messages.transform``）调用时，
    stdout 必须是 JSON ``{status, current_node, node_status, progress}``，plugin
    据此提取顶层字段替换 user 消息文本。人类 UX 默认多行文本（v7 行为保留）。
    """
    from orca.events.replay import replay_state

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

    tape_path = _default_tape_path(run_id)
    if not tape_path.exists():
        typer.echo(typer.style(f"run {run_id!r} 无 tape", fg=typer.colors.RED))
        raise typer.Exit(1)
    state = replay_state(Tape(tape_path, run_id=run_id))
    done = sum(1 for s in state.node_status.values() if s == "done")
    progress = f"{done}/{len(state.node_status)}"

    if json_output:
        # SPEC §2.6.2 plugin 改写契约：顶层字段供 plugin rewriteText 提取。
        typer.echo(json.dumps({
            "run_id": run_id,
            "status": state.status,
            "current_node": state.current_node,
            "node_status": dict(state.node_status),
            "progress": progress,
        }, ensure_ascii=False))
        return

    typer.echo(f"run {run_id}")
    typer.echo(f"  status:      {state.status}")
    typer.echo(f"  current_node: {state.current_node}")
    typer.echo(f"  node_status: {dict(state.node_status)}")
    typer.echo(f"  progress:    {progress} done")


@app.command()
def stop(
    run_id: str = typer.Argument(
        None, help="要停的 run id（省略时按 --owner 查激活 marker，SPEC §2.6.2 plugin 入口用）",
    ),
    owner: str = typer.Option(
        None, "--owner", help="opencode sessionID（marker 文件名 key，省 run_id 时按此查 marker）",
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """停一个 run：清激活 marker + per-call flock emit ``workflow_cancelled``。

    SPEC §2.6.2 plugin 入口契约：``/orca stop`` 不带 args → plugin 用 sessionID 作
    ``--owner`` 查 marker 拿 run_id；CLI 据此 stop。两调用形态共用本子命令（单一接口）。
    """
    _setup_logging(log_level)

    # run_id 省略时按 --owner 查 marker（plugin transform 入口路径）。
    if run_id is None and owner is not None:
        mpath0 = marker_path(_default_rundir(), owner)
        m = read_marker(mpath0)
        if m is None:
            typer.echo(json.dumps({
                "ok": False, "run_id": None, "reason": "no-active-marker-for-owner",
            }))
            return
        run_id = m.run_id

    if run_id is None:
        typer.echo(json.dumps({
            "ok": False, "run_id": None,
            "reason": "missing run_id (positional or --owner required)",
        }))
        raise typer.Exit(1)

    tape_path = _default_tape_path(run_id)
    rundir = tape_path.parent
    mpath = find_marker_by_run_id(rundir, run_id)

    if not tape_path.exists():
        # 无 tape：仅清 marker（stop 幂等，允许「run 已清理但 marker 残留」）。
        if mpath is not None:
            clear_marker(mpath)
        typer.echo(json.dumps({"run_id": run_id, "ok": True, "note": "no-tape"}))
        return

    acquired = _try_acquire_flock(tape_path)
    if acquired is None:
        typer.echo(json.dumps({"done": False, "reason": "busy"}))
        return
    fd, _ = acquired
    tape_obj = Tape(tape_path, run_id=run_id, resume=True)
    bus = EventBus(tape_obj)
    try:
        asyncio.run(bus.emit("workflow_cancelled", {"reason": "user_stop"}))
    finally:
        try:
            _release_flock(fd)
        finally:
            bus.close()
            if mpath is not None:
                clear_marker(mpath)
    typer.echo(json.dumps({"run_id": run_id, "ok": True, "done": True}))


@app.command()
def start(
    yaml: Path = typer.Argument(..., help="workflow YAML 路径", exists=True),
    model: str = typer.Option(
        "deepseek/deepseek-v4-flash", "--model", help="provider/model（记入 marker）",
    ),
) -> None:
    """CC + opencode 双宿主准备：写 marker（CC）+ 生成 opencode plugin/command 模板文件
    + 打印 CC settings.json Stop/PostToolUse hook 片段 + 接入指引。

    CC：用户把 stdout 的 ``settings.json`` 片段贴进 ``.claude/settings.json``，CC 启动后
    Stop/PostToolUse hook 自动 spawn ``orca in-session next`` 驱动 workflow。

    opencode（v8）：把仓库模板 ``orca.ts`` + ``orca.md`` 写入项目 ``.opencode/plugin/`` +
    ``.opencode/command/``（一次性安装）。用户在 opencode 里敲 ``/orca run <wf>`` →
    ``experimental.chat.messages.transform`` 入口 → marker 派发 → CLI。
    """
    from orca.iface.in_session.templates import render_cc_settings_fragment

    wf = load_workflow(yaml)
    run_id = gen_run_id(wf.name)
    tape_path = _default_tape_path(run_id)
    yaml_real = os.path.realpath(yaml)
    tape_real = os.path.realpath(tape_path)

    # 写 marker（owner = run_id，CC 路无 sessionID 概念）。
    rundir = tape_path.parent
    mpath = marker_path(rundir, run_id)
    write_marker(mpath, ActivationMarker(
        run_id=run_id, tape_path=tape_real, yaml=yaml_real,
        owner=run_id, model=model, session_id=None, no_output_count=0,
    ))

    # v8：写 opencode 模板文件（.opencode/plugin/orca.ts + .opencode/command/orca.md）。
    opencode_paths = _install_opencode_templates()

    fragment = render_cc_settings_fragment(
        run_id=run_id, tape_path=tape_real, yaml_path=yaml_real, model=model,
    )
    typer.echo(f"workflow: {wf.name}")
    typer.echo(f"run_id:   {run_id}")
    typer.echo(f"tape:     {tape_real}")
    typer.echo(f"marker:   {mpath}")
    typer.echo("")
    typer.echo(typer.style(
        f"opencode 模板已写入：{opencode_paths['plugin']} + {opencode_paths['command']}",
        fg=typer.colors.GREEN,
    ))
    typer.echo(typer.style(
        "  → 重启 opencode 后敲 `/orca doctor` 自检入口链路。",
        fg=typer.colors.GREEN,
    ))
    typer.echo(typer.style(
        "  → `$ARGUMENTS` 限制：args 不得含 `>` 或换行（marker regex 行首/行尾锚定）。",
        fg=typer.colors.YELLOW,
    ))
    typer.echo("")
    typer.echo(typer.style("把以下片段贴进 .claude/settings.json（CC 路）：", fg=typer.colors.CYAN, bold=True))
    typer.echo(json.dumps(fragment, indent=2, ensure_ascii=False))
    typer.echo("")
    typer.echo(typer.style("然后：", fg=typer.colors.CYAN))
    typer.echo("  1. 在 CC 里打开一个会话（Stop/PostToolUse hook 自动生效）。")
    typer.echo("  2. 让主 session 派 Task subagent 执行节点（每节点一 turn）。")
    typer.echo(f"  3. 跑完用 `orca in-session status {run_id}` 看结果。")
    typer.echo("  4. opencode 路径：重启 opencode → `/orca doctor` → `/orca run <wf>`。")


def _install_opencode_templates() -> dict[str, Path]:
    """把仓库模板 ``orca.ts`` + ``orca.md`` 写入项目 cwd 的 ``.opencode/``。

    幂等：文件存在且内容相同则不覆盖（避免抹掉用户改动）；存在但内容不同 → 覆盖并 backup
    旧文件（``.bak``）。返回写入路径。
    """
    import importlib.resources as ir

    tmpl_root = Path(__file__).parent / "templates" / "opencode"
    src_plugin = tmpl_root / "orca.ts"
    src_command = tmpl_root / "command" / "orca.md"

    dst_plugin_dir = Path(".opencode/plugin")
    dst_command_dir = Path(".opencode/command")
    dst_plugin_dir.mkdir(parents=True, exist_ok=True)
    dst_command_dir.mkdir(parents=True, exist_ok=True)

    dst_plugin = dst_plugin_dir / "orca.ts"
    dst_command = dst_command_dir / "orca.md"

    _atomic_write_with_backup(dst_plugin, src_plugin.read_text(encoding="utf-8"))
    _atomic_write_with_backup(dst_command, src_command.read_text(encoding="utf-8"))

    return {"plugin": dst_plugin, "command": dst_command}


def _atomic_write_with_backup(dst: Path, content: str) -> None:
    """幂等写入：内容相同跳过；不同先 backup 再覆盖（``dst.bak``）。"""
    if dst.exists():
        same: bool | None = None
        try:
            same = dst.read_text(encoding="utf-8") == content
        except OSError as e:
            # 读失败（权限 / 编码异常）→ log warn 后 passthrough（按「内容不同」处理）
            logger.warning("read %s 比对失败（按覆盖处理）：%s", dst, e)
            same = False
        if same:
            return   # 内容一致，不动
        # 内容不同 → backup
        bak = dst.with_suffix(dst.suffix + ".bak")
        try:
            dst.replace(bak)
        except OSError as e:
            logger.warning("backup %s → %s 失败：%s", dst, bak, e)
    tmp = dst.with_suffix(dst.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, dst)


@app.command()
def doctor(
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """入口链路自检（SPEC v8 §2.7，D-v8-2）。

    只报能自证的 3 项（B3 闭环）：
      1. **plugin 加载 + transform 触发**：doctor 能被调到 = plugin 已加载 +
         ``experimental.chat.messages.transform`` 已注册并触发（自证价值）。
      2. **marker 派发**：transform 正确 regex 解析 ``doctor`` 子命令并 spawn 到 CLI
         （doctor 在跑即证，§2.6.1）。
      3. **CLI 可达**：``orca in-session`` 版本 + 关键依赖（load_workflow / Tape /
         advance_step / marker）导入无误 + orca 版本。

    **不在 doctor 范围**（B3 自检盲区，§2.7）：
      - ``session.idle`` hook 真触发：静态跑 doctor 时无 workflow 在跑，idle 必然 N=0；
        报告仅标注「需跑 ``/orca run`` 验证」，不给 pass/fail。

    输出 JSON ``{ok, report, checks:[{name, pass, detail}]}``：plugin ``messages.transform``
    据 §2.6.2 提取 ``.report`` 替换 user 消息文本。

    **一次性消费保证**（§2.6.1）：``report`` 文本描述 marker 用反引号 `` `orca:cmd` ``，
    不写完整 marker（避免下一轮 transform 误命中）。
    """
    _setup_logging(log_level)

    checks: list[dict[str, Any]] = []

    # ① plugin 加载 + transform 触发：doctor 在跑 = 此链路通（自证，B3 闭环）。
    checks.append({
        "name": "plugin_load_and_transform_trigger",
        "pass": True,
        "detail": (
            "doctor 被 spawn 到 = opencode plugin 已加载 + "
            "`experimental.chat.messages.transform` 已注册并触发（自证）"
        ),
    })

    # ② marker 派发：regex 正确解析 `doctor` 子命令（doctor 在跑即证，§2.6.1）。
    # 用 Python 复跑 SPEC §2.6.1 regex 验证 `doctor` 字面能命中。
    # 一次性消费：detail 描述 marker 用反引号，不写完整 marker（§2.6.1）。
    import re
    from orca.iface.in_session.templates import MARKER_LITERAL, MARKER_REGEX

    pattern = re.compile(MARKER_REGEX)
    # 构造 sample 用于内测，但 detail 文本不直接 echo sample 字面（一次性消费守门）。
    sample_sub = "doctor"
    sample_full_marker = f"<!--orca:cmd {sample_sub}-->"
    match = pattern.match(sample_full_marker)
    checks.append({
        "name": "marker_dispatch",
        "pass": match is not None and match.group(1) == "doctor",
        "detail": (
            f"regex 命中 `orca:cmd {sample_sub}` marker（sub=doctor）"
            if match else
            "regex 未命中 `orca:cmd doctor` marker（异常）"
        ),
    })

    # ③ CLI 可达：关键依赖导入 + orca 版本。
    import_errors: list[str] = []
    try:
        from orca.compile import load_workflow as _wf  # noqa: F401
    except Exception as e:
        import_errors.append(f"load_workflow: {e}")
    try:
        from orca.events.tape import Tape as _Tape  # noqa: F401
    except Exception as e:
        import_errors.append(f"Tape: {e}")
    try:
        from orca.run.step import advance_step as _adv  # noqa: F401
    except Exception as e:
        import_errors.append(f"advance_step: {e}")
    try:
        from orca.iface.in_session import marker as _marker  # noqa: F401
    except Exception as e:
        import_errors.append(f"marker: {e}")

    try:
        import orca as _orca_pkg
        version = getattr(_orca_pkg, "__version__", "unknown")
    except Exception as e:
        version = f"unknown (import failed: {e})"

    cli_ok = not import_errors
    cli_detail = (
        f"orca v{version}; imports ok (load_workflow/Tape/advance_step/marker)"
        if cli_ok else
        f"orca v{version}; import errors: {'; '.join(import_errors)}"
    )
    checks.append({
        "name": "cli_imports_ok",
        "pass": cli_ok,
        "detail": cli_detail,
    })

    ok = all(c["pass"] for c in checks)

    # report 文本（SPEC §2.7）：3 项 + idle 标注。一次性消费：marker 用反引号描述。
    lines = ["Orca in-session 入口链路自检（v8 §2.7）", ""]
    for c in checks:
        mark = "PASS" if c["pass"] else "FAIL"
        lines.append(f"[{mark}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append(
        "注：`session.idle` hook 真触发 + 多 session 绑定（M3）不在 doctor 自检范围 —— "
        "需跑 `/orca run <wf>` 验证（静态跑 doctor 时 idle 必 N=0）。"
    )
    lines.append(
        "marker 入口：plugin 在每条 user 消息最后 text part 检测 "
        "`orca:cmd <sub> <args>` marker（行首/行尾锚定），命中则 spawn 对应 CLI 子命令。"
    )
    report = "\n".join(lines)

    typer.echo(json.dumps({
        "ok": ok,
        "report": report,
        "checks": checks,
    }, ensure_ascii=False))


@app.command()
def serve(
    yaml: Path = typer.Option(..., "--yaml", help="workflow YAML"),
    tape: Path = typer.Option(..., "--tape", help="tape 文件路径（daemon 独占）"),
    run_id: str = typer.Option(..., "--run-id", help="run id"),
    inputs: str = typer.Option("{}", "--inputs", help="workflow inputs（JSON）"),
    opencode_url: str = typer.Option(None, "--opencode-url", help="opencode serve 的 base_url（无头 CI 形态）"),
    session: str = typer.Option(None, "--session", help="opencode session id"),
    model: str = typer.Option("deepseek/deepseek-v4-flash", "--model", help="provider/model"),
    opencode_auth: str = typer.Option(None, "--opencode-auth", help='opencode serve basic auth "user:password"'),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """**无头 CI / 长跑批处理** daemon 入口（ADR I3.3a）。

    主 UX（交互 opencode / CC）不使用本命令——主 UX 走 ``bootstrap`` + ``next`` per-call
    CLI。本命令保留 v5 自驱动 SSE 循环，供无人值守跑长 workflow（CI / 批处理）使用。
    """
    _setup_logging(log_level)
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
            "serve 为无头 CI 形态，需 --opencode-url + --session；"
            "主 UX（交互 opencode / CC）请用 `orca in-session bootstrap/next`"
        )
