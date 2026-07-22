"""test_push_chain_smoke.py —— SPEC §6 fast e2e 冒烟测试。

**意图**（SPEC §6）：零模型调用 + <5s 验证 daemon→bus→WS 全链路通。复用现有 resolver
（``ORCA_CC_SIDECHAIN_ROOT`` env）+ 假 jsonl+meta.json + 真 daemon subprocess + ephemeral
web + WS subscribe 等收。**负向用例**：不写 meta.json → daemon 全跳过 → WS 收不到 → fail。

**核心技巧**（SPEC §6）：``ORCA_CC_SIDECHAIN_ROOT=<tmpdir>``（cc_jsonl resolver source="env"
第一优先级）→ 手写假 ``agent-<task_id>.jsonl + agent-<task_id>.meta.json`` → daemon 当真
子 agent ingest → tape → web follow task → bus.relay → WS pump → 客户端。

**跨平台注意**：daemon subprocess 用 ``_sidechain_daemon_alive`` 探活仅在 Linux 可靠
（依赖 ``/proc``）；本测试改用「poll tape 出现 agent_message」判活，不依赖 pidfile 探活。
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _append_tape_event(tape_path: Path, etype: str, *, seq: int, node: str | None = None,
                      session_id: str | None = None, data: dict | None = None) -> None:
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tape_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "seq": seq, "type": etype, "timestamp": time.time(),
            "node": node, "session_id": session_id, "data": data or {},
        }) + "\n")


def _write_fake_sidechain_child(
    sidechain_root: Path, task_id: str, text: str = "smoke event",
) -> None:
    """写假 agent-<task_id>.jsonl + agent-<task_id>.meta.json（SPEC §6 核心技巧）。"""
    sidechain_root.mkdir(parents=True, exist_ok=True)
    # CC sidechain jsonl line：assistant text 消息（与 _assistant_text helper 同构）。
    payload = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    }
    (sidechain_root / f"agent-{task_id}.jsonl").write_text(
        json.dumps(payload) + "\n", encoding="utf-8",
    )
    (sidechain_root / f"agent-{task_id}.meta.json").write_text(
        json.dumps({"agentType": "task", "description": "smoke test fake child"}),
        encoding="utf-8",
    )


def _spawn_daemon(
    run_id: str, tape_path: Path, host_session: str, *, family: str | None = None,
    poll_interval: float = 0.1,
) -> subprocess.Popen:
    """spawn sidechain daemon subprocess（同 ``_spawn_sidechain_daemon`` argv + SPEC §6 poll_interval=0.1）。"""
    cmd = [
        sys.executable, "-m", "orca.iface.in_session.sidechain_daemon",
        "--run-id", run_id,
        "--tape", str(tape_path),
        "--backend", "cc",
        "--host-session", host_session,
        # SPEC §6 step 3：poll_interval=0.1（默认 0.5s 会让冒烟超 5s 阈值）。
        "--poll-interval", str(poll_interval),
    ]
    if family is not None:
        cmd.extend(["--family", family])
    log_path = tape_path.parent / run_id / "sidechain_daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fd = open(log_path, "a", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fd.close()
    return proc


async def _wait_ws_receives_agent_message(
    ws_url: str, run_id: str, timeout: float = 5.0,
    *,
    expected_session_id: str | None = None,
) -> dict | None:
    """连 WS subscribe run_id，等收 agent_message 事件 ≤ timeout 秒。返事件 dict 或 None。

    SPEC §6 step 5「session_id == task_id」契约：``expected_session_id`` 给定时校验事件
    ``session_id`` 字段匹配——堵住「pump 串流无关 agent_message 误判 pass」漏洞（review T-1）。
    """
    import websockets
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "subscribe", "run_id": run_id}))
        # 给 server 时间起 pump task（_handle_subscribe → create_task(_pump)）。
        await asyncio.sleep(0.2)
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
                )
            except asyncio.TimeoutError:
                continue
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if (
                payload.get("type") == "agent_message"
                and payload.get("run_id") == run_id
            ):
                # SPEC §6 step 5：可选 session_id==task_id 校验（防 pump 串流）。
                if (
                    expected_session_id is not None
                    and payload.get("session_id") != expected_session_id
                ):
                    continue
                return payload
    return None


async def _smoke_run_async(
    runs_dir: Path, tape_path: Path, run_id: str, sidechain_root: Path, host_session: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    negative: bool = False,
    expected_session_id: str | None = None,
) -> dict:
    """跑一次 smoke：起 ephemeral web + attach_run + spawn daemon + 等收。

    ``negative=True``：不写 sidechain 子代理（验证 daemon 全跳过 → WS 收不到 → 测试 fail）。

    **env 隔离**（review blocker #1）：``monkeypatch.setenv`` 让 env 改动在 test 退出时
    auto-restore，不污染后续 in_session 测试。
    """
    import uvicorn
    from orca.iface.web.run_manager import RunManager
    from orca.iface.web.server import create_app

    monkeypatch.setenv("ORCA_CC_SIDECHAIN_ROOT", str(sidechain_root))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", host_session)

    # negative 模式不写假子代理（验证 daemon 全跳过 → WS 收不到）。
    if not negative:
        _write_fake_sidechain_child(sidechain_root, "smoke-task-1", text="smoke event")

    manager = RunManager(runs_dir=runs_dir, max_concurrent=1)
    app = create_app(manager)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    server.force_exit = True
    server_task = asyncio.create_task(server.serve())

    proc: subprocess.Popen | None = None
    try:
        # 等 server 起。
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if getattr(server, "started", False):
                break
            await asyncio.sleep(0.05)
        # 拿端口。
        port = None
        for srv in getattr(server, "servers", None) or []:
            for sock in (list(srv.sockets) if hasattr(srv, "sockets") else []):
                try:
                    port = int(sock.getsockname()[1])
                    break
                except (OSError, TypeError, IndexError):
                    continue
            if port:
                break
        assert port is not None, "uvicorn 未起端口"

        # attach_run 让 follow task tail-follow tape（daemon 写 → bus.relay → WS pump）。
        await manager.attach_run(str(tape_path), run_id=run_id)

        # spawn daemon（同 _spawn_sidechain_daemon argv + SPEC §6 poll_interval=0.1）。
        proc = _spawn_daemon(run_id, tape_path, host_session, poll_interval=0.1)

        ws_url = f"ws://127.0.0.1:{port}/ws"
        event = await _wait_ws_receives_agent_message(
            ws_url, run_id, timeout=5.0, expected_session_id=expected_session_id,
        )
        return {"received": event is not None, "event": event}
    finally:
        if proc is not None:
            proc.send_signal(signal.SIGTERM)
            try:
                # 用 to_thread 避免阻塞 event loop（review 🟢#1）。
                await asyncio.to_thread(lambda: proc.wait(timeout=5))
            except subprocess.TimeoutExpired:
                proc.kill()
        server.should_exit = True
        # 分开 except（review 🟡#3）：TimeoutError 是预期的，CancelledError 重抛不吞。
        try:
            await asyncio.wait_for(server_task, timeout=3.0)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            server_task.cancel()
        try:
            await manager.shutdown(timeout=2.0)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass


def _bootstrap_smoke_tape(tape_path: Path, run_id: str, host_session: str) -> None:
    """SPEC §6 step 1：bootstrap 模拟——手写 workflow_started + node_started。

    不用 ``tests/e2e_redesign/tars_harness.bootstrap_run``：那是真 orca CLI 子进程，拉起太多
    （spawn daemon / chart / web），冒烟测试只测 daemon→bus→WS 链路不需要。手写轻 10x。

    daemon ingest 需要 ``node_started`` 派生 agent_* 的 node 字段
    （sidechain_ingestor._derive_current_node）。
    """
    _append_tape_event(
        tape_path, "workflow_started", seq=1,
        data={"host_session": host_session, "run_id": run_id, "workflow_name": "smoke"},
    )
    _append_tape_event(tape_path, "node_started", seq=2, node="N1")


def _read_daemon_log_tail(tape_path: Path, run_id: str, *, max_chars: int = 2000) -> str:
    """读 daemon log 尾部供 fail message 定位（review 🟡#2：测试 fail loud）。"""
    log_path = tape_path.parent / run_id / "sidechain_daemon.log"
    if not log_path.is_file():
        return "(no daemon log)"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(read log failed: {e})"
    return text[-max_chars:] if len(text) > max_chars else text


# ── tests ────────────────────────────────────────────────────────────────────


def test_fast_e2e_smoke_daemon_to_ws(tmp_path, monkeypatch):
    """SPEC §6 / §7-6 happy：bootstrap + 假子代理 + daemon → WS 5s 内收到 agent_message。

    跨平台：不依赖 ``_sidechain_daemon_alive``（Linux-only），用 WS 等收判活。
    SPEC §6 预期 <5s（零模型 / poll 0.1s / ephemeral port / 单节点 wf）。
    SPEC §6 step 5：断言 session_id==task_id（防 pump 串流误判 pass）。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    sidechain_root = tmp_path / "sidechain"
    run_id = "smoke-cc-run"
    host_session = "smoke-host"
    tape_path = runs_dir / f"{run_id}.jsonl"

    _bootstrap_smoke_tape(tape_path, run_id, host_session)

    t0 = time.monotonic()
    result = asyncio.run(_smoke_run_async(
        runs_dir, tape_path, run_id, sidechain_root, host_session, monkeypatch,
        expected_session_id="smoke-task-1",  # SPEC §6 step 5：session_id==task_id 守门
    ))
    elapsed = time.monotonic() - t0

    assert result["received"], (
        "5s 内 WS 未收到 session_id=smoke-task-1 的 agent_message——daemon→bus→WS 链路断"
        f"；event={result['event']}"
        f"；daemon_log_tail=\n{_read_daemon_log_tail(tape_path, run_id)}"
    )
    # SPEC §6 预期 <5s（含 daemon spawn + poll + ingest + follow + WS）；容忍 <15s 跨平台抖动。
    assert elapsed < 15.0, f"smoke 跑了 {elapsed:.1f}s（SPEC §6 预期 <5s 理想 / <15s 容忍）"


def test_fast_e2e_smoke_negative_no_meta_no_event(tmp_path, monkeypatch):
    """SPEC §6 负向用例 / §7-6 负向：不写 meta.json → daemon 全跳过 → WS 收不到 → 测试 fail。

    防假绿：证明 smoke 真在校验推送链路（不是侥幸收到无关事件）。
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    sidechain_root = tmp_path / "sidechain"
    run_id = "smoke-cc-neg"
    host_session = "smoke-host-neg"
    tape_path = runs_dir / f"{run_id}.jsonl"
    _bootstrap_smoke_tape(tape_path, run_id, host_session)

    # negative=True → 不写假子代理 → daemon discover 返空 → 无 ingest → WS 收不到。
    result = asyncio.run(_smoke_run_async(
        runs_dir, tape_path, run_id, sidechain_root, host_session, monkeypatch,
        negative=True,
    ))

    assert not result["received"], (
        "负向用例：daemon 应全跳过（无 meta.json），但 WS 收到了事件——daemon 过滤逻辑漂移"
        f"；event={result['event']}"
    )
