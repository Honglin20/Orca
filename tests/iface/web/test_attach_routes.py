"""test_attach_routes.py —— ``POST /api/runs/attach`` routes 层契约测试（SPEC §6.7 / §8 AC9）。

RunManager 层的 ``PermissionError('not-orca-tape')`` 由 ``routes/attach.py`` 映射为 HTTP 403。
本文件在 FastAPI TestClient 层断 ``status_code`` + ``detail`` 文本——保护 routes 映射不被
误改为 400/500（RunManager 层契约由 ``test_attach.py`` 单测覆盖）。

不在 ``test_integration.py``（那里整文件 ``pytest.mark.integration`` 默认 CI skip）——本测试
纯 TestClient（不起真 server），快且无外部依赖，应作为 CI 默认回归守门。
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from orca.iface.web.run_manager import RunManager
from orca.iface.web.server import create_app

# starlette TestClient 的 httpx 弃用警告与本测试意图无关，过滤之。
warnings.filterwarnings("ignore", category=DeprecationWarning)


def test_attach_non_orca_tape_returns_403(tmp_path: Path):
    """首行完整可解析但非 ``workflow_started`` → HTTP 403 not-orca-tape。

    SPEC §6.7 / §8 AC9。``routes/attach.py:60-62`` 把 ``PermissionError`` → 403；若有人误
    改成 400/500，本测试 fail loud。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    bad_tape = runs_dir / "notorca.jsonl"
    bad_tape.write_text(
        '{"seq":1,"type":"agent_message","timestamp":1.0,"node":"x",'
        '"session_id":null,"data":{}}\n',
        encoding="utf-8",
    )

    manager = RunManager(runs_dir=runs_dir)
    app = create_app(manager)

    with TestClient(app) as client:
        resp = client.post(
            "/api/runs/attach",
            json={"tape_path": str(bad_tape), "run_id": "notorca-fresh"},
        )
        assert resp.status_code == 403
        detail = resp.json().get("detail", "")
        assert "not-orca-tape" in detail
        # 未注册进 registry（routes 层 PermissionError 不留副作用）
        assert manager.get_handle("notorca-fresh") is None


def test_attach_valid_tape_returns_200(tmp_path: Path):
    """正常 Orca tape → 200（routes 层 happy path 回归守门）。"""
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    tape = runs_dir / "good.jsonl"
    tape.write_text(
        '{"seq":1,"type":"workflow_started","timestamp":1.0,"node":null,'
        '"session_id":null,"data":{"run_id":"good","workflow_name":"wf",'
        '"inputs":{},"topology":{"entry":"n1","nodes":[{"name":"n1","kind":"agent"}],'
        '"routes":[{"from":"n1","to":"$end"}],"parallel":[]}}}\n',
        encoding="utf-8",
    )

    manager = RunManager(runs_dir=runs_dir)
    app = create_app(manager)

    with TestClient(app) as client:
        resp = client.post(
            "/api/runs/attach",
            json={"tape_path": str(tape), "run_id": "good-run"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == "good-run"
        assert manager.get_handle("good-run") is not None
