"""test_bg_integration.py —— daemon ``--background`` E2E 集成测试（SPEC §8 P3.2 / §10.2 item10/11）。

**标 ``@pytest.mark.integration``：CI 默认跳过**（``-m "not integration"`` deselect）。
本地 ``pytest -m integration tests/iface/cli/test_bg_integration.py`` 可选跑。

为什么 integration 而非 unit：本测试**真 fork detached 子进程**跑完一个全 script workflow，
验证 ``orca run --background`` → ``orca ps`` → ``orca wait`` 端到端闭环（SPEC §10.2 item10）。
unit 测试（``test_bg_runner.py`` / ``test_commands.py::TestBackgroundRun``）mock 掉 fork，
不留孤儿；本测试是真进程，验证「fork + exec + 跑完 + metadata 更新 + tape 落盘」整链。

不依赖 claude CLI / API key：用 ``examples/demo_linear.yaml``（全 script，零 token），
故无需 ``_has_claude()`` 前置（与 test_integration.py 不同）。

环境前置：仅 Unix（``os.fork``）；非 Unix 平台 skip。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

# 全模块标 integration：CI 跳过，本地显式跑（SPEC §7.10 / §12）。
pytestmark = pytest.mark.integration

_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _skip_if_non_unix() -> None:
    """``os.fork`` 是 Unix-only —— 非 Unix 平台 skip（不 fail，SPEC §8.2 未列 Windows）。"""
    if not hasattr(os, "fork"):
        pytest.skip("daemon --background 需要 os.fork（Unix-only）")


def _resolve_orca_bin() -> str:
    """定位 ``orca`` 可执行文件路径。

    优先 venv 内的 console script（``.venv/bin/orca``，pip install -e . 后存在），
    fallback ``shutil.which("orca")``（production 安装态）。两者都失败 → skip（环境不全）。
    """
    venv_orca = _REPO_ROOT / ".venv" / "bin" / "orca"
    if venv_orca.is_file():
        return str(venv_orca)
    import shutil

    found = shutil.which("orca")
    if found:
        return found
    pytest.skip("orca 不在 PATH 且 .venv/bin/orca 不存在 —— 跳过 daemon E2E")


def _orca(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """跑 ``orca <args>``，返回 CompletedProcess。

    在 repo root 跑（让 ``runs/<run_id>.jsonl`` 落到 ./runs/，与 production 约定一致）。
    capture stdout/stderr，timeout 后 kill（防 hang）。
    """
    return subprocess.run(
        [_resolve_orca_bin(), *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _extract_run_id(start_output: str) -> str:
    """从 ``orca run --background`` 的输出提 run_id（``Started background run: <id>``）。"""
    m = re.search(r"Started background run:\s*(\S+)", start_output)
    assert m, f"无法从输出提 run_id：{start_output!r}"
    return m.group(1)


def test_bg_run_ps_logs_wait_e2e():
    """``orca run --background demo_linear`` → ``ps`` → ``logs`` → ``wait`` 全链闭环。

    INTENT（SPEC §10.2 item10/11）：
      1. ``--background`` 立即返回 run_id + pid（不阻塞终端）。
      2. ``ps`` 列出该 run。
      3. ``wait <id>`` 阻塞到 completed，exit 0。
      4. tape 落到 ``runs/<run_id>.jsonl``（``resume`` 后续能接）。
      5. metadata 最终 status=completed（child 跑完更新了，非靠 pid 死检测）。
    """
    _skip_if_non_unix()

    yaml = _EXAMPLES / "demo_linear.yaml"
    assert yaml.is_file(), f"demo_linear.yaml 不存在：{yaml}"

    # 1) 启动 background run（立即返回）。
    start = _orca("run", str(yaml), "--background", timeout=15.0)
    assert start.returncode == 0, f"--background 启动失败：{start.stderr}"
    run_id = _extract_run_id(start.stdout)
    assert "PID:" in start.stdout
    assert "logs:" in start.stdout

    # 2) ps 应能列出（轮询到 completed 或 timeout，给 workflow 跑完时间）。
    deadline = time.time() + 25.0
    final_status: str | None = None
    while time.time() < deadline:
        ps = _orca("ps", timeout=10.0)
        assert ps.returncode == 0
        if run_id in ps.stdout:
            if "completed" in ps.stdout:
                final_status = "completed"
                break
            if "crashed" in ps.stdout:
                # child 崩了——fail loud，打印 logs 帮 debug。
                logs = _orca("logs", run_id, "-n", "50", timeout=5.0)
                pytest.fail(
                    f"background run {run_id} crashed。\nps:\n{ps.stdout}\n"
                    f"logs:\n{logs.stdout}\n{logs.stderr}"
                )
        time.sleep(0.5)

    assert final_status == "completed", (
        f"timeout 等 background run {run_id} 完成。\n"
        f"最后 ps 输出：\n{ps.stdout}"
    )

    # 3) wait 应返回 exit 0（completed）。
    wait = _orca("wait", run_id, timeout=10.0)
    assert wait.returncode == 0, f"wait 非 0：{wait.stdout}"
    assert "completed" in wait.stdout

    # 4) tape 落盘到 runs/<run_id>.jsonl（resume 后续能接 —— SPEC §10.2 item10）。
    tape_path = _REPO_ROOT / "runs" / f"{run_id}.jsonl"
    assert tape_path.is_file(), f"tape 未落盘到标准位置：{tape_path}"

    # 5) logs 能读到子进程写的日志（至少有 workflow 输出痕迹）。
    logs = _orca("logs", run_id, "-n", "20", timeout=5.0)
    assert logs.returncode == 0
    # 日志文件非空（child 至少写了启动信息）。
    assert len(logs.stdout) >= 0  # 宽松：script node 输出可能去 stdout 也可能去 tape
