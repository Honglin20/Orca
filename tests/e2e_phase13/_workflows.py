"""tests/e2e_phase13/_workflows.py —— 动态生成 phase-13 e2e workflow YAML。

每个 test 用例的 script path 不同（指向不同 scripts/*.py + 不同 argv），动态写 YAML 到
tmp_path 再用 ``load_workflow`` 加载最简洁。
"""

from __future__ import annotations

import textwrap
from pathlib import Path


def write_workflow(tmp_path: Path, yaml_body: str, name: str = "p13_wf") -> Path:
    """把 YAML body 写到 ``<tmp_path>/<name>.yaml`` 并返回路径。"""
    p = tmp_path / f"{name}.yaml"
    p.write_text(textwrap.dedent(yaml_body).lstrip(), encoding="utf-8")
    return p


def basic_chart_wf(tmp_path: Path, script: Path) -> Path:
    """E2E-1：单 script node → 调 chart_demo.py 推 line chart。"""
    return write_workflow(
        tmp_path,
        f"""
        name: p13_basic
        description: E2E-1 single script chart
        entry: worker
        nodes:
          - name: worker
            kind: script
            command: "python3 {script}"
            routes:
              - to: $end
        outputs:
          result: "{{{{ worker.output.stdout }}}}"
        """,
        name="p13_basic",
    )


def parallel_chart_wf(tmp_path: Path, script: Path) -> Path:
    """E2E-2：3 个 parallel script 同时跑（3 个独立 run 各推一张 bar）。

    实际「multi-run parallel」由 test 起多个 RunManager.start_run 实现，单 workflow
    只需 1 个 script node（每 run 各跑一份）。这里保持 workflow 极简。
    """
    return write_workflow(
        tmp_path,
        f"""
        name: p13_parallel
        description: E2E-2 multi-run parallel（test 起 N 个 run 各跑一份）
        entry: worker
        nodes:
          - name: worker
            kind: script
            command: "python3 {script}"
            routes:
              - to: $end
        outputs:
          result: "{{{{ worker.output.stdout }}}}"
        """,
        name="p13_parallel",
    )


def large_chart_wf(tmp_path: Path, script: Path, rows: int, max_points: int) -> Path:
    """E2E-3 / E2E-4：大数据降采样 / 超限拒绝。

    rows=100_000 + max_points=2000 → 降采样通过（E2E-3）。
    rows=500_000 + max_points=200_000 → client raise + script exit 2（E2E-4）。
    """
    return write_workflow(
        tmp_path,
        f"""
        name: p13_large
        description: E2E-3/4 大数据 + max_points
        entry: worker
        nodes:
          - name: worker
            kind: script
            command: "python3 {script} {rows} {max_points}"
            routes:
              - to: $end
        outputs:
          result: "{{{{ worker.output.stdout }}}}"
        """,
        name="p13_large",
    )


def pressure_chart_wf(tmp_path: Path, script: Path) -> Path:
    """E2E-5：压测（3 run × 10 chart）—— 单 run 1 script 推 10 chart。

    多 run 由 test 起 3 个 RunManager.start_run 实现。
    """
    return write_workflow(
        tmp_path,
        f"""
        name: p13_pressure
        description: E2E-5 pressure（multi-run 由 test 控制）
        entry: worker
        nodes:
          - name: worker
            kind: script
            command: "python3 {script}"
            routes:
              - to: $end
        outputs:
          result: "{{{{ worker.output.stdout }}}}"
        """,
        name="p13_pressure",
    )
