"""test_approval_broker.py —— ApprovalBroker 单测（SPEC in-session-permission-hook）。

覆盖（SPEC §11 验收）：
  - request/resolve（happy path）
  - first-wins（多源并发 resolve，仅首个生效）
  - late respond（已 resolved 后再 resolve → ok=False + approval_resolved_late 审计事件）
  - yolo（on → 即时 allow；持久化 best-effort）
  - timeout-policy（broker timeout → 按 ORCA_APPROVAL_TIMEOUT_POLICY，allow/ask/deny）
  - disconnect-abort（_disconnect_poller 探测断连 → resolve aborted）
  - uuid 唯一（N 并发 request → N 个 uuid4，无碰撞）
  - redact（_TOKEN/_KEY/_PASSWORD/_SECRET env、URL user:pass@、sk-ant-/sk-、Authorization/Cookie）
  - session_id 缺失 / 未注册 → ask（behavior=ask, resolved_by=native-fallback）
  - subscribe / publish（per-run 投递，跨 run 不串台）

约定（同 conftest）：``asyncio.run`` 跑 async 测试，不用 pytest-asyncio。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orca.gates.context_registry import SessionContextRegistry
from orca.iface.web.approval_broker import (
    ApprovalBroker,
    _redact,
    _compile_redact_patterns,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_yolo_persist(tmp_path: Path, monkeypatch):
    """每个测试隔离持久 yolo 文件（防前测残留 yolo=True 污染 broker 初始状态）。"""
    import orca.iface.web.approval_broker as mod
    monkeypatch.setattr(mod, "_YOLO_PATH", tmp_path / "approval-yolo.json")


def _make_broker(timeout: float | None = None, policy: str | None = None) -> ApprovalBroker:
    """构造 broker（注入 timeout/policy；绕过 env 默认）。"""
    reg = SessionContextRegistry()
    broker = ApprovalBroker(reg, timeout=timeout if timeout is not None else 30.0)
    if policy is not None:
        broker._policy = policy  # 测试注入
    return broker


class _FakeRequest:
    """模拟 starlette/FastAPI Request 的 ``is_disconnected``（async，对齐 starlette 签名）。

    BUG A 修复后 broker 用 ``await request.is_disconnected()``——故本 mock 必须 ``async def``。
    """

    def __init__(self, disconnected: bool = False):
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


# ── request / resolve happy path ─────────────────────────────────────────────


def test_request_unknown_session_returns_ask():
    """SPEC §6 / §7：未命中活跃 run → ask（behavior=ask, resolved_by=native-fallback）。"""
    broker = _make_broker()
    result = run(broker.request({"session_id": "unknown", "tool": "Bash", "tool_input": {}}))
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "native-fallback"
    assert result["approval_id"] is None


def test_request_resolve_happy_path():
    """命中 run → 广播 requested → 用户 resolve(allow) → 返回 allow。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-1", "run-A", "nodeA")

    async def scenario():
        # 起 request task（阻塞等 resolve）。
        task = asyncio.create_task(
            broker.request(
                {"session_id": "sid-1", "tool": "Bash", "tool_input": {"cmd": "ls"}},
                http_request=_FakeRequest(),
            )
        )
        # 等 broker 注册 pending。
        await asyncio.sleep(0.05)
        # 取 pending approval_id（broker 内部状态）。
        approval_ids = list(broker._pending.keys())
        assert len(approval_ids) == 1
        aid = approval_ids[0]
        # 用户 web resolve allow。
        result_resolve = broker.resolve(aid, "allow", "web")
        assert result_resolve["ok"] is True
        # 等 request task 返回。
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result["behavior"] == "allow"
        assert result["resolved_by"] == "web"
        assert result["approval_id"] == aid

    run(scenario())


def test_request_session_id_missing_returns_ask():
    """SPEC §7：session_id 全空 → resolve_session_context 返 unknown → ask。"""
    broker = _make_broker()
    result = run(broker.request({"tool": "Bash", "tool_input": {}}))
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "native-fallback"


# ── first-wins + late respond ────────────────────────────────────────────────


def test_first_wins_resolve_only_first_takes_effect():
    """SPEC §3.2 / §4.2：多源并发 resolve，仅首个生效；后续 → ok=False。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-2", "run-B", None)

    async def scenario():
        task = asyncio.create_task(
            broker.request(
                {"session_id": "sid-2", "tool": "Write", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
        await asyncio.sleep(0.05)
        aid = next(iter(broker._pending.keys()))
        # 并发两路 resolve。
        r1 = broker.resolve(aid, "allow", "web")
        r2 = broker.resolve(aid, "deny", "mcp")
        assert r1["ok"] is True
        assert r2["ok"] is False  # late respond
        result = await asyncio.wait_for(task, timeout=2.0)
        # 首个 allow 生效（first-wins，deny 被丢）。
        assert result["behavior"] == "allow"
        assert result["resolved_by"] == "web"

    run(scenario())


def test_resolve_unknown_approval_id_returns_ok_false():
    """SPEC §4.2：未知 approval_id → ok=False（fail loud，不崩）。"""
    broker = _make_broker()
    r = broker.resolve("nonexistent-aid", "allow", "web")
    assert r["ok"] is False
    assert r["resolved_by"] == "unknown"


def test_late_respond_emits_audit_event():
    """SPEC §3.2 N2：已 resolved 后再 resolve → emit approval_resolved_late（仅审计，不翻盘）。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-late", "run-L", None)
    received = []
    q = broker.subscribe("run-L")
    received.append(q)  # 保持引用

    async def scenario():
        task = asyncio.create_task(
            broker.request(
                {"session_id": "sid-late", "tool": "Bash", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
        await asyncio.sleep(0.05)
        aid = next(iter(broker._pending.keys()))
        broker.resolve(aid, "allow", "web")
        # 再次 resolve（late）→ ok=False + emit approval_resolved_late。
        late = broker.resolve(aid, "deny", "web")
        assert late["ok"] is False
        await asyncio.wait_for(task, timeout=2.0)
        # 排空 queue：应有 approval_requested / approval_resolved / approval_resolved_late。
        kinds = []
        while not q.empty():
            event = q.get_nowait()
            kinds.append(event.kind)
        assert "approval_requested" in kinds
        assert "approval_resolved" in kinds
        assert "approval_resolved_late" in kinds

    run(scenario())


# ── yolo ─────────────────────────────────────────────────────────────────────


def test_yolo_on_immediate_allow():
    """SPEC §3.3：yolo on → request 即时 allow（< 500ms）。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-yolo", "run-Y", None)
    broker.set_yolo(True)
    assert broker.yolo is True

    t0 = time.monotonic()
    result = run(
        broker.request(
            {"session_id": "sid-yolo", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    elapsed = time.monotonic() - t0
    assert result["behavior"] == "allow"
    assert result["resolved_by"] == "yolo"
    assert elapsed < 0.5, f"yolo 应即时 allow，实际 {elapsed:.3f}s"


def test_yolo_toggle_publishes_event_to_subscribers():
    """SPEC §3.3：yolo 切换 → 广播 yolo_changed 给所有订阅者。"""
    broker = ApprovalBroker(SessionContextRegistry(), timeout=5.0)
    q = broker.subscribe("run-Y")
    broker.set_yolo(True)
    # set_yolo 内 _publish。
    assert not q.empty()
    event = q.get_nowait()
    assert event.kind == "yolo_changed"
    assert event.payload["yolo"] is True


def test_yolo_persistence_best_effort(tmp_path: Path, monkeypatch):
    """SPEC §3.3 B-8：~/.orca/approval-yolo.json 重启恢复。"""
    import orca.iface.web.approval_broker as mod

    # 本测试需要持久的 file，覆盖 autouse fixture 到显式路径。
    yolo_file = tmp_path / "yolo-persist.json"
    monkeypatch.setattr(mod, "_YOLO_PATH", yolo_file)
    broker = ApprovalBroker(SessionContextRegistry(), timeout=5.0)
    broker.set_yolo(True)
    # 文件确实写入。
    assert yolo_file.is_file()
    # 重读持久文件。
    val = mod._load_yolo_persisted()
    assert val is True
    # 新 broker 实例从持久恢复。
    broker2 = ApprovalBroker(SessionContextRegistry(), timeout=5.0)
    assert broker2.yolo is True


# ── timeout policy ───────────────────────────────────────────────────────────


def test_timeout_policy_default_allow():
    """SPEC §3.5：默认 policy=allow → broker timeout 时 resolve allow。"""
    broker = _make_broker(timeout=0.2, policy="allow")
    broker.registry.register("sid-to", "run-TO", None)

    result = run(
        broker.request(
            {"session_id": "sid-to", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "allow"
    assert result["resolved_by"] == "timeout"


def test_timeout_policy_deny():
    broker = _make_broker(timeout=0.2, policy="deny")
    broker.registry.register("sid-d", "run-D", None)
    result = run(
        broker.request(
            {"session_id": "sid-d", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "deny"
    assert result["resolved_by"] == "timeout"


def test_timeout_policy_ask():
    broker = _make_broker(timeout=0.2, policy="ask")
    broker.registry.register("sid-ask", "run-ASK", None)
    result = run(
        broker.request(
            {"session_id": "sid-ask", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "timeout"


# ── disconnect abort ─────────────────────────────────────────────────────────


def test_disconnect_aborts_approval():
    """SPEC §3.2 P1：HTTP disconnect → resolve aborted。

    broker_timeout 设大（10s → broker_timeout=5s），disconnect poller 间隔 1s 先 fire。
    """
    broker = _make_broker(timeout=10.0)
    broker.registry.register("sid-disc", "run-DISC", None)

    async def scenario():
        # FakeRequest 立即返 disconnected=True。
        req = _FakeRequest(disconnected=True)
        task = asyncio.create_task(
            broker.request(
                {"session_id": "sid-disc", "tool": "Bash", "tool_input": {}},
                http_request=req,
            )
        )
        result = await asyncio.wait_for(task, timeout=4.0)
        # disconnect poller 探测后 resolve aborted。
        assert result["behavior"] == "aborted"
        assert result["resolved_by"] == "disconnect"

    run(scenario())


def test_real_starlette_request_not_disconnected_does_not_abort():
    """BUG A 回归（2026-08-05 test-agent 发现）：

    原实现 ``disconnected = http_request.is_disconnected()`` 缺 ``await``——starlette
    ``Request.is_disconnected`` 是 ``async def``，不 await 返回 coroutine → 永远 truthy →
    首次 1s poll 即误判 disconnect，timeout 路径完全失效（用户明示的 notify-proceed
    timeout-allow 被破坏）。

    回归用真实 starlette ``Request``（``is_disconnected`` 在客户端未发 ``http.disconnect``
    时返 ``False``）+ 短 broker timeout 验证：超时必须走 policy（``allow``），而非 disconnect
    aborted。让「缺 await」这类在 CI 就炸。

    disconnect 路径由 ``test_disconnect_aborts_approval`` 覆盖（async ``_FakeRequest``）；
    不再用「sleep-then-disconnect」假 receive 测真实 disconnect 路径——starlette
    ``is_disconnected`` 的内部 receive poll 对假 receive 兼容性差，留给真 ASGI e2e。
    """
    from starlette.requests import Request

    # 构造真实 starlette Request：minimal ASGI scope + 阻塞 receive（不发 disconnect）。
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/approval",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 7428),
        "root_path": "",
        "app": type("FakeApp", (), {}),
    }

    async def receive():
        # 客户端一直连着不发 disconnect —— 模拟「httpx 请求阻塞中等响应」。
        await asyncio.sleep(60.0)
        return {"type": "http.request", "body": b"", "more_body": False}

    broker = _make_broker(timeout=2.0, policy="allow")
    broker.registry.register("sid-real", "run-REAL", None)

    async def scenario():
        req = Request(scope, receive)
        result = await asyncio.wait_for(
            broker.request(
                {"session_id": "sid-real", "tool": "Bash", "tool_input": {}},
                http_request=req,
            ),
            timeout=5.0,
        )
        # 必须走 timeout-policy 路径（allow），**不是** disconnect aborted。
        assert result["behavior"] == "allow", (
            f"BUG A 复发：未断连却被 force-abort，实际行为={result['behavior']}, "
            f"resolved_by={result['resolved_by']}"
        )
        assert result["resolved_by"] == "timeout", (
            f"应 timeout（policy 路径），实际 resolved_by={result['resolved_by']}"
        )

    run(scenario())


# ── uuid 唯一 ────────────────────────────────────────────────────────────────


def test_uuid_uniqueness_concurrent_requests():
    """SPEC §3.2 N1：N 并发 request → N 个 uuid4，无碰撞。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-uu", "run-UU", None)

    async def scenario():
        tasks = [
            asyncio.create_task(
                broker.request(
                    {"session_id": "sid-uu", "tool": "Bash", "tool_input": {}},
                    http_request=_FakeRequest(),
                )
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0.1)
        ids = list(broker._pending.keys())
        # 全部唯一。
        assert len(ids) == 5
        assert len(set(ids)) == 5
        # 清：abort 所有（让 task 退出）。
        for aid in ids:
            broker.resolve(aid, "allow", "test")
        results = await asyncio.gather(*tasks)
        assert all(r["behavior"] == "allow" for r in results)

    run(scenario())


# ── redact ───────────────────────────────────────────────────────────────────


def test_redact_env_token_key_password_secret():
    """SPEC §4.3 N3：env 名含 _TOKEN|_KEY|_PASSWORD|_SECRET → ***。"""
    patterns = _compile_redact_patterns()
    obj = {
        "API_TOKEN": "sk-real-token",
        "PRIVATE_KEY": "-----BEGIN-----",
        "DB_PASSWORD": "p@ssw0rd",
        "AUTH_SECRET": "abc",
        "normal_field": "visible",
        "nested": {"MY_KEY": "v", "ok": 1},
    }
    out = _redact(obj, patterns)
    assert out["API_TOKEN"] == "***"
    assert out["PRIVATE_KEY"] == "***"
    assert out["DB_PASSWORD"] == "***"
    assert out["AUTH_SECRET"] == "***"
    assert out["normal_field"] == "visible"
    assert out["nested"]["MY_KEY"] == "***"
    assert out["nested"]["ok"] == 1


def test_redact_url_userpass_and_sk_ant():
    """SPEC §4.3：URL user:pass@、sk-ant-、sk-、Authorization/Cookie → ***。"""
    patterns = _compile_redact_patterns()
    s1 = "https://alice:hunter2@example.com/x"
    s2 = "token sk-ant-AAA111222333"
    s3 = "sk-abc1234567890"
    s4 = "Authorization: Bearer abc.def.ghi"
    s5 = "Cookie: session=xyz"
    assert "alice:hunter2@" not in _redact(s1, patterns)
    assert "hunter2" not in _redact(s1, patterns)
    assert "sk-ant-AAA111222333" not in _redact(s2, patterns)
    assert "sk-abc1234567890" not in _redact(s3, patterns)
    assert "Bearer abc.def.ghi" not in _redact(s4, patterns)
    assert "session=xyz" not in _redact(s5, patterns)


def test_redact_authorization_and_cookie_full_value_no_leak():
    r"""BUG B 回归（2026-08-05 test-agent 发现）：

    原正则 ``[^\s&,]+`` 在空白截断 → ``Authorization: Bearer abcdef`` 仅替换 ``Bearer``
    前缀，token 主体 ``abcdef`` 残留泄露。修复后必须覆盖完整 header 值到行尾。
    """
    patterns = _compile_redact_patterns()
    # 单行：Bearer token 主体必须不残留。
    out = _redact("Authorization: Bearer abcdef", patterns)
    assert "abcdef" not in out, f"Authorization Bearer token 泄露：{out!r}"
    assert "Bearer" not in out, f"Authorization scheme 残留：{out!r}"
    # Cookie 值含 = 也覆盖完整。
    out2 = _redact("Cookie: session=xyz", patterns)
    assert "session=xyz" not in out2
    assert "xyz" not in out2, f"Cookie 值泄露：{out2!r}"
    # 多行 header block：每行独立 redact，不跨行（多行模式下 ``$`` 锚行尾）。
    multi = "Authorization: Bearer AAAA\nX-Other: visible\nCookie: sess=BBBB"
    out4 = _redact(multi, patterns)
    assert "AAAA" not in out4
    assert "BBBB" not in out4
    assert "visible" in out4


def test_redact_extra_env_patterns(monkeypatch):
    """SPEC §4.3 P3：ORCA_APPROVAL_REDACT_PATTERNS env 追加正则。"""
    monkeypatch.setenv("ORCA_APPROVAL_REDACT_PATTERNS", r"INTERNAL_API_KEY=\w+")
    patterns = _compile_redact_patterns()
    out = _redact("INTERNAL_API_KEY=abc123", patterns)
    assert "abc123" not in out


def test_redact_extra_env_invalid_pattern_skipped(monkeypatch):
    """非法正则 skip + warn（不崩）。"""
    monkeypatch.setenv("ORCA_APPROVAL_REDACT_PATTERNS", "[invalid(,]+")
    patterns = _compile_redact_patterns()  # 不抛
    # 默认模式仍生效。
    out = _redact({"X_TOKEN": "v"}, patterns)
    assert out["X_TOKEN"] == "***"


# ── subscribe / publish per-run ──────────────────────────────────────────────


def test_subscribe_publish_per_run_no_crosstalk():
    """SPEC §4.3 N10：approval 卡只推订阅了该 run 的连接。"""
    from orca.iface.web.approval_broker import ApprovalEvent
    broker = _make_broker()
    q_a = broker.subscribe("run-A")
    q_b = broker.subscribe("run-B")
    broker._publish(
        "run-A",
        ApprovalEvent("approval_requested", {"approval_id": "aid1", "run_id": "run-A"}),
    )
    broker._publish(
        "run-B",
        ApprovalEvent("approval_requested", {"approval_id": "aid2", "run_id": "run-B"}),
    )
    assert not q_a.empty()
    assert not q_b.empty()
    a_event = q_a.get_nowait()
    b_event = q_b.get_nowait()
    assert a_event.kind == "approval_requested"
    assert a_event.payload["approval_id"] == "aid1"
    assert b_event.payload["approval_id"] == "aid2"


def test_unsubscribe_idempotent():
    broker = _make_broker()
    q = broker.subscribe("run-X")
    broker.unsubscribe("run-X", q)
    # 再 unsubscribe 不抛。
    broker.unsubscribe("run-X", q)
    broker.unsubscribe("nonexistent", q)


# ── shutdown ─────────────────────────────────────────────────────────────────


def test_shutdown_aborts_pending_approvals():
    """SPEC §3.2 / §6：shutdown 广播 approval_resolved(resolved_by=shutdown)。"""
    broker = _make_broker(timeout=5.0)
    broker.registry.register("sid-sd", "run-SD", None)
    q = broker.subscribe("run-SD")

    async def scenario():
        task = asyncio.create_task(
            broker.request(
                {"session_id": "sid-sd", "tool": "Bash", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
        await asyncio.sleep(0.05)
        await broker.shutdown()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result["behavior"] == "aborted"
        assert result["resolved_by"] == "shutdown"
        # 订阅者收到 approval_resolved(resolved_by=shutdown)。
        kinds = []
        while not q.empty():
            kinds.append(q.get_nowait().kind)
        assert "approval_resolved" in kinds

    run(scenario())


# ── snapshot ──────────────────────────────────────────────────────────────────


def test_snapshot_includes_pending_and_yolo():
    broker = _make_broker()
    broker._yolo = True
    # 手动注入 pending。
    from orca.iface.web.approval_broker import Approval
    import asyncio as _aio
    aid = "snap-aid"
    broker._pending[aid] = Approval(
        id=aid, run_id="run-S", tool="Bash", tool_input_redacted={"x": 1},
        created_at=1.0, fut=_aio.new_event_loop().create_future(),
    )
    snap = broker.snapshot()
    assert snap["yolo"] is True
    assert any(a["approval_id"] == aid for a in snap["approvals"])
    pending_run = broker.pending_for_run("run-S")
    assert any(a["approval_id"] == aid for a in pending_run)


# ── import 守门（N11） ────────────────────────────────────────────────────────


def test_approval_broker_not_import_forbidden_modules():
    """SPEC §11 N11 / AC4：approval_broker 不 import tape/handler/exec/events.bus/run。

    结构化 AST 检查（非裸 grep，防 ``from orca.run import X`` 漏网）；模块边界精确匹配，
    ``orca.runtime`` 等前缀相似模块不受误伤。
    """
    import ast
    import orca.iface.web.approval_broker as mod
    src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = (
        "orca.gates.handler",
        "orca.tape",
        "orca.exec",
        "orca.events.bus",
        "orca.run",
    )
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(n.name for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    for mod_name in modules:
        assert not any(
            mod_name == prefix or mod_name.startswith(prefix + ".")
            for prefix in forbidden
        ), f"approval_broker 不应 import {mod_name!r}"


# ── hook 脚本 stdlib 守门 + 行为 ──────────────────────────────────────────────


HOOK_SRC = (
    Path(__file__).resolve().parents[3]
    / "orca" / "iface" / "in_session" / "templates" / "orca-permission-hook.py"
)


def test_hook_script_uses_stdlib_only():
    """SPEC §3.1 铁律：hook 仅 import stdlib（urllib/json/os/sys/uuid/time）。"""
    import ast
    src = HOOK_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed = {"json", "os", "sys", "time", "urllib", "uuid", "socket", "__future__"}
    # 顶层 sys.path 摘除逻辑 import sys as _sys — 允许。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                top = n.name.split(".")[0]
                assert top in allowed, (
                    f"hook 仅 stdlib，发现 import {n.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            assert top in allowed, (
                f"hook 仅 stdlib，发现 from {node.module}"
            )


# ── active-run 兜底路由（SPEC 2026-08-07 T1–T6 / T16 / T17） ────────────────


def _make_fallback_broker(
    resolver_result: str | None,
    calls: list[str],
    *,
    timeout: float = 5.0,
    policy: str | None = None,
) -> ApprovalBroker:
    """经公开构造器注入 resolver spy 的 broker：记录入参并返回固定 run_id/None。

    走 ``active_run_resolver=`` 公开参数（非私有属性注入），保证新 public API 被真实测试。
    """
    holder: dict[str, ApprovalBroker] = {}

    def resolver(session_id: str) -> str | None:
        calls.append(session_id)
        # spy 不变量：resolver 先于 Approval 创建（pending 尚空）被执行。
        assert len(holder["broker"]._pending) == 0
        return resolver_result

    broker = ApprovalBroker(
        SessionContextRegistry(),
        timeout=timeout,
        active_run_resolver=resolver,
    )
    holder["broker"] = broker
    if policy is not None:
        broker._policy = policy  # 测试注入（同 _make_broker 约定）。
    return broker


def test_resolver_hit_yolo_on_immediate_allow():
    """SPEC T1：registry miss → resolver 命中 + yolo on → 即时 allow（resolved_by=yolo）。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-fb", calls, timeout=5.0)
    broker.set_yolo(True)
    q = broker.subscribe("run-fb")

    result = run(
        broker.request(
            {"session_id": "ses-unregistered", "tool": "Bash", "tool_input": {"cmd": "ls"}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "allow"
    assert result["resolved_by"] == "yolo"
    assert result["approval_id"] is not None
    # spy：registry miss 时被调用一次，入参 = hook session_id。
    assert calls == ["ses-unregistered"]
    # WS 可见 requested + resolved(yolo)。
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait().kind)
    assert "approval_requested" in kinds
    assert "approval_resolved" in kinds


def test_resolver_hit_yolo_off_creates_run_scoped_approval():
    """SPEC T2：resolver 命中 + yolo off → run-scoped Approval + web resolve allow。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-fb", calls, timeout=5.0)
    q = broker.subscribe("run-fb")

    async def scenario():
        task = asyncio.create_task(
            broker.request(
                {"session_id": "ses-unregistered", "tool": "Bash", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
        await asyncio.sleep(0.05)
        aid = next(iter(broker._pending.keys()))
        # Approval run-scoped：run_id = resolver 返回的 run-fb（非 registry 路径）。
        assert broker._pending[aid].run_id == "run-fb"
        r = broker.resolve(aid, "allow", "web")
        assert r["ok"] is True
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result["behavior"] == "allow"
        assert result["resolved_by"] == "web"
        assert result["approval_id"] == aid

    run(scenario())
    assert calls == ["ses-unregistered"]
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait().kind)
    assert "approval_requested" in kinds
    assert "approval_resolved" in kinds


def test_resolver_hit_yolo_off_user_deny():
    """resolver 命中 + yolo off + 用户 deny → deny（web 通道完整走通，含拒绝分支）。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-fb", calls, timeout=5.0)

    async def scenario():
        task = asyncio.create_task(
            broker.request(
                {"session_id": "ses-unregistered", "tool": "Bash", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
        await asyncio.sleep(0.05)
        aid = next(iter(broker._pending.keys()))
        r = broker.resolve(aid, "deny", "web")
        assert r["ok"] is True
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result["behavior"] == "deny"
        assert result["resolved_by"] == "web"

    run(scenario())
    assert calls == ["ses-unregistered"]


def test_resolver_miss_falls_back_to_ask():
    """SPEC T3：resolver 未命中 → ask / native-fallback，无 pending。"""
    calls: list[str] = []
    broker = _make_fallback_broker(None, calls, timeout=5.0)
    result = run(
        broker.request(
            {"session_id": "ses-x", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "native-fallback"
    assert result["approval_id"] is None
    assert calls == ["ses-x"]
    assert len(broker._pending) == 0


def test_resolver_none_default_unchanged_behavior():
    """SPEC T4：resolver=None（默认）→ 行为与现状一致（未注册 → ask）。"""
    broker = _make_broker()
    result = run(broker.request({"session_id": "unknown", "tool": "Bash", "tool_input": {}}))
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "native-fallback"


def test_resolver_raises_falls_back_to_ask(caplog):
    """SPEC T5：resolver 抛异常 → ask + warning（不自动 allow，不传播）。"""
    broker = _make_broker(timeout=5.0)

    def boom(_session_id: str) -> str | None:
        raise RuntimeError("synthetic resolver crash")

    broker._active_run_resolver = boom
    with caplog.at_level(logging.WARNING, logger="orca.iface.web.approval_broker"):
        result = run(
            broker.request(
                {"session_id": "ses-x", "tool": "Bash", "tool_input": {}},
                http_request=_FakeRequest(),
            )
        )
    assert result["behavior"] == "ask"
    assert result["resolved_by"] == "native-fallback"
    assert any("resolver" in r.message for r in caplog.records)


def test_resolver_not_called_when_session_id_missing_or_empty():
    """SPEC T6：session_id 缺失/空 → resolver spy 不被调用，直接 ask。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-x", calls, timeout=5.0)
    r1 = run(broker.request({"tool": "Bash", "tool_input": {}}))
    r2 = run(broker.request({"session_id": "", "tool": "Bash", "tool_input": {}}))
    assert r1["behavior"] == "ask" and r2["behavior"] == "ask"
    assert calls == []


def test_resolver_hit_timeout_policy():
    """SPEC T16：resolver 命中 + 无响应 → BROKER_TIMEOUT → 按 policy（默认 allow）。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-fb", calls, timeout=0.2, policy="allow")
    result = run(
        broker.request(
            {"session_id": "ses-x", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(),
        )
    )
    assert result["behavior"] == "allow"
    assert result["resolved_by"] == "timeout"
    assert calls == ["ses-x"]


def test_resolver_hit_disconnect_aborts():
    """SPEC T17：resolver 命中 + HTTP disconnect → aborted（resolved_by=disconnect）。"""
    calls: list[str] = []
    broker = _make_fallback_broker("run-fb", calls, timeout=10.0)
    result = run(
        broker.request(
            {"session_id": "ses-x", "tool": "Bash", "tool_input": {}},
            http_request=_FakeRequest(disconnected=True),
        )
    )
    assert result["behavior"] == "aborted"
    assert result["resolved_by"] == "disconnect"
    assert calls == ["ses-x"]
