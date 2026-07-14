"""orca/iface/in_session/cli.py —— ``orca`` 顶层 CLI（in-session shell，SPEC v3 §2）。

**薄 CLI = 唯一大脑 + 唯一 tape 写者**（D-v7-1）。宿主（opencode plugin / CC hook
脚本 / 主 session 自调）是**哑传输**：spawn CLI + 读 stdout JSON 顶层字段。Orca 业务
逻辑（advance_step 决策 / marker RMW / 合规计数 / 失败 taxonomy）全在此模块，可单测。

v3 §2 接口定型（7 命令，删 ``in-session`` 子命令层）：
  - ``orca list`` —— catalog（name+description，与 ``teams list`` 同源）。
  - ``orca <wf> --inputs '{...}'`` —— bootstrap 语法糖：接 wf 名（或 yaml 路径）位置参数，
    返 entry prompt + 驱动协议。**重复 bootstrap 同 wf → fail loud**（§7.3 m12，防孤儿）。
  - ``orca next --run-id <id> [--output '<产出>']`` —— 推进一步（主 session 逐步自调）。
  - ``orca status [--run-id <id>]`` —— 读 tape replay_state 报进度（spec §2.1：``--run-id``
    形态与 ``next`` 统一；位置参数 ``[<run_id>]`` 兼容旧调用）。
  - ``orca stop <run_id>`` —— 清 marker + per-call flock emit ``workflow_cancelled``。
  - ``orca open [<run_id>]`` —— 打开 web 监控（默认当前活跃 run，复用 web attach）。
  - ``orca doctor`` —— 自检（skill 落点 + CLI imports；hook 心跳可选）。

marker v3 §7.2：只 ``{run_id, model, no_output_count}``；文件名固定 ``orca-<run_id>.json``，
``next``/``stop`` 用 ``marker_path(rundir, run_id)`` O(1) 定位（删扫描）。

**铁律 1**：跨进程 sanctioned 写者（per-call CLI 形态，I3.3b）；每次触发短命
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

from orca.compile import ConfigurationError, load_workflow
from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session.marker import (
    ActivationMarker,
    clear_marker,
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

# 诊断开关（2026-07-08）：doctor 探两钩子（transform 入口 / idle 推进）是否真 fire。
# 开关 = 环境变量 ORCA_DIAGNOSE=1；plugin（TS）读同 env，开启时写心跳文件（见下），
# doctor 读取作证。未设/0 = 关（plugin 零 I/O）。env 名与 orca.ts 的 DIAGNOSE 字面同步。
DIAGNOSE_ENV = "ORCA_DIAGNOSE"
# 心跳文件名（plugin 作用域，落在 rundir = runs/；与 orca.ts PROBE_*_REL 字面同步）。
PROBE_ENTRY_NAME = ".orca-probe-entry.json"
PROBE_ADVANCE_NAME = ".orca-probe-advance.json"
_PROBE_FRESH_SEC = 300  # 心跳新鲜阈值：5 分钟内算 fresh

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


def _drive_protocol(run_id: str) -> str:
    """model-driven advance 协议文本（补丁 2026-07-09，v3 §1.2）：附在每个节点 prompt 后。

    告诉主 session 模型如何**自己**推进 workflow——派子代理 → 调 ``orca next
    --output`` → 读返回 → 重复/停。取代 plugin idle 钩子的 REST 产出抽取（旧路径依赖
    opencode dev server REST，断链即 replay 死循环；见 plugin event 钩子注释）。
    **run_id 是唯一句柄**：tape 路径是 run_id 的纯函数（bootstrap 用 _default_tape_path 建），
    next 按 --run-id 自己定位 tape，模型只需传 run_id + 产出，不必背 tape 路径。
    合规计数器仍是兜底：模型连续 3 次不调 next/无 output → CLI emit workflow_failed（卡住可见）。

    v3 §8 step 1（B1 闭环）：命令从 ``orca in-session next`` 上移为 ``orca next``——
    与顶层接口定型同 commit 改全调用点（cli.py 驱动协议 / orca.ts spawn / command 模板）。
    """
    return (
        "\n【Orca 驱动协议】你（主 session）负责推进，不要等系统自动推进：\n"
        "1. 用 task 工具派一个子代理执行上面这个节点：**由子代理 Read 节点指令文件**并按要求"
        "做完；**你不许自己 Read 该文件**（会撑爆你的上下文）。\n"
        "2. 子代理返回后，把它的产出作为 --output，**原样**执行下面这条命令"
        "（--run-id 是唯一句柄，必须用下面给定的值；tape 由 Orca 自己按 run_id 定位，不要传）：\n"
        f"   orca next --run-id {run_id} --output '<子代理的产出>'\n"
        "3. 读上面命令的 stdout（一段 JSON）：\n"
        "   - 若 done 为 true → workflow 已结束，停止推进，向用户总结结果。\n"
        "   - 否则 JSON 里的 prompt 字段就是下一个节点的指令 → 回到第 1 步继续。\n"
        "要点：--output 用单引号包住整段产出；产出含撇号（it's / 影评）时用 `'\\''` 转义"
        "（it's → it'\\''s）；--run-id 必传；严格按 next 返回的 prompt 逐节点做。\n"
    )


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


# ── wf 名 / 路径解析（``orca <wf>`` 语法糖，§2.1 / §4.2）─────────────────────────


def _resolve_wf_path(wf_arg: str) -> Path:
    """``orca <wf>`` 的位置参数 → yaml 路径（SPEC §2.1）。

    接受两种形态：
      - **yaml 路径**（相对 / 绝对，文件存在）→ 直接用。
      - **wf 名**（非路径）→ ``catalog.find_workflow_yaml_path`` 精确反查（按 ``wf.name``
        字段，first-wins project-local）。未找到 → fail loud。

    B3 闭环：不做模糊匹配 / 不从用户意图抽 inputs（那归 skill，§4.2）。仅精确名 → 路径。
    """
    p = Path(wf_arg)
    if p.is_file():
        return p
    # 当 wf 名解析：延迟 import catalog（iface/mcp 子包，按依赖边界不顶层引入）。
    from orca.iface.mcp.catalog import find_workflow_yaml_path
    resolved = find_workflow_yaml_path(wf_arg)
    if resolved is None:
        raise typer.BadParameter(
            f"找不到 workflow：{wf_arg!r} 既不是存在的 yaml 文件，也不在 catalog"
            f"（./workflows + ~/.orca/workflows，按 wf.name 匹配）。"
            f"用 `orca list` 查可用 workflow。"
        )
    return Path(resolved)


def _read_workflow_name(tape_path: Path) -> str | None:
    """读 tape 首条 ``workflow_started.data.workflow_name``（m12 重复 bootstrap 检测用）。

    tape 不存在 / 无 workflow_started / 损坏 → None（调用方按「无信息」跳过，不崩）。
    扫前 ``_TAPE_HEAD_SCAN_LIMIT`` 行找不到 workflow_started 即放弃（workflow_started
    正常是首条事件；corrupt tape 截掉首行时不读整个大文件，review m5）。
    """
    if not tape_path.is_file():
        return None
    try:
        with open(tape_path, encoding="utf-8") as f:
            for _i, line in enumerate(f):
                if _i >= _TAPE_HEAD_SCAN_LIMIT:
                    return None
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if obj.get("type") == "workflow_started":
                    return obj.get("data", {}).get("workflow_name")
    except (json.JSONDecodeError, OSError):
        return None
    return None


# tape 头扫描行数上限（workflow_started 正常是首条；超此仍无即放弃，防读大文件）。
_TAPE_HEAD_SCAN_LIMIT = 100


def _find_active_run_for_wf(rundir: Path, wf_name: str) -> str | None:
    """SPEC §7.3（m12）：扫活跃 marker，返同 wf.name 的在途 run_id（无则 None）。

    marker 文件存在 ≡ run 活跃（终态时 clear_marker 清掉）。对每个活跃 marker，按其 run_id
    读对应 tape 的 workflow_started.workflow_name，与目标 wf_name 比。tape 读失败 → 跳过
    （不误判；crash 孤儿 marker 由 doctor 另行检测，§9#2）。

    marker 只 3 字段（无 wf 名），故必须读 tape 的 workflow_started——这是「tape 唯一真相源」
    铁律的体现：wf 归属信息从 tape 派生，不在 marker 复存（避免 desync，§7.2）。
    """
    if not rundir.exists():
        return None
    for mp in sorted(rundir.glob("orca-*.json")):
        marker = read_marker(mp)
        if marker is None:
            continue
        tape_path = Path(rundir) / f"{marker.run_id}.jsonl"
        if _read_workflow_name(tape_path) == wf_name:
            return marker.run_id
    return None


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


# ── Typer app（orca 顶层，§2.1 七命令）───────────────────────────────────────


class _OrcaTopLevelGroup(typer.core.TyperGroup):
    """``orca <wf-name>`` 语法糖（SPEC §2.1）：未注册的首 token 当 wf 名 → 重写为 bootstrap。

    保 ``orca list/next/status/stop/open/doctor``（注册命令）正常派发；``orca mywf --inputs ...``
    重写为 ``orca bootstrap mywf --inputs ...``。保留字黑名单（compile 期 §2.2）保证无 wf
    能取 ``status`` 等命令名 → 派发无歧义（ wf 取保留名在 load_workflow 期已 fail loud）。
    """

    def resolve_command(self, ctx, args):
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["bootstrap", *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    name="orca",
    help=(
        "Orca in-session shell —— 主 session（CC/opencode/NGA/CAC）驱动 workflow。"
        " 7 命令：list / <wf> / next / status / stop / open / doctor。"
    ),
    no_args_is_help=True,
    add_completion=False,
    cls=_OrcaTopLevelGroup,
    epilog=(
        "语法糖：`orca <wf-name>` ≡ `orca bootstrap <wf-name>`"
        "（wf 名取保留字如 list/next/status 会被 compile 拒）。"
        " 后端/headless 命令在另一入口（见 `teams --help`）。"
    ),
)


@app.command(hidden=True)
def bootstrap(
    wf: str = typer.Argument(..., help="workflow 名（catalog 精确匹配）或 yaml 路径"),
    inputs: str = typer.Option("{}", "--inputs", help="workflow inputs（JSON）"),
    model: str = typer.Option(
        "deepseek/deepseek-v4-flash", "--model", help="provider/model（记入 marker）",
    ),
    format: str = typer.Option(
        "json", "--format",
        help="输出格式：json（默认，机器读）/ prompt（command 入口用：只回 entry "
             "prompt 纯文本，让主 session 直接据其派子代理）",
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """起一个 in-session run 的首步（gen run_id + tape + marker + emit ws+ns → entry prompt）。

    v3 §7.3（m12）：同 wf 已有活跃 marker（未终态）→ **fail loud**（不静默新建孤儿）。
    提示续跑（`orca next --run-id <id>`）或先停（`orca stop <id>`）。
    """
    _setup_logging(log_level)
    yaml_path = _resolve_wf_path(wf)  # fail loud：名/路径都解析不到 → BadParameter
    wf_obj = load_workflow(yaml_path)  # fail loud：非法 yaml 抛 ConfigurationError
    try:
        inp = json.loads(inputs) if inputs else {}
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--inputs 不是合法 JSON：{e}") from e

    tape_path_probe = _default_tape_path("__probe__")
    rundir = tape_path_probe.parent

    # ── bootstrap serialize lock（well-known 路径，NOT per-run_id）──────────────
    # 关键：锁文件名必须独立于 run_id。``gen_run_id`` 每次返新值，若锁文件用
    # ``orca-<run_id>.json.flock``，两并发 bootstrap 同 wf 各 gen 不同 run_id → 各锁不同
    # 文件 → 互不阻塞 → 都过 dupe check → 两个孤儿 run（TOCTOU，review B1）。
    # 全局锁（单一 ``.orca-bootstrap.lock``）serialize 所有 bootstrap：同 wf 并发 →
    # 第二个等第一个写完 marker 再跑 dupe check → 看到 marker → fail loud。bootstrap
    # 是低频操作（每 run 一次），全局串行无性能影响。
    rundir.mkdir(parents=True, exist_ok=True)
    bootstrap_lock = rundir / ".orca-bootstrap.lock"
    mlock_fd = open(bootstrap_lock, "w")
    try:
        fcntl.flock(mlock_fd.fileno(), fcntl.LOCK_EX)

        # dupe check（§7.3 m12）：扫活跃 marker + 读 tape workflow_name。
        # 注：按 ``wf.name`` 匹配（SPEC §7.3 字面是 yaml realpath，但 marker 只 3 字段不存
        # yaml；wf.name 经 compile 保唯一，是 realpath 的合理代理——两不同 yaml 同名 wf
        # 视为同一 wf，符合「同 wf 不重复 bootstrap」语义）。
        existing = _find_active_run_for_wf(rundir, wf_obj.name)
        if existing is not None:
            typer.echo(json.dumps({
                "done": True,
                "reason": "duplicate-active-run",
                "run_id": existing,
                "hint": (
                    f"已有 run `{existing}` 在跑本 wf（{wf_obj.name}）。"
                    f"用 `orca next --run-id {existing}` 续跑，"
                    f"或 `orca stop {existing}` 后再建。"
                ),
            }, ensure_ascii=False))
            raise typer.Exit(1)

        # gen run_id 在锁内（保两并发 bootstrap 同 wf 不各 gen 不同 id）。
        run_id = gen_run_id(wf_obj.name)
        tape_path = _default_tape_path(run_id)
        mpath = marker_path(rundir, run_id)

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
            result = asyncio.run(_advance_and_emit(
                bus, wf_obj, tape, output=None, inputs=inp, run_id=run_id, elapsed=0.0,
                prompts_dir=_prompts_dir_for(tape_path, run_id),
                yaml_path=os.path.realpath(yaml_path),
            ))
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
                run_id=run_id, model=model, no_output_count=0,
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
        # model-driven advance 补丁：entry prompt 后附「驱动协议」，模型据此自调 next --output。
        if prompt_text:
            reply["prompt"] = prompt_text + _drive_protocol(run_id)
        if result.prompt_file:
            reply["prompt_file"] = result.prompt_file

        # command 入口用 ``--format prompt`` —— 回 entry prompt 纯文本（pointer）
        # **+ 驱动协议**（model-driven advance：模型据此自调 next --output）。
        # 兜底：无 prompt（如非 agent entry / 异常）→ 仍回 JSON 让模型看到结构化信息。
        if format == "prompt" and prompt_text:
            typer.echo(prompt_text + _drive_protocol(run_id))
        else:
            typer.echo(json.dumps(reply, ensure_ascii=False))
    finally:
        try:
            fcntl.flock(mlock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            mlock_fd.close()


@app.command()
def next(
    tape: Path = typer.Option(None, "--tape", help="tape 文件路径（可选；省略则按 --run-id 派生默认路径 runs/<run_id>.jsonl）"),
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

    # --tape 可选（model-driven advance 补丁）：省略时 tape 路径是 run_id 的纯函数
    # （bootstrap 用 _default_tape_path 建 tape），故 run_id 即可定位，模型不必背 tape 路径。
    tape_path = Path(tape) if tape else _default_tape_path(run_id)

    # per-call flock（LOCK_NB → busy 0 退出，F5）。
    acquired = _try_acquire_flock(tape_path)
    if acquired is None:
        typer.echo(json.dumps({"done": False, "reason": "busy"}))
        return
    fd, _ = acquired

    # 定位激活 marker（v3 §7.2：文件名固定 orca-<run_id>.json，O(1) 直定位，删扫描）。
    rundir = tape_path.parent
    mpath = marker_path(rundir, run_id)

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
    # model-driven advance 补丁：每个 next 返回的 prompt 也附「驱动协议」，让模型继续自驱。
    if prompt_text:
        reply["prompt"] = prompt_text + _drive_protocol(run_id)
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
    yaml_path: str | None = None,
):
    """调 advance_step + emit_batch（单次 write 原子化，B1）。"""
    result = advance_step(
        tape, wf, output=output, inputs=inputs, run_id=run_id, elapsed=elapsed,
        prompts_dir=prompts_dir, yaml_path=yaml_path,
    )
    if result.emits:
        await bus.emit_batch(_emits_to_event_datas(result.emits))
    return result


def _read_workflow_yaml_path(tape_path: Path) -> str | None:
    """读 tape 首条 ``workflow_started.data.yaml_path``（v3 §7.2：yaml 从 tape 派生）。

    无 yaml_path（老 tape / daemon 未传）→ None（调用方 fallback 到 catalog 名查）。
    """
    if not tape_path.is_file():
        return None
    try:
        with open(tape_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                if obj.get("type") == "workflow_started":
                    yp = obj.get("data", {}).get("yaml_path")
                    if yp:
                        return str(yp)
                    return None
    except (json.JSONDecodeError, OSError):
        return None
    return None


def _load_wf_for_run(run_id: str, tape: Tape) -> "Workflow":
    """从 tape 反查 wf（v3 §7.2：marker 不存 yaml，运行时从 tape 唯一真相源派生）。

    优先读 ``workflow_started.data.yaml_path``（bootstrap 期记入，O(1) 精确定位）；
    无 yaml_path（老 tape / daemon）→ fallback 按 ``workflow_name`` 查 catalog。
    失败（无 workflow_started / yaml 坏 / catalog 找不到）→ raise InSessionError(state_corrupt)。
    """
    tape_path = Path(getattr(tape, "path", "") or "")
    yp = _read_workflow_yaml_path(tape_path)
    if yp:
        try:
            return load_workflow(yp)
        except (ConfigurationError, FileNotFoundError) as e:
            raise InSessionError(
                f"run {run_id} 的 tape 指向 yaml {yp!r} 但加载失败：{e}",
                error_kind="state_corrupt",
            ) from e
    # fallback：按名查 catalog（兼容老 tape / daemon 形态）。
    from orca.iface.mcp.catalog import find_workflow
    wname = _read_workflow_name(tape_path)
    if wname is None:
        raise InSessionError(
            f"run {run_id} 的 tape 无 workflow_started，无法定位 workflow",
            error_kind="state_corrupt",
        )
    found = find_workflow(wname)
    if found is None:
        raise InSessionError(
            f"workflow {wname!r}（run {run_id}）在 tape 无 yaml_path 且 catalog 找不到"
            f"（./workflows + ~/.orca/workflows）",
            error_kind="state_corrupt",
        )
    wf_obj, _yaml = found
    return wf_obj


async def _next_in_critical_section(
    bus: EventBus, tape: Tape, run_id: str, output: str | None,
    inputs: dict, start_ts: float, mpath: Path,
    prompts_dir: Path | None = None,
):
    """flock 临界区内的 next 主体：advance + emit_batch + marker RMW（N2）+ 合规计数 (F11)。

    返回 ``(result, compliance_failed)``。``compliance_failed=True`` 时已 emit
    workflow_failed，调用方据 ``done=True`` 停注入。
    """
    from orca.run.step import StepResult
    # marker 缺 → 调用方未 bootstrap 或 marker 已清；幂等吞 + warn（不 raise）。
    marker = read_marker(mpath)
    if marker is None:
        logger.warning("next 找不到 %s 的激活 marker，无法推进（需先 bootstrap）", run_id)
        return StepResult(done=False, reason="no-marker"), False

    # wf 从 tape 反查（v3 §7.2：marker 不存 yaml）。
    wf = _load_wf_for_run(run_id, tape)
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
    run_id: str = typer.Argument(
        None, help="run id（位置参数，省略则列 runs/ 下全部 run tape；与 --run-id 二选一）",
    ),
    run_id_opt: str = typer.Option(
        None, "--run-id",
        help="run id（spec §2.1 与 next 统一的 --run-id 形态；与位置参数二选一）",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="输出 JSON（plugin messages.transform 入口用，SPEC §2.6.2 改写契约）",
    ),
) -> None:
    """查看 in-session run 的 workflow 进度（读 tape replay_state）。

    ``run_id`` 两种传法都接受（spec §2.1 统一 ``--run-id`` 形态，位置参数 ``[<run_id>]``
    保留兼容旧调用 / 主 session 既有测试）：``orca status --run-id <id>`` 或 ``orca status <id>``。
    两者同传且值不一致 → fail loud（BadParameter）。均省略 → 列全部活跃 run。
    """
    from orca.events.replay import replay_state

    # --run-id 与位置参数二选一（同传且不同值 → fail loud；同传同值 → 视作一次）。
    if run_id is not None and run_id_opt is not None and run_id != run_id_opt:
        raise typer.BadParameter(
            f"位置参数 run_id={run_id!r} 与 --run-id={run_id_opt!r} 不一致，请二选一"
        )
    rid = run_id if run_id is not None else run_id_opt

    if rid is None:
        runs_dir = Path("runs")
        if not runs_dir.exists():
            out = {"ok": False, "reason": "no runs/ directory"}
            typer.echo(json.dumps(out, ensure_ascii=False) if json_output else "(无 runs/ 目录)")
            return
        tapes = sorted(runs_dir.glob("*.jsonl"))
        if not tapes:
            out = {"ok": False, "reason": "no run tapes"}
            typer.echo(json.dumps(out, ensure_ascii=False) if json_output else "(无 run tape)")
            return
        names = [tp.stem for tp in tapes]
        if json_output:
            typer.echo(json.dumps({"runs": names}, ensure_ascii=False))
            return
        for tp in tapes:
            typer.echo(f"- {tp.stem}")
        typer.echo("\n用 `orca status --run-id <run_id>` 看详情。")
        return

    tape_path = _default_tape_path(rid)
    if not tape_path.exists():
        typer.echo(typer.style(f"run {rid!r} 无 tape", fg=typer.colors.RED))
        raise typer.Exit(1)
    state = replay_state(Tape(tape_path, run_id=rid))
    done = sum(1 for s in state.node_status.values() if s == "done")
    progress = f"{done}/{len(state.node_status)}"

    if json_output:
        # SPEC §2.6.2 plugin 改写契约：顶层字段供 plugin rewriteText 提取。
        typer.echo(json.dumps({
            "run_id": rid,
            "status": state.status,
            "current_node": state.current_node,
            "node_status": dict(state.node_status),
            "progress": progress,
        }, ensure_ascii=False))
        return

    typer.echo(f"run {rid}")
    typer.echo(f"  status:      {state.status}")
    typer.echo(f"  current_node: {state.current_node}")
    typer.echo(f"  node_status: {dict(state.node_status)}")
    typer.echo(f"  progress:    {progress} done")


@app.command()
def stop(
    run_id: str = typer.Argument(..., help="要停的 run id"),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """停一个 run：清激活 marker + per-call flock emit ``workflow_cancelled``。"""
    _setup_logging(log_level)

    tape_path = _default_tape_path(run_id)
    rundir = tape_path.parent
    mpath = marker_path(rundir, run_id)

    if not tape_path.exists():
        # 无 tape：仅清 marker（stop 幂等，允许「run 已清理但 marker 残留」）。
        clear_marker(mpath)
        typer.echo(json.dumps({"run_id": run_id, "ok": True, "done": True, "note": "no-tape"}))
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
        # 嵌套 try/finally：bus.close() 异常不得跳过 clear_marker（否则孤儿 marker
        # 让 dupe-check / open 误判此 run 仍活跃，review M4）。
        try:
            _release_flock(fd)
        finally:
            try:
                bus.close()
            finally:
                clear_marker(mpath)
    typer.echo(json.dumps({"run_id": run_id, "ok": True, "done": True}))


def _scan_skill_install(
    *, home: Path | None = None, cwd: Path | None = None,
) -> dict[str, bool]:
    """扫四前端 skill 目录，返 ``{platform: 是否装了 orca skill}``（v5 §4.3 / A6）。

    每个平台查 user-scope + project-scope 两个可能落点（``<root>/skills/orca/SKILL.md``），
    任一存在即该平台算已装。doctor 的 ``skill_install`` 检查据此 pass/fail。

    ``home`` / ``cwd`` 可注入（对齐 ``resolve_roots`` 模式）——单测隔离真实 ``~/.claude``
    等，否则装过 orca 的开发机上 ``fail_when_absent`` 测试会反向失败（review 🔴#2）。
    """
    # 延迟 import：skill_cmds 属 iface.cli 子包，避免顶层循环；HOST_DOTDIR 单一真相源。
    from orca.iface.cli.skill_cmds import HOST_DOTDIR, SKILL_HOSTS, opencode_global_root

    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()
    # 各平台的候选根（user-scope + project-scope）。opencode user-scope 走全局 config 根。
    candidates: dict[str, list[Path]] = {}
    for host in SKILL_HOSTS:
        if host == "opencode":
            user_root = opencode_global_root(home)
            proj_root = cwd / HOST_DOTDIR[host]
        else:
            user_root = home / HOST_DOTDIR[host]
            proj_root = cwd / HOST_DOTDIR[host]
        candidates[host] = [user_root, proj_root]
    return {
        platform: any((root / "skills" / "orca" / "SKILL.md").is_file() for root in roots)
        for platform, roots in candidates.items()
    }


@app.command()
def doctor(
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """诊断 in-session 集成层（v5 §2.1 / §4.4：skill 落点 + CLI imports 为准；hook 心跳可选）。

    v5：B 路径（主 session 自调 next）不依赖 hook 推进；``orca.ts`` transform 派发已禁用
    （step 2b）。doctor 主验两件**硬**事——① ``skill_install``（四前端是否装了 orca skill）
    + ② ``cli_imports_ok``（CLI 后端可达）。旧两钩子（transform / idle）的心跳退居**可选**
    诊断（``ORCA_DIAGNOSE=1`` 时 plugin 写心跳，doctor 读取作证），**不计入 ok**——
    hook 不再推进，缺心跳不是故障。
    """
    _setup_logging(log_level)

    diag_on = os.environ.get(DIAGNOSE_ENV) == "1"
    rundir = _default_rundir()

    def _read_probe(name: str) -> dict | None:
        p = rundir / name
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    entry = _read_probe(PROBE_ENTRY_NAME)
    advance = _read_probe(PROBE_ADVANCE_NAME)
    now = int(time.time())

    checks: list[dict[str, Any]] = []

    # 每条 check 带 ``hard``：True = 计入 ok（skill_install / cli_imports_ok）；
    # False = 可选诊断（diag/hook），不计数。避免硬编码 name tuple（typo 静默丢失硬检查）。
    # ① skill_install（A6，硬）：四前端是否装了 orca skill。
    installed = _scan_skill_install()
    where = [p for p, ok_flag in installed.items() if ok_flag]
    checks.append({
        "name": "skill_install",
        "hard": True,
        "status": "pass" if where else "fail",
        "detail": (
            f"PASS：orca skill 已装于 {', '.join(where)}。" if where
            else "FAIL：四前端（cc/opencode/cac/nga，user+project scope）均未找到 orca skill。"
                 "跑 `teams install --target <platform>` 安装后重启前端。"
        ),
    })

    # ② cli_imports_ok（硬）：CLI 后端可达。
    import_errors: list[str] = []
    for mod in ("orca.compile", "orca.events.tape", "orca.run.step",
                "orca.iface.in_session.marker"):
        try:
            __import__(mod)
        except Exception as e:  # noqa: BLE001
            import_errors.append(f"{mod}: {e}")
    try:
        import orca as _orca_pkg
        version = getattr(_orca_pkg, "__version__", "unknown")
    except Exception as e:  # noqa: BLE001
        version = f"unknown (import failed: {e})"
    cli_ok = not import_errors
    checks.append({
        "name": "cli_imports_ok",
        "hard": True,
        "status": "pass" if cli_ok else "fail",
        "detail": (
            f"orca v{version}; imports ok (compile/tape/step/marker)"
            if cli_ok else
            f"orca v{version}; import errors: {'; '.join(import_errors)}"
        ),
    })

    # ③ diag_switch（可选）：开关状态 + 切换方法。不计入 ok。
    checks.append({
        "name": "diag_switch",
        "hard": False,
        "status": "pass" if diag_on else "unknown",
        "detail": (
            f"ORCA_DIAGNOSE={'1 (诊断开，钩子写心跳)' if diag_on else '未设/0 (诊断关，零 I/O)'}。"
            " 切换：export ORCA_DIAGNOSE=1 后重启 opencode（plugin 加载时读 env）。"
            "（v5：hook 已退居可选诊断，不推进。）"
        ),
    })

    # ④ entry_hook（transform，可选）：v5 transform 派发已禁用（step 2b），心跳仅证明 plugin
    #   加载/被调——非推进依赖，故只报 pass/unknown，**不 fail**。dispatch_count 在派发禁用后
    #   生产恒为 0，不再展示（避免「累计 0 次」误导）。
    if not diag_on:
        e_status, e_detail = "unknown", (
            "诊断关，无心跳。B 路径（主 session 自调 next）不依赖 transform；"
            "若需验 plugin 加载，设 ORCA_DIAGNOSE=1 并在 session 内对话几句。"
        )
    elif entry is None:
        e_status, e_detail = "unknown", (
            "UNKNOWN：诊断开但无 transform 心跳（plugin 未加载或未触发）。"
            "v5 transform 派发已禁用，这不影响推进（入口在 orca skill）。"
        )
    else:
        age = now - int(entry.get("last_called_at", 0))
        e_status = "pass" if age <= _PROBE_FRESH_SEC else "unknown"
        tag = "PASS" if e_status == "pass" else "STALE"
        e_detail = f"{tag}：transform 钩子被调（{age}s 前）= plugin 已加载。dispatch 已禁用，仅诊断。"
    checks.append({"name": "entry_hook", "hard": False, "status": e_status, "detail": e_detail})

    # ⑤ advance_hook（idle，可选）：nudge 载体（step 2b(7)），不 fail。
    if not diag_on:
        a_status, a_detail = "unknown", (
            "诊断关，无心跳。B 路径不依赖 idle 推进（主 session 自调 next）。"
        )
    elif advance is None:
        a_status, a_detail = "unknown", (
            "UNKNOWN：诊断开但未观察到 session.idle。B 路径不依赖它；idle 仅 nudge 用。"
        )
    else:
        age = now - int(advance.get("last_idle_at", 0))
        idle_n = int(advance.get("idle_count", 0))
        a_status = "pass" if age <= _PROBE_FRESH_SEC else "unknown"
        tag = "PASS" if a_status == "pass" else "STALE"
        a_detail = f"{tag}：idle fire 过 {idle_n} 次（{age}s 前）。仅 nudge，非推进依赖。"
    checks.append({"name": "advance_hook", "hard": False, "status": a_status, "detail": a_detail})

    # ok = 仅 ``hard=True`` 的检查无 fail。可选检查（diag/hook）不计数。
    ok = all(c["status"] != "fail" for c in checks if c.get("hard"))

    lines = ["Orca in-session 诊断（v5：B 路径，skill 驱动入口）", ""]
    for c in checks:
        lines.append(f"[{c['status'].upper()}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("硬检查（skill_install + cli_imports_ok）决定 ok；其余为可选诊断。")
    lines.append("v5 §1 执行模型：主 session 经 orca skill 自调 `orca next` 推进，不依赖 hook。")
    lines.append("")
    lines.append("心跳文件（仅 ORCA_DIAGNOSE=1 时 plugin 写，可选）：")
    lines.append(f"  {rundir / PROBE_ENTRY_NAME}")
    lines.append(f"  {rundir / PROBE_ADVANCE_NAME}")
    report = "\n".join(lines)

    typer.echo(json.dumps({
        "ok": ok,
        "diag": diag_on,
        "report": report,
        "checks": checks,
    }, ensure_ascii=False))


@app.command(name="list")
def list_workflows() -> None:
    """列出可用 workflow（SPEC v5 §2.3，给 skill/LLM 选 wf + 抽 inputs 用）。

    返 ``{workflows:[{name, description, inputs_schema:[{name,type,description}]}]}``——
    一个命令给齐「选 wf（据 description）+ 知 inputs（据 inputs_schema）」，故无 describe
    命令（v5 决策：冗余）。**无 has_setup**（B3）。

    **单一 catalog 真相源**（coordinator 铁律）：本命令与 ``teams list``（commands.run_list，
    人类可读，给运维）**调同一个** ``catalog.list_workflows()``——catalog 是唯一实现，
    渲染层按消费者不同（skill 要 JSON / 运营要文本），非两套 list 逻辑。
    """
    from orca.iface.mcp.catalog import list_workflows as _catalog_list

    items = _catalog_list()
    # 恰取 skill/LLM 需要的 3 字段（name/description/inputs_schema）；catalog item 的其余
    # 字段（has_setup/entry/inputs_count）留给 MCP/teams 渲染，不暴露给 orca list（B3）。
    workflows = [
        {
            "name": it["name"],
            "description": it["description"],
            "inputs_schema": it["inputs_schema"],
        }
        for it in items
    ]
    typer.echo(json.dumps({"workflows": workflows}, ensure_ascii=False))


@app.command(name="open")
def open_run(
    run_id: str = typer.Argument(
        None, help="要打开的 run_id（省略则用当前唯一活跃 run；多个活跃时需显式指定）",
    ),
    tape: Path | None = typer.Option(
        None, "--tape",
        help="显式 tape 路径（默认 ``runs/<run_id>.jsonl``）",
    ),
    host: str | None = typer.Option(
        None, "--host",
        help="web server 监听地址（默认 0.0.0.0 远程可访问；ORCA_WEB_HOST env 同效）",
    ),
    port: int | None = typer.Option(
        None, "--port",
        help="web server 端口（默认探测 7428：是 orca 则复用，否则起后台 serve）",
    ),
) -> None:
    """打开 web 监控面板（SPEC §2.1，复用 web attach）。

    ``run_id`` 省略时自动取**当前唯一活跃 run**（扫活跃 marker）；多个活跃 → fail loud
    提示指定 run_id；无活跃 → fail loud 提示先 ``orca <wf>``。复用 ``teams open`` 同款
    ``_open_run``（read-only attach + tail-follow）。
    """
    if run_id is None:
        run_id = _default_active_run_id()
    raise typer.Exit(_open_run_inproc(run_id, tape_path=tape, host=host, port=port))


def _default_active_run_id() -> str:
    """无 run_id 时取当前唯一活跃 run（扫 marker）。多个/无 → fail loud。"""
    rundir = _default_rundir()
    if not rundir.exists():
        typer.echo("无活跃 run（先 `orca <wf>` 启动）", err=True)
        raise typer.Exit(1)
    actives = [read_marker(mp) for mp in sorted(rundir.glob("orca-*.json"))]
    actives = [m for m in actives if m is not None]
    if not actives:
        typer.echo("无活跃 run（先 `orca <wf>` 启动）", err=True)
        raise typer.Exit(1)
    if len(actives) > 1:
        typer.echo(
            "多个活跃 run，请用 `orca open <run_id>` 指定："
            + ", ".join(m.run_id for m in actives),
            err=True,
        )
        raise typer.Exit(1)
    return actives[0].run_id


def _open_run_inproc(
    run_id: str, *, tape_path: Path | None, host: str | None, port: int | None
) -> int:
    """复用 ``teams open`` 的 ``_open_run``（read-only attach + browser）。延迟 import 防循环。"""
    from orca.iface.cli.commands import _open_run

    return _open_run(run_id, tape_path=tape_path, host=host, port=port)


def main() -> None:
    """console_scripts 入口（pyproject ``[project.scripts] orca``）。

    函数内 import（保模块导入零副作用，对齐 commands.py 的 textual 延迟 import 纪律）：
    把 ~/.orca/config.json 的 binary override 注入对应 env var，之后所有 orca run 生效。
    """
    from orca.iface.cli.config import bootstrap_config

    bootstrap_config()
    app()


if __name__ == "__main__":
    main()
