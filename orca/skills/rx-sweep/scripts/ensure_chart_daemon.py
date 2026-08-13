#!/usr/bin/env python3
"""ensure_chart_daemon.py —— 为纯 skill 调用起 session chart daemon + 建 web 可发现的 run。

**为什么需要**：chart daemon 是 per-orca-run 的（由 ``orca <wf>`` bootstrap 起）。纯 skill
调用没有 workflow run → 没 daemon → push_results 的 render_chart fail loud → 回退 JSON，
推不到 web。本脚本补这个缺口，并把 run 写成 **web 列表可发现** 的形态：

  <runs-dir>/<run_id>.jsonl    # tape（daemon 写 chart 事件至此；web 读它渲染图）
  <runs-dir>/<run_id>.json     # sidecar（web 列表发现 run 的元数据：run_id/yaml_path/tape_path/status）
  <runs-dir>/orca_env.sh       # 4 个 ORCA_* 身份键（push_results source 后 render_chart 可用）

daemon 指向该 tape。push_results ``source orca_env.sh`` 后 render_chart → socket → daemon → tape。
web 扫 ``<runs-dir>/*.json`` sidecar 发现 run → 列表显示 → 点开读 tape 渲染图。

**默认 runs-dir = ~/.orca/runs**（web ``~/.orca/.orca-web.json`` 的 runs_dir_fp 绑定 ~/.orca）。
可用 ``--runs-dir`` 覆盖（指向其它已注册的 runs 目录）。

**幂等**：同 runs-dir + run_id → 同 tape/sidecar/socket。daemon 活着 → probe 命中 → 不重起。
sidecar 每次重写（started_at 不变则内容一致；pid/status 可能刷新）。

**依赖**：需 orca 可 import（``chart_sock_path``）+ 可 ``-m orca.iface.in_session.chart_daemon``
（依赖 pydantic 等）。orca 未装 → fail loud（exit 1）；调用方应视推图为 sidecar、容错回退 JSON。

契约：reference/contracts.md §7。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_NODE = "rx-sweep"
_SESSION = "rx-sweep"
_YAML = "rx-sweep"  # sidecar yaml_path（无实际 yaml，仅作 web 列表显示名）


def _sock_path(run_id: str) -> Path:
    from orca.chart._paths import chart_sock_path

    return Path(chart_sock_path(run_id))


def _alive(sock_path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _write_env(env_file: Path, run_id: str, sock: Path) -> None:
    """写 orca_env.sh（4 身份键）。load_run_env_from_artifacts 标志行 = ^export ORCA_CHART_SOCK=。"""
    env_file.write_text(
        f"export ORCA_RUN_ID={run_id}\n"
        f"export ORCA_NODE={_NODE}\n"
        f"export ORCA_SESSION_ID={_SESSION}\n"
        f"export ORCA_CHART_SOCK={sock}\n",
        encoding="utf-8",
    )


def _write_sidecar(sidecar: Path, run_id: str, tape: Path, pid: int, started_at: float) -> None:
    """写 web 列表发现用的 sidecar（格式同 orca run 的 <run_id>.json）。

    tape_path 用绝对路径，避免 web 相对路径解析歧义。
    """
    import json

    sidecar.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "pid": pid,
                "yaml_path": _YAML,
                "started_at": started_at,
                "log_path": str(tape.parent / run_id / "log"),
                "tape_path": str(tape),
                "status": "running",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _spawn(python: str, run_id: str, tape: Path, sock: Path, ttl: int, log: Path) -> int:
    """detach 起 chart daemon。返 daemon 子进程 pid（best-effort，Popen 后可能已回收）。"""
    try:
        sock.unlink(missing_ok=True)  # stale socket → bind 冲突，先清
    except OSError:
        pass
    tape.parent.mkdir(parents=True, exist_ok=True)
    tape.touch(exist_ok=True)
    cmd = [
        python, "-m", "orca.iface.in_session.chart_daemon",
        "--run-id", run_id, "--tape", str(tape), "--ttl", str(ttl),
    ]
    log_fd = open(log, "a", encoding="utf-8")
    try:
        popen = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
        return popen.pid
    finally:
        log_fd.close()


def _wait_alive(sock: Path, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _alive(sock):
            return True
        time.sleep(0.1)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="起 session chart daemon + 建 web 可发现的 run（contracts §7）。"
    )
    ap.add_argument(
        "--runs-dir", default=None,
        help="web 扫描的 runs 目录（默认 ~/.orca/runs，即 web 绑定处）。run tape + sidecar 落此。",
    )
    ap.add_argument(
        "--run-id", default=None,
        help="run_id（默认 rx-sweep-<sha1(runs-dir)[:8]>，稳定可复用）。",
    )
    ap.add_argument("--ttl", type=int, default=86_400, help="daemon 自退 TTL 秒（默认 24h）。")
    ap.add_argument(
        "--python", default=sys.executable,
        help="起 daemon 的 python（需能 -m orca.iface.in_session.chart_daemon）。",
    )
    args = ap.parse_args(argv)

    try:
        from orca.chart._paths import chart_sock_path  # noqa: F401
    except ImportError as e:
        print(
            f"DAEMON_FAILED reason=orca 不可 import（{e}）—— live 推图不可用，"
            f"调用方应回退 push_results 的 fallback JSON。",
            file=sys.stderr,
        )
        return 1

    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else Path.home() / ".orca" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"rx-sweep-{hashlib.sha1(str(runs_dir).encode()).hexdigest()[:8]}"
    tape = runs_dir / f"{run_id}.jsonl"
    sidecar = runs_dir / f"{run_id}.json"
    env_file = runs_dir / "orca_env.sh"
    log = runs_dir / run_id / "chart_daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    sock = _sock_path(run_id)

    _write_env(env_file, run_id, sock)  # 幂等

    started_at = time.time()
    spawned = False
    pid = 0
    if not _alive(sock):
        pid = _spawn(args.python, run_id, tape, sock, args.ttl, log)
        spawned = True
        if not _wait_alive(sock):
            print(
                f"DAEMON_FAILED reason=socket 起来后 connect 探仍失败（见 {log}）",
                file=sys.stderr,
            )
            return 1

    _write_sidecar(sidecar, run_id, tape, pid, started_at)  # web 列表发现

    print(
        f"DAEMON_READY run_id={run_id} sock={sock} runs_dir={runs_dir} "
        f"tape={tape} sidecar={sidecar} env_file={env_file} "
        f"spawned={'true' if spawned else 'false(已活)'}"
    )
    print(
        f"SOURCE_ENV source {env_file}   # 推图前 source 此，让 push_results 的 render_chart 拿到 ORCA_* env",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
