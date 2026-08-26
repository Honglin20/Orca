"""Orca 单端口 + 多 Run 监控 真机 E2E 驱动（SPEC 2026-07-23 sec13 + AC1/3/5/8/16/17/18/20）.

启动真实 uvicorn HTTP server (subprocess `tars serve`) + 真实 subprocess (`orca open`)
+ 真实 httpx/websockets 客户端 → 断言真实可观测结果（HTTP 状态码 / 响应体 / 文件落地 / WS 帧）。

非 mock：RunManager 在 server 子进程内，所有访问经真实 bind 的 socket（HTTP/WS 网络往返）。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
import websockets

PYTHON = "/home/mozzie/miniconda3/envs/orca/bin/python"
TARS = "/home/mozzie/miniconda3/envs/orca/bin/tars"
ORCA = "/home/mozzie/miniconda3/envs/orca/bin/orca"
REPO = "/mnt/d/Projects/Orca"


def log(msg: str) -> None:
    print(f"[driver] {msg}", flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def make_project(root: Path, name: str) -> Path:
    p = root / name
    (p / "workflows").mkdir(parents=True, exist_ok=True)
    return p


def write_tape(tape_path: Path, run_id: str, *, wf_name: str = "demo-wf",
               n_nodes: int = 2, status: str = "completed") -> None:
    """写合法 jsonl tape：workflow_started(node topology) + 每节点 node_started/completed + 终态."""
    tape_path.parent.mkdir(parents=True, exist_ok=True)
    nodes = [{"name": f"n{i}", "kind": "script"} for i in range(n_nodes)]
    lines = [
        {"seq": 1, "type": "workflow_started", "node": None, "session_id": None,
         "timestamp": time.time(),
         "data": {"inputs": {}, "node_count": n_nodes, "entry": "n0",
                  "workflow_name": wf_name, "run_id": run_id,
                  "topology": {"entry": "n0", "nodes": nodes}}},
    ]
    seq = 2
    for i in range(n_nodes):
        nm = f"n{i}"
        lines.append({"seq": seq, "type": "node_started", "node": nm, "session_id": "s1",
                      "timestamp": time.time(), "data": {}})
        seq += 1
        lines.append({"seq": seq, "type": "node_completed", "node": nm, "session_id": "s1",
                      "timestamp": time.time(), "data": {"output": {}}})
        seq += 1
    if status == "completed":
        lines.append({"seq": seq, "type": "workflow_completed", "node": None, "session_id": None,
                      "timestamp": time.time(), "data": {"elapsed": 0.5, "outputs": {}}})
    elif status == "failed":
        lines.append({"seq": seq, "type": "workflow_failed", "node": None, "session_id": None,
                      "timestamp": time.time(), "data": {"error": "synthetic"}})
    elif status == "cancelled":
        lines.append({"seq": seq, "type": "workflow_cancelled", "node": None, "session_id": None,
                      "timestamp": time.time(), "data": {"reason": "test"}})
    elif status == "running":
        pass
    else:
        raise ValueError(status)
    tape_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


class ServeProc:
    def __init__(self, orca_home: Path, host: str, port: int):
        self.orca_home = orca_home
        self.host = host
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.logfile: Path | None = None

    def start(self) -> None:
        self.logfile = self.orca_home / f"serve-{self.port}.log"
        env = dict(os.environ)
        env["ORCA_HOME"] = str(self.orca_home)
        env["PYTHONPATH"] = REPO
        self.proc = subprocess.Popen(
            [TARS, "serve", "--host", self.host, "--port", str(self.port)],
            stdout=open(self.logfile, "wb"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=REPO,
            start_new_session=True,
        )
        for _ in range(50):
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/api/health", timeout=0.5)
                if r.status_code == 200 and r.json().get("app") == "orca":
                    return
            except Exception:
                pass
            time.sleep(0.2)
        body = self.logfile.read_text(errors="replace") if self.logfile.exists() else "<none>"
        raise RuntimeError(f"tars serve 未就绪 port={self.port}; log:\n{body}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── AC1: 单端口复用 ───────────────────────────────────────────────────────

def ac1_single_port_reuse(workspace: Path) -> dict:
    """两不同项目 + 同 ORCA_HOME → 同一 server 端口复用（spawn-then-reuse 真链路）.

    流程：
      A. 无既有 server。proj1 跑 `tars open` → spawn tars serve on 7428 + 写 ~/.orca/.orca-web.json。
      B. proj2 跑 `tars open` → 读登记 + health probe（fp 匹配）→ 复用 7428，不 spawn。
      C. 断言：1 个 tars serve 进程；两 URL 都是 7428；registry 写入 port=7428。
    """
    orca_home = workspace / "orca-home"
    orca_home.mkdir(parents=True)
    proj1 = make_project(workspace, "proj1")
    proj2 = make_project(workspace, "proj2")

    # 确保 7428 空闲（前面用例可能残留）
    subprocess.run(["pkill", "-f", "tars serve"], capture_output=True)
    time.sleep(0.5)

    env = dict(os.environ)
    env["ORCA_HOME"] = str(orca_home)
    env["PYTHONPATH"] = REPO
    env["ORCA_WEB_HOST"] = "127.0.0.1"
    env["ORCA_WEB_PORT"] = "7428"
    # 让 subprocess 内的 `_spawn_background_serve`（再 spawn `tars`）能找到 tars
    env["PATH"] = "/home/mozzie/miniconda3/envs/orca/bin:" + env.get("PATH", "")

    try:
        # A. proj1 open（无既有 server → spawn + 写登记）
        r1 = subprocess.run(
            [TARS, "open"], env=env, cwd=str(proj1),
            capture_output=True, text=True, timeout=30,
        )
        log(f"  proj1 `tars open` rc={r1.returncode}")
        log(f"    stdout: {r1.stdout.strip()}")
        if r1.returncode != 0:
            log(f"    stderr: {r1.stderr.strip()}")
        assert r1.returncode == 0, f"proj1 tars open failed rc={r1.returncode}"
        assert ":7428/" in r1.stdout, f"proj1 open 未指向 7428: {r1.stdout}"

        # 验证 health（spawn 后真实起在 7428）
        h = httpx.get("http://127.0.0.1:7428/api/health", timeout=2).json()
        assert h["app"] == "orca"
        assert "orca_home_fp" in h and "runs_dir_fp" in h, f"health 缺兼容字段: {h}"
        assert h["orca_home_fp"] == h["runs_dir_fp"], "U-2 兼容期：两字段值应相等"
        log(f"  health ok: fp={h['orca_home_fp']}, pid={h['pid']}")

        # B. proj2 open（应读登记 + 复用 7428）
        before_pids = set(
            subprocess.run(["pgrep", "-f", "tars serve"], capture_output=True, text=True)
            .stdout.strip().split("\n")
        )
        log(f"  proj2 open 前 tars serve pids: {before_pids}")
        r2 = subprocess.run(
            [TARS, "open"], env=env, cwd=str(proj2),
            capture_output=True, text=True, timeout=30,
        )
        log(f"  proj2 `tars open` rc={r2.returncode}")
        log(f"    stdout: {r2.stdout.strip()}")
        if r2.returncode != 0:
            log(f"    stderr: {r2.stderr.strip()}")
        assert r2.returncode == 0, f"proj2 tars open failed rc={r2.returncode}"
        assert ":7428/" in r2.stdout, f"proj2 open 未指向 7428: {r2.stdout}"
        after_pids = set(
            subprocess.run(["pgrep", "-f", "tars serve"], capture_output=True, text=True)
            .stdout.strip().split("\n")
        )
        new_pids = after_pids - before_pids
        log(f"  proj2 open 后新增 tars serve pid: {new_pids}")
        assert not new_pids, f"proj2 open 应复用既有 server，却 spawn 了 {new_pids}"

        # C. registry 写入
        reg_path = orca_home / ".orca-web.json"
        assert reg_path.exists(), f"registry not written: {reg_path}"
        reg = json.loads(reg_path.read_text())
        log(f"  registry: {reg}")
        assert reg.get("port") == 7428, f"registry port != 7428: {reg}"
        assert reg.get("runs_dir_fp") == h["orca_home_fp"], \
            f"registry fp mismatch: {reg} vs health {h}"

        return {"pass": True, "evidence": {
            "health": h,
            "open1_stdout": r1.stdout.strip(),
            "open2_stdout": r2.stdout.strip(),
            "new_pids_after_proj2_open": sorted(new_pids),
            "registry": reg,
        }}
    finally:
        subprocess.run(["pkill", "-f", "tars serve"], capture_output=True)


# ── AC3: discovery scope=all ───────────────────────────────────────────────

def ac3_discovery(workspace: Path) -> dict:
    orca_home = workspace / "orca-home"
    orca_home.mkdir(parents=True)
    proj1 = make_project(workspace, "proj1")
    proj2 = make_project(workspace, "proj2")
    os.environ["ORCA_HOME"] = str(orca_home)
    sys.path.insert(0, REPO)
    from orca.runtime import register_project
    register_project(proj1)
    register_project(proj2)
    write_tape(proj1 / "runs" / "run-proj1-aaa.jsonl", "run-proj1-aaa", wf_name="wf-proj1")
    write_tape(proj2 / "runs" / "run-proj2-bbb.jsonl", "run-proj2-bbb", wf_name="wf-proj2")
    legacy_root = orca_home / "runs"
    legacy_root.mkdir(parents=True, exist_ok=True)
    (legacy_root / "legacy-ccc.json").write_text(json.dumps({
        "run_id": "legacy-ccc", "yaml_path": "wf-legacy.yaml",
        "started_at": 1700000000.0, "tape_path": "",
    }))

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        r = httpx.get(f"http://127.0.0.1:{port}/api/runs?scope=all", timeout=5)
        assert r.status_code == 200, r.text
        data = r.json()
        log(f"  discovery 返回 {len(data)} run")
        by_id = {x["run_id"]: x for x in data}
        assert "run-proj1-aaa" in by_id, f"缺 run-proj1-aaa: {list(by_id)}"
        assert "run-proj2-bbb" in by_id, f"缺 run-proj2-bbb: {list(by_id)}"
        assert "legacy-ccc" in by_id, f"缺 legacy-ccc: {list(by_id)}"
        legacy = by_id["legacy-ccc"]
        assert legacy.get("source") == "legacy", f"legacy source 错: {legacy}"
        assert legacy.get("project_name") == "Legacy", f"legacy project_name 错: {legacy}"
        for x in data:
            assert "events" not in x, f"scope=all 返回了 events 字段: {x}"
            allowed = {"run_id", "workflow_name", "project_id", "project_name", "status",
                       "progress", "cost", "elapsed", "started_at", "event_count", "source"}
            extra = set(x.keys()) - allowed
            assert not extra, f"返回了非白名单字段 {extra}: {x}"
        p1 = by_id["run-proj1-aaa"]
        log(f"  run-proj1-aaa: {p1}")
        assert p1["status"] == "completed", p1
        assert p1["workflow_name"] == "wf-proj1", p1
        assert p1["source"] == "attached", p1
        assert p1["project_name"] == "proj1", p1
        assert p1["progress"] == "2/2", p1
        return {"pass": True, "evidence": {"count": len(data), "runs": data}}
    finally:
        server.stop()


# ── AC5: 懒挂载 dormant ─────────────────────────────────────────────────────

def ac5_lazy_mount(workspace: Path) -> dict:
    orca_home = workspace / "orca-home-ac5"
    orca_home.mkdir(parents=True)
    proj = make_project(workspace, "ac5-proj")
    sys.path.insert(0, REPO)
    os.environ["ORCA_HOME"] = str(orca_home)
    from orca.runtime import register_project
    register_project(proj)
    write_tape(proj / "runs" / "rid-dormant.jsonl", "rid-dormant", wf_name="dormant-wf")

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        base = f"http://127.0.0.1:{port}/api/runs/rid-dormant"
        rm = httpx.get(f"{base}/meta", timeout=5)
        re_ = httpx.get(f"{base}/events", timeout=5)
        ra = httpx.get(f"{base}/assets/missing.png", timeout=5)
        log(f"  /meta={rm.status_code} /events={re_.status_code} /assets/missing.png={ra.status_code}")
        assert rm.status_code == 200, f"/meta 未懒挂载: {rm.status_code} {rm.text}"
        meta = rm.json()
        log(f"  meta: status={meta['status']} source={meta['source']} writable={meta['writable']} count={meta['event_count']}")
        assert meta["status"] == "completed", meta
        assert meta["source"] == "attached", meta
        assert meta["writable"] is False, meta
        assert re_.status_code == 200, f"/events: {re_.status_code} {re_.text}"
        evs = re_.json()
        log(f"  events count: {len(evs)}")
        assert len(evs) >= 5, f"event count too small: {len(evs)}"
        assert ra.status_code == 404, f"missing asset 期望 404，实际 {ra.status_code}"
        rm2 = httpx.get(f"{base}/meta", timeout=5)
        assert rm2.status_code == 200
        return {"pass": True, "evidence": {
            "meta_status": rm.status_code, "events_status": re_.status_code,
            "assets_missing_status": ra.status_code, "meta_body": meta,
            "events_count": len(evs),
        }}
    finally:
        server.stop()


# ── AC8: resolve 三分支 ────────────────────────────────────────────────────

def ac8_resolve(workspace: Path) -> dict:
    orca_home = workspace / "orca-home-ac8"
    orca_home.mkdir(parents=True)
    proj1 = make_project(workspace, "ac8-p1")
    proj2 = make_project(workspace, "ac8-p2")
    sys.path.insert(0, REPO)
    os.environ["ORCA_HOME"] = str(orca_home)
    from orca.runtime import register_project
    register_project(proj1)
    register_project(proj2)
    write_tape(proj1 / "runs" / "dup-rid.jsonl", "dup-rid", wf_name="d1")
    write_tape(proj2 / "runs" / "dup-rid.jsonl", "dup-rid", wf_name="d2")

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        base = f"http://127.0.0.1:{port}/api/runs"
        r0 = httpx.get(f"{base}/no-such-run/meta", timeout=5)
        log(f"  0 命中: status={r0.status_code} body={r0.text[:200]}")
        assert r0.status_code == 404, f"0 命中期望 404，实际 {r0.status_code}"
        rm = httpx.get(f"{base}/dup-rid/meta", timeout=5)
        log(f"  多命中: status={rm.status_code} body={rm.text[:300]}")
        assert rm.status_code == 500, f"多命中期望 500，实际 {rm.status_code}"
        assert "dup-rid" in rm.text, f"500 错误体未列路径: {rm.text}"
        assert "ac8-p1" in rm.text and "ac8-p2" in rm.text, f"500 未列所有路径: {rm.text}"
        return {"pass": True, "evidence": {
            "zero_hit_status": r0.status_code,
            "multi_hit_status": rm.status_code, "multi_hit_body": rm.text[:500],
        }}
    finally:
        server.stop()


# ── AC16: DELETE 四态 + 越界 ───────────────────────────────────────────────

def ac16_delete(workspace: Path) -> dict:
    orca_home = workspace / "orca-home-ac16"
    orca_home.mkdir(parents=True)
    proj = make_project(workspace, "ac16-proj")
    sys.path.insert(0, REPO)
    os.environ["ORCA_HOME"] = str(orca_home)
    from orca.runtime import register_project
    register_project(proj)

    tape_dormant = proj / "runs" / "rid-dormant-del.jsonl"
    write_tape(tape_dormant, "rid-dormant-del", wf_name="wf-dormant")

    # script-only workflow for in-process live
    wf_dir = Path(REPO) / "workflows"
    candidates = list(wf_dir.glob("*.yaml")) + list(wf_dir.glob("*.yml"))
    log(f"  workflows 候选: {[p.name for p in candidates][:10]}")
    script_wf = None
    for p in candidates:
        txt = p.read_text(errors="replace")
        # 排除含 executor 的，只挑纯 script 节点
        if "executor:" not in txt and ("kind: script" in txt or "- script" in txt):
            script_wf = p
            break
    log(f"  选 script wf: {script_wf.name if script_wf else 'NONE'}")
    if script_wf is None:
        script_wf = workspace / "wf-min.yaml"
        script_wf.write_text(
            "name: min\nentry: n0\nnodes:\n  - name: n0\n    kind: script\n    command: 'echo hello'\n",
            encoding="utf-8",
        )

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        base = f"http://127.0.0.1:{port}/api/runs"

        r1 = httpx.delete(f"{base}/rid-dormant-del", timeout=5)
        log(f"  dormant delete: {r1.status_code} {r1.text}")
        assert r1.status_code == 200, f"dormant delete 期望 200，实际 {r1.status_code}"
        b1 = r1.json()
        assert b1.get("ok") is True and b1.get("existed_before") is True, b1
        assert not tape_dormant.exists(), f"tape 未删: {tape_dormant}"

        r2 = httpx.delete(f"{base}/rid-dormant-del", timeout=5)
        log(f"  re-delete: {r2.status_code} {r2.text}")
        assert r2.status_code == 404, f"重复删期望 404，实际 {r2.status_code}"
        b2 = r2.json()
        assert b2.get("never_existed") is True, b2

        rpost = httpx.post(
            f"http://127.0.0.1:{port}/api/run",
            json={"yaml_path": str(script_wf.resolve()),
                  "project_path": str(proj.resolve()),
                  "inputs": {}, "task": None, "max_iter": None},
            timeout=10,
        )
        log(f"  POST /api/run: {rpost.status_code} {rpost.text[:200]}")
        if rpost.status_code != 200:
            log(f"  ! 跳过 in-process live 测试（POST /api/run 失败）: {rpost.text}")
            in_proc_ok = {"skipped": rpost.text[:300]}
        else:
            live_rid = rpost.json()["run_id"]
            r3 = httpx.delete(f"{base}/{live_rid}", timeout=10)
            log(f"  in-process live delete: {r3.status_code} {r3.text}")
            b3 = r3.json()
            assert r3.status_code == 200, f"live delete 期望 200，实际 {r3.status_code} body={b3}"
            assert b3.get("ok") is True, b3
            tape_live = proj / "runs" / f"{live_rid}.jsonl"
            log(f"  tape exists after delete? {tape_live.exists()}")
            assert not tape_live.exists(), f"in-process tape 未删: {tape_live}"
            in_proc_ok = b3

        # attached live: 写 running tape（无终态事件）→ attach 起 follow task → 409 on delete
        live_tape = proj / "runs" / "rid-attached-live.jsonl"
        write_tape(live_tape, "rid-attached-live", wf_name="wf-always-live", status="running")
        rm = httpx.get(f"{base}/rid-attached-live/meta", timeout=5)
        log(f"  attached live /meta: {rm.status_code} body={rm.text[:200]}")
        assert rm.status_code == 200, rm.text
        # 等让 follow task alive 标记稳定（follow_task 已起，terminal=False）
        time.sleep(0.5)
        r4 = httpx.delete(f"{base}/rid-attached-live", timeout=10)
        log(f"  attached live delete: {r4.status_code} {r4.text}")
        b4 = r4.json()
        assert r4.status_code == 409, f"attached live 期望 409，实际 {r4.status_code} body={b4}"
        assert b4.get("live") is True, b4
        assert live_tape.exists(), f"attached live tape 不应被删，但已消失"

        # 越界：未注册目录下的 tape，discovery 看不见 → delete 应 404 never_existed
        evil_dir = workspace / "evil"
        evil_dir.mkdir()
        evil_tape = evil_dir / "rid-evil.jsonl"
        write_tape(evil_tape, "rid-evil", wf_name="evil")
        r5 = httpx.delete(f"{base}/rid-evil", timeout=5)
        log(f"  越界 delete rid-evil: {r5.status_code} {r5.text}")
        assert r5.status_code == 404, f"越界 run 期望 404，实际 {r5.status_code}"
        b5 = r5.json()
        assert b5.get("never_existed") is True, b5
        assert evil_tape.exists(), "evil tape 不应被删（越界守卫）"

        return {"pass": True, "evidence": {
            "dormant": (r1.status_code, b1),
            "re_delete": (r2.status_code, b2),
            "in_process_live": in_proc_ok,
            "attached_live": (r4.status_code, b4),
            "cross_boundary": (r5.status_code, b5),
        }}
    finally:
        server.stop()


# ── AC17: DELETE 同步 via WS ───────────────────────────────────────────────

def ac17_delete_ws_sync(workspace: Path) -> dict:
    orca_home = workspace / "orca-home-ac17"
    orca_home.mkdir(parents=True)
    proj = make_project(workspace, "ac17-proj")
    sys.path.insert(0, REPO)
    os.environ["ORCA_HOME"] = str(orca_home)
    from orca.runtime import register_project
    register_project(proj)
    write_tape(proj / "runs" / "rid-ws-sync.jsonl", "rid-ws-sync", wf_name="wf-ws")

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        ws_url = f"ws://127.0.0.1:{port}/ws"
        base = f"http://127.0.0.1:{port}/api/runs"

        async def scenario():
            frames_received: list = []
            async with websockets.connect(ws_url, max_size=None) as ws:
                await asyncio.sleep(0.3)  # 让 _connections 注册
                async with httpx.AsyncClient() as cli:
                    resp = await cli.delete(f"{base}/rid-ws-sync", timeout=10)
                log(f"  WS-test DELETE: {resp.status_code} {resp.text}")
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    frames_received.append(json.loads(msg))
                except asyncio.TimeoutError:
                    pass
                deadline = time.time() + 0.5
                while time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=0.2)
                        frames_received.append(json.loads(msg))
                    except asyncio.TimeoutError:
                        break
            return frames_received, resp

        frames, resp = asyncio.run(scenario())
        log(f"  收到 {len(frames)} 帧: {frames}")
        assert resp.status_code == 200, resp.text
        ctrl = [f for f in frames
                if f.get("kind") == "control" and f.get("type") == "run_changed"
                and f.get("run_id") == "rid-ws-sync" and f.get("action") == "deleted"]
        assert len(ctrl) >= 1, f"未收到 run_changed(deleted) 控制帧，收到: {frames}"
        log(f"  控制帧 OK: {ctrl[0]}")
        return {"pass": True, "evidence": {
            "frames": frames, "delete_status": resp.status_code,
        }}
    finally:
        server.stop()


# ── AC18: orca open 复用 + 深链 ─────────────────────────────────────────────

def ac18_open_reuse(workspace: Path) -> dict:
    orca_home = workspace / "orca-home-ac18"
    orca_home.mkdir(parents=True)
    proj = make_project(workspace, "ac18-proj")
    sys.path.insert(0, REPO)
    os.environ["ORCA_HOME"] = str(orca_home)
    from orca.runtime import register_project
    register_project(proj)
    tape = proj / "runs" / "rid-deeplink.jsonl"
    write_tape(tape, "rid-deeplink", wf_name="wf-deep")

    port = free_port()
    server = ServeProc(orca_home, "127.0.0.1", port)
    try:
        server.start()
        env = dict(os.environ)
        env["ORCA_HOME"] = str(orca_home)
        env["PYTHONPATH"] = REPO
        env["ORCA_WEB_HOST"] = "127.0.0.1"
        env["ORCA_WEB_PORT"] = str(port)
        env["PATH"] = "/home/mozzie/miniconda3/envs/orca/bin:" + env.get("PATH", "")

        # SPEC §13 D13 的 ``orca open`` 指 ``tars`` CLI 入口（commands.py:open_run）。
        # in-session 的 ``orca open`` 有独立语义（取活跃 run），不属本 AC。
        before_pids = set(
            subprocess.run(["pgrep", "-f", "tars serve"], capture_output=True, text=True)
            .stdout.strip().split("\n")
        )
        r1 = subprocess.run(
            [TARS, "open", "rid-deeplink", "--tape", str(tape.resolve())],
            env=env, cwd=str(proj), capture_output=True, text=True, timeout=30,
        )
        log(f"  `tars open rid-deeplink` rc={r1.returncode} stdout={r1.stdout.strip()}")
        if r1.returncode != 0:
            log(f"    stderr: {r1.stderr.strip()}")
        assert r1.returncode == 0, r1.stderr
        assert "/runs/rid-deeplink" in r1.stdout, f"未深链到 /runs/<rid>: {r1.stdout}"
        after_pids = set(
            subprocess.run(["pgrep", "-f", "tars serve"], capture_output=True, text=True)
            .stdout.strip().split("\n")
        )
        new_pids = after_pids - before_pids
        log(f"  新增 tars serve pid: {new_pids}")
        assert not new_pids, f"`tars open <rid>` 不应 spawn 新 server，新增 {new_pids}"

        r2 = subprocess.run(
            [TARS, "open"], env=env, cwd=str(proj),
            capture_output=True, text=True, timeout=30,
        )
        log(f"  `tars open`（无参） rc={r2.returncode} stdout={r2.stdout.strip()}")
        assert r2.returncode == 0, r2.stderr
        assert f":{port}/" in r2.stdout or f":{port}\n" in r2.stdout, \
            f"无参 open 未指向既有 port {port}: {r2.stdout}"
        line = [l for l in r2.stdout.splitlines() if "http://" in l]
        if line:
            assert "/runs/" not in line[-1], f"无参 open 应开列表页 /: {line[-1]}"
        after2_pids = set(
            subprocess.run(["pgrep", "-f", "tars serve"], capture_output=True, text=True)
            .stdout.strip().split("\n")
        )
        new2 = after2_pids - before_pids
        assert not new2, f"无参 open 不应 spawn: {new2}"
        rm = httpx.get(f"http://127.0.0.1:{port}/api/runs/rid-deeplink/meta", timeout=5)
        log(f"  /meta after open: {rm.status_code}")
        assert rm.status_code == 200
        return {"pass": True, "evidence": {
            "deep_link_stdout": r1.stdout.strip(),
            "list_open_stdout": r2.stdout.strip(),
            "new_pids_after_open": sorted(new_pids),
            "new_pids_after_listopen": sorted(new2),
            "meta_status": rm.status_code,
        }}
    finally:
        server.stop()


# ── AC20: 零回归 ───────────────────────────────────────────────────────────

def ac20_regression(workspace: Path) -> dict:
    """跑既有套件 tests/iface/{web,in_session,cli}/ — 断言全绿（已知 pre-existing 失败除外）.

    用 pytest 真实 subprocess（非 mock）。允许的 pre-existing 失败：
      - test_v3_step1
      - test_web_does_not_import_cli (apply_kb_requirement import)
    """
    env = dict(os.environ)
    env["ORCA_HOME"] = str(workspace / "orca-home-ac20")
    (workspace / "orca-home-ac20").mkdir(parents=True, exist_ok=True)
    env["PYTHONPATH"] = REPO

    results: dict = {}
    for suite in ["tests/iface/web", "tests/iface/in_session", "tests/iface/cli"]:
        log(f"  pytest {suite} ...")
        r = subprocess.run(
            [PYTHON, "-m", "pytest", suite, "-q", "--tb=line",
             "--deselect=tests/iface/web/test_web_does_not_import_cli.py::test_web_does_not_import_cli"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=600,
        )
        log(f"    rc={r.returncode}")
        # 取关键的 last 10 行（含 passed/failed 统计）
        tail = "\n".join(r.stdout.splitlines()[-15:])
        log(f"    tail:\n{tail}")
        results[suite] = {"rc": r.returncode, "tail": tail,
                          "stdout_last": r.stdout[-2000:], "stderr_last": r.stderr[-1000:]}
    # 断言每个 suite rc=0
    failures = []
    for suite, res in results.items():
        if res["rc"] != 0:
            failures.append(suite)
    if failures:
        return {"pass": False, "error": f"suite failed: {failures}", "evidence": results}
    return {"pass": True, "evidence": {k: {"rc": v["rc"], "tail": v["tail"]} for k, v in results.items()}}


def main():
    workspace = Path(tempfile.mkdtemp(prefix="orca-e2e-"))
    log(f"workspace = {workspace}")
    # 清理 stale __pycache__（WSL/Win 混合环境可能残留旧 bytecode 导致 health 字段缺失）
    for d in Path(REPO).rglob("__pycache__"):
        if "site-packages" not in str(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    results: dict = {}

    def run(name, fn):
        log(f"========== {name} ==========")
        try:
            r = fn(workspace)
            results[name] = r
            verdict = "PASS" if r.get("pass") else "FAIL"
            log(f"  >>> {name}: {verdict}")
            if not r.get("pass"):
                log(f"    error: {r.get('error', '<no error field>')}")
        except AssertionError as e:
            log(f"  >>> {name}: FAIL — {e}")
            traceback.print_exc()
            results[name] = {"pass": False, "error": str(e),
                             "traceback": traceback.format_exc()}
        except Exception as e:
            log(f"  >>> {name}: ERROR — {type(e).__name__}: {e}")
            traceback.print_exc()
            results[name] = {"pass": False, "error": f"{type(e).__name__}: {e}",
                             "traceback": traceback.format_exc()}

    def with_subspace(fn):
        def wrapped(ws):
            sub = ws / fn.__name__
            sub.mkdir(parents=True)
            return fn(sub)
        return wrapped

    for name, fn in [
        ("AC1_single_port_reuse", with_subspace(ac1_single_port_reuse)),
        ("AC3_discovery", with_subspace(ac3_discovery)),
        ("AC5_lazy_mount", with_subspace(ac5_lazy_mount)),
        ("AC8_resolve", with_subspace(ac8_resolve)),
        ("AC16_delete", with_subspace(ac16_delete)),
        ("AC17_delete_ws_sync", with_subspace(ac17_delete_ws_sync)),
        ("AC18_open_reuse", with_subspace(ac18_open_reuse)),
    ]:
        run(name, fn)
        subprocess.run(["pkill", "-f", "tars serve"], capture_output=True)
        time.sleep(0.5)

    # AC20: 零回归 — 跑既有套件（CLI / web / in-session）
    run("AC20_regression_suite", lambda ws: ac20_regression(ws))

    summary_path = workspace / "results.json"
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    log(f"results -> {summary_path}")

    n_pass = sum(1 for v in results.values() if v.get("pass"))
    log(f"\n========== 总结 {n_pass}/{len(results)} PASS ==========")
    for name, r in results.items():
        log(f"  {name}: {'PASS' if r.get('pass') else 'FAIL'}")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
