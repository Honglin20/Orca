"""approval_broker.py —— in-session 权限审批 web 桥 broker（SPEC §3.2）。

回答「in-session workflow 运行时 CC 触发 PermissionRequest，怎么把它推到 web 让用户
审批，且不破坏 AttachedRunHandle 只读契约？」：进程级 singleton broker，per-approval
``asyncio.Future`` + ``threading.Lock`` first-wins，与 run/tape 解耦——独立 CC↔web 决策
通道（非 gate），不碰 tape、不碰 HumanGateHandler。

**为什么是新 broker（非 gate，SPEC §1.1）**：``HumanGateHandler`` 绑 in-process run handle
且**写 tape**；in-session 的 run 是 ``AttachedRunHandle``（只读 follow tape，不该写 tape）。
审批是 CC↔工具决策，非 workflow 事件，不该走 tape/gate 通道。故新增 broker。

设计规则（SPEC §3.2 / §3.3 / §4.3）：
  - **进程级 singleton**：FastAPI ``app.lifespan`` 构造、shutdown 清理 pending + 广播
    ``approval_resolved(resolved_by:"shutdown")``。pending 池在内存（重启即弃，SPEC §3.2）。
  - **per-approval Future + Lock**：``async request`` ``await fut``（不阻塞 uvicorn worker），
    ``resolve`` 同步 first-wins（Lock 保护，仿 ``handler.py:79``）。
  - **approval_id = uuid4**（禁 timestamp 类可碰撞方案，N1）。
  - **BROKER_TIMEOUT = ORCA_APPROVAL_TIMEOUT - 5s**（比 hook 短，保证 broker 先 resolve）。
  - **yolo 开关**：内存 + ``~/.orca/approval-yolo.json``（全局，重启恢复）。on 时即时 allow。
  - **run-scoped 投递**：``subscribe(run_id) -> AsyncIterator[ApprovalEvent]`` 给
    ws_handler 第二条 pump（不经 handle.bus）；只推订阅了该 run_id 的连接。
  - **HTTP disconnect（P1）**：``request`` 内 ``asyncio.gather(wait_for(fut, BROKER_TIMEOUT),
    _disconnect_poller(req, fut))`` 组合超时与断连两条竞速——断连则 ``resolve("aborted",
    "disconnect")``。
  - **late respond（N2）**：first-wins；后续 respond 返 ``ok=False`` + emit 独立
    ``approval_resolved_late`` 审计事件（仅审计可见，不翻盘 UI）。

依赖单向：本模块仅 import ``orca.gates.http_endpoint.resolve_session_context`` + 标准库 +
fastapi.Request（断连探测）；**不 import** ``orca.gates.handler`` / ``orca.tape*`` /
``orca.exec.*`` / ``orca.events.bus``（grep 守门 N11）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Final
from uuid import uuid4

from orca.gates.http_endpoint import resolve_session_context

logger = logging.getLogger(__name__)

# SPEC §3.4 / §3.5：ORCA_APPROVAL_TIMEOUT 默认 600s（语义级「人未及时响应」）。
_DEFAULT_TIMEOUT: Final[float] = 600.0
# SPEC §3.2：broker timeout 比 hook 短 5s，保证 broker 先 resolve、hook 收到响应。
_BROKER_MARGIN: Final[float] = 5.0
_DISCONNECT_POLL_INTERVAL: Final[float] = 1.0
_DEFAULT_POLICY: Final[str] = "allow"  # SPEC §3.5
_VALID_POLICIES: Final[tuple[str, ...]] = ("allow", "ask", "deny")

_YOLO_PATH = Path.home() / ".orca" / "approval-yolo.json"

# SPEC §4.3 redact 默认模式（N3）。正则大小写不敏感。``ORCA_APPROVAL_REDACT_PATTERNS``
# env 逗号分隔追加。**非穷尽**——远程审批必须配 auth（§8），不可仅依赖 redact。
#
# BUG B 修复（2026-08-05 test-agent 发现）：``Authorization``/``Cookie`` header 值含空格
# （如 ``Bearer abcdef``），原 ``[^\s&,]+`` 在空白截断 → token 主体泄露。改 ``(?im)`` 多行
# +行尾锚定 ``.+$``，覆盖完整字段值；多行模式下 ``$`` 锚到行尾不跨字段。
_DEFAULT_REDACT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?im)(authorization|cookie)\s*[:=]\s*.+$"),
    re.compile(r"(?i)sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"(?i)sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)://[^:/@\s]+:[^:/@\s]+@"),  # URL user:pass@
)


def _compile_redact_patterns() -> tuple[re.Pattern[str], ...]:
    """默认 + ``ORCA_APPROVAL_REDACT_PATTERNS``（逗号分隔追加）。非法正则 skip+warn。"""
    extra_raw = os.environ.get("ORCA_APPROVAL_REDACT_PATTERNS", "")
    extras: list[re.Pattern[str]] = []
    for piece in extra_raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            extras.append(re.compile(piece))
        except re.error as e:
            logger.warning(
                "ORCA_APPROVAL_REDACT_PATTERNS 非法正则 skip=%r: %s", piece, e,
            )
    return _DEFAULT_REDACT_PATTERNS + tuple(extras)


def _redact(obj: Any, patterns: tuple[re.Pattern[str], ...]) -> Any:
    """递归 redact dict/list/str。env 名含 _TOKEN|_KEY|_PASSWORD|_SECRET → '***'。

    字符串字段额外套正则模式（Authorization header / sk-ant- / URL user:pass@）。
    返回深拷贝（不改原 obj）。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and re.search(
                r"(?i)(_|^)(token|key|password|secret)(_|$)", k,
            ):
                out[k] = "***"
            else:
                out[k] = _redact(v, patterns)
        return out
    if isinstance(obj, list):
        return [_redact(v, patterns) for v in obj]
    if isinstance(obj, str):
        s = obj
        for pat in patterns:
            s = pat.sub("***", s)
        return s
    return obj


@dataclass
class Approval:
    """一条 pending 审批。``fut`` 由 broker 创建并 await。"""

    id: str
    run_id: str
    tool: str
    tool_input_redacted: dict | list
    created_at: float
    fut: "asyncio.Future[tuple[str, str]]"
    # 记录最早 resolve 的（answer, source）——first-wins；后续 late respond 用。
    resolved: tuple[str, str] | None = None


@dataclass
class ApprovalEvent:
    """broker → 前端 WS 事件（subscribe 队列里流转）。``kind`` 对齐 SPEC §4.3 类型。"""

    kind: str  # approval_requested | approval_resolved | approval_resolved_late | yolo_changed
    payload: dict


def _load_yolo_persisted() -> bool:
    """读 ~/.orca/approval-yolo.json（best-effort，损坏 → False）。"""
    try:
        if not _YOLO_PATH.is_file():
            return False
        raw = json.loads(_YOLO_PATH.read_text(encoding="utf-8"))
        return bool(raw.get("yolo", False))
    except (OSError, json.JSONDecodeError):
        logger.warning("approval yolo 持久文件读失败，视作 off（%s）", _YOLO_PATH)
        return False


def _save_yolo_persisted(value: bool) -> None:
    """best-effort 写 ~/.orca/approval-yolo.json（失败 warn 不阻断）。"""
    try:
        _YOLO_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _YOLO_PATH.with_name(_YOLO_PATH.name + ".tmp")
        tmp.write_text(
            json.dumps({"yolo": value}, ensure_ascii=False), encoding="utf-8",
        )
        os.replace(tmp, _YOLO_PATH)
    except OSError as e:
        logger.warning("approval yolo 持久写失败（%s）：%s", _YOLO_PATH, e)


class ApprovalBroker:
    """进程级 singleton broker（SPEC §3.2）。

    生命周期：``app.lifespan`` 启动时构造、关闭时调 ``shutdown()`` 清理 pending +
    广播 ``approval_resolved(resolved_by:"shutdown")``。
    """

    def __init__(self, registry, *, timeout: float | None = None) -> None:
        """:param registry: ``SessionContextRegistry``（``resolve_session_context`` 用）。"""
        self._registry = registry
        self._timeout = float(
            timeout
            if timeout is not None
            else _env_timeout()
        )
        self._broker_timeout = max(1.0, self._timeout - _BROKER_MARGIN)
        self._policy = _env_policy()
        self._redact_patterns = _compile_redact_patterns()
        # approval_id → Approval。
        self._pending: dict[str, Approval] = {}
        # run_id → 订阅者队列列表（broker.publish 推到每条 queue）。
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # first-wins / late-respond 串行化（resolve 可能从 ws_handler task + disconnect
        # poller task 并发调用——必须显式锁）。
        self._lock = threading.Lock()
        # yolo 全局开关（SPEC §3.3）：启动时从持久文件恢复。
        self._yolo: bool = _load_yolo_persisted()

    # ── 公开属性 ────────────────────────────────────────────────────────────

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def broker_timeout(self) -> float:
        return self._broker_timeout

    @property
    def policy(self) -> str:
        return self._policy

    @property
    def yolo(self) -> bool:
        return self._yolo

    @property
    def registry(self):
        return self._registry

    # ── 生命周期 ────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """SPEC §3.2 / §6：lifespan 关闭时清理 pending + 广播 shutdown resolved。"""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for ap in pending:
            # 唤醒 await fut（hook 已 TCP 失败走 ask，此处不 emit allow）。
            if not ap.fut.done():
                ap.fut.set_result(("aborted", "shutdown"))
            self._publish(
                ap.run_id,
                ApprovalEvent(
                    "approval_resolved",
                    {
                        "approval_id": ap.id,
                        "behavior": "aborted",
                        "resolved_by": "shutdown",
                    },
                ),
            )

    # ── 核心路径 ────────────────────────────────────────────────────────────

    async def request(
        self,
        payload: dict[str, Any],
        http_request: Any | None = None,
    ) -> dict[str, Any]:
        """hook POST /approval 入口。返回 ``{behavior, approval_id, resolved_by}``。

        - ``resolve_session_context`` 未命中活跃 run → ``{behavior:"ask",
          resolved_by:"native-fallback"}``（SPEC §1.3 / §6）。
        - 命中 → 建 Approval；yolo on → 即时 allow；
          否则 ``gather(wait_for(fut, BROKER_TIMEOUT), _disconnect_poller)`` 竞速。
        """
        run_id, _node = resolve_session_context(self._registry, payload)
        if run_id == "unknown":
            # SPEC §6 / §7：未命中活跃 run → ask（行为同未装，不干扰日常 CC）。
            return {
                "behavior": "ask",
                "approval_id": None,
                "resolved_by": "native-fallback",
            }

        approval_id = uuid4().hex
        tool = str(payload.get("tool") or "<unknown>")
        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, (dict, list)):
            tool_input = {}
        tool_input_redacted = _redact(tool_input, self._redact_patterns)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[tuple[str, str]] = loop.create_future()
        approval = Approval(
            id=approval_id,
            run_id=run_id,
            tool=tool,
            tool_input_redacted=tool_input_redacted,
            created_at=time.time(),
            fut=fut,
        )
        with self._lock:
            self._pending[approval_id] = approval

        # 广播 approval_requested（即便下面 yolo 即时 resolve 也要先 publish requested，
        # 让前端有「瞬时 requested + resolved」可见性，SPEC §3.3 末段）。
        self._publish(
            run_id,
            ApprovalEvent(
                "approval_requested",
                {
                    "approval_id": approval_id,
                    "run_id": run_id,
                    "tool": tool,
                    "tool_input": tool_input_redacted,
                    "created_at": approval.created_at,
                },
            ),
        )

        # yolo on → 即时 allow（不阻塞）。
        if self._yolo:
            self._resolve_locked(approval_id, "allow", "yolo")
        else:
            # P1：``asyncio.wait_for`` 在超时会 cancel 底层 future，使后续 set_result 抛
            # InvalidStateError——故用手写 timer task + 直接 await fut，三路（user resolve /
            # timeout / disconnect）都经 ``_resolve_locked`` first-wins 设 fut 结果。
            timer_task = asyncio.create_task(
                self._timeout_timer(approval_id),
                name=f"orca-approval-timeout-{approval_id}",
            )
            disconnect_task: asyncio.Task | None = None
            if http_request is not None:
                disconnect_task = asyncio.create_task(
                    self._disconnect_poller(http_request, approval_id),
                    name=f"orca-approval-disconnect-{approval_id}",
                )
            try:
                await fut
            finally:
                for t in (timer_task, disconnect_task):
                    if t is not None and not t.done():
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass

        # fut 已被某路径 resolve（user/yolo/timeout/disconnect）。取结果；不应抛 CancelledError
        # （我们从未 cancel fut 本身——cancel 的是 timer/poller task）。
        final_answer, final_source = fut.result()
        # 出口 pop pending（让后续 late respond 走「unknown」分支；窗口期内的 late respond
        # 在 resolve/public _resolve_locked 内已 emit 审计事件）。
        with self._lock:
            self._pending.pop(approval_id, None)
        return {
            "behavior": final_answer,
            "approval_id": approval_id,
            "resolved_by": final_source,
        }

    def resolve(
        self, approval_id: str, answer: str, source: str = "web",
    ) -> dict[str, Any]:
        """前端 POST /approval/respond 入口。返回 ``{ok, approval_id, resolved_by}``。

        first-wins：已 resolved / 未知 → ``ok=False`` + emit ``approval_resolved_late``
        （仅审计可见，不翻盘，SPEC §3.2 N2）。
        """
        # 锁内只做「判 / set_result / 标记」最小临界区；锁外 publish（与 ``_resolve_locked``
        # 对称）—— ``_publish`` 内 ``put_nowait`` 不阻塞，但锁内调 queue 操作违反「锁内最小临界」
        # 惯例，且与 ``_resolve_locked`` 不一致增加推理负担。
        with self._lock:
            ap = self._pending.get(approval_id)
            if ap is None:
                logger.warning(
                    "approval_resolved_late: %s 未知或已清（source=%s answer=%s）",
                    approval_id, source, answer,
                )
                return {"ok": False, "approval_id": approval_id, "resolved_by": "unknown"}
            if ap.resolved is not None:
                # late respond（N2）：返 ok=False + 审计事件，不翻盘。
                prev_answer, prev_source = ap.resolved
                logger.warning(
                    "approval_resolved_late: %s 已 resolved(%s/%s)，late source=%s answer=%s",
                    approval_id, prev_source, prev_answer, source, answer,
                )
                run_id = ap.run_id
                late_payload = {
                    "approval_id": approval_id,
                    "answer": answer,
                    "prev_answer": prev_answer,
                    "prev_source": prev_source,
                    "note": "late-respond-after-resolve",
                }
                is_first = False
            else:
                # first-wins：设 fut 结果 + 标记 resolved（**不**立即 pop——让 request() 出口
                # 在 await fut 返回后 pop；同时让 late respond 在窗口内能查到 run_id）。
                ap.fut.set_result((answer, source))
                ap.resolved = (answer, source)
                run_id = ap.run_id
                is_first = True

        # 锁外 publish（防 lock 内 put_nowait 拉长临界区 + 与 _resolve_locked 对称）。
        if is_first:
            self._publish(
                run_id,
                ApprovalEvent(
                    "approval_resolved",
                    {
                        "approval_id": approval_id,
                        "behavior": answer,
                        "resolved_by": source,
                    },
                ),
            )
            return {"ok": True, "approval_id": approval_id, "resolved_by": source}
        # late 分支：emit 审计事件（不翻盘 UI）。
        self._publish(run_id, ApprovalEvent("approval_resolved_late", late_payload))
        return {
            "ok": False,
            "approval_id": approval_id,
            "resolved_by": prev_source,
        }

    def _resolve_locked(
        self, approval_id: str, answer: str, source: str,
    ) -> None:
        """锁内 first-wins resolve（timeout/disconnect/yolo 路径用）。

        与 ``resolve`` 公开方法共用 Lock——但 ``request`` 路径调用本方法时已持
        ``self._pending[approval_id]`` 的逻辑所有权；本方法仍 Lock 内取 / 判 / set_result，
        与 ``resolve`` 完全对称，故两条路径并发安全（任一赢家生效）。
        """
        with self._lock:
            ap = self._pending.get(approval_id)
            if ap is None or ap.resolved is not None:
                return  # 已被别人 resolve 或已清，no-op
            ap.fut.set_result((answer, source))
            ap.resolved = (answer, source)
            run_id = ap.run_id

        self._publish(
            run_id,
            ApprovalEvent(
                "approval_resolved",
                {
                    "approval_id": approval_id,
                    "behavior": answer,
                    "resolved_by": source,
                },
            ),
        )

    # ── disconnect 探测 ─────────────────────────────────────────────────────

    async def _timeout_timer(self, approval_id: str) -> None:
        """BROKER_TIMEOUT 到 → first-wins resolve(policy, "timeout")。

        被 cancel 不影响 approval（_resolve_locked 幂等 first-wins）。
        """
        try:
            await asyncio.sleep(self._broker_timeout)
            self._resolve_locked(approval_id, self._policy, "timeout")
        except asyncio.CancelledError:
            raise

    async def _disconnect_poller(
        self, http_request: Any, approval_id: str,
    ) -> None:
        """P1：每 1s 调 ``await request.is_disconnected()``，断连则 resolve aborted。

        **必须 await**：starlette ``Request.is_disconnected`` 是 ``async def``，不 await 返回
        coroutine → 永远 truthy → 首次 poll（1s 后）即误判断连 → 所有审批 force-abort
        （BUG A 修复，2026-08-05 test-agent 发现）。正确 await 后才反映真实连接状态。

        与 ``wait_for(fut)`` 竞速：任一先完成即让 ``request`` return。被 cancel 不影响
        已 resolve 的 approval（_resolve_locked 是幂等 first-wins）。
        """
        try:
            while True:
                await asyncio.sleep(_DISCONNECT_POLL_INTERVAL)
                try:
                    disconnected = await http_request.is_disconnected()
                except Exception:  # noqa: BLE001 — 探测异常保守视作未断连
                    disconnected = False
                if disconnected:
                    self._resolve_locked(approval_id, "aborted", "disconnect")
                    return
        except asyncio.CancelledError:
            raise

    # ── 订阅 / publish ──────────────────────────────────────────────────────

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """订阅某 run 的 approval 事件（ws_handler 第二条 pump 用）。

        返回 ``asyncio.Queue``；ws_handler 起 task 从 queue 取 event 推到 WS 出站 queue。
        ``unsubscribe`` 由 ws_handler 在 cancel_sub / cleanup 时调用（同时 cancel pump task，
        None 哨兵从未使用——pump 经 task.cancel 退出）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        """移除订阅者（幂等）。close 队列由调用方负责（put None 哨兵）。"""
        subs = self._subscribers.get(run_id)
        if subs is None:
            return
        try:
            subs.remove(q)
        except ValueError:
            return
        if not subs:
            self._subscribers.pop(run_id, None)

    def _publish(self, run_id: str, event: ApprovalEvent) -> None:
        """推事件给该 run 的所有订阅者队列。QueueFull → drop + warn（慢消费者兜底）。"""
        subs = self._subscribers.get(run_id, [])
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "approval subscriber queue full run=%s — dropping %s",
                    run_id, event.kind,
                )

    # ── yolo / snapshot ─────────────────────────────────────────────────────

    def set_yolo(self, value: bool) -> bool:
        """SPEC §3.3：设 yolo。内存 + 持久化 + 广播 yolo_changed。"""
        prev = self._yolo
        self._yolo = bool(value)
        _save_yolo_persisted(self._yolo)
        if prev != self._yolo:
            # 广播给所有订阅者（所有 run）—— yolo 是 broker 全局开关。
            for run_id in list(self._subscribers.keys()):
                self._publish(
                    run_id,
                    ApprovalEvent(
                        "yolo_changed", {"yolo": self._yolo},
                    ),
                )
        return self._yolo

    def snapshot(self) -> dict[str, Any]:
        """SPEC §4.3 approval_snapshot：所有 pending（含 run_id 标签）+ yolo 状态。"""
        with self._lock:
            approvals = [
                {
                    "approval_id": ap.id,
                    "run_id": ap.run_id,
                    "tool": ap.tool,
                    "tool_input": ap.tool_input_redacted,
                    "created_at": ap.created_at,
                }
                for ap in self._pending.values()
            ]
        return {"approvals": approvals, "yolo": self._yolo}

    def pending_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """snapshot 的 per-run 视图（subscribe 后初始 reconcile 用）。"""
        snap = self.snapshot()
        return [a for a in snap["approvals"] if a["run_id"] == run_id]


def _env_timeout() -> float:
    """读 ORCA_APPROVAL_TIMEOUT（默认 600）。测试用 ORCA_APPROVAL_TIMEOUT_TEST_OVERRIDE 缩窗。"""
    raw = os.environ.get("ORCA_APPROVAL_TIMEOUT_TEST_OVERRIDE")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        return float(os.environ.get("ORCA_APPROVAL_TIMEOUT", _DEFAULT_TIMEOUT))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _env_policy() -> str:
    raw = os.environ.get("ORCA_APPROVAL_TIMEOUT_POLICY", _DEFAULT_POLICY).strip().lower()
    return raw if raw in _VALID_POLICIES else _DEFAULT_POLICY
