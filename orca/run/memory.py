"""orca/run/memory.py —— 节点记忆(Node Memory)的读写 helper(SPEC: node-memory-design-draft)。

回答「上一轮 ``node_completed.data.output`` 怎么沉淀到项目内、下一轮怎么读回来注入 prompt?」:

  - **写侧**(``write_node_memory``):引擎确定性覆盖写 ``<project_root>/.orca/memory/
    <wf.name>/<node.name>.md``(frontmatter 4 字段 + body)。best-effort:失败仅结构化
    warn,不阻断 run(记忆是派生缓存,tape 才是真相源,SPEC §0.8 / §3.1)。
  - **读侧**(``read_node_memory_body``):读 MD,strip frontmatter 取 body;不存在 /
    损坏 → None(首跑 / 文件坏,静默降级)。
  - **注入**(``inject_memory_prompt``):body 存在 → 拼「上一轮记忆 + 复用协议」到 rendered
    末尾(SPEC §4.1 模板);否则原样返回。

依赖方向:schema ← run/memory ← (run/step + iface/_step_io),单向。本模块是 run 层,
被 iface(step_io)调,不反向(SPEC §6)。零事件 / tape / reducer 依赖。

不在此模块:写记忆的触发(归 ``apply_step_result``)、注入的触发(归 ``_deliver``)。
仅提供纯函数 helper + 项目级文件 I/O。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# SPEC §4.1 复用协议字面量(单一真相源,与文档字面同步;改协议必同步 SPEC)。
_REUSE_PROTOCOL = (
    "若上述上轮结果仍适用于本轮输入,直接基于它产出本轮 output"
    "(可原样或微调),不必重跑完整流程;否则正常执行,忽略上轮结果。"
)


def _memory_dir(project_root: Path, wf_name: str) -> Path:
    """``<project_root>/.orca/memory/<wf_name>``(SPEC §2.1)。"""
    return Path(project_root) / ".orca" / "memory" / wf_name


def _memory_path(project_root: Path, wf_name: str, node_name: str) -> Path:
    return _memory_dir(project_root, wf_name) / f"{node_name}.md"


def _serialize_body(output: Any, node: Any) -> str:
    """按 ``node.output_schema`` 序列化 body(SPEC §2.2)。

    - ``output_schema is None`` → output 原文(自由文本);
    - 非 None → ``json.dumps(parsed, ensure_ascii=False, indent=2)``(deterministic)。
    - 空 output(``""`` / ``None``)→ 空 body(仅 frontmatter,SPEC §0.6)。

    注:调用方(``apply_step_result``)传入的 output 是 ``_parse_output`` 已解析后的形态
    (schema=None 时为裸 str;非 None 时为 parsed JSON 对象)。故非 None 分支直接 dumps。
    """
    if output is None or output == "":
        return ""
    if getattr(node, "output_schema", None) is None:
        return str(output)
    return json.dumps(output, ensure_ascii=False, indent=2)


def write_node_memory(
    wf: Any, node: Any, output: Any, *, run_id: str, project_root: Path,
) -> None:
    """覆盖写 ``.orca/memory/<wf.name>/<node.name>.md``(SPEC §3.1)。

    - 构造 frontmatter(run_id / timestamp / wf.name / node.name)+ body(按 §2.2 序列化)。
    - ``mkdir parents=True, exist_ok=True``;``tmp + os.replace`` 原子写。
    - best-effort:OSError → 结构化 warn(``event=memory_write_failed``),**不阻断 run**。
      (SPEC §3.1 deviation:记忆是派生缓存,tape 才是真相源。)
    """
    wf_name = getattr(wf, "name", "") or ""
    node_name = getattr(node, "name", "") or ""
    body = _serialize_body(output, node)
    # frontmatter 4 字段(SPEC §0.7 / §2.2)。YAML front matter 字面契约。
    frontmatter = (
        f"---\n"
        f"run_id: {run_id}\n"
        f"timestamp: {time.time()}\n"
        f"workflow: {wf_name}\n"
        f"node: {node_name}\n"
        f"---\n\n"
    )
    content = frontmatter + body

    final = _memory_path(Path(project_root), wf_name, node_name)
    tmp = final.with_name(f".{final.name}.tmp.{os.getpid()}")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, final)
    except OSError:
        # 清残 tmp(write 后 replace 前失败会留残文件;missing_ok=True 兼容 mkdir 阶段未创建)。
        tmp.unlink(missing_ok=True)
        # 结构化 warn(SPEC §3.1 / §8.8):消息文本含 ``event=memory_write_failed`` token
        # 保证默认 stderr formatter 也能 grep 命中(不只是 extra 字段)。
        logger.warning(
            "event=memory_write_failed 写节点记忆失败(run_id=%s, node=%s, path=%s)",
            run_id, node_name, final,
            extra={"event": "memory_write_failed", "run_id": run_id, "node": node_name},
            exc_info=True,
        )


def read_node_memory_body(
    wf: Any, node: Any, *, project_root: Path,
) -> str | None:
    """读 ``.orca/memory/<wf.name>/<node.name>.md`` 的 body(strip frontmatter)。

    返回:
      - body 字符串(可能为空):MD 存在且格式合法。
      - ``None``:MD 不存在(首跑)/ 损坏 / 格式不合(静默降级,因 tape 才是真相源)。
    """
    wf_name = getattr(wf, "name", "") or ""
    node_name = getattr(node, "name", "") or ""
    path = _memory_path(Path(project_root), wf_name, node_name)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    # strip frontmatter:首行 ``---`` 起、第二个独立行 ``---`` 止。格式不合 → None。
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    # body 是结束 ``---`` 之后的内容;原文件 frontmatter 后有一空行分隔,strip 一次保
    # 「body == 序列化结果」语义(_serialize_body 写入时 body 末尾无换行,read 回来应一致)。
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return body


def inject_memory_prompt(
    node: Any, wf: Any, rendered: str, *, project_root: Path,
) -> str:
    """读 MD body;存在则拼「上一轮记忆 + 复用协议」到 rendered 末尾(SPEC §4.1)。

    MD 不存在 / 损坏 → 原样返回 rendered(首跑 / 文件坏,静默降级)。
    """
    body = read_node_memory_body(wf, node, project_root=project_root)
    if body is None:
        return rendered
    # SPEC §4.1 注入模板字面量。body 已 strip frontmatter;空 body 也注入(是信号:
    # 「上轮 output 为空」,agent 据此判断)。
    return (
        f"{rendered}\n"
        f"\n---\n"
        f"【上一轮记忆】(本节点上一次执行的 output 快照)\n"
        f"{body}\n"
        f"\n【复用协议】\n"
        f"{_REUSE_PROTOCOL}\n"
    )
