#!/usr/bin/env python3
"""ensure_chart_daemon.py —— 为纯 skill 调用起 session chart daemon + 写 orca_env.sh。

**为什么需要**：chart daemon 是 per-orca-run 的（由 ``orca <wf>`` bootstrap 起）。纯 skill
调用没有 workflow run → 没 daemon → push_results 的 render_chart fail loud → 回退 JSON，
推不到 web。本脚本补这个缺口：挑一个 session run_id，probe socket，死了就 detach 起
``orca.iface.in_session.chart_daemon``，并写 ``orca_env.sh``（含 render_chart 强依赖的 4 个
``ORCA_*`` 身份键）。之后 push_results 的 ``load_run_env_from_artifacts`` 从 results.jsonl
向上找到本 orca_env.sh → 补 env → live 推图通。

**幂等**：同 work-dir 派生同 run_id → 同 socket 路径。daemon 活着 → probe 命中 → 不重起。
daemon 死了（TTL/被杀）→ 重起。socket 路径由 ``chart_sock_path(run_id)`` 确定性派生，
重起复用同一路径，orca_env.sh 不需改。

**results.jsonl 必须落 work-dir 内**（默认 ``<work-dir>/results.jsonl``）——
``load_run_env_from_artifacts`` 从 results.jsonl 向上找 orca_env.sh，work-dir 即其父。

**依赖**：需 orca 可 import（``chart_sock_path``）+ 可 ``-m orca.iface.in_session.chart_daemon``
（依赖 pydantic 等）。本脚本在 orca 侧（opencode session 宿主）跑，不在裸训练服务器跑。
orca 未装 → fail loud（exit 1），调用方（SKILL.md）应视推图为 sidecar、容错回退 JSON。

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


def _sock_path(run_id: str) -> Path:
    """与 daemon 同源派生 socket 路径（orca.chart._paths.chart_sock_path）。"""
    from orca.chart._paths import chart_sock_path

    return Path(chart_sock_path(run_id))


def _alive(sock_path: Path) -> bool:
    """connect 探（socket 文件存在 ≠ 有监听者；connect 成功才算活）。"""
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _write_env(env_file: Path, run_id: str, sock: Path) -> None:
    """写 orca_env.sh（4 身份键）。load_run_env_from_artifacts 的标志行 = ^export ORCA_CHART_SOCK=。"""
    env_file.write_text(
        f"export ORCA_RUN_ID={run_id}\n"
        f"export ORCA_NODE={_NODE}\n"
        f"export ORCA_SESSION_ID={_SESSION}\n"
        f"export ORCA_CHART_SOCK={sock}\n",
        encoding="utf-8",
    )


def _spawn(
    python: str, run_id: str, tape: Path, sock: Path, ttl: int, log: Path
) -> None:
    """detach 起 chart daemon（start_new_session 脱父进程组，跨本脚本退出存活）。"""
    # stale socket 文件（上次 daemon 被杀未清理）→ bind 会冲突，先 unlink。
    try:
        sock.unlink(missing_ok=True)
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
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_fd.close()


def _wait_alive(sock: Path, timeout: float = 8.0) -> bool:
    """轮询 connect 探等 daemon bind+listen 就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _alive(sock):
            return True
        time.sleep(0.1)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="为纯 skill 调用起 session chart daemon + 写 orca_env.sh（contracts §7）。"
    )
    ap.add_argument(
        "--work-dir", required=True,
        help="session 工作目录：orca_env.sh / tape.jsonl / results.jsonl 都落此。results.jsonl 必须在此目录内。",
    )
    ap.add_argument(
        "--run-id", default=None,
        help="run_id（默认按 work-dir 绝对路径 sha1[:8] 派生，稳定可复用）。同 work-dir → 同 daemon。",
    )
    ap.add_argument(
        "--ttl", type=int, default=86_400,
        help="daemon 自退 TTL 秒（默认 24h；daemon 无终态事件，靠 TTL 兜底防泄漏）。",
    )
    ap.add_argument(
        "--python", default=sys.executable,
        help="起 daemon 用的 python（默认本脚本的解释器，需能 -m orca.iface.in_session.chart_daemon）。",
    )
    args = ap.parse_args(argv)

    try:
        from orca.chart._paths import chart_sock_path  # noqa: F401 —— 探测 orca 可 import
    except ImportError as e:
        print(
            f"DAEMON_FAILED reason=orca 不可 import（{e}）—— live 推图不可用，"
            f"调用方应回退 push_results 的 fallback JSON。",
            file=sys.stderr,
        )
        return 1

    work = Path(args.work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"rx-sweep-{hashlib.sha1(str(work).encode()).hexdigest()[:8]}"
    sock = _sock_path(run_id)
    tape = work / "tape.jsonl"
    env_file = work / "orca_env.sh"
    log = work / "chart_daemon.log"

    _write_env(env_file, run_id, sock)  # 幂等：每次写（路径不变，内容一致）

    spawned = False
    if not _alive(sock):
        _spawn(args.python, run_id, tape, sock, args.ttl, log)
        spawned = True
        if not _wait_alive(sock):
            print(
                f"DAEMON_FAILED reason=socket 起来后 connect 探仍失败（见 {log}）",
                file=sys.stderr,
            )
            return 1

    results_path = work / "results.jsonl"
    print(
        f"DAEMON_READY run_id={run_id} sock={sock} work_dir={work} "
        f"env_file={env_file} tape={tape} results_path={results_path} "
        f"spawned={'true' if spawned else 'false(已活)'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
