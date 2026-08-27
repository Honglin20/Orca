"""layout.py —— workflows 目录布局双形态解析（单一真相源）。

2026-08 per-workflow 目录隔离改造（plan 2026-08-27 批 C）：workflows 目录从平铺布局
（根 ``*.yaml`` + 共享 ``agents/`` + ``subagents/<wf-name>/``）迁移到 per-wf 自包含
（``<wf>/workflow.yaml + <wf>/agents/ + <wf>/subagents/ + ...``）。迁移期与向后兼容期
**双形态并存**，本模块是双形态解析公式的唯一实现——catalog（yaml 扫描）、validator
（subagents md 校验）、orchestrator（``RunContext.subagents_root``）三处消费方一律
import 此处，禁止各自复制实现（DRY；plan-adversary Q10 验证点：三处必须真共享）。

依赖单向：本模块属 ``orca.compile``，仅依赖 stdlib（pathlib）；被 compile 内
（catalog / validator）与上层 run 层（orchestrator）import 合法（compile ← run），
不依赖 run/exec/iface。
"""

from __future__ import annotations

from pathlib import Path


def scan_workflow_yamls(d: Path) -> list[Path]:
    """双形态收集目录下的 workflow yaml：平铺 ``*.yaml`` 优先 + per-wf ``*/workflow.yaml``。

    平铺优先：同目录两形态混存且 yaml 内 ``name`` 撞名时，catalog 的 first-wins
    （先见者胜）取平铺那份——列表序即优先级。name 匹配语义不变（按 yaml 内 ``name``
    字段，不是文件名/目录名）。
    """
    return sorted(d.glob("*.yaml")) + sorted(d.glob("*/workflow.yaml"))


def resolve_subagents_dir(workflow_dir: Path | None, wf_name: str) -> Path | None:
    """解析 point-to-file 协议的 subagents 目录（双形态）。

    - 新形态（per-wf）：``workflow_dir/subagents/`` 是目录**且其下有直接 ``*.md````
      → 返回该目录（subagents/ 与 workflow.yaml 同目录）。
    - 旧形态（平铺）：``workflow_dir/subagents/<wf_name>/`` 是目录 → 返回该目录。
    - 均未命中 → ``None``（无子 agent 的 workflow 正常，SPEC §3.3）。

    误命中坑（回归锁）：旧形态下 ``workflow_dir/subagents`` 目录本身存在（md 在二级
    ``<wf_name>/`` 内），**不能只查 is_dir** 判新形态——否则平铺布局解析到无 md 的
    ``subagents/`` 壳目录，agent Read 全空。必须 ``any(sub.glob("*.md"))`` 才判新形态。
    """
    if workflow_dir is None or not wf_name:
        return None
    sub = workflow_dir / "subagents"
    if sub.is_dir() and any(sub.glob("*.md")):
        return sub
    legacy = sub / wf_name
    return legacy if legacy.is_dir() else None
