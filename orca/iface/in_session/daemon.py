"""orca/iface/in_session/daemon.py —— in-session shell 的 daemon（v5，hook-driven）。

回答「opencode/CC 主 session 怎么连到 Orca 的确定性编排？」：daemon **独占一个 run
的 tape**（ADR v2 D1=c + 铁律 1 扩展），**对外**暴露 observe/next 两 RPC（单一接口，
铁律 8），**对内**委托 ``orca.run.step.advance_step`` 原子决策（D-v4-1：observe 只缓存
output 不落盘，next 一次原子 emit ``[node_completed, route_taken, node_started]``，
消除中断悬空态 A）。

推进由 **hook 驱动**（非模型调工具）：模型不调任何 Orca 工具，只接收注入的节点 prompt
并用自带 subagent 执行；hook 在 turn 结束时推进。两宿主前端（v5）：
  - **opencode**：daemon = 主动 SSE 订阅者，连 ``opencode serve`` 的 ``/event``，见
    ``session.next.tool.success``(tool=task)→observe、``session.idle``→next+``prompt_async``
    注入下一 prompt（Demo 5 实测 3-turn 循环可靠）。
  - **CC**：daemon = 被动 Unix socket，hook 脚本（Stop/PostToolUse）经 socket 调
    observe/next（D-v4-3=b：不开 MCP stdio，模型不调 Orca 工具）。

护栏（ADR v2 §2）：flock 独占 + pid 探活 + 仅本地 FS + ``Tape(resume=True)`` 半写恢复 +
宿主存活检测（孤儿锁反向）+ cleanup（atexit/SIGTERM 幂等）。

不在此模块：CLI 命令面（``cli.py``）、CC hook 脚本模板（``cli.py start`` 生成）。
"""

from __future__ import annotations

import atexit
import fcntl
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import requests

from orca.events.bus import EventBus
from orca.events.tape import Tape
from orca.run.lifecycle import make_workflow_failed, now_monotonic
from orca.run.step import InSessionError, advance_step

if TYPE_CHECKING:
    from orca.schema.workflow import Workflow

logger = logging.getLogger(__name__)


class InSessionDaemon:
    """独占一个 run 之 tape 的 daemon（v5 hook-driven）。

    生命周期：构造（抢 flock + 半写恢复 + pid）→ ``run_opencode(...)`` / Unix socket
    服务 → 退出 ``cleanup()``。
    """

    def __init__(
        self,
        wf: Workflow,
        tape_path: Path,
        run_id: str,
        inputs: dict[str, Any] | None = None,
    ) -> None:
        self.wf = wf
        self.tape_path = Path(tape_path)
        self.run_id = run_id
        self.inputs = inputs or {}
        self._lock_fd: Any = None
        self._cleaned = False
        self._start_ts = now_monotonic()
        self._pending_output: str | None = None   # observe 缓存（D-v4-1：不落盘）
        self._host_alive_ts = now_monotonic()
        self._pid_path = self.tape_path.with_suffix(self.tape_path.suffix + ".pid")
        self._lock_path = self.tape_path.with_suffix(self.tape_path.suffix + ".lock")

        self._acquire()             # flock + pid 探活（fail loud）
        self.tape = Tape(self.tape_path, run_id=run_id, resume=True)   # 半写恢复
        self.bus = EventBus(self.tape)

        atexit.register(self.cleanup)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    # ── 启动护栏 ──────────────────────────────────────────────────────────
    def _acquire(self) -> None:
        self.tape_path.parent.mkdir(parents=True, exist_ok=True)
        if self._pid_path.exists():
            try:
                old = int(self._pid_path.read_text().strip())
                os.kill(old, 0)
                raise InSessionError(
                    f"tape {self.tape_path} 已被存活 daemon (pid={old}) 占用"
                )
            except (ValueError, ProcessLookupError, PermissionError):
                logger.warning("清除孤儿 pid 文件 %s", self._pid_path)
                self._pid_path.unlink(missing_ok=True)
        self._lock_fd = open(self._lock_path, "w")
        try:
            fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise InSessionError(
                f"无法对 {self.tape_path} 取得 flock（另一进程持有；"
                f"NFS/网络盘不支持，见 ADR v2 I3.3）"
            ) from e
        self._pid_path.write_text(str(os.getpid()))

    def _on_signal(self, signum: int, _frame: Any) -> None:
        logger.warning("daemon 收到信号 %s，cleanup 后退出", signum)
        self.cleanup()
        sys.exit(128 + signum)

    # ── 对外两 RPC（D-v4-1：observe 缓存 / next 原子委托）──────────────────
    def observe(self, output: str | None) -> dict[str, Any]:
        """缓存 subagent 输出（不落盘，D-v4-1）。无 running 节点时幂等吞 + warn（Q13②）。"""
        if output is None:
            return {"ok": True, "note": "no output, ignored"}
        self._pending_output = output
        self._host_alive_ts = now_monotonic()
        logger.info("observe 缓存 output (run=%s, len=%d)", self.run_id, len(output))
        return {"ok": True}

    async def next(self) -> dict[str, Any]:
        """推进：委托 advance_step(output=cached) 一次原子决策 → 逐条 emit → 清缓存。

        advance_step 内部三分支（bootstrap/advance/idempotent-replay），见 SPEC §2.1。
        """
        output = self._pending_output
        self._pending_output = None
        try:
            result = advance_step(
                self.tape, self.wf, output=output,
                inputs=self.inputs, run_id=self.run_id,
                elapsed=now_monotonic() - self._start_ts,
            )
            for emit in result.emits:                   # 原子批量 emit（反例 A 消除）
                await self.bus.emit(emit.type, emit.data, node=emit.node)
        except InSessionError as e:
            return await self._fail(e)
        self._host_alive_ts = now_monotonic()
        reply: dict[str, Any] = {"done": result.done}
        if result.node:
            reply["node"] = result.node
        if result.prompt:
            reply["prompt"] = result.prompt
        if result.reason:
            reply["reason"] = result.reason
        return reply

    async def _fail(self, exc: Exception) -> dict[str, Any]:
        """fail loud：落 ``workflow_failed`` 终态到 tape（单真相源），再返错误信封。"""
        error_type = "in_session_error" if isinstance(exc, InSessionError) else "internal_error"
        logger.exception("next 推进失败，emit workflow_failed (run=%s)", self.run_id)
        try:
            t, d = make_workflow_failed(error_type, str(exc))
            await self.bus.emit(t, d)
        except Exception:
            logger.exception("emit workflow_failed 也失败（tape 可能已坏）")
        return {"done": True, "reason": f"failed: {exc}"}

    # ── opencode 前端：SSE 订阅自驱动（Demo 5 验证形态）───────────────────
    def run_opencode(
        self,
        base_url: str,
        session_id: str,
        model: dict[str, str],
        auth: tuple[str, str] | None = None,
        idle_timeout_s: int = 600,
    ) -> None:
        """连 opencode serve 的 /event，hook 驱动循环。

        bootstrap：next() 取 entry prompt → prompt_async 注入。
        循环：tool.success(task)→observe；session.idle→next→prompt_async。
        终止：next 返 done / SSE 断 / 宿主心跳超时（孤儿锁）。
        ``auth``：opencode serve 的 basic auth (user, password)。
        """
        import asyncio
        asyncio.run(self._opencode_loop(base_url, session_id, model, auth, idle_timeout_s))

    async def _opencode_loop(
        self, base_url: str, session_id: str, model: dict[str, str],
        auth: tuple[str, str] | None, idle_timeout_s: int,
    ) -> None:
        sess = requests.Session()
        if auth:
            sess.auth = auth
        prompt_url = f"{base_url}/session/{session_id}/prompt_async"
        msg_url = f"{base_url}/session/{session_id}/message"
        # bootstrap
        reply = await self.next()
        if reply.get("done"):
            logger.info("workflow 已终态，无需推进 (run=%s)", self.run_id)
            return
        r = sess.post(prompt_url, json=_prompt_body(reply["prompt"], model), timeout=30)
        logger.info("bootstrap 注入 entry prompt (run=%s, http %s)", self.run_id, r.status_code)
        if r.status_code >= 400:
            logger.error("bootstrap prompt_async 失败：%s", r.text[:200])
        # SSE 订阅：idle 驱动（拉 assistant 文本作 output → observe+next+注入）。
        # 不依赖 task 工具——模型用 write/task/任意工具都行（Q7 鲁棒性）。
        resp = sess.get(f"{base_url}/event", stream=True, timeout=None)
        for raw in resp.iter_lines(decode_unicode=True):
            if self._host_stale(idle_timeout_s):       # 孤儿锁：宿主心跳超时
                logger.warning("宿主心跳超时 %ss，daemon 退出 (run=%s)", idle_timeout_s, self.run_id)
                break
            line = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except Exception:
                continue
            if ev.get("type") == "session.idle":
                self._host_alive_ts = now_monotonic()
                self.observe(_last_assistant_text(sess, msg_url))
                reply = await self.next()
                if reply.get("done"):
                    logger.info("workflow 完成 (run=%s, reason=%s)", self.run_id, reply.get("reason"))
                    break
                pr = sess.post(prompt_url, json=_prompt_body(reply["prompt"], model), timeout=30)
                logger.info("注入下一节点 prompt (run=%s, http %s)", self.run_id, pr.status_code)
        self.cleanup()

    def _host_stale(self, timeout_s: int) -> bool:
        return (now_monotonic() - self._host_alive_ts) > timeout_s

    # ── 退出 cleanup（幂等）──────────────────────────────────────────────
    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            self.bus.close()
        except Exception:
            logger.exception("bus.close 异常")
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                self._lock_fd.close()
            except Exception:
                logger.exception("释放 flock 异常")
        self._pid_path.unlink(missing_ok=True)


# ── opencode 辅助（SPEC §2.5 observe 入参：idle 时拉最后 assistant 文本）─────

def _last_assistant_text(sess: requests.Session, msg_url: str) -> str | None:
    """GET /session/{id}/message，返回最后一条 assistant 消息的 text 拼接（节点 output）。

    opencode 1.14 消息结构：``{info:{role}, parts:[{type:text,text},...]}``。
    """
    try:
        msgs = sess.get(msg_url, timeout=15).json()
    except Exception:
        logger.exception("拉 session message 失败 %s", msg_url)
        return None
    arr = msgs if isinstance(msgs, list) else msgs.get("data", [])
    for m in reversed(arr):
        if m.get("info", {}).get("role") == "assistant":
            parts = [p.get("text", "") for p in m.get("parts", [])
                     if isinstance(p, dict) and p.get("type") == "text"]
            if parts:
                return "\n".join(parts)
    return None


def _prompt_body(prompt: str, model: dict[str, str]) -> dict[str, Any]:
    """构造 prompt_async 请求体（model={providerID,modelID}，Demo 5 验证）。"""
    return {
        "parts": [{"type": "text", "text": prompt}],
        "model": {"providerID": model["providerID"], "modelID": model["modelID"]},
    }
