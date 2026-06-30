"""tests/iface/web/conftest.py —— web 后端测试共享 fixtures + helpers。

约定（同 tests/run/conftest.py）：本仓库不用 pytest-asyncio，异步统一 ``asyncio.run``。
``run_async`` / ``make_manager`` / ``demo_yaml`` 在本文件定义，被同包测试引用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from orca.iface.web.run_manager import RunManager


def run_async(coro):
    """统一 asyncio.run（无 pytest-asyncio）。"""
    return asyncio.run(coro)


def demo_linear_yaml(tmp_path: Path) -> Path:
    """最小线性纯 script workflow（a→b→$end，零 token，零 claude 依赖）。

    用本文件现造（不依赖 examples/），保证测试自包含。
    """
    p = tmp_path / "demo.yaml"
    p.write_text(
        """
name: demo
description: 线性纯 script demo（测试用）
entry: a
nodes:
  - name: a
    kind: script
    command: "echo step_a"
    routes:
      - to: b
  - name: b
    kind: script
    command: "echo step_b"
    routes:
      - to: $end
outputs:
  result: "{{ b.output.stdout }}"
""",
        encoding="utf-8",
    )
    return p


def make_manager(tmp_path: Path, max_concurrent: int = 3) -> RunManager:
    """构造 RunManager（runs_dir 写 tmp_path，不污染 cwd）。"""
    return RunManager(max_concurrent=max_concurrent, runs_dir=tmp_path / "runs")


@pytest.fixture
def manager(tmp_path: Path) -> RunManager:
    """默认 RunManager fixture（max_concurrent=3，runs 写 tmp_path）。"""
    return make_manager(tmp_path)


@pytest.fixture
def yaml_path(tmp_path: Path) -> Path:
    """demo workflow yaml 路径（纯 script，零 claude）。"""
    return demo_linear_yaml(tmp_path)
