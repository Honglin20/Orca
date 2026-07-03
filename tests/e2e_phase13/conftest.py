"""tests/e2e_phase13/conftest.py —— phase-13 e2e 公共 fixture。

SPEC §8.3 / §8.4：E2E-1~5 真 run + E2E-6 opencode+deepseek TUI。

关键约束：
  - **macOS SOCK_PATH_MAX 90**：runs_dir 用 ``/tmp/orca-<test>/runs/`` 短路径
    （pytest tmp_path 通常 > 90 字节，会触发 RunManager.start_run 的 RuntimeError）。
  - **真调 orca.chart.render_chart**：script 内真调客户端库走完整 socket 链路。
  - **多 run 并行**：用 ``RunManager.start_run``（不是 ``orca run`` CLI）。
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent / "scripts"
WORKFLOWS_DIR = Path(__file__).parent / "workflows"
ARTIFACTS_DIR = Path(__file__).parent / "_artifacts"


@pytest.fixture
def short_runs_dir(tmp_path) -> Path:
    """macOS-friendly 短 runs_dir（< SOCK_PATH_MAX=90 字节）。

    用 ``/tmp/orca-<hash>/runs/`` —— pytest tmp_path 通常在 ``/var/folders/.../`` 长 > 100。
    每个测试独立 hash 避免跨测试 sock 冲突。
    """
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    short = Path(f"/tmp/orca-p13-{h}/runs")
    short.mkdir(parents=True, exist_ok=True)
    yield short
    # 清理（避免 /tmp 累积）
    shutil.rmtree(short.parent, ignore_errors=True)


@pytest.fixture
def chart_demo_script() -> Path:
    return SCRIPTS_DIR / "chart_demo.py"


@pytest.fixture
def chart_parallel_script() -> Path:
    return SCRIPTS_DIR / "chart_parallel.py"


@pytest.fixture
def chart_pressure_script() -> Path:
    return SCRIPTS_DIR / "chart_pressure.py"


@pytest.fixture
def chart_large_script() -> Path:
    return SCRIPTS_DIR / "chart_large.py"


@pytest.fixture
def artifacts_dir() -> Path:
    ARTIFACTS_DIR.mkdir(exist_ok=True)
    return ARTIFACTS_DIR
