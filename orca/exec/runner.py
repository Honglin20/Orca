"""runner.py —— CLIRunner：通用 asyncio 子进程基础设施（claude / 未来 CLI backend 共享）。

回答「怎么 spawn 一个 headless CLI、喂 stdin、逐行读 stdout、超时杀掉？」：
SPEC §2.2 / §2.5 / §4.4。重写自 AgentHarness ``_cli_subprocess.py`` 的子进程模式
（**不迁移代码**，采纳 stdin-pump / readline / SIGTERM→SIGKILL 协议事实）。

职责（SPEC §4.4）：
  - ``spawn``：``create_subprocess_exec``（非 shell），三 PIPE；``cli_path`` 多 token
    经 ``shlex.split`` 拆 argv。
  - **stdin pump**：写 UTF-8 prompt bytes → ``drain()`` → ``close()`` → ``wait_closed()``
    （claude 靠 EOF 知道输入结束，SPEC §2.2）。
  - **stdout 逐行 readline**：``asyncio.StreamReader.readline()`` 按 ``\\n`` 分割，
    decode UTF-8 + ``rstrip``，空行跳过，每行 yield；同时检测 result 行回调 ``on_result``。
  - **stderr 累积**：分块读，用于错误诊断（非事件流）。
  - **超时**：``asyncio.wait_for(proc.wait(), timeout)`` 超时 → SIGTERM → 等 10s grace
    → 仍存活 SIGKILL；kill **单进程**（非 killpg，SPEC §2.5）。
  - **result 行检测在 CLIRunner**（不在 translator）：``json.loads(line)`` 后若
    ``type=="result"`` → 回调 ``on_result(raw_result, usage, cost)``（SPEC §4.4 关键约束）。

设计规则：
  - **json_decode 不 fail loud**（SPEC §6 例外）：非 JSON 行 debug log + 跳过
    （claude stream-json 偶发非 JSON 心跳行）。
  - ``stream()`` 是 async generator：spawn → pump → readline 循环 → 超时/退出判定
    全在内部，调用方只 ``async for line in runner.stream()``。
  - 属性 ``timed_out`` / ``exit_code`` / ``elapsed``：executor 据此做有序错误判定（SPEC §2.4）。

依赖单向：本模块只依赖标准库（asyncio/shlex/json/signal/os/time/logging），不依赖
schema/events/profiles/run/compile。它是纯子进程基础设施，backend 无关。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

logger = logging.getLogger(__name__)

# 超时后 SIGTERM 的 grace 秒数；仍存活才 SIGKILL（SPEC §2.5）。
_KILL_GRACE_SECONDS = 10.0
# stderr 累积上限（防异常 claude 喷爆内存；错误诊断只需末尾片段）。
_STDERR_MAX_BYTES = 64 * 1024


# on_result 回调签名：(raw_result 文本, usage dict, cost_usd float, is_error bool)。
# translator 仍翻译该行产 agent_usage，但 result 文本经此回调交 executor（保持 translator 纯函数）。
# is_error 透传：executor 据 result.is_error 做 stream 错误判定（SPEC §2.4 第 3 项 / §6）。
# 注：SPEC §4.4 草图写的是 ``(str, dict, float)``，这里加 ``is_error`` 是为满足 §2.4 的
# 有序错误判定（否则 executor 无法区分 success result 与 error result）—— 见 release note。
OnResult = Callable[[str, dict[str, Any], float, bool], None]


@dataclass
class SpawnConfig:
    """spawn 一个 CLI backend 所需的全部输入（SPEC §4.4）。

    ``cli_path`` 是 ``profile.resolve_cli_path()`` 返回的**未拆分原始串**
    （如 ``"ccr code"``），CLIRunner 内部 ``shlex.split``。flags 来自 profile，
    extra_args 是 executor 动态拼的 ``--model`` / ``--allowed-tools`` 等。
    """

    cli_path: str
    flags: tuple[str, ...]
    extra_args: list[str] = field(default_factory=list)
    mcp_flag_args: list[str] = field(default_factory=list)
    prompt: str = ""
    prompt_channel: Literal["stdin", "argv"] = "stdin"
    env_overlay: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass
class CliRunResult:
    """CLIRunner 一次运行的最终判定（SPEC §4.4）。

    executor 据此做有序错误判定（SPEC §2.4）：timed_out → exit_code → result.is_error → 无 result。
    """

    exit_code: int = -1  # -1 = 未知（超时强杀等）
    stderr: str = ""
    timed_out: bool = False
    elapsed: float = 0.0


class CLIRunner:
    """通用 asyncio 子进程 runner（SPEC §4.4）。

    用法（executor 视角）::

        runner = CLIRunner(cfg, on_result=lambda r, u, c: holder.update(...))
        async for line in runner.stream():
            for ev in translator(line, session_id):
                yield ev
        if runner.timed_out: raise ExecError(phase="timeout", ...)
    """

    def __init__(self, cfg: SpawnConfig, on_result: OnResult | None = None) -> None:
        self._cfg = cfg
        self._on_result = on_result
        self._result = CliRunResult()
        self._start_time: float | None = None
        # phase 11 §4.2：当前子进程句柄（send_sigint 用）。stream() 期间赋值，结束清空。
        self._proc: asyncio.subprocess.Process | None = None
        # phase 11 §4.2：是否被用户 SIGINT 中断（send_sigint 置 True）。
        # ClaudeExecutor 据此区分「用户主动中断」与「子进程崩」（前者不当 error）。
        self._was_interrupted: bool = False

    # ── 对外属性（executor 判定用）────────────────────────────────────────────

    @property
    def timed_out(self) -> bool:
        return self._result.timed_out

    @property
    def was_interrupted(self) -> bool:
        """是否被用户 SIGINT 中断（phase 11 §4.2）。

        ``send_sigint`` 置 True；ClaudeExecutor 据此跳过 spawn 错误判定（用户主动中断
        不是 transient error，retry 也应短路，SPEC §9.5.2）。
        """
        return self._was_interrupted

    @property
    def exit_code(self) -> int:
        return self._result.exit_code

    @property
    def elapsed(self) -> float:
        return self._result.elapsed

    @property
    def stderr(self) -> str:
        return self._result.stderr

    # ── 核心流 ────────────────────────────────────────────────────────────────

    async def stream(self) -> AsyncIterator[str]:
        """spawn → stdin pump → readline 循环 → yield 每行 stdout（已 decode + rstrip）。

        - 检测 result 行（``json.loads`` 后 ``type=="result"``）→ 回调 ``on_result``。
        - 非 JSON 行：debug log + 跳过（SPEC §6 json_decode 例外，不 fail loud）。
        - 超时：SIGTERM → 10s grace → SIGKILL（SPEC §2.5）。
        - 退出后记 exit_code / elapsed；stderr 累积在 ``CliRunResult.stderr``。

        超时或子进程异常结束，生成器正常结束（属性已填好），由 executor 据属性判错。
        """
        self._start_time = time.monotonic()
        argv = self._build_argv()

        # env：base=os.environ 叠加 cfg.env_overlay（profile 声明的前缀，SPEC §2.6）。
        env = dict(os.environ)
        env.update(self._cfg.env_overlay)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        # phase 11 §4.2：暴露 proc 句柄给 send_sigint（用户 Ctrl+G 中断用）。
        self._proc = proc

        # stderr 累积 task（与 stdout readline 并行；超时/结束都收尾）。
        stderr_chunks: list[bytes] = []
        stderr_task = asyncio.create_task(self._drain_stderr(proc, stderr_chunks))

        try:
            # stdin pump（prompt_channel=stdin 时写 prompt 后 close；claude 靠 EOF 知输入结束）。
            if self._cfg.prompt_channel == "stdin":
                await self._pump_stdin(proc, self._cfg.prompt)

            # readline 循环 + 超时守卫。超时语义：**两行之间**的间隔（每行 readline 各自包
            # wait_for），非总墙钟——慢但持续的流（token 间隔 < timeout）不会误杀；停滞的
            # 进程（无新行）会触发。phase 4 ClaudeExecutor 当前传 timeout=None（不限制），
            # phase 5 orchestrator 若需「总墙钟」超时可在 exec 外层包 asyncio.wait_for。
            try:
                async for line in self._readlines(proc):
                    yield line
            except asyncio.TimeoutError:
                await self._handle_timeout(proc)
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                # 不在此 _finalize：finally 块统一收尾（避免双重调用）。
                return

            # 流自然结束：等 proc 退出 + stderr drain 完。
            await proc.wait()
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
            self._finalize(proc, stderr_chunks)

    # ── argv ─────────────────────────────────────────────────────────────────

    def _build_argv(self) -> list[str]:
        """拼 argv：``shlex.split(cli_path)`` + flags + extra_args + mcp_flag_args。

        prompt_channel=argv 时 prompt 进 argv 末尾（一期 claude 走 stdin，此分支预留）。
        """
        argv: list[str] = list(shlex.split(self._cfg.cli_path))
        argv.extend(self._cfg.flags)
        argv.extend(self._cfg.extra_args)
        argv.extend(self._cfg.mcp_flag_args)
        if self._cfg.prompt_channel == "argv":
            argv.append(self._cfg.prompt)
        return argv

    # ── stdin pump ────────────────────────────────────────────────────────────

    async def _pump_stdin(self, proc: asyncio.subprocess.Process, prompt: str) -> None:
        """写 UTF-8 prompt bytes → drain → close → wait_closed（SPEC §2.2）。

        claude 靠 stdin EOF 知道输入结束；写完立即 close，不等 claude 读取（OS buffer 兜底）。
        """
        assert proc.stdin is not None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            # claude 提前退出（如参数错）→ 写失败，不阻断后续 stdout/stderr 读取（错误会
            # 经 exit_code != 0 兜住，SPEC §2.4）。记 warning 以可见。
            logger.warning("写 claude stdin 失败（可能子进程已退出）：%s", e)
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, RuntimeError):
                # close/wait_closed 在子进程已退出时偶发；忽略（exit_code 会兜底）。
                pass

    # ── stdout readline（带 result 检测 + 超时守卫）────────────────────────────

    async def _readlines(self, proc: asyncio.subprocess.Process) -> AsyncIterator[str]:
        """逐行 yield stdout（decode + rstrip "\\n"），空行跳过。

        result 行检测在此（非 translator，SPEC §4.4 关键约束）：``json.loads`` 后
        ``type=="result"`` → 回调 ``on_result``。非 JSON 行 debug log + 跳过（铁律 4 例外）。

        超时守卫：每行 readline 包 ``wait_for(timeout)``——超时抛 ``TimeoutError``，
        由 ``stream()`` 捕获走 SIGTERM 路径。timeout=None 时不设守卫（不限时）。
        """
        assert proc.stdout is not None
        timeout = self._cfg.timeout
        while True:
            if timeout is not None:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            else:
                raw = await proc.stdout.readline()
            if not raw:
                # EOF：claude 关闭 stdout（正常结束或崩溃）
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line:
                continue  # 空行跳过（SPEC §2.2）
            self._maybe_fire_on_result(line)
            yield line

    def _maybe_fire_on_result(self, line: str) -> None:
        """检测 result 行 → 回调 on_result（SPEC §4.4 关键约束）。

        非 JSON 行：debug log + 跳过（claude 偶发非 JSON 心跳行，SPEC §6 json_decode 例外）。
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("claude stdout 非 JSON 行（跳过）：%s", line[:200])
            return
        if not isinstance(obj, dict) or obj.get("type") != "result":
            return
        if self._on_result is None:
            return
        raw_result = obj.get("result", "")
        usage = obj.get("usage") or {}
        cost = obj.get("total_cost_usd") or 0.0
        is_error = bool(obj.get("is_error", False))
        try:
            self._on_result(raw_result, usage, cost, is_error)
        except Exception:  # noqa: BLE001 - on_result 是调用方回调，其异常不应阻断流
            logger.exception("on_result 回调抛异常（已忽略，不阻断 stdout 流）")

    # ── stderr drain ──────────────────────────────────────────────────────────

    async def _drain_stderr(
        self, proc: asyncio.subprocess.Process, chunks: list[bytes]
    ) -> None:
        """分块读 stderr 累积到 chunks（错误诊断用，非事件流，SPEC §2.2）。

        上限 ``_STDERR_MAX_BYTES``：异常 claude 喷爆时只留末尾（诊断够用）。
        """
        assert proc.stderr is not None
        total = 0
        try:
            while True:
                block = await proc.stderr.read(4096)
                if not block:
                    return
                if total < _STDERR_MAX_BYTES:
                    chunks.append(block)
                    total += len(block)
                # 超上限：继续读但丢弃（保持 drain 不阻塞 claude 的 stderr 写）。
        except asyncio.CancelledError:
            # stream() 结束/超时时主动 cancel；正常收尾。
            raise

    # ── 超时处理 ──────────────────────────────────────────────────────────────

    async def _handle_timeout(self, proc: asyncio.subprocess.Process) -> None:
        """超时 → SIGTERM → 10s grace → SIGKILL（SPEC §2.5）。

        kill **单进程**（非 killpg / 非 setsid，与 AgentHarness 一致）。
        """
        self._result.timed_out = True
        logger.warning("claude 子进程超时（timeout=%ss），发送 SIGTERM", self._cfg.timeout)
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return  # 已退出
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
            return  # grace 内退出
        except asyncio.TimeoutError:
            logger.warning(
                "claude 子进程 SIGTERM 后 %ss 未退出，发送 SIGKILL", _KILL_GRACE_SECONDS
            )
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass

    # ── 收尾 ─────────────────────────────────────────────────────────────────

    def _finalize(
        self, proc: asyncio.subprocess.Process, stderr_chunks: list[bytes]
    ) -> None:
        """填 CliRunResult（exit_code / stderr / elapsed）。"""
        self._result.exit_code = (
            proc.returncode if proc.returncode is not None else -1
        )
        stderr_bytes = b"".join(stderr_chunks)
        # 保留末尾 _STDERR_MAX_BYTES（错误诊断只需尾部）
        if len(stderr_bytes) > _STDERR_MAX_BYTES:
            stderr_bytes = stderr_bytes[-_STDERR_MAX_BYTES:]
        self._result.stderr = stderr_bytes.decode("utf-8", errors="replace")
        if self._start_time is not None:
            self._result.elapsed = time.monotonic() - self._start_time
        # phase 11 §4.2：清 proc 句柄（stream 结束，proc 不再可 SIGINT）。
        # **不**复位 _was_interrupted：executor 在 stream() 返回后读此标志判定「用户中断 vs 崩」，
        # 复位会丢信号。CLIRunner 是一次性使用（ClaudeExecutor.exec 每次 new），不复用——
        # 见 send_sigint docstring 的 one-use 契约。
        self._proc = None

    # ── 用户中断（phase 11 §4.2）─────────────────────────────────────────────

    def send_sigint(self) -> bool:
        """向子进程发 SIGINT（用户 Ctrl+G + CONTINUE 触发中断时调）。

        -p 路线：SIGINT 让 claude 优雅退出（写最后的 stream-json result 行后关闭）。
        比 kill -9 友好（不丢失 buffered output）。executor 据 ``was_interrupted`` 把
        非零退出码判为「用户中断」而非「子进程崩」（SPEC §4.2）。

        返回是否真的发了信号（proc 存活时 True；已退出 / 未启动时 False，幂等）。
        线程安全：从 TUI loop 调，proc.send_signal 是同步 syscall（asyncio subprocess
        的 Process 对象跨 loop 安全，底层是 OS pid）。
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False  # 未启动 / 已退出
        self._was_interrupted = True
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            # 极端 race：刚检查存活，发信号前已退出 → 幂等返回 False。
            return False
        return True
