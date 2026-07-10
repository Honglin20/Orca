"""setup_phase.py —— setup_outputs 校验 + setup phase 跳过逻辑（SPEC phase-10 §5.9 / §2.7）。

回答「MCP 模式下主 session 替 setup agent 跑完，收集到的 ``setup_outputs`` 怎么校验？」：
``validate_setup_outputs`` 三步校验（fail loud 不救济）：

  1. workflow 无 setup phase → 直接返空 dict（跳过）
  2. workflow 有 setup phase 但 ``setup_outputs`` 未给 → raise ``SetupRequired``
  3. ``setup_outputs.keys()`` 不匹配 setup agent names → raise ``SetupOutputsMismatch``
  4. 每个 agent 的 ``output_schema``（如有）校验 → raise ``SetupOutputsInvalid``

三重杠杆 B（§2.8）：``start_workflow`` 调本函数拦截「跳过 setup 直接 start」。

依赖单向：本模块依赖 ``orca.schema.workflow``（AgentNode）+ ``jsonschema``（可选）。
不依赖 run/exec/events。纯函数 + 异常。
"""

from __future__ import annotations

from typing import Any

from orca.schema.workflow import AgentNode


class SetupRequired(Exception):
    """workflow 有 setup phase 但 ``setup_outputs`` 没给（§0.1 第八条杠杆 B）。

    ``agent_names`` 供 MCP 层构造引导 ``_hint``（"必须先调 get_agent_prompt 收集 outputs"）。
    """

    def __init__(self, agent_names: list[str]) -> None:
        self.agent_names = agent_names
        super().__init__(
            f"Workflow has setup phase (agents: {agent_names}). "
            f"You must collect setup_outputs first via get_agent_prompt."
        )


class SetupOutputsMismatch(Exception):
    """``setup_outputs`` 的 key 集合不匹配 setup agent names。"""

    def __init__(self, expected: list[str], actual: list[str]) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"setup_outputs keys mismatch. Expected: {expected}. Got: {actual}."
        )


class SetupOutputsInvalid(Exception):
    """``setup_outputs`` 的某个 agent 的 output 不满足 ``output_schema``。"""

    def __init__(self, agent_name: str, detail: str) -> None:
        self.agent_name = agent_name
        self.detail = detail  # schema 校验失败详情（给 MCP 层构造 _hint 用）
        super().__init__(
            f"setup_outputs for agent '{agent_name}' failed schema validation: {detail}"
        )


def validate_setup_outputs(
    setup_agents: list[AgentNode],
    setup_outputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """校验 ``setup_outputs`` 严格匹配 setup phase 定义（SPEC §5.9）。

    Args:
        setup_agents: workflow.setup 列表（空 list = 无 setup phase）。
        setup_outputs: MCP 主 session 收集的 outputs（``{agent_name: {field: value}}``）。

    Returns:
        校验通过的 setup_context（直接注入 workflow runtime 的 ``{setup: <setup_context>}``）。
        无 setup phase → 空 dict。

    Raises:
        SetupRequired: 有 setup phase 但 setup_outputs 未给。
        SetupOutputsMismatch: key 集合不匹配。
        SetupOutputsInvalid: 某 agent output 不满足 output_schema。
    """
    if not setup_agents:
        return {}

    if setup_outputs is None:
        raise SetupRequired([a.name for a in setup_agents])

    expected = {a.name for a in setup_agents}
    actual = set(setup_outputs.keys())
    if expected != actual:
        raise SetupOutputsMismatch(sorted(expected), sorted(actual))

    for agent in setup_agents:
        if agent.output_schema:
            _validate_json_schema(
                setup_outputs[agent.name],
                agent.output_schema,
                agent_name=agent.name,
            )

    return setup_outputs


def _validate_json_schema(
    instance: Any, schema: dict, *, agent_name: str
) -> None:
    """用 jsonschema 校验 instance 是否满足 schema（fail loud → SetupOutputsInvalid）。

    ``jsonschema`` 是 Orca 已有依赖（schema 校验在 exec 层也用）。若 import 失败
    （环境异常），退化为手动 ``required`` 字段检查（保底，不静默吞）。
    """
    try:
        import jsonschema

        jsonschema.validate(instance=instance, schema=schema)
    except ImportError:
        # 保底：只校验 required 字段存在（jsonschema 不可用时退化，不静默吞错）。
        required = schema.get("required", [])
        if isinstance(instance, dict):
            missing = [f for f in required if f not in instance]
            if missing:
                raise SetupOutputsInvalid(
                    agent_name, f"missing required fields: {missing}"
                )
    except Exception as e:
        raise SetupOutputsInvalid(agent_name, str(e)) from e
