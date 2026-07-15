"""orca/iface/in_session/daemon.py —— in-session shell 无头 CI daemon（ADR v3 I3.3a）。

**降级**（SPEC v7）：主 UX 改用 per-call 薄 CLI（``cli.py bootstrap/next``，I3.3b）。
本模块保留为**无头 CI / 长跑批处理**形态：连 ``opencode serve`` SSE 自驱动跑无人值守
workflow，不依赖交互界面。daemon 持续持锁（I3.3a：pid 探活 + 孤儿锁接管）。

回答「无头 CI 怎么跑 in-session workflow？」：daemon **独占一个 run 的 tape**（ADR v3
+ 铁律 1 扩展），构造时抢 flock + 写 pid 文件 + ``Tape(resume=True)`` 半写恢复，
``run_opencode`` 连 opencode serve SSE 自驱动推进。

护栏（ADR v3 §2 I3.3a）：flock 独占 + pid 探活 + 仅本地 FS + 半写恢复 +
宿主存活检测（孤儿锁反向）+ cleanup（atexit/SIGTERM 幂等）。

不在此模块：
  - 主 UX 路径（``cli.py bootstrap/next/stop/status``，per-call CLI 形态）
  - opencode plugin 模板（``templates/opencode/``，零业务逻辑哑传输）
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
from orca.iface.in_session._step_io import apply_step_result, fail_in_session
from orca.run.lifecycle import now_monotonic
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
        """推进：委托 advance_step(output=cached) 一次原子决策 → emit_batch 落整批 → 清缓存。

        advance_step 内部三分支（bootstrap/advance/idempotent-replay），见 SPEC §2.1。
        emit 经 ``apply_step_result`` 单次 write 原子化（v5 §8 step 5b：反旧逐条 emit——SIGTERM
        落批内 N 与 N+1 之间会留半截 tape → resume state_corrupt；铁律 12）。
        """
        output = self._pending_output
        self._pending_output = None
        try:
            result = advance_step(
                self.tape, self.wf, output=output,
                inputs=self.inputs, run_id=self.run_id,
                elapsed=now_monotonic() - self._start_ts,
            )
        except InSessionError as e:
            return await fail_in_session(self.bus, e)
        reply = await apply_step_result(self.bus, result)
        self._host_alive_ts = now_monotonic()
        return reply

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
