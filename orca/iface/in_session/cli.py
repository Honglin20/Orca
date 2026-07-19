"""orca/iface/in_session/cli.py —— ``orca`` 顶层 CLI（in-session shell，SPEC v3 §2）。

**薄 CLI = 唯一大脑 + 唯一 tape 写者**（D-v7-1）。宿主（opencode plugin / CC hook
脚本 / 主 session 自调）是**哑传输**：spawn CLI + 读 stdout JSON 顶层字段。Orca 业务
逻辑（advance_step 决策 / marker RMW / 合规计数 / 失败 taxonomy）全在此模块，可单测。

v3 §2 接口定型（7 命令，删 ``in-session`` 子命令层）：
  - ``orca list`` —— catalog（name+description，与 ``tars list`` 同源）。
  - ``orca <wf> --inputs '{...}'`` —— bootstrap 语法糖：接 wf 名（或 yaml 路径）位置参数，
    返 entry prompt + 驱动协议。**重复 bootstrap 同 wf → fail loud**（§7.3 m12，防孤儿）。
  - ``orca next --run-id <id> [--output '<产出>']`` —— 推进一步（主 session 逐步自调）。
  - ``orca status [--run-id <id>]`` —— 读 tape replay_state 报进度（spec §2.1：``--run-id``
    形态与 ``next`` 统一；位置参数 ``[<run_id>]`` 兼容旧调用）。
  - ``orca stop [--run-id <id>]`` —— 清 marker + per-call flock emit ``workflow_cancelled``
    （spec §2.1：``--run-id`` 形态；位置参数 ``<run_id>`` 兼容旧调用；至少指定一个）。
  - ``orca open [--run-id <id>]`` —— 打开 web 监控（默认当前活跃 run，复用 web attach）。
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
import shlex
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import typer

from orca.chart._paths import chart_sock_path
from orca.compile import ConfigurationError, catalog, load_workflow
from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.iface.in_session._step_io import (
    _emit_workflow_failed,
    apply_step_result,
    fail_in_session,
)
from orca.iface.in_session.marker import (
    ActivationMarker,
    clear_marker,
    marker_path,
    read_marker,
    write_marker,
)
from orca.run.lifecycle import (
    gen_run_id,
    now_monotonic,
)
from orca.run._errors import INPUTS_VALIDATION_ERROR
from orca.run.step import InSessionError, advance_step

logger = logging.getLogger(__name__)

# subagent 合规超限阈值（D-v7-6：连续 N 次 next 无 output → workflow_failed）。
_COMPLIANCE_LIMIT = 3

# SPEC §3 O4：busy 信封固定 retry_after_ms（毫秒）。撞 tape flock 时让主 session 等待
# 重试**同一 next 命令本身**（不重派子代理 / 不重发 prompt —— 避免 advance_step 不持锁
# 调用契约冲突）。500ms 是保守上界：flock 通常 <100ms 释放（CLI 短命 open/emit/close），
# 500ms 避免紧密轮询撞锁 + 给 tape 终态 emit 留余量。
_BUSY_RETRY_AFTER_MS = 500

# 诊断开关（2026-07-08）：doctor 探两钩子（transform 入口 / idle 推进）是否真 fire。
# 开关 = 环境变量 ORCA_DIAGNOSE=1；plugin（TS）读同 env，开启时写心跳文件（见下），
# doctor 读取作证。未设/0 = 关（plugin 零 I/O）。env 名与 orca.ts 的 DIAGNOSE 字面同步。
DIAGNOSE_ENV = "ORCA_DIAGNOSE"
# 心跳文件名（plugin 作用域，落在 rundir = runs/；与 orca.ts PROBE_*_REL 字面同步）。
# FU-2：``PROBE_ENTRY_NAME`` 已删——transform 派发 step 4 整删后入口心跳永不再写，dead。
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


def _host_session_from_env() -> str | None:
    """宿主 session id（host-session-binding v2 §4.2）：优先级 ORCA_HOST_SESSION_ID
    > CLAUDE_CODE_SESSION_ID > None。

    - **CC**：零配置（CC 给所有 bash 子进程注入 ``CLAUDE_CODE_SESSION_ID``，spike 实测）。
    - **opencode**：需 plugin ``shell.env`` 钩子注入 ``ORCA_HOST_SESSION_ID``（v2 §4.5）；
      未注入 → 返 None（fail-safe：该 run 的 host_session 落 tape 为 null，nudge 跳过）。

    单一真相源铁律：host_session 单路采集（env → bootstrap → tape），marker 不复存（§2.2）。
    """
    return os.environ.get("ORCA_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")


def _run_dir_for(tape_path: Path, run_id: str) -> Path:
    """per-run 资源根目录：``<rundir>/<run_id>/``（prompts / orca_env.sh 都落此）。"""
    return Path(tape_path).parent / run_id


def _env_file_path(tape_path: Path, run_id: str) -> Path:
    """``runs/<run_id>/orca_env.sh`` —— per-run env 文件，子代理 ``source`` 后获 ORCA_* 身份。

    in-session 路径下节点子代理由宿主 session 派发、不经 ClaudeExecutor，env 没人注入；本文件
    用字面值（按 run_id / 当前 node / 当前 session_id / sock / resources_root 算好）替代 executor
    的 spawn overlay。子代理只需 ``source`` 一行（字面），不亲自 typing 任何 ORCA_* 值
    （单调信息流：run_id → 派生值，agent 无法伪造或指向别 run）。
    """
    return _run_dir_for(tape_path, run_id) / "orca_env.sh"


def _build_pointer(result: Any, *, env_file: Path | None = None) -> str:
    """把 StepResult(prompt_file, resources_root) 拼成 host-facing 指针文本。

    主 session 收到这句即知：派 task 子代理、读哪个文件、可选资源目录。子代理读文件执行，
    其输出即本节点输出（仍经 plugin 的 ToolPart.state.output 提取 → next --output）。

    ``env_file`` 非空（in-session 生产路径恒传）→ 追加一行 ``source`` 指针：子代理照抄这一行
    （字面 source，非 typing 值）后，shell 的 viz/训练脚本调 ``render_chart`` 时 env 齐备 → 连
    **自己 run 的** socket → 守护写 tape；``$ORCA_AGENT_RESOURCES`` 也可被 folder-agent 引用。
    """
    lines = [
        "【Orca 节点执行】请用 task 工具派一个子代理执行本节点，不要自己直接回答。",
        f"完整节点指令已写入：{result.prompt_file}",
        "请子代理先 Read 该文件，按其要求执行；子代理的输出即本节点的输出。",
    ]
    if result.resources_root:
        lines.append(f"附资源目录（脚本/参考，按需 Read）：{result.resources_root}")
    if env_file is not None:
        lines.append(
            f"运行任何脚本前先 `source {env_file}`（注入 ORCA_* 身份 + agent 资源路径，"
            "子代理不要自己 export 这些变量）。"
        )
    return "\n".join(lines) + "\n"


def _reply_prompt(result: Any, *, env_file: Path | None = None) -> str | None:
    """compact：``prompt_file`` 给定 → 返指针；否则 inline 回退全量 ``prompt``。

    inline 回退仅在 advance_step 未传 prompts_dir 时出现（daemon 形态 / 直调单测）；
    生产路径（bootstrap/next）恒传 prompts_dir → 恒走指针。

    ``env_file`` 透传 ``_build_pointer``（in-session 生产路径给值，附 ``source`` 行）。
    """
    if result.prompt_file:
        return _build_pointer(result, env_file=env_file)
    return result.prompt


# ── in-session chart ingestor 守护 + env 文件（phase-13 §3 in-session 衔接）─────────
#
# 为什么这一层在 in_session：web/tars-run 路径下 ClaudeExecutor spawn 时一次性注入 env
# + RunHandle 起 ingestor（同进程）；in-session 路径下子代理由宿主 session 派发，不经 executor
# → env 没人注入 + ingestor 没人起。本层补缺口：bootstrap detach 起守护进程，next 写 per-node
# env 文件。子代理 source env 文件后调 render_chart → socket → 守护 emit custom(chart) → tape。
# 详细见 ``orca/iface/in_session/chart_daemon.py`` 模块 docstring。

# bootstrap 等 socket bind 就绪的 poll 超时（脱离 bootstrap CLI 前给守护 bind 的时间）。
# 5s 覆盖 python 解释器冷启 + 或 orca 包 import（含 pydantic schema 等）+ start_unix_server；
# 高负载机器（CI 并发 / 重 fixtures）下 import 可达 2-3s。超时仅 warning（不 fail bootstrap），
# 因为 host 派 subagent 还要数十秒，守护很可能在那期间就绪。
_SOCK_READY_TIMEOUT = 5.0
_SOCK_READY_POLL = 0.05


def _spawn_chart_daemon(run_id: str, tape_path: Path) -> None:
    """detach 起 in-session chart ingestor 守护进程（phase-13 §3 in-session 衔接）。

    用 ``start_new_session=True`` 让守护脱离 bootstrap CLI 的 process group / controlling
    terminal —— bootstrap 是一次性 CLI，退出后守护继续（节点执行发生在宿主 session 时间窗）。
    参考 ``orca/exec/runner.py`` 的进程组隔离模式（同 ``spawn_kwargs_for_process_group`` 理念）。

    日志：守护 stdout/stderr 落 ``<rundir>/<run_id>/chart_daemon.log`` 便于诊断（守护脱离
    bootstrap 后无 tty，必须重定向；DEVNULL 会丢排查信息）。

    不等待：``Popen`` 返回即视为派发完成；socket bind 就绪由 ``_wait_for_sock`` 兜底。
    POSIX-only：项目已 fcntl.flock 前提 POSIX（CLAUDE.md / ADR I3.3）。
    """
    run_dir = _run_dir_for(tape_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "chart_daemon.log"

    cmd = [
        sys.executable, "-m", "orca.iface.in_session.chart_daemon",
        "--run-id", run_id,
        "--tape", str(tape_path),
    ]
    # log_fd 在 Popen 后由 child dup；parent 即刻 close 自己的 fd（不持有）。
    # ``close_fds=True`` 防 bootstrap 其它 fd（如 flock fd）泄漏进守护。
    log_fd = open(log_path, "a", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fd.close()
    logger.info("run %s: chart 守护已 detach spawn（log=%s）", run_id, log_path)


def _wait_for_sock(sock_path: Path, *, timeout: float = _SOCK_READY_TIMEOUT) -> bool:
    """poll **connect 探** 等守护 bind + listen 就绪；超时返 False（调用方仅 warn 不 fail）。

    用 connect 探（``_chart_daemon_alive``）而非 ``exists()``：socket 文件存在 ≠ 有监听者。
    SIGKILL 残留的 stale socket 文件会让 ``exists()`` 假阳性（误判 ready、实际无 listener）
    → 在 respawn 路径上 subagent 紧接着连会 ``ConnectionRefused``。connect 成功才真有监听者。

    ``chart_ingestor`` 的 ``asyncio.start_unix_server`` bind + listen 后 connect 才会成功
    （文件出现早于 listen 就绪；connect 是更强的就绪判定）。connect 的副作用 = 零（连上即 close，
    handler 读 EOF 静默；见 ``_chart_daemon_alive``）。bootstrap 首启路径同样适用：bind 前文件
    不存在 → connect FileNotFoundError → 继续轮询；bind 后 connect 成功 → 返 True。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _chart_daemon_alive(sock_path):
            return True
        time.sleep(_SOCK_READY_POLL)
    return False


# ``_chart_daemon_alive`` connect 探测超时。守护同机 Unix socket，正常 <10ms；500ms 仅
# 是高负载下的保守上界。超时（活但 event loop 阻塞 >500ms，如大 tape 首次扫 / GC）→ 保守视
# dead → 触发 respawn。假阴性比假阳性安全：最坏产生一个无害孤儿守护（新守护 unlink+rebind
# 把 socket 路径指向自己，老守护监听的 inode 失去路径、收不到新连接，由终态/TTL 自清）。
#
# SPEC §5 S9：实现已抽到 ``iface/in_session/_daemon_liveness.py``（socket connect-probe +
# pidfile+/proc/cmdline 两类守护共享）。本模块保留薄 wrapper 兼容旧测试 import + 内部调用点。
from orca.iface.in_session._daemon_liveness import (  # noqa: E402
    _DEFAULT_PROBE_TIMEOUT as _DAEMON_PROBE_TIMEOUT,
    socket_daemon_alive,
)


def _chart_daemon_alive(sock_path: Path) -> bool:
    """探本 run 的 chart 守护 socket 是否有监听者（确定性健康探，**不靠进程名 grep**）。

    SPEC §5 S9：实现抽到 ``_daemon_liveness.socket_daemon_alive``（与 sidechain 守护的
    pidfile 探共享 helper，DRY）。本函数为薄 wrapper，保旧测试 import +
    ``cli._wait_for_sock`` / ``_ensure_chart_daemon`` 调用点不改。

    实现语义 / 副作用 / 异常策略详见 ``_daemon_liveness.socket_daemon_alive`` docstring
    （connect 成功 = 有监听者 = 守护活；所有 OSError → False，保守触发 respawn）。
    """
    return socket_daemon_alive(sock_path)


def _ensure_chart_daemon(run_id: str, tape_path: Path) -> None:
    """探 chart 守护是否存活，死了 respawn（``next`` 路径补 bootstrap 之后的缺口）。

    **背景**：bootstrap spawn 守护**一次**即退出；run 中途守护被杀（如 ``pkill opencode`` 顺带
    SIGTERM 了 detached 守护）后，``next`` 恢复 run 时**不 respawn** → 后续节点 subagent 的
    ``render_chart`` 连不上 socket、chart 全丢。本 helper 补这个缺口：每次 ``next`` 推进到有
    下一节点的状态后探一次，死了拉起。

    **并发安全 / 不会双写**（三层兜底，按真实性排序，**不靠单一锁**）：
      1. **宿主时序串行**：bootstrap 完整跑完 spawn + ``_wait_for_sock`` 才返回 → 宿主派 subagent
         → 之后才调 ``next``。故 bootstrap 的 spawn 与 ``next`` 的 respawn 在时间上不重叠。
         （注意：bootstrap 的 spawn 在释 tape flock **之后**跑 —— 见 bootstrap ``finally`` 释锁
         vs ``_spawn_chart_daemon`` 的先后；tape flock 并**不** serialize bootstrap↔next。）
      2. **next↔next 由 tape flock serialize**：并发 ``next``（同 run）由 ``LOCK_NB`` busy-exit
         互斥（见 ``next`` ``acquired is None`` 分支）→ 同一时刻只有一个 ``next`` 进 respawn。
      3. **unlink + rebind 孤立老 listener**：即便上述被打破（如 bootstrap 守护冷启 >5s、
         ``_wait_for_sock`` 超时、``next`` 又探到 dead 而 respawn），``chart_ingestor`` 入口
         ``if sock_path.exists(): unlink()`` 后 ``start_unix_server`` 重 bind → socket 路径指向
         新守护，老守护监听的 inode 失去路径、收不到新连接，变无害孤儿，由 ``_watch_terminal``
         终态事件或 6h TTL 自退。
    故无需额外的 respawn 专用锁（KISS/YAGNI）：next↔next 已被 tape flock serialize；跨阶段
    （bootstrap↔next）由时序 + unlink+rebind 兜底，最坏产生一个无害孤儿守护，绝不双写 tape
    （单一写路径 + ``_FlockSafeTape`` 跨进程互斥仍守）。

    **socket 路径不变**：``chart_sock_path(run_id)`` 按 run_id 确定性派生，respawn 复用同一路径
    → ``orca_env.sh`` 里 ``ORCA_CHART_SOCK`` 仍正确，子代理无需任何改动。``next`` 已在
    ``_next_in_critical_section`` 按下一节点身份重写了 env 文件，本 helper 不重复写。

    **stale socket 自清**：守护若被 SIGKILL（不跑 finally unlink）会留 stale socket 文件；
    本 helper 探到 ``ConnectionRefused``（stale）判 dead → respawn → 新守护的
    ``chart_ingestor`` 在 ``start_unix_server`` 前 ``if sock_path.exists(): unlink()`` 清 stale
    再 bind（已有逻辑，零改动）。

    失败语义（同 bootstrap）：``_wait_for_sock`` 超时仅 warn 不 fail next —— 守护是 chart 便利
    层，缺了只让 chart 连不上（fail loud 在 subagent 侧的 ``render_chart``），不阻塞 workflow
    推进本身。
    """
    sock_path = chart_sock_path(run_id)
    if _chart_daemon_alive(sock_path):
        return
    logger.info(
        "run %s: chart 守护不在（socket %s 无监听者）—— next 路径 respawn",
        run_id, sock_path,
    )
    # spawn 失败（Popen OSError：资源限制 / fd 耗尽 / 磁盘满开不了 log）降级为 warn —— 守护是
    # chart 便利层，缺了只让 render_chart 在 subagent 侧 fail loud，不应以裸 traceback 崩 next
    # （``next`` 的 except 仅 catch ``InSessionError``，不接 ``OSError``）。
    try:
        _spawn_chart_daemon(run_id, tape_path)
    except OSError:
        logger.warning(
            "run %s: respawn chart 守护失败（socket %s，Popen OSError；chart 将不可用直到下次 next 重试）",
            run_id, sock_path, exc_info=True,
        )
        return
    if not _wait_for_sock(sock_path):
        logger.warning(
            "run %s: respawn 后 socket %s 在 %.1fs 内未就绪"
            "（host 派 subagent 期间可能补上）",
            run_id, sock_path, _SOCK_READY_TIMEOUT,
        )


# ── in-session sidechain 守护（SPEC-B v4 B2：子 agent 过程 → tape）──────────────
#
# 与 chart 守护同款「detach spawn + next respawn」pattern。差异：sidechain 守护无 socket
# （主动 tail / 查询子 agent 事件源），liveness 探改用 pidfile + ``/proc/<pid>/cmdline``
# （见 ``sidechain_daemon._sidechain_daemon_alive``）。backend 由 env 派生（CC vs opencode），
# 作为 ``--backend`` 启动参数传 daemon —— 是 SPEC §0 grep 守门豁免的「启动参数」位置。
#
# 为什么 bootstrap + next 都要 spawn / ensure：与 chart 同款（见 ``_ensure_chart_daemon``
# 并发安全三层兜底说明）。sidechain 守护是 B2 实时推送的脊梁，run 中途被杀 → 子 agent 过程
# 在 web 不可见 → respawn 必要。


def _detect_backend_from_env() -> str | None:
    """从 env 推断宿主 backend（``cc`` / ``opencode``），spawn sidechain 守护用。

    推断规则（SPEC-B v4 §0：backend 选择属 daemon 启动参数，grep 守门豁免）：
      - ``CLAUDE_CODE_SESSION_ID`` 存在 → ``"cc"``（CC 自动注入所有 bash 子进程）。
      - 否则 ``ORCA_HOST_SESSION_ID`` 存在 → ``"opencode"``（opencode plugin 显式注入）。
      - 都无 → ``None``（非 in-session 起的 run，B2 守护无法启动）。

    返 ``None`` 时调用方应 skip spawn + warn（fail-open：run 仍可推进，只是子 agent 过程
    不进 web；与 SPEC §5 opencode host_session=None fail-open 语义一致）。
    """
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "cc"
    if os.environ.get("ORCA_HOST_SESSION_ID"):
        return "opencode"
    return None


def _spawn_sidechain_daemon(run_id: str, tape_path: Path) -> None:
    """detach 起 sidechain 守护（SPEC-B v4 §4）。

    镜像 ``_spawn_chart_daemon`` 的 detach pattern（``start_new_session=True`` 脱 bootstrap
    CLI 的 process group）。差异：
      - 额外 argv ``--backend`` / ``--host-session`` / ``--family``（启动参数，daemon 内选 adapter
        + 家族 → dotdir 映射；SPEC §P4）。
      - 日志落 ``<rundir>/<run_id>/sidechain_daemon.log``。
      - 无 ``_wait_for_sock``（无 socket；liveness 走 pidfile + ``/proc`` 校验，由
        ``_sidechain_daemon_alive`` 实现，import 自 daemon 模块）。

    family 来源（SPEC §P4）：``load_merged_config().get("sidechain", {}).get("family")``——
    iface 层读 config 合法（events 层 resolver 不读 config，依赖铁律）。``sidechain`` 不入
    ``CONFIG_FIELDS``（三 spawn 维度），独立读：sidechain 不是 spawn 参数维度，是路径解析维度。

    失败语义：``OSError``（Popen / log fd）→ 上抛给调用方（bootstrap/next 决定 warn 降级）。
    无 ``host_session`` / 无 ``backend`` → 静默 skip + warn（fail-open：B2 守护是便利层，
    缺了只让子 agent 过程不进 web，不阻塞 workflow）。
    """
    host_session = _host_session_from_env()
    if not host_session:
        logger.info(
            "run %s: 无 host_session env（CLAUDE_CODE_SESSION_ID / ORCA_HOST_SESSION_ID），"
            "skip sidechain 守护（B2 子 agent 过程不进 web）",
            run_id,
        )
        return
    backend = _detect_backend_from_env()
    if backend is None:
        logger.info(
            "run %s: 无法识别 backend（无 CC/opencode env），skip sidechain 守护",
            run_id,
        )
        return

    # SPEC §P4：family 从 config 透传给 daemon（events 层不读 config）。
    family = _read_sidechain_family_from_config()

    run_dir = _run_dir_for(tape_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "sidechain_daemon.log"

    cmd = [
        sys.executable, "-m", "orca.iface.in_session.sidechain_daemon",
        "--run-id", run_id,
        "--tape", str(tape_path),
        "--backend", backend,
        "--host-session", host_session,
    ]
    if family is not None:
        cmd.extend(["--family", family])
    log_fd = open(log_path, "a", encoding="utf-8")
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fd.close()
    logger.info(
        "run %s: sidechain 守护已 detach spawn（backend=%s, host=%s, family=%s, log=%s）",
        run_id, backend, host_session, family, log_path,
    )


def _read_sidechain_family_from_config() -> str | None:
    """读 config ``sidechain.family``（SPEC §P4；iface 层读 config 合法）。

    ``load_merged_config`` 已透传未知 key（``CONFIG_FIELDS`` 仅管 binaries/flags/prompt_channel
    三 spawn 维度，``sidechain.*`` 是路径解析维度，独立读——不加进 ``CONFIG_FIELDS``）。

    Returns:
        family 字符串（"cc"/"cac"/"opencode"/"nga"，由用户填）或 None（未设）。**不做合法性
        校验**——resolver 会 raise ValueError，doctor 会报 fail；本函数仅透传。
    """
    from orca.iface.cli.config import load_merged_config
    sidechain = load_merged_config().get("sidechain")
    if not isinstance(sidechain, dict):
        return None
    fam = sidechain.get("family")
    return fam if isinstance(fam, str) else None


def _ensure_sidechain_daemon(run_id: str, tape_path: Path) -> None:
    """探 sidechain 守护是否存活，死了 respawn（``next`` 路径补 bootstrap 之后的缺口）。

    与 ``_ensure_chart_daemon`` 同款「probe + spawn」pattern。差异：
      - liveness 探改用 pidfile + ``/proc/<pid>/cmdline``（sidechain 无 socket）。
      - 无 ``_wait_for_sock``（pidfile 由 daemon 自己写，spawn 后下次 next 探到 alive）。

    并发安全：同 chart 守护（next↔next 由 tape flock serialize；跨阶段时序 + pidfile 兜底）。
    失败语义：spawn 失败（Popen OSError）→ warn 不 fail next（守护是 B2 便利层）。
    无 host_session / 无 backend → 静默 skip（与 ``_spawn_sidechain_daemon`` 一致）。
    """
    # lazy import 避开 cli 模块初始化期循环（sidechain_daemon 反向 import cli._flock_path）。
    from orca.iface.in_session.sidechain_daemon import _sidechain_daemon_alive

    if _sidechain_daemon_alive(run_id):
        return
    logger.info(
        "run %s: sidechain 守护不在（pidfile + /proc 校验未过）—— next 路径 respawn",
        run_id,
    )
    try:
        _spawn_sidechain_daemon(run_id, tape_path)
    except OSError:
        logger.warning(
            "run %s: respawn sidechain 守护失败（Popen OSError；B2 子 agent 过程将不可见直到下次 next 重试）",
            run_id, exc_info=True,
        )


def _write_orca_env(
    env_path: Path,
    *,
    run_id: str,
    node: str,
    session_id: str,
    sock_path: Path,
    resources_root: str | None,
) -> None:
    """原子写 ``runs/<run_id>/orca_env.sh``（5 个变量，按当前节点身份）。

    内容（**字面值**，子代理只 ``source`` 不 typing）::

        export ORCA_RUN_ID=<run_id>
        export ORCA_NODE=<当前节点名>
        export ORCA_SESSION_ID=<本次 dispatch 的 uuid>
        export ORCA_CHART_SOCK=<chart_sock_path(run_id)>
        export ORCA_AGENT_RESOURCES=<folder-agent resources_root>   # 或 ``unset`` 清 stale

    ``resources_root`` 非空（folder-agent）→ ``export``；为 None（inline-prompt 节点）→
    ``unset ORCA_AGENT_RESOURCES`` 清潜在 stale（同一 shell 内前一次 source 的残留）。

    原子写：tmp + ``os.replace``（与 marker / step.py ``_write_prompt_file`` 同模式）。OSError
    → warn（不 fail next：env 文件是子代理侧便利，缺了也只让 chart/资源引用 fail loud 在子代理侧）。
    """
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"export ORCA_RUN_ID={shlex.quote(run_id)}",
        f"export ORCA_NODE={shlex.quote(node)}",
        f"export ORCA_SESSION_ID={shlex.quote(session_id)}",
        f"export ORCA_CHART_SOCK={shlex.quote(str(sock_path))}",
    ]
    if resources_root:
        lines.append(f"export ORCA_AGENT_RESOURCES={shlex.quote(str(resources_root))}")
    else:
        lines.append("unset ORCA_AGENT_RESOURCES")
    content = "\n".join(lines) + "\n"

    tmp = env_path.with_name(f".{env_path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, env_path)
    except OSError:
        tmp.unlink(missing_ok=True)
        logger.warning("run %s: 写 env 文件 %s 失败（子代理 chart/资源可能受影响）",
                       run_id, env_path, exc_info=True)


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

    SPEC §3 O4：协议补 ``reason=busy`` 重试规则 —— 主 session 等 ``retry_after_ms`` 后重试
    **同一 next 命令本身**（不重派子代理 / 不重发 prompt；避免 advance_step 不持锁调用契约冲突）。
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
        "   - 若 reason 为 busy（撞锁，罕见）→ **不要重派子代理、不要重发 prompt**；"
        "等返回的 retry_after_ms 毫秒后**重试同一条 next 命令**（参数一字不改）。\n"
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


# SPEC §4 F3：手写 TYPE_MAP（isinstance），**不引入 jsonschema 依赖**（与 step.py
# ``_parse_output`` 用 jsonschema 区分：output_schema 是节点产物校验、有真 schema；
# inputs 是 bootstrap 期轻量校验、用 wf.inputs 的 type 字符串 + Python type 即可）。
#
# type 字符串（来自 ``InputDef.type``，YAML 里写）→ Python isinstance 检查函数。
# 不在白名单的 type（如自定义 ``FileType`` / ``Url`` 等）→ pass-through（YAGNI：旧 wf
# loose-typed 零回归，调用方不校验）。
#
# bool/int 隔离：Python ``isinstance(True, int) is True`` —— 若用户传 ``True`` 给
# ``count: int``，应判错（不是静默把 True 当 1）。故 ``int``/``float`` 检查排除 bool。
def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    # int 也接受为 number / float（数学上 int ⊂ real；YAGNI 不强求 float 字面）。
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_TYPE_MAP: dict[str, Any] = {
    "string": lambda v: isinstance(v, str),
    "str": lambda v: isinstance(v, str),
    "int": _is_int,
    "integer": _is_int,
    "float": _is_number,
    "number": _is_number,
    "boolean": lambda v: isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "array": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "object": lambda v: isinstance(v, dict),
}


def _validate_inputs(
    inputs: dict[str, Any], schema: list[dict[str, Any]],
) -> tuple[bool, str]:
    """SPEC §4 F3：bootstrap ``--inputs`` 校验（手写 TYPE_MAP，无 jsonschema 依赖）。

    Args:
        inputs: 用户传入的 inputs dict（已 ``json.loads``）。
        schema: ``catalog.inputs_schema_list(wf)`` 输出（``[{name, type, description}]``）。

    Returns:
        ``(ok, error_message)``。``ok=False`` 时 ``error_message`` 描述首错（含字段名 + 期望
        type + 实际 type，定位给用户）。``ok=True`` 时 ``error_message`` 为空串。

    校验规则（SPEC AC）：
      - 未声明 ``type``（旧 wf loose-typed）→ **pass-through**（零回归）。
      - ``type`` 不在 ``_TYPE_MAP`` 白名单（自定义 type）→ **pass-through**（YAGNI 不校验）。
      - ``description`` 开头是 ``[default]`` / ``[advanced]`` 标签 → 缺省时不触发 required
        （SKILL 教主 session 省略此类字段，让 wf 用 default）。
      - 显式 ``type`` + 非标签字段 + 缺省 → ``inputs_validation_error``（fail loud + 字段名定位）。
      - 显式 ``type`` + 给值但类型不对 → ``inputs_validation_error``（fail loud + 类型对比）。

    **与 ``InputDef.required`` 字段的关系**（设计约束，code-reviewer 🟡#2 显式化）：
    ``inputs_schema_list``（``catalog.py:156-171``）**不透出** ``required`` 字段；本函数依赖
    SKILL tag 契约（``SKILL.md:84-100``）——约定每个可选字段必须带 ``[default]``/``[advanced]``
    标签。若 wf YAML 写 ``required: false`` 但 description 无标签，本函数仍判 missing required
    （保守；防 SKILL 漏抽 + 与 ``InputDef.required=True`` 默认值对齐）。这是 SKILL 约定优先
    于 schema 字段的明确选择（SPEC v4.1 闭环 v2-B3 / v2-M10）。
    """
    for field_def in schema:
        name = field_def.get("name")
        ftype = field_def.get("type")
        desc = field_def.get("description") or ""

        # 未声明 type → pass-through（旧 wf loose-typed 零回归）。
        if not ftype:
            continue
        # type 不在白名单 → pass-through（YAGNI：自定义 type 不校验）。
        check_fn = _TYPE_MAP.get(ftype)
        if check_fn is None:
            continue

        # description 开头 [default] / [advanced] 标签 → SKILL 教省略（不触发 required）。
        is_optional_tag = (
            desc.startswith("[default]") or desc.startswith("[advanced]")
        )

        # required check（仅对显式 type + 非标签字段）。
        if name not in inputs:
            if is_optional_tag:
                continue  # 省略合法（wf 用内置 default）
            return False, (
                f"missing required input {name!r} (type={ftype!r})."
                f" 若想省略走默认，在 description 开头加 [default] 或 [advanced] 标签。"
            )

        # type check（value 存在）。
        value = inputs[name]
        if not check_fn(value):
            return False, (
                f"input {name!r} expected type {ftype!r}, "
                f"got {type(value).__name__} (value={value!r:.80})"
            )

    return True, ""





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
    # 当 wf 名解析：catalog 按 ``wf.name`` 精确反查（compile 层，顶层 import）。
    resolved = catalog.find_workflow_yaml_path(wf_arg)
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


def _echo_busy_reply() -> None:
    """撞锁 busy 0 退出信封（SPEC §3 O4：含 ``retry_after_ms`` 让主 session 等待重试）。

    bootstrap / next / stop 三命令在 ``_try_acquire_flock`` 返 None（LOCK_NB 撞锁）时调本
    helper 统一信封形态：``{done: False, reason: "busy", retry_after_ms: 500}``。主 session
    据 ``retry_after_ms`` 等待后**重试同一 next**（不重派子代理 / 不重发 prompt）。
    """
    typer.echo(json.dumps({
        "done": False,
        "reason": "busy",
        "retry_after_ms": _BUSY_RETRY_AFTER_MS,
    }))


# 注：ADR I3.3「仅本地 FS」不在 CLI 启动期主动检测——``fcntl.flock`` 在 NFS / 网络盘
# 上会显式失败或行为异常，运行时会暴露（不靠启发式检测给读者虚假安全感）。本地 FS
# 假设由 SPEC §0 明示（用户在 repo 根目录跑 ./runs/）。


# ── 失败 taxonomy / 信封拼装（SPEC §2.5 表，F6 闭环）─────────────────────────
#
# ``_classify_in_session_error`` / ``_emit_workflow_failed`` / ``apply_step_result`` /
# ``fail_in_session`` 已抽到 ``iface/in_session/_step_io.py``（v5 §8 step 5b：daemon + cli
# 两路共享 IO 边界，单一分类轴 ``InSessionError.error_kind``）。本模块 import 复用：
#   - ``apply_step_result``：成功路径 emit_batch + 基础信封（bootstrap/next 成功）。
#   - ``fail_in_session``：失败路径 emit workflow_failed + 错误信封（bootstrap/next except）。
#   - ``_emit_workflow_failed``：合规计数 / marker 写失败（字面 error_kind，非 InSessionError）。
# 分类函数 ``_classify_in_session_error`` 是 helper 内部实现（``fail_in_session`` 调用），
# 单测直接从 ``_step_io`` import 守门分类契约。


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
        " 后端/headless 命令在另一入口（见 `tars --help`）。"
    ),
)


@app.command(hidden=True)
def bootstrap(
    wf: str = typer.Argument(..., help="workflow 名（catalog 精确匹配）或 yaml 路径"),
    inputs: str = typer.Option(
        None, "--inputs",
        help="workflow inputs（JSON）。省略 → 不启动，只返 inputs_schema（给 skill 抽 inputs）",
    ),
    model: str = typer.Option(
        "deepseek/deepseek-v4-flash", "--model", help="provider/model（记入 marker）",
    ),
    format: str = typer.Option(
        "json", "--format",
        help="输出格式：json（默认，机器读）/ prompt（command 入口用：只回 entry "
             "prompt 纯文本，让主 session 直接据其派子代理）",
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
    no_memory: bool = typer.Option(
        False, "--no-memory",
        help="禁用节点记忆:整 run 不写 .orca/memory/、不注入「上一轮记忆」段"
             "(测试隔离 / 显式禁用复用协议)",
    ),
) -> None:
    """起一个 in-session run 的首步（gen run_id + tape + marker + emit ws+ns → entry prompt）。

    **不带 ``--inputs`` → 不启动，只返 ``{name, description, inputs_schema}``**（纯只读，
    不建 run/tape/marker）：schema 是「启动 wf 时」才需要的信息，选 wf 已由 ``orca list`` 的
    name+description 完成，这里按需给 skill 抽 inputs。带 ``--inputs``（含 ``{}``）→ 真启动。
    ``--format`` 仅启动路径生效；schema 查询（不带 ``--inputs``）恒返 JSON。

    v3 §7.3（m12）：同 wf 已有活跃 marker（未终态）→ **fail loud**（不静默新建孤儿）。
    提示续跑（`orca next --run-id <id>`）或先停（`orca stop <id>`）。
    """
    _setup_logging(log_level)
    yaml_path = _resolve_wf_path(wf)  # fail loud：名/路径都解析不到 → BadParameter
    wf_obj = load_workflow(yaml_path)  # fail loud：非法 yaml 抛 ConfigurationError

    # 不带 --inputs → 只返 inputs_schema，不启动（选 wf 已由 orca list 完成；schema 在此
    # 按需给）。纯只读：不 gen run_id / 不建 tape / 不写 marker / 不 spawn 守护。
    if inputs is None:
        typer.echo(json.dumps({
            "name": wf_obj.name,
            "description": wf_obj.description,
            "inputs_schema": catalog.inputs_schema_list(wf_obj),
        }, ensure_ascii=False))
        raise typer.Exit(0)

    try:
        inp = json.loads(inputs)
    except json.JSONDecodeError as e:
        raise typer.BadParameter(f"--inputs 不是合法 JSON：{e}") from e

    # SPEC §4 F3：bootstrap 期 inputs 校验（手写 TYPE_MAP，无 jsonschema 依赖；fail loud
    # ``inputs_validation_error``）。在 bootstrap_lock 之前（不触碰任何 state；与 dupe check
    # 同属「fail fast 在 gen run_id 前」层）。不符 → reply 错误信封 + exit 1。
    inputs_ok, inputs_err = _validate_inputs(inp, catalog.inputs_schema_list(wf_obj))
    if not inputs_ok:
        typer.echo(json.dumps({
            "done": True,
            "error_kind": INPUTS_VALIDATION_ERROR,
            "reason": f"failed: inputs_validation_error: {inputs_err}",
        }, ensure_ascii=False))
        raise typer.Exit(1)

    tape_path_probe = _default_tape_path("__probe__")
    rundir = tape_path_probe.parent

    # ── bootstrap serialize lock（well-known 路径，NOT per-run_id）──────────────
    # 关键：锁文件名必须独立于 run_id。``gen_run_id`` 每次返新值，若锁文件用
    # ``orca-<run_id>.json.flock``，两并发 bootstrap 同 wf 各 gen 不同 run_id → 各锁不同
    # 文件 → 互不阻塞 → 都过 dupe check → 两个孤儿 run（TOCTOU，review B1）。
    # 全局锁（单一 ``.orca-bootstrap.lock``）serialize 所有 bootstrap：同 wf 并发 →
    # 第二个等第一个写完 marker 再跑 dupe check → 看到 marker → fail loud。bootstrap
    # 是低频操作（每 run 一次），全局串行无性能影响。
    #
    # SPEC §3 O2（v4.1）：锁临界区**只**包 dupe check + gen run_id + advance+emit +
    # write_marker。``_write_orca_env`` + ``_spawn_*_daemon`` + ``_wait_for_sock`` 移锁外
    # （run_id 派生路径，不参与 dupe 判定）。**dupe-check 不变量仍成立**：同 wf 并发
    # bootstrap → 第二个等第一个写完 marker 再跑 dupe check → 看到 marker → fail loud。
    # 收益：bootstrap 持锁时间从「spawn + 5s socket wait」降到「emit + write_marker」
    # （典型 <100ms），消除 next 路径 / 第二个 bootstrap 等 socket 的时间税。
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
            _echo_busy_reply()
            return
        fd, _ = acquired
        try:
            result = asyncio.run(_advance_and_emit(
                bus, wf_obj, tape, output=None, inputs=inp, run_id=run_id, elapsed=0.0,
                prompts_dir=_prompts_dir_for(tape_path, run_id),
                yaml_path=os.path.realpath(yaml_path),
                host_session=_host_session_from_env(),
                project_root=Path.cwd(),
                no_memory=no_memory,
            ))
        except InSessionError as e:
            # bootstrap 失败 = 配置坏（如 entry 不是 agent 节点），fail loud。
            # fail_in_session：emit workflow_failed（tape data.kind = error_kind）+ 返
            # 含 ``error_kind`` 的错误信封（v5 §8 step 5b，SPEC §2.3 信封契约）。
            reply = asyncio.run(fail_in_session(bus, e))
            bus.close()
            typer.echo(json.dumps(reply, ensure_ascii=False))
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
            typer.echo(json.dumps({
                "done": True, "error_kind": "internal_error",
                "reason": f"failed: write_marker: {e}",
            }, ensure_ascii=False))
            raise typer.Exit(1)
    finally:
        # SPEC §3 O2：bootstrap_lock 释放在 write_marker 之后、spawn daemons 之前。
        # 释放后第二个 bootstrap 能立刻进 dupe check（看到本 run 的 marker → fail loud），
        # 不必等本 run spawn + socket wait。
        try:
            fcntl.flock(mlock_fd.fileno(), fcntl.LOCK_UN)
        finally:
            mlock_fd.close()

    # ── 以下在 bootstrap_lock **外**（SPEC §3 O2）：run_id 派生路径，不参与 dupe 判定 ──
    # 变量 run_id / tape_path / result 在锁内已赋值；非 early-exit 路径（return / raise Exit）
    # 才会到达此处，故变量必已初始化（dupe check / busy / InSessionError / write_marker 失败
    # 都已 early-exit，本块仅在「marker 已落 + workflow_started 已 emit」时跑）。
    #
    # code-reviewer 🟡#3：``assert`` 守门，防未来新增「锁内不 raise 的 return」漏赋变量
    # 导致锁外 NameError（fail loud 在断言，而非 NameError 在 chart_sock_path(run_id)）。
    assert run_id, "O2 invariant: post-lock code requires run_id set inside lock"
    assert tape_path is not None, "O2 invariant: post-lock code requires tape_path set"
    assert result is not None, "O2 invariant: post-lock code requires result set"

    # ── in-session chart ingestor 守护 + env 文件（phase-13 §3 in-session 衔接）──────────
    # bootstrap 后子代理执行发生在宿主 session 时间窗；detach 起守护让它跨 bootstrap CLI
    # 退出存活，bind socket 收 chart。env 文件给 entry 节点身份（每次 next 重写下一节点）。
    sock_path = chart_sock_path(run_id)
    env_path = _env_file_path(tape_path, run_id)
    _write_orca_env(
        env_path,
        run_id=run_id,
        node=result.node or "",
        session_id=uuid.uuid4().hex,
        sock_path=sock_path,
        resources_root=result.resources_root,
    )
    _spawn_chart_daemon(run_id, tape_path)
    if not _wait_for_sock(sock_path):
        logger.warning(
            "run %s: chart 守护 socket %s 在 %.1fs 内未就绪（host 派 subagent 期间可能补上）",
            run_id, sock_path, _SOCK_READY_TIMEOUT,
        )
    # ── in-session sidechain 守护（SPEC-B v4 B2：子 agent 过程 → tape）───────────
    # 与 chart 守护并列 spawn：B2 实时推送子 agent msg/tool/thinking 到 web。失败语义同
    # chart：OSError → warn 不 fail bootstrap（守护是便利层）。无 host_session / 无 backend
    # → 静默 skip（fail-open）。
    try:
        _spawn_sidechain_daemon(run_id, tape_path)
    except OSError:
        logger.warning(
            "run %s: spawn sidechain 守护失败（Popen OSError；B2 子 agent 过程将不进 web）",
            run_id, exc_info=True,
        )

    reply: dict[str, Any] = {
        "run_id": run_id,
        "tape": str(tape_path),
        "done": result.done,
    }
    if result.node:
        reply["node"] = result.node
    prompt_text = _reply_prompt(result, env_file=env_path)
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
    no_memory: bool = typer.Option(
        False, "--no-memory",
        help="禁用节点记忆:整 run 不写 .orca/memory/、不注入「上一轮记忆」段"
             "(测试隔离 / 显式禁用复用协议)",
    ),
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
        _echo_busy_reply()
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
            env_path=_env_file_path(tape_path, run_id),
            project_root=Path.cwd(),
            no_memory=no_memory,
        ))
        # in-session chart 守护存活检查 + respawn（phase-13 §3 in-session 衔接）：
        # bootstrap 只 spawn 一次；run 中途守护被杀（pkill opencode 误伤等）后，next 必须拉起
        # 否则后续节点 subagent 的 render_chart 连不上 socket、chart 全丢。仅在「有下一节点」
        # （非终态 / 非合规失败 / 节点名非空）时探 —— 与下方 env 文件写守卫同条件。终态 run
        # 守护会由 tape 终态事件自退；no-marker（node=None）无推进意义 —— 二者都不该 respawn。
        # 在 tape flock 临界区内（finally 才释放）→ 同 run 并发 next 已被 LOCK_NB busy-exit
        # 串行化，此处 probe+spawn 不会起多个守护（见 ``_ensure_chart_daemon`` 不变量）。
        if result.node is not None and not (result.done or compliance_failed):
            _ensure_chart_daemon(run_id, tape_path)
            # ── in-session sidechain 守护 respawn（SPEC-B v4 B2）───────────────────
            # 与 chart 守护同款：bootstrap spawn 一次，next respawn。B2 守护是实时推送脊梁，
            # 死了 → 子 agent 过程不进 web → 必拉起。同 chart 的 fail-open 语义（warn 不 fail next）。
            _ensure_sidechain_daemon(run_id, tape_path)
    except InSessionError as e:
        # fail_in_session：emit workflow_failed（tape data.kind = error_kind）+ 返含
        # ``error_kind`` 的错误信封（v5 §8 step 5b，SPEC §2.3 信封契约）。
        reply = asyncio.run(fail_in_session(bus, e))
        # 终态后清 marker（workflow_failed 已落 tape，marker 不再需要）。
        clear_marker(mpath)
        typer.echo(json.dumps(reply, ensure_ascii=False))
        raise typer.Exit(1)
    finally:
        try:
            _release_flock(fd)
        finally:
            bus.close()

    reply: dict[str, Any] = {"done": result.done or compliance_failed}
    if result.node:
        reply["node"] = result.node
    prompt_text = _reply_prompt(result, env_file=_env_file_path(tape_path, run_id))
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
    host_session: str | None = None,
    project_root: Path | None = None,
    no_memory: bool = False,
):
    """调 advance_step + emit_batch（单次 write 原子化，B1）。

    emit 经 ``apply_step_result``（共享 helper）；返 ``result`` 供 bootstrap 命令拼富信封
    （run_id/tape/prompt_file/驱动协议）。helper 返的基础信封在此丢弃（bootstrap 自建）。

    ``host_session`` 透传给 advance_step（仅 pending 首节点分支写 tape，SPEC §4.1 emit 真链）。
    ``project_root`` / ``no_memory`` 透传 advance_step + apply_step_result(node-memory SPEC §5)。
    """
    result = advance_step(
        tape, wf, output=output, inputs=inputs, run_id=run_id, elapsed=elapsed,
        prompts_dir=prompts_dir, yaml_path=yaml_path, host_session=host_session,
        project_root=project_root, no_memory=no_memory,
    )
    await apply_step_result(
        bus, result, wf=wf, run_id=run_id, no_memory=no_memory, project_root=project_root,
    )   # emit_batch + 记忆写入（基础信封丢弃，bootstrap 命令自建富信封）
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
    wname = _read_workflow_name(tape_path)
    if wname is None:
        raise InSessionError(
            f"run {run_id} 的 tape 无 workflow_started，无法定位 workflow",
            error_kind="state_corrupt",
        )
    found = catalog.find_workflow(wname)
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
    env_path: Path | None = None,
    project_root: Path | None = None,
    no_memory: bool = False,
):
    """flock 临界区内的 next 主体：advance + emit_batch + marker RMW（N2）+ 合规计数 (F11)
    + per-node env 文件重写（in-session chart/资源 衔接）。

    返回 ``(result, compliance_failed)``。``compliance_failed=True`` 时已 emit
    workflow_failed，调用方据 ``done=True`` 停注入。

    ``env_path`` 给定时（生产路径恒传）：在 ``apply_step_result`` 之后、按下一节点身份重写
    ``runs/<run_id>/orca_env.sh``（ORCA_NODE / ORCA_SESSION_ID / ORCA_AGENT_RESOURCES 按新节点）。
    终态（done/compliance_failed）不写 —— 无下一节点，env 文件保留前值（run 即将结束）。
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
        project_root=project_root, no_memory=no_memory,
    )
    # B1 单次 write 原子化整批 [nc, rt, ns] / [nc, rt, workflow_completed]（共享 helper）。
    # apply_step_result 内部按 emits 写 node_completed 的记忆 MD(node-memory SPEC §3.2)。
    await apply_step_result(
        bus, result, wf=wf, run_id=run_id, no_memory=no_memory, project_root=project_root,
    )

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

    # in-session chart/资源 env 文件：非终态 + 下一节点存在 → 按下一节点身份重写。
    # 终态时不写（无下一节点；守护会由 tape 终态事件自退，env 文件不再被 source）。
    if env_path is not None and not (result.done or compliance_failed) and result.node:
        _write_orca_env(
            env_path,
            run_id=run_id,
            node=result.node,
            session_id=uuid.uuid4().hex,
            sock_path=chart_sock_path(run_id),
            resources_root=result.resources_root,
        )

    # marker RMW（N2）：flock 临界区内回写。终态 → 清 marker（不复用）。
    if result.done or compliance_failed:
        clear_marker(mpath)
    else:
        write_marker(mpath, marker)
    return result, compliance_failed


def _merge_run_id(run_id: str | None, run_id_opt: str | None) -> str | None:
    """位置参数与 ``--run-id`` option 合流：同值容错 / 异值 fail loud / 都空返 None。

    status/stop/open 三命令共用（FU-1，DRY：防三处合流逻辑漂移）。都空返 None，
    **None 的语义由调用方按命令分别处理**：status → 列全部活跃 run；stop → fail loud
    （显式守卫）；open → 取活跃 run 默认。
    """
    if run_id is not None and run_id_opt is not None and run_id != run_id_opt:
        raise typer.BadParameter(
            f"位置参数 run_id={run_id!r} 与 --run-id={run_id_opt!r} 不一致，请二选一"
        )
    return run_id if run_id is not None else run_id_opt


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

    rid = _merge_run_id(run_id, run_id_opt)

    if rid is None:
        # FU-3（SPEC §2.1/§2.3）：无参只列**活跃** run（marker 存在 ≡ 活跃，SPEC §7.2 完成
        # 契约：bootstrap 写 / 终态清）。completed run 无 marker → 不列。输出结构化
        # ``{runs:[{run_id,node,status,last_next_at,elapsed}]}``（非裸 stem）。
        runs_dir = _default_rundir()
        markers = sorted(runs_dir.glob("orca-*.json")) if runs_dir.exists() else []
        # ``now`` 取在循环外：多 run 共享同一快照基准（elapsed 跨 run 一致，非各算各的）。
        now = time.time()
        runs: list[dict[str, Any]] = []
        for mp in markers:
            marker = read_marker(mp)
            if marker is None:
                # 损坏/孤儿 marker：跳过不崩（doctor 另行检测）。
                continue
            tape_path = _default_tape_path(marker.run_id)
            if not tape_path.is_file():
                # marker 残留但 tape 缺（不可恢复孤儿）：跳过。
                continue
            tape = Tape(tape_path, run_id=marker.run_id)
            state = replay_state(tape)
            # 时间字段取 tape 末事件 ``Event.timestamp``（epoch 秒，``time.time()`` 基）。
            # RunState 零时间字段（schema/state.py），时间只能从 tape 事件派生（spec-reviewer #1）。
            last_ts = 0.0
            for ev in tape.replay():
                last_ts = ev.timestamp
            # elapsed 与 Event.timestamp 同基（time.time()）；混用 monotonic 会得无意义差值。
            elapsed = (now - last_ts) if last_ts > 0 else None
            runs.append({
                "run_id": marker.run_id,
                "node": state.current_node,
                "status": state.status,
                "last_next_at": last_ts if last_ts > 0 else None,
                "elapsed": elapsed,
            })
        if json_output:
            # 空列表 shape 与非空一致（消费方恒读 reply["runs"]，spec-reviewer #5）。
            typer.echo(json.dumps({"runs": runs}, ensure_ascii=False))
            return
        if not runs:
            typer.echo("(无活跃 run)")
            return
        for r in runs:
            typer.echo(
                f"- {r['run_id']} [{r['status']}] node={r['node']} elapsed={r['elapsed']}"
            )
        typer.echo("\n用 `orca status --run-id <run_id>` 看详情。")
        return

    tape_path = _default_tape_path(rid)
    if not tape_path.exists():
        typer.echo(typer.style(f"run {rid!r} 无 tape", fg=typer.colors.RED))
        raise typer.Exit(1)
    state = replay_state(Tape(tape_path, run_id=rid))
    done = sum(1 for s in state.node_status.values() if s == "done")
    progress = f"{done}/{len(state.node_status)}"

    # SPEC §3 O3：从激活 marker 读 ``no_output_count`` 透出（raw 观测用，主 session 不反应）。
    # 无 marker（run 已终态 / 还未 bootstrap / 损坏）→ None（不阻塞 status）。
    rundir = tape_path.parent
    marker = read_marker(marker_path(rundir, rid))
    no_output_count = marker.no_output_count if marker is not None else None

    if json_output:
        # 顶层字段供主 session（经 orca skill）直接消费；step 4 后 plugin transform 段
        # 已退场，--json 仍保留作 skill / LLM 友好的结构化出口（SPEC §2.3 单 run 详情契约）。
        # SPEC §3 O3：加 ``no_output_count``（raw 透出，主 session 不据它改行为；compliance
        # 是 orca 自我保护，到 _COMPLIANCE_LIMIT 自己 fail）。
        typer.echo(json.dumps({
            "run_id": rid,
            "status": state.status,
            "current_node": state.current_node,
            "node_status": dict(state.node_status),
            "progress": progress,
            "no_output_count": no_output_count,
        }, ensure_ascii=False))
        return

    typer.echo(f"run {rid}")
    typer.echo(f"  status:      {state.status}")
    typer.echo(f"  current_node: {state.current_node}")
    typer.echo(f"  node_status: {dict(state.node_status)}")
    typer.echo(f"  progress:    {progress} done")
    typer.echo(f"  no_output_count: {no_output_count}")


@app.command()
def stop(
    run_id: str = typer.Argument(
        None, help="要停的 run id（位置参数，与 --run-id 二选一；至少指定一个）",
    ),
    run_id_opt: str = typer.Option(
        None, "--run-id",
        help="要停的 run id（spec §2.1 与 next/status 统一的 --run-id 形态；与位置参数二选一）",
    ),
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """停一个 run：清激活 marker + per-call flock emit ``workflow_cancelled``。

    ``run_id`` 两种传法都接受（spec §2.1 统一 ``--run-id`` 形态，位置参数保留兼容旧调用 /
    主 session 既有测试）：``orca stop --run-id <id>`` 或 ``orca stop <id>``。两者同传且值不
    一致 → fail loud（BadParameter）。**都省略 → fail loud**（stop 无「列全部」模式，必须
    显式指定停哪个 run）。
    """
    _setup_logging(log_level)

    rid = _merge_run_id(run_id, run_id_opt)
    if rid is None:
        # stop 无 status 的「无参列全部」模式：None 必须 fail loud（保 exit 2 回归）。
        raise typer.BadParameter("stop 需指定 run_id：用 --run-id 或位置参数")

    tape_path = _default_tape_path(rid)
    rundir = tape_path.parent
    mpath = marker_path(rundir, rid)

    if not tape_path.exists():
        # 无 tape：仅清 marker（stop 幂等，允许「run 已清理但 marker 残留」）。
        clear_marker(mpath)
        typer.echo(json.dumps({"run_id": rid, "ok": True, "done": True, "note": "no-tape"}))
        return

    acquired = _try_acquire_flock(tape_path)
    if acquired is None:
        _echo_busy_reply()
        return
    fd, _ = acquired
    tape_obj = Tape(tape_path, run_id=rid, resume=True)
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
    typer.echo(json.dumps({"run_id": rid, "ok": True, "done": True}))


def _scan_skill_install(
    *, home: Path | None = None, cwd: Path | None = None,
) -> dict[str, bool]:
    """扫四前端 skill 目录，返 ``{platform: 是否装了入口 skill}``（v5 §4.3 / A6）。

    入口 skill 名取自 ``ENTRY_SKILL_NAME``（单一真相源，现 = tars）。每个平台查
    user-scope + project-scope 两个可能落点（``<root>/skills/<ENTRY_SKILL_NAME>/SKILL.md``），
    任一存在即该平台算已装。doctor 的 ``skill_install`` 检查据此 pass/fail。

    ``home`` / ``cwd`` 可注入（对齐 ``resolve_roots`` 模式）——单测隔离真实 ``~/.claude``
    等，否则装过 orca 的开发机上 ``fail_when_absent`` 测试会反向失败（review 🔴#2）。
    """
    # 延迟 import：skill_cmds 属 iface.cli 子包，避免顶层循环；HOST_DOTDIR 单一真相源。
    # ENTRY_SKILL_NAME = 入口 skill 目录名（单一真相源，防 doctor check 与 install 目录漂移）。
    from orca.iface.cli.skill_cmds import (
        ENTRY_SKILL_NAME, HOST_DOTDIR, SKILL_HOSTS, opencode_global_root,
    )

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
        platform: any(
            (root / "skills" / ENTRY_SKILL_NAME / "SKILL.md").is_file()
            for root in roots
        )
        for platform, roots in candidates.items()
    }


def _check_sidechain_backend() -> dict[str, Any]:
    """SPEC §P4：构造 ``sidechain_backend`` check（hard=False）。

    检测家族 + resolved root/DB + source + 存在性 + host_session 可用性 + 修复建议。
    用户从 doctor 获取 resolved 路径（SPEC §P4 验收 #3）。

    家族决策（env 主导 + config 覆盖 + 探测细分）：
      - ``CLAUDE_CODE_SESSION_ID`` 存在 → CC 家族（cc/cac；config/probe 细分）
      - ``ORCA_HOST_SESSION_ID`` 存在 → opencode 家族（opencode/nga）
      - 都无 → status="unknown"（非 in-session 起 run，B2 不适用）

    hint（SPEC §P4）：
      - cc+cac 同存无 config → 提示设 sidechain.family
      - root 不存在 → 提示 ``tars install --target cac``（或等 nga）
      - host_session 缺 → fail-loud 提示（设 sidechain.host_session 或显式 --host-session）

    Returns:
        ``{name, hard, status, detail}``；status ∈ {"pass","unknown","fail"}。
    """
    # lazy import：events 层 adapter resolver（events → iface 单向依赖，合法方向）。
    from orca.events.adapters._family import (
        CC_FAMILY_DOTDIR,
        OPENCODE_FAMILY_DOTDIR,
        detect_cc_existing_roots,
        detect_opencode_existing_dbs,
        resolve_cc_sidechain_root,
        resolve_opencode_db,
    )

    host_session = _host_session_from_env()
    cfg_family = _read_sidechain_family_from_config()
    has_cc_env = bool(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    has_opencode_env = bool(os.environ.get("ORCA_HOST_SESSION_ID"))

    hints: list[str] = []

    if has_cc_env:
        # CC 家族：cc/cac。resolver 探测歧义默认 cc。
        try:
            root, src = resolve_cc_sidechain_root(
                host_session or "", family=cfg_family, cwd=os.getcwd(),
            )
        except ValueError as e:
            return {
                "name": "sidechain_backend", "hard": False, "status": "fail",
                "detail": f"CC 家族 backend 解析失败：{e}",
            }
        existing = detect_cc_existing_roots(host_session or "", cwd=os.getcwd())
        root_exists = root.exists()
        available = root_exists and bool(host_session)

        # family effective（用于报告；resolver 内部已决策）：
        # 注意：本模块顶层有 typer 命令 ``def next(...)`` 遮蔽 Python builtin ``next``，
        # 故用 ``list(existing)[0]`` 取单元素（不能用 ``next(iter(...))``）。
        if cfg_family in CC_FAMILY_DOTDIR:
            fam_eff, fam_src = cfg_family, "config"
        elif len(existing) == 1:
            fam_eff, fam_src = list(existing)[0], "probe"
        elif len(existing) == 2:
            fam_eff, fam_src = "cc", "probe-ambig"
        else:
            fam_eff, fam_src = "cc", "default"

        if len(existing) == 2 and cfg_family is None:
            hints.append(
                "探测到 .claude + .cac 两存歧义（默认 .claude）；"
                "建议 config 设 sidechain.family 明确（'cc' 或 'cac'）。"
            )
        if not root_exists:
            hints.append(
                f"resolved root 不存在：{root}；"
                "若用 cac，确认 `tars install --target cac` 已跑且 session 已派过子 agent。"
            )
        if not host_session:
            hints.append(
                "未检测到 host_session env（CLAUDE_CODE_SESSION_ID）；B2 子 agent 过程推送不可用。"
                "in-session 路径下宿主 shell 必须有此 env（CC/cac 自动注入）；daemon 也接受显式"
                " ``--host-session`` argv 作 fallback。"
            )
        status = "pass" if available else ("unknown" if not host_session else "fail")
        return {
            "name": "sidechain_backend", "hard": False, "status": status,
            "detail": (
                f"family={fam_eff}（source={fam_src}）；"
                f"resolved_root={root}；root_source={src}；"
                f"root_exists={root_exists}；host_session_set={bool(host_session)}；"
                f"available={available}。"
                f"hint: {' '.join(hints) if hints else '（无）'}"
            ),
        }

    if has_opencode_env:
        try:
            db_path, src = resolve_opencode_db(family=cfg_family)
        except ValueError as e:
            return {
                "name": "sidechain_backend", "hard": False, "status": "fail",
                "detail": f"opencode 家族 DB 解析失败：{e}",
            }
        existing = detect_opencode_existing_dbs()
        db_exists = db_path.is_file()
        available = db_exists and bool(host_session)

        if cfg_family in OPENCODE_FAMILY_DOTDIR:
            fam_eff, fam_src = cfg_family, "config"
        elif len(existing) == 1:
            # ``list(existing)[0]`` 而非 ``next(iter(...))``——本模块 ``def next`` 遮蔽 builtin。
            fam_eff, fam_src = list(existing)[0], "probe"
        elif len(existing) == 2:
            fam_eff, fam_src = "opencode", "probe-ambig"
        else:
            fam_eff, fam_src = "opencode", "default"

        if len(existing) == 2 and cfg_family is None:
            hints.append(
                "探测到 .opencode + .nga 两存歧义（默认 opencode）；"
                "建议 config 设 sidechain.family 明确。"
            )
        if not db_exists:
            hints.append(
                f"DB 不存在：{db_path}；若用 nga，确认 `tars install --target nga` 已跑"
                "且 opencode/nga session 已活跃。"
            )
        if not host_session:
            hints.append(
                "未检测到 host_session env（ORCA_HOST_SESSION_ID）；B2 子 agent 过程推送不可用。"
                "in-session 路径下需 opencode plugin ``shell.env`` hook 注入；daemon 也接受显式"
                " ``--host-session`` argv 作 fallback。"
            )
        status = "pass" if available else ("unknown" if not host_session else "fail")
        return {
            "name": "sidechain_backend", "hard": False, "status": status,
            "detail": (
                f"family={fam_eff}（source={fam_src}）；"
                f"resolved_db={db_path}；db_source={src}；"
                f"db_exists={db_exists}；host_session_set={bool(host_session)}；"
                f"available={available}。"
                f"hint: {' '.join(hints) if hints else '（无）'}"
            ),
        }

    # 都无 env：非 in-session 起 run（如纯 headless 或 CI 跑 doctor）。
    return {
        "name": "sidechain_backend", "hard": False, "status": "unknown",
        "detail": (
            "未检测到 CC/opencode env（CLAUDE_CODE_SESSION_ID / ORCA_HOST_SESSION_ID）；"
            "sidechain 守护仅在 in-session run 中起作用（B2 子 agent 过程推送）。"
            "如确在 in-session 跑，检查 plugin 是否注入了 host_session env。"
        ),
    }


def _check_sidechain_daemon_liveness(rundir: Path) -> dict[str, Any]:
    """SPEC §5 D3：构造 ``sidechain_daemon`` liveness check（hard=False）。

    对每个活跃 run（marker 存在）的 sidechain 守护调 ``_sidechain_daemon_alive`` 探针。
    **覆盖守护死亡**（pidfile 残 / pid 死 / cmdline 不匹配 → next 路径会 respawn，此处仅观测）；
    **不覆盖守护存活但持续 iterate 失败**（§8#4：YAGNI 不做 socket 查询，靠 daemon log + 用户排查）。

    状态语义：
      - 无 host_session env / 无 rundir / 无活跃 marker → ``unknown``（守护本就不该起，
        与 ``_check_sidechain_backend`` 的 unknown 等价）。
      - 任意活跃 run 的守护死 → ``fail``（degraded；hard=False 不计 ok）。
      - 全部存活 → ``pass``。

    与 ``_check_sidechain_backend`` 的关系：后者查 **静态基础设施**（DB/dotdir 存在 +
    host_session env 设）；本 check 查 **运行时守护存活**。前者 unknown（无 env）时
    后者也 unknown（语义一致）；前者 pass 但后者 fail = 基础设施 OK 但 run 中守护被杀
    （respawn 由 next 兜底，观测用）。
    """
    # lazy import：sidechain_daemon 反向 import cli._flock_path；避免顶层循环。
    from orca.iface.in_session.sidechain_daemon import _sidechain_daemon_alive

    # 无 host_session env → sidechain 守护本就不该起（与 _check_sidechain_backend 的 unknown 等价）。
    if not _host_session_from_env():
        return {
            "name": "sidechain_daemon", "hard": False, "status": "unknown",
            "detail": (
                "未检测到 host_session env（CLAUDE_CODE_SESSION_ID / ORCA_HOST_SESSION_ID）；"
                "sidechain 守护仅在 in-session run 中起作用，无 env 时不会启动。"
                "（与 sidechain_backend check 的 unknown 同因。）"
            ),
        }

    if not rundir.exists():
        return {
            "name": "sidechain_daemon", "hard": False, "status": "unknown",
            "detail": "无 runs/ 目录（无活跃 run，sidechain 守护未起）。",
        }

    actives: list[str] = []
    for mp in sorted(rundir.glob("orca-*.json")):
        marker = read_marker(mp)
        if marker is not None:
            actives.append(marker.run_id)

    if not actives:
        return {
            "name": "sidechain_daemon", "hard": False, "status": "unknown",
            "detail": "无活跃 run marker（sidechain 守护仅在活跃 run 中起作用）。",
        }

    per_run: list[str] = []
    any_dead = False
    for rid in actives:
        alive = _sidechain_daemon_alive(rid)
        per_run.append(f"{rid}={'alive' if alive else 'dead'}")
        if not alive:
            any_dead = True

    if any_dead:
        return {
            "name": "sidechain_daemon", "hard": False, "status": "fail",
            "detail": (
                f"活跃 run 守护状态：{', '.join(per_run)}。"
                "dead 守护的 run 在下一次 `orca next` 时会自动 respawn（不阻塞推进）；"
                "若需立即拉起，调一次 next 即可。"
                "**本探针只覆盖守护死亡，不覆盖守护存活但持续 iterate 失败**（§8#4，"
                "靠 daemon log + 用户排查）。"
            ),
        }
    return {
        "name": "sidechain_daemon", "hard": False, "status": "pass",
        "detail": (
            f"活跃 run 守护状态：{', '.join(per_run)}（全部存活）。"
            "**本探针不覆盖守护存活但持续 iterate 失败**（§8#4）。"
        ),
    }


@app.command()
def doctor(
    log_level: str = typer.Option("INFO", "--log-level", help="INFO/DEBUG/WARN/ERROR"),
) -> None:
    """诊断 in-session 集成层（v5 §2.1 / §4.4：skill 落点 + CLI imports 为准；hook 心跳可选）。

    v5：B 路径（主 session 自调 next）不依赖 hook 推进；``orca.ts`` transform 派发已禁用
    （step 2b）。doctor 主验两件**硬**事——① ``skill_install``（四前端是否装了入口 skill，
    TARS 品牌；底层 orca CLI 引擎）+ ② ``cli_imports_ok``（CLI 后端可达）。旧两钩子
    （transform / idle）的心跳退居**可选**诊断（``ORCA_DIAGNOSE=1`` 时 plugin 写心跳，
    doctor 读取作证），**不计入 ok**——hook 不再推进，缺心跳不是故障。

    SPEC §P4 新增 ``sidechain_backend`` 可选 check（hard=False）：检测家族（env+config+探测）
    + 输出 resolved root/DB + source + 存在性 + host_session 可用性 + 修复建议（cac/nga 适配
    诊断；用户从 doctor 获取 resolved 路径）。

    SPEC §5 D3 新增 ``sidechain_daemon`` 可选 check（hard=False）：对每个活跃 run 调
    ``_sidechain_daemon_alive`` 探针。**覆盖守护死亡**（pidfile 残 / pid 死 / cmdline 不匹配）；
    **不覆盖守护存活但持续 iterate 失败**（§8#4：YAGNI 不做 socket 查询，靠 daemon log）。
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

    advance = _read_probe(PROBE_ADVANCE_NAME)
    now = int(time.time())

    checks: list[dict[str, Any]] = []

    # 每条 check 带 ``hard``：True = 计入 ok（skill_install / cli_imports_ok）；
    # False = 可选诊断（diag/hook），不计数。避免硬编码 name tuple（typo 静默丢失硬检查）。
    # ① skill_install（A6，硬）：四前端是否装了入口 skill（TARS 用户面；底层 orca CLI 引擎）。
    installed = _scan_skill_install()
    where = [p for p, ok_flag in installed.items() if ok_flag]
    checks.append({
        "name": "skill_install",
        "hard": True,
        "status": "pass" if where else "fail",
        "detail": (
            f"PASS：TARS skill 已装于 {', '.join(where)}。" if where
            else "FAIL：四前端（cc/opencode/cac/nga，user+project scope）均未找到 TARS skill。"
                 "跑 `tars install --target <platform>` 安装后重启前端。"
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

    # ④ advance_hook（idle，可选）：nudge 载体（SPEC §4.4），不 fail。
    # FU-2：entry_hook check 已删——transform 派发 step 4 整删 orca.ts transform 后，
    # PROBE_ENTRY 心跳永不再写（dead check）。advance_hook 保留：idle hook 仍写 PROBE_ADVANCE。
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

    # ⑤ sidechain_backend（可选诊断，SPEC §P4）：B2 子 agent 过程推送依赖的路径解析情况。
    # hard=False：doctor 不因此 fail；输出 resolved 路径 + source + 存在性 + hint 供用户排查
    # cac/nga 适配（SPEC §P4 验收「从 doctor 获取 resolved 路径」）。
    checks.append(_check_sidechain_backend())

    # ⑥ sidechain_daemon（可选诊断，SPEC §5 D3）：守护**存活探针**（per-active-run）。
    # hard=False：守护死亡不阻塞 doctor（next 路径自动 respawn）；覆盖死亡，**不覆盖持续
    # iterate 失败**（§8#4）。与 sidechain_backend 互补：前者查静态基础设施，本 check 查运行时存活。
    checks.append(_check_sidechain_daemon_liveness(rundir))

    # ok = 仅 ``hard=True`` 的检查无 fail。可选检查（diag/hook/sidechain）不计数。
    ok = all(c["status"] != "fail" for c in checks if c.get("hard"))

    lines = ["Orca in-session 诊断（v5：B 路径，skill 驱动入口）", ""]
    for c in checks:
        lines.append(f"[{c['status'].upper()}] {c['name']}: {c['detail']}")
    lines.append("")
    lines.append("硬检查（skill_install + cli_imports_ok）决定 ok；其余为可选诊断。")
    lines.append("v5 §1 执行模型：主 session 经 orca skill 自调 `orca next` 推进，不依赖 hook。")
    lines.append("")
    lines.append("心跳文件（仅 ORCA_DIAGNOSE=1 时 plugin 写，可选）：")
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
    """列出可用 workflow（给 skill/LLM **选 wf** 用，只发现/选择）。

    返 ``{workflows:[{name, description}]}``——选 wf 只需 description 做语义匹配。
    **不返 inputs_schema**：它是「启动某 wf 之后」才需要的信息（用来抽 inputs），全量塞进
    list 会让选 wf 阶段被 schema 噪音淹没（单个 wf 可达 20+ 字段）。inputs_schema 改由
    启动命令 ``orca <wf>``（不带 ``--inputs``）按需带出——见 ``bootstrap`` 的 ``inputs is
    None`` 分支。

    **单一 catalog 真相源**（coordinator 铁律）：本命令与 ``tars list``（commands.run_list，
    人类可读，给运维）**调同一个** ``catalog.list_workflows()``——catalog 是唯一实现，
    渲染层按消费者不同（orca list 要精简 / 运营要文本 / MCP 要全字段），非多套 list 逻辑。
    catalog item 的 inputs_schema / entry / inputs_count 留给 ``orca <wf>`` / MCP / tars 按需取。
    """
    items = catalog.list_workflows()
    # 只取选 wf 需要的 name/description；inputs_schema 移到 `orca <wf>` 启动命令（选 wf
    # 阶段不需要，见 bootstrap 的 inputs is None 分支）。
    workflows = [
        {"name": it["name"], "description": it["description"]}
        for it in items
    ]
    typer.echo(json.dumps({"workflows": workflows}, ensure_ascii=False))


@app.command(name="open")
def open_run(
    run_id: str = typer.Argument(
        None, help="要打开的 run_id（位置参数，与 --run-id 二选一；省略则用当前唯一活跃 run）",
    ),
    run_id_opt: str = typer.Option(
        None, "--run-id",
        help="要打开的 run id（spec §2.1 与 next/status/stop 统一的 --run-id 形态；与位置参数二选一）",
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

    ``run_id`` 两种传法都接受（spec §2.1 统一 ``--run-id`` 形态）：``orca open --run-id <id>``
    或 ``orca open <id>``。两者同传且值不一致 → fail loud（BadParameter）。**都省略 → 取当前
    唯一活跃 run**（多个活跃 → fail loud 提示指定 run_id；无活跃 → fail loud 提示先 ``orca <wf>``）。
    复用 ``tars open`` 同款 ``_open_run``（read-only attach + tail-follow）。
    """
    rid = _merge_run_id(run_id, run_id_opt)
    if rid is None:
        rid = _default_active_run_id()
    raise typer.Exit(_open_run_inproc(rid, tape_path=tape, host=host, port=port))


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
    """复用 ``tars open`` 的 ``_open_run``（read-only attach + browser）。延迟 import 防循环。"""
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
