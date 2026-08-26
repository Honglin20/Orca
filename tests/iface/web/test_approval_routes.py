"""test_approval_routes.py —— /approval + /approval/respond + /approval/yolo HTTP 路由单测。

用 starlette TestClient + 真 ApprovalBroker 验证 HTTP 层分派（broker 行为由
``test_approval_broker.py`` 覆盖）。
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from orca.gates.context_registry import SessionContextRegistry
from orca.iface.web.approval_broker import ApprovalBroker
from orca.iface.web.routes.approval import build_router


def _make_app(timeout: float = 5.0) -> tuple[FastAPI, ApprovalBroker]:
    broker = ApprovalBroker(SessionContextRegistry(), timeout=timeout)
    app = FastAPI()
    app.include_router(build_router(broker))
    return app, broker


def test_approval_endpoint_unknown_session_returns_ask():
    """SPEC §6：未注册 session → {behavior:ask, resolved_by:native-fallback}。"""
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post("/approval", json={"session_id": "x", "tool": "Bash", "tool_input": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["behavior"] == "ask"
        assert body["resolved_by"] == "native-fallback"


def test_approval_respond_missing_fields_returns_400():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post("/approval/respond", json={"approval_id": "x"})
        assert resp.status_code == 400


def test_approval_respond_invalid_answer_returns_400():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/approval/respond",
            json={"approval_id": "x", "answer": "bogus"},
        )
        assert resp.status_code == 400


def test_approval_yolo_endpoint_toggles():
    """SPEC §3.3：POST /approval/yolo {yolo:bool} → broker.set_yolo。"""
    app, broker = _make_app()
    with TestClient(app) as client:
        resp = client.post("/approval/yolo", json={"yolo": True})
        assert resp.status_code == 200
        assert resp.json()["yolo"] is True
        assert broker.yolo is True
        # 关掉。
        resp = client.post("/approval/yolo", json={"yolo": False})
        assert resp.json()["yolo"] is False
        assert broker.yolo is False


def test_approval_yolo_missing_field_returns_400():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post("/approval/yolo", json={})
        assert resp.status_code == 400


def test_approval_snapshot_endpoint_returns_pending_and_yolo():
    app, broker = _make_app()
    broker.set_yolo(True)
    with TestClient(app) as client:
        resp = client.get("/approval/snapshot")
        assert resp.status_code == 200
        body = resp.json()
        assert "approvals" in body
        assert body["yolo"] is True


def test_approval_endpoint_broker_exception_returns_500(monkeypatch):
    """SPEC §7 N4：broker.request 异常 → HTTP 500 → hook 视作 HTTP 错 → deny+warn。

    注入 broker.request 抛异常；endpoint 经 try/except 转 500。
    """
    app, broker = _make_app()

    async def boom(_payload, http_request=None):
        raise RuntimeError("synthetic broker crash")

    monkeypatch.setattr(broker, "request", boom)
    with TestClient(app) as client:
        resp = client.post("/approval", json={"tool": "Bash", "tool_input": {}})
        assert resp.status_code == 500


def test_approval_endpoint_resolve_session_context_exception_returns_500(monkeypatch):
    """SPEC §7「marker 损坏 / resolve_session_context 抛异常」→ 500（fail loud，不静默吞）。

    broker.request 内调 resolve_session_context；若它抛异常（registry 损坏等），异常上抛
    经 endpoint try/except 转 500。本测通过 patch resolve_session_context 抛异常验证。
    """
    import orca.iface.web.approval_broker as broker_mod

    def boom(_registry, _payload):
        raise RuntimeError("synthetic registry corruption")

    monkeypatch.setattr(broker_mod, "resolve_session_context", boom)
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post("/approval", json={"tool": "Bash", "tool_input": {}})
        assert resp.status_code == 500
