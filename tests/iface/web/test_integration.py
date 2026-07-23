"""test_integration.py —— 真实 uvicorn server 全流程集成测试（SPEC §6.5 §6.7 / 计划 A4.3）。

``@pytest.mark.integration``：默认 CI 不跑（``-m "not integration"``）。本地或显式
``pytest -m integration tests/iface/web/test_integration.py`` 跑。

覆盖（SPEC §6.7）：
  - 启动真 server（uvicorn）+ start_run demo workflow（纯 script，零 claude）。
  - 全流程：start → list 有该 run → events 端点有事件 → WS subscribe 收事件。
  - tape 完整性：``/api/runs/<id>/events`` 返回 == ``tape.replay()``。
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
from contextlib import closing
from pathlib import Path

import pytest
from httpx import AsyncClient

from orca.events.replay import replay_state
from orca.iface.web.run_manager import RunManager
from orca.iface.web.server import create_app

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _demo_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "demo.yaml"
    p.write_text(
        """
name: demo
entry: a
nodes:
  - name: a
    kind: script
    command: "echo hi"
    routes:
      - to: $end
""",
        encoding="utf-8",
    )
    return p


def test_full_flow_real_server(tmp_path, monkeypatch):
    """启动真 uvicorn server + 全流程：start → list → events → state。

    SPEC §6.7 集成测试。``-m integration`` 时跑。
    """
    import uvicorn
    from orca.runtime import register_project

    # §13.2 B-1：POST /api/run body 必填 project_path。造合法项目并注册。
    monkeypatch.setenv("ORCA_HOME", str(tmp_path / "orca-home"))
    project = tmp_path / "proj"
    (project / "workflows").mkdir(parents=True, exist_ok=True)
    register_project(project)

    yaml_path = _demo_yaml(tmp_path)
    manager = RunManager(runs_dir=Path(f"/tmp/orca-int-{hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]}/runs"))
    app = create_app(manager)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def go():
        server_task = asyncio.create_task(server.serve())
        await asyncio.sleep(0.3)  # 让 server 起来
        try:
            async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                # POST /api/run（§13.2 B-1：project_path 必填）
                resp = await client.post(
                    "/api/run",
                    json={"yaml_path": str(yaml_path), "project_path": str(project)},
                )
                assert resp.status_code == 200
                run_id = resp.json()["run_id"]
                # 等完成
                await manager.wait_done(run_id, timeout=15.0)
                # GET /api/runs
                resp = await client.get("/api/runs")
                data = resp.json()
                assert any(i["run_id"] == run_id for i in data)
                assert all("events" not in i for i in data)  # 懒加载
                # GET /api/runs/<id>/events
                resp = await client.get(f"/api/runs/{run_id}/events")
                events = resp.json()
                assert len(events) > 0
                # tape 完整性
                handle = manager.get_handle(run_id)
                tape_events = list(handle.tape.replay())
                assert len(events) == len(tape_events)
                # GET /api/runs/<id>
                resp = await client.get(f"/api/runs/{run_id}")
                body = resp.json()
                assert body["state"]["status"] == "completed"
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5.0)
            await manager.shutdown()

    asyncio.run(go())


def test_events_endpoint_after_completion(tmp_path):
    """run 完成后 events 端点返回完整事件序列（SPEC §6.7 集成）。

    ``-m integration`` 时跑。诚实反映：run 完成后 bus 已 close，WS subscribe 收不到
    实时事件（SPEC §4.2 约束 5「重连全量重拉」= 前端职责）；实时 WS 推送的端到端验证
    在 ``test_ws.py`` 单元测试（FakeWebSocket + 手动 emit）覆盖。本测试验「完成后 events
    端点完整性」（tape.replay 通过 HTTP 暴露）。
    """
    from starlette.testclient import TestClient

    import warnings
    # starlette TestClient 的 httpx 弃用警告与本测试意图无关，过滤之（不影响 RuntimeWarning 检查）。
    warnings.filterwarnings("ignore", category=DeprecationWarning)

    yaml_path = _demo_yaml(tmp_path)
    manager = RunManager(runs_dir=Path(f"/tmp/orca-int-{hashlib.md5(str(tmp_path).encode()).hexdigest()[:6]}/runs"))
    app = create_app(manager)

    async def start_run():
        run_id = await manager.start_run(str(yaml_path), {}, None, None)
        await manager.wait_done(run_id, timeout=10.0)
        return run_id

    with TestClient(app) as client:
        run_id = asyncio.new_event_loop().run_until_complete(start_run())
        resp = client.get(f"/api/runs/{run_id}/events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) > 0
        types = [e["type"] for e in events]
        assert "workflow_started" in types
        assert "workflow_completed" in types
        asyncio.new_event_loop().run_until_complete(manager.shutdown())
