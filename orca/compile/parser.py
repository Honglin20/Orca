"""parser.py —— YAML → 校验过的 Workflow（SPEC §3）。

四步流水线：读 YAML → pydantic 结构校验 → prompt 约定加载 → 语义校验。
对外只暴露 ``load_workflow``（对外极简，内部校验要全——学 Conductor）。

依赖单向：本模块 → ``orca.compile.validator``（用 validate_workflow + ConfigurationError）。
零反向依赖：不 import run/exec/events/iface。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from orca.compile.validator import ConfigurationError, validate_workflow
from orca.schema import AgentNode, Workflow


def load_workflow(path: str | Path) -> Workflow:
    """YAML 文件 → 校验过的 Workflow。失败抛 ConfigurationError（含所有 errors+warnings）。

    失败模式（全部 fail loud，SPEC §3）：
      - YAML 语法错 → ``yaml.YAMLError`` 透传
      - pydantic 结构错 → 包装成 ConfigurationError（对外单一错误类型）
      - prompt 约定文件缺失 → ConfigurationError（精确点名 agent）
      - 语义校验失败 → ConfigurationError（含所有 errors + warnings）
    """
    yaml_path = Path(path)
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    try:
        wf = Workflow(**raw)
    except ValidationError as e:
        # 对外只暴露一种错误类型：把 pydantic 结构错包装成 ConfigurationError
        raise ConfigurationError([f"结构校验失败：{e}"], []) from e

    _load_prompts(wf, base_dir=yaml_path.parent)  # prompt 约定加载
    validate_workflow(wf)                          # 语义校验（内部聚合后 raise）
    return wf


def _load_prompts(wf: Workflow, base_dir: Path) -> None:
    """对每个 prompt=None 的顶层 agent，从 ``agents/<name>.md`` 加载（约定）。

    文件缺失 → 收集所有缺失项后**聚合**抛一次 ConfigurationError（比首个即抛更
    LLM 友好，符合 SPEC §1 聚合精神）。foreach 的无名 body 不走约定（无 name）。
    加载后每个 agent.prompt 都是确定字符串，run/ 不再管文件加载。
    """
    missing: list[str] = []
    for node in wf.nodes:
        if isinstance(node, AgentNode) and node.prompt is None:
            prompt_path = base_dir / "agents" / f"{node.name}.md"
            if not prompt_path.exists():
                missing.append(
                    f"agent '{node.name}' 未声明 prompt 且找不到约定文件 {prompt_path}"
                )
                continue
            node.prompt = prompt_path.read_text(encoding="utf-8")
    if missing:
        raise ConfigurationError(missing, [])
