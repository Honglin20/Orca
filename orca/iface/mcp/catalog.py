"""catalog.py —— workflow catalog（纯函数 + 文件系统，SPEC phase-10 §5.6）。

回答「``list_workflows`` / ``describe_workflow`` 数据从哪来？」：扫项目级 + 用户级
``workflows/`` 目录，用 ``compile.load_workflow`` 加载每个 YAML，提取元信息。

设计约束（§5.6 关键约束）：
  - 纯函数 + 文件系统，无 daemon 注册表、无 db。
  - 加载失败（yaml 语法错 / agent 引用缺失）→ log warning + 跳过，不中断列表。
  - ``find_workflow_by_name`` / ``find_workflow_yaml_path`` 给 ``start_workflow`` /
    ``describe_workflow`` 反查用（name → yaml_path）。

in-session v5 §6.2：setup phase 全栈删除，catalog 不再返 ``has_setup`` / setup 元信息。

依赖单向：本模块依赖 ``orca.compile``（load_workflow）+ ``orca.schema``（Workflow）。
不依赖 run/exec/events。纯函数。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from orca.compile import ConfigurationError, load_workflow
from orca.schema.workflow import Workflow

logger = logging.getLogger(__name__)


# workflow catalog 扫描目录（first-wins：project-local 优先于 user-global）。
def _workflow_dirs() -> list[Path]:
    """catalog 扫描目录列表（project-local + user-global）。

    返回顺序决定 ``find_workflow_by_name`` 的优先级（first-wins）。``~/.orca/workflows``
    在 user home 下，跨 project 共享（与 agent pool 同模式）。
    """
    return [
        Path.cwd() / "workflows",
        Path.home() / ".orca" / "workflows",
    ]


def list_workflows() -> list[dict[str, Any]]:
    """扫描 catalog 目录，返回 workflow 元信息列表（SPEC §5.6 / §2.2）。

    每项字段：``{name, description, entry, inputs_count, inputs_schema}``。
    ``inputs_schema``（v5 §2.3）= ``[{name, type, description}]``，给 ``orca list`` 的
    skill/LLM 选 wf + 抽 inputs（无 describe 命令，一个命令给齐）。

    加载失败的 YAML 跳过（log warning，不中断列表）。
    """
    seen: dict[str, dict[str, Any]] = {}
    for d in _workflow_dirs():
        if not d.is_dir():
            continue
        try:
            yaml_paths = sorted(d.glob("*.yaml"))
        except OSError:
            continue
        for yaml_path in yaml_paths:
            try:
                wf = load_workflow(yaml_path)
            except (ConfigurationError, Exception) as e:  # noqa: BLE001
                logger.warning(
                    "catalog: 跳过 %s（加载失败：%s）", yaml_path, e
                )
                continue
            if wf.name in seen:
                continue  # first-wins（project-local 优先）
            seen[wf.name] = {
                "name": wf.name,
                "description": wf.description,
                "entry": wf.entry,
                "inputs_count": len(wf.inputs),
                # v5 §2.3：orca list 给 skill/LLM 选 wf + 抽 inputs 的全部信息——
                # 一个命令搞定（无 describe）。每项 {name, type, description}，从 wf.inputs 派生。
                "inputs_schema": _inputs_to_schema_list(wf),
            }
    return list(seen.values())


def describe_workflow(wf: Workflow) -> dict[str, Any]:
    """从已加载的 ``Workflow`` 提取详查字典（SPEC §2.2）。

    返回字段：``{name, description, inputs_schema}``。``inputs_schema`` 是 dict
    ``{key: {type, required, description}}``，给 MCP describe_workflow 工具展示用。
    """
    return {
        "name": wf.name,
        "description": wf.description,
        "inputs_schema": _inputs_to_schema(wf),
    }


def find_workflow(name: str) -> tuple[Workflow, str] | None:
    """按 ``wf.name`` 查找并加载 workflow（SPEC §5.6 / §2.3 start_workflow 依赖）。

    **按 workflow name 字段匹配，不是文件名**：用户可把 ``setup_demo`` workflow
    存在 ``my_setup.yaml`` 里，catalog 按 ``wf.name`` 找到它。

    Returns ``(Workflow, yaml_path)`` 元组（DRY：避免 server 层重复扫 catalog 两次）。
    first-wins：project-local 优先于 user-global。未找到 → None。
    """
    for d in _workflow_dirs():
        if not d.is_dir():
            continue
        try:
            yaml_paths = sorted(d.glob("*.yaml"))
        except OSError:
            continue
        for yaml_path in yaml_paths:
            try:
                wf = load_workflow(yaml_path)
            except (ConfigurationError, Exception) as e:  # noqa: BLE001
                logger.warning(
                    "catalog: 加载 %s 失败：%s", yaml_path, e
                )
                continue
            if wf.name == name:
                return (wf, str(yaml_path))
    return None


def find_workflow_by_name(name: str) -> Workflow | None:
    """按 ``wf.name`` 查找并加载 workflow（薄 wrapper，SPEC §5.6）。

    仅返 Workflow（不需要 yaml_path 的调用方用）。需要 yaml_path 的场景调
    ``find_workflow`` 取元组（DRY，避免重复扫 catalog）。
    """
    result = find_workflow(name)
    return result[0] if result is not None else None


def find_workflow_yaml_path(name: str) -> str | None:
    """按 ``wf.name`` 反查 yaml_path（薄 wrapper）。"""
    result = find_workflow(name)
    return result[1] if result is not None else None


def _inputs_to_schema(wf: Workflow) -> dict[str, dict[str, Any]]:
    """wf.inputs → JSON-schema 友好的 ``{key: {type, required, description}}`` 字典。"""
    out: dict[str, dict[str, Any]] = {}
    for key, idef in wf.inputs.items():
        out[key] = {
            "type": idef.type,
            "required": idef.required,
            "description": idef.description,
        }
        if idef.default is not None:
            out[key]["default"] = idef.default
    return out


def _inputs_to_schema_list(wf: Workflow) -> list[dict[str, Any]]:
    """wf.inputs → ``[{name, type, description}, ...]`` 列表（v5 §2.3）。

    给 ``orca list`` 返回的 ``inputs_schema``：skill/LLM 据此从用户意图抽 inputs（一个
    命令给齐「选 wf + 知 inputs」，故无 describe 命令）。与 ``_inputs_to_schema``（dict
    形态，给 MCP describe_workflow 用）并存——两者面向不同消费者、形态不同（list 带 name
    vs dict keyed），非重复逻辑。
    """
    return [
        {
            "name": key,
            "type": idef.type,
            "description": idef.description,
        }
        for key, idef in wf.inputs.items()
    ]
