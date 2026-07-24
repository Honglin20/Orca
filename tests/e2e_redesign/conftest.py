"""conftest.py —— tests/e2e_redesign/ 共享 fixtures。

``recent_run_cleanup``（autouse）：每测试后扫 ``runs/<wf>-*``，清近 600s mtime 的 run
目录 + marker。**mtime 退避**确保绝不碰用户既有的活跃 run（如 kd-nas-20260720，2 天前）。
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests.e2e_redesign.contract import WORKFLOWS

REPO = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO / "runs"
_LOG = logging.getLogger(__name__)

# 清理窗口：只清测试期（近 RECENCY_SEC 秒）创建的 run dir；超出窗口的绝不碰。
_RECENCY_SEC = 600


@pytest.fixture
def recent_run_cleanup():
    """每测试后清近 600s 创建的 8-workflow run dir + marker（不碰用户老 run）。

    **非 autouse**：仅 walk / sentinel 等真创建 run 的测试经 ``usefixtures`` 显式 opt-in；
    纯静态契约测试不创建 run，不付 teardown 开销。
    """
    yield
    # 早退：若无近窗口 run dir，跳过 status 查询（省 ~3-5s/teardown）
    threshold = time.time() - _RECENCY_SEC
    recent = [p for p in _wf_run_dirs() if _mtime_safe(p) > threshold]
    if not recent:
        return
    _cleanup_recent(threshold, exclude_ids=_user_protected_run_ids())


def _wf_run_dirs() -> list[Path]:
    """8 workflow 命名的 run **目录**（过滤掉 .jsonl / .jsonl.lock 等同名文件）。"""
    if not RUNS_DIR.is_dir():
        return []
    out: list[Path] = []
    for stem in WORKFLOWS:
        out.extend(p for p in RUNS_DIR.glob(f"{stem}-*") if p.is_dir())
    return out


def _mtime_safe(p: Path) -> float:
    """取 mtime，文件已删返 0.0（不 raise）。"""
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _user_protected_run_ids() -> set[str]:
    """用户既有活跃/老 run 的 id（绝不删）。经 ``orca status --json`` 查 + mtime 老目录兜底。"""
    ids: set[str] = set()
    try:
        proc = subprocess.run(
            ["orca", "status", "--json"], capture_output=True, text=True,
            timeout=15, check=False,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout or "{}")
            for run in data.get("runs", []):
                rid = run.get("run_id", "")
                if rid:
                    ids.add(rid)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    # 兜底：mtime 老于窗口的 run dir 名也保护（双保险，防 status 漏报）
    threshold = time.time() - _RECENCY_SEC
    for p in _wf_run_dirs():
        try:
            if p.stat().st_mtime <= threshold:
                ids.add(p.name)
        except FileNotFoundError:
            continue
    return ids


def _cleanup_recent(threshold: float, *, exclude_ids: set[str]) -> None:
    """清近 threshold mtime 的 run dir：先 orca stop marker（best-effort），再 rmtree。"""
    for p in _wf_run_dirs():
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if mtime <= threshold or p.name in exclude_ids:
            continue
        # 先 stop marker（best-effort；已终态会 fail，容忍）
        try:
            subprocess.run(
                ["orca", "stop", "--run-id", p.name],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (subprocess.SubprocessError, OSError) as e:
            _LOG.debug("cleanup stop skip %s: %r", p.name, e)
        try:
            shutil.rmtree(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            _LOG.debug("cleanup rmtree skip %s: %r", p, e)
