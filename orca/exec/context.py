"""context.py —— RunContext（节点间数据传递契约）。

回答「执行单个 node 时上下游数据怎么取？」：frozen dataclass，由 phase 5 orchestrator
构造传给 ``executor.exec(node, ctx)``。

字段（SPEC §4.7）：
  - ``inputs``：workflow 输入（``{{ inputs.iterations }}``）
  - ``outputs``：已完成 node 的输出累积（``{node_name: {"output": node_output}}``）；
    Jinja2 渲染时 ``{{ optimizer.output.structure }}`` 从 ``outputs["optimizer"]`` 取。
    （注意：存的是 ``{"output": raw}`` 包装，与 render.py ``_namespace`` 约定一致 ——
    模板统一 ``{{ node.output.field }}``。）
  - ``run_id``：当前 run id（透传到事件 / 日志）。
  - ``task``：可选位置参数 task（CLI ``orca run <yaml> <task>`` 语法糖），
    同时注入 ``inputs.task``；保留此字段供 lifecycle 事件 / 日志引用（非必须，默认 None）。
  - ``locals``：foreach body 注入的局部变量（``{{ item }}`` / ``{{ _index }}``）。
    空 dict = 非 foreach 上下文；foreach 时由 orchestrator 经 ``with_locals`` 派生新实例。
  - ``user_guidance``（phase 11 §4）：累积的用户纠偏话（Ctrl+G + CONTINUE 时追加）。
    frozen → 用 tuple 累积（``with_guidance`` 派生新实例）；render_prompt 把它拼成
    ``[User Guidance]`` 段附到 agent prompt 末尾。默认空 tuple = 无 guidance（向后兼容）。
  - ``interrupt_history``（phase 11 §2.1）：历次中断记录（debug/replay 用，{node/action/
    guidance/elapsed}）。本 step 字段先加，记录由 orchestrator 在 _handle_interrupt 写入。

frozen：执行上下文是不可变快照（一个 node 执行期间上游输出不应变）；
orchestrator 在 node 间构造新 RunContext（累加新输出 / guidance）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RunContext:
    """单 node 执行上下文（SPEC §4.7）。

    frozen：执行期间不可变。orchestrator 在 node 间构造新实例（append 上游输出）。

    ``locals`` 默认空 dict（普通 node 执行）；foreach body 经 ``with_locals`` 派生带
    item/index 的新实例，render 的 ``_namespace`` 把 locals 摊到 Jinja2 顶层。

    ``user_guidance`` 默认空 tuple（无纠偏）；用户 Ctrl+G + CONTINUE 时 orchestrator 经
    ``with_guidance`` 派生带新 guidance 的实例，render_prompt 拼 ``[User Guidance]`` 段。
    """

    inputs: dict[str, Any]  # workflow 输入
    outputs: dict[str, Any]  # {node_name: {"output": raw}} 累积 map
    run_id: str  # 当前 run id
    task: str | None = None  # 位置参数 task（同时进 inputs.task；此字段供日志/事件）
    locals: dict[str, Any] = field(default_factory=dict)  # foreach body 局部变量
    # phase 11 §4：累积的用户 guidance（Ctrl+G + CONTINUE 追加）。
    user_guidance: tuple[str, ...] = ()
    # phase 11 §2.1：历次中断记录（debug/replay 用）。
    interrupt_history: tuple[dict[str, Any], ...] = ()
    # phase 11 §2.1 / §6：Dialog 多轮历史（agent 跑完后用户按 d 追问）。
    # 每条 {"role": "user"|"agent", "text": str, "turn": int}。frozen → tuple 累积
    # （``with_dialog_turn`` 派生新实例）。默认空 tuple = 无 dialog（向后兼容）。
    #
    # **真相源说明（review 裁定）**：dialog 的唯一真相在 **tape 的 ``dialog_message`` 事件**——
    # DialogHandler 不写本字段（dialog 是 post-run，ctx 已不在 orchestrator 流水里，写它无回流
    # 路径）。本字段 + ``with_dialog_turn`` 是为**未来 web shell replay 注入**预留的 mutation
    # 原语（web 端从 tape 重放 dialog_message 构造 ctx 时用）。当前 CLI 路径保持空 tuple，
    # 不构成第二真相源（反 AgentHarness 多 store 漂移，本项目顶层铁律）。
    dialog_history: tuple[dict[str, Any], ...] = ()

    def with_locals(self, locals_: dict[str, Any]) -> RunContext:
        """派生带 locals 的新 frozen 实例（foreach body 用，注入 item / _index）。

        用 ``dataclasses.replace``（与 ``with_guidance`` / ``with_dialog_turn`` 一致），
        自动携带所有既有字段——手工列字段会在新加字段时漏传（历史 bug 模式）。
        不 mutate：返回新 dataclass 实例（frozen 语义）。普通 node 不调用此方法。
        """
        return dataclasses.replace(self, locals=dict(locals_))

    def with_guidance(self, text: str) -> RunContext:
        """派生带追加 guidance 的新 frozen 实例（phase 11 §4.1）。

        orchestrator ``_handle_interrupt`` 的 continue 分支调此方法把用户纠偏话累积进 ctx；
        后续 ``render_prompt`` 经 ``guidance_prompt_section`` 把它拼到 agent prompt 末尾。

        frozen → 累积用 tuple + ``dataclasses.replace``（与 outputs 累加机制一致）。
        空 / 全空白 text 不追加（无意义，防 prompt 末尾空 guidance 段）。
        """
        if not text or not text.strip():
            return self  # 空 guidance 不累积（向后兼容：CONTINUE 无文本 = 无 guidance）
        return dataclasses.replace(self, user_guidance=self.user_guidance + (text,))

    def guidance_prompt_section(self) -> str | None:
        """拼 ``[User Guidance]`` prompt 段（phase 11 §4.1，逐字对齐 Conductor）。

        无 guidance → None（render_prompt 不追加）。有 → 返回：

            \\n\\n[User Guidance]\\n
            The following guidance was provided by the user during workflow execution. \\
            Incorporate this guidance into your response:\\n
            - <g1>\\n
            - <g2>

        多条 guidance 全部列出（累积语义：每次 Ctrl+G + CONTINUE 都追加，agent 看到全部历史纠偏）。
        """
        if not self.user_guidance:
            return None
        entries = "\n".join(f"- {g}" for g in self.user_guidance)
        return (
            "\n\n[User Guidance]\n"
            "The following guidance was provided by the user during workflow execution. "
            "Incorporate this guidance into your response:\n"
            f"{entries}"
        )

    def with_dialog_turn(self, role: str, text: str, turn: int) -> RunContext:
        """派生带追加 dialog 轮次的新 frozen 实例（phase 11 §6）。

        DialogHandler ``send_turn`` / ``end_dialog`` 调此方法把每轮 user/agent 话累积进 ctx。
        与 ``with_guidance`` 同 pattern（frozen → tuple + ``dataclasses.replace``），但条目是
        dict（含 role / text / turn，三类语义信息），而非纯字符串（guidance 只需话本身）。

        Args:
            role: ``"user"`` 或 ``"agent"``（由 DialogHandler 决定，本方法不校验——
                调用方契约保证；非法值会让 dialog_history 后续渲染混乱，属调用方 bug）。
            text: 这一轮的文本（agent reply 或 user 输入）。
            turn: 轮次序号（DialogHandler 维护计数器；同一次 dialog 内 user 与 agent
                共享同一 turn 号——一轮对话 = 一个 user turn + 一个 agent turn）。

        空 / 全空白 text 不追加（无意义，防 dialog_history 留空轮次）。
        """
        if not text or not text.strip():
            return self
        entry = {"role": role, "text": text, "turn": turn}
        return dataclasses.replace(self, dialog_history=self.dialog_history + (entry,))

