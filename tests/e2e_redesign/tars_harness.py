"""tars_harness.py —— headless TARS-SKILL 行为投影（Stage 3 §6）。

**职责**：复用 ``tests/spike_ask_user/`` 的基建（``orca_cli`` / ``tars_loop`` /
``MockSubagentBackend`` / ``sentinel``），把它从「2 节点 spike workflow」扩成「能 bootstrap
真 workflow + 驱动节点链 + 跑哨兵路径」的统一 E2E harness。**经 TARS skill 路径**——
调 ``orca <wf> --inputs`` + ``orca next --run-id``（这是 TARS skill 内部调的命令），
**禁用** ``orca run`` / 手搓 next 循环绕过 TARS（任务硬约束）。

**三条能力**（对应任务 §1/§2 + finding 收集）：

1. ``bootstrap_run(wf_name, inputs)`` —— 经 TARS 路径启动 workflow（``orca <wf> --inputs``）。
   证明：compile validator 通过 + inputs 解析 + 节点图合法 + 首节点 prompt 渲染无 Jinja 错。
   副作用：创建一个活跃 run（**调用方负责 stop**——walk_dag/sentinel_e2e_run 自清，bootstrap_run
   返回 ``BootstrapResult`` 含 run_id，调用方据它 stop）。
2. ``walk_dag(wf_name, inputs, max_steps)`` —— 用 ``schema_faker`` 合成的最小合规 JSON 喂
   ``orca next``，逐节点推进到 ``done:true``（单节点）或 max_steps/路由不可评估（多节点）。
   证明：output_schema 链不破 + 引擎接受合成产出。**用 mock 子 agent 产出喂 next**——
   不是 ``orca run`` 自驱。
3. ``sentinel_e2e_run(wf_name, inputs, ...)`` —— 用 ``MockSubagentBackend`` 剧本：
   spawn→哨兵→resume→真实 output，经 ``tars_loop.drive_workflow`` 闭环到 done:true。
   证明：哨兵在 TARS 层拦截（不进 ``--output``）+ task_id 复用 + MAX_ASK 兜底。

**依赖单向**：``tests.spike_ask_user.{orca_cli,tars_loop,backend,mock_backend,sentinel}``
+ ``orca.compile.parser``（纯数据读）+ 本目录 ``schema_faker``。不 import run/exec/events/iface。

**fail loud**：bootstrap/next 非 0 退出 → ``OrcaCLIError`` 冒出；walk 超 max_steps →
``WalkLimitExceeded``；哨兵路径 ``SentinelLoopExhausted`` / ``FabricationDetected`` 原样冒。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from orca.schema.workflow import AgentNode, Workflow
from tests.e2e_redesign.contract import WORKFLOWS, WF_DIR, load_parsed
from tests.e2e_redesign.schema_faker import synthesize_for_schema
from tests.spike_ask_user.mock_backend import MockSubagentBackend
from tests.spike_ask_user.orca_cli import (
    BootstrapResult,
    NextResult,
    OrcaCLIError,
    bootstrap,
)
from tests.spike_ask_user.sentinel import looks_fabricated
from tests.spike_ask_user.tars_loop import (
    RealOrcaCLI,
    WorkflowDriveLog,
    WorkflowDriverProtocol,
    drive_workflow,
)

logger = logging.getLogger(__name__)


class WalkLimitExceeded(RuntimeError):
    """walk_dag 超 max_steps（多节点 workflow 可能有循环未在 max_steps 内收敛）。"""


class DAGStallError(RuntimeError):
    """引擎不变式违反：``orca next`` 既未 done 也未给 next node（不该发生）。

    与「多节点路由依赖真数据导致 next 非零退出」的预期失败区分：后者是 ``OrcaCLIError``（被
    ``walk_dag`` catch 成 ``result.error``），本异常是引擎状态机 bug，**必须 fail loud 冒出**
    不被吞。详见 ``walk_dag`` 的异常分类。
    """


@dataclass
class WalkStep:
    """单步 walk 记录（哪个节点、喂了什么产出、引擎是否 done）。"""

    node: str
    output_preview: str
    done: bool
    next_node: str = ""
    raw_reason: str = ""


@dataclass
class WalkResult:
    """walk_dag 全程记录。``reached_done=True`` = 走到 ``done:true``（单节点典型）。"""

    run_id: str
    steps: list[WalkStep] = field(default_factory=list)
    reached_done: bool = False
    final: NextResult | None = None
    error: str = ""

    @property
    def node_sequence(self) -> list[str]:
        """访问过的节点序列（断言「链推进」用）。"""
        return [s.node for s in self.steps]


# ── inputs 构造 ─────────────────────────────────────────────────────────────────


def minimal_inputs(wf_name: str) -> dict[str, Any]:
    """据 inputs_schema 合成最小 inputs（[default]/[advanced] 省略走默认；其余按 type 给占位）。

    bootstrap 校验（SPEC §6.1）：``[default]`` / ``[advanced]`` 标签字段可省；``[ask]`` /
    无标签 / ``[infer]`` 字段必填。本 helper 据描述前缀标签分桶（与 TARS skill 抽 inputs 同语义）。
    """
    wf = load_parsed(wf_name)
    inputs: dict[str, Any] = {}
    for name, spec in (wf.inputs or {}).items():
        desc = (spec.description or "").strip()
        tag = desc.split()[0] if desc else ""
        if tag in ("[default]", "[advanced]"):
            continue  # 省略 → 走 workflow 声明的 default
        inputs[name] = _placeholder_for_type(spec.type)
    return inputs


def _placeholder_for_type(type_str: str) -> Any:
    """按 InputDef.type 给类型正确的占位（bootstrap 校验只查声明的 type）。"""
    if type_str == "int":
        return 0
    if type_str == "number" or type_str == "float":
        return 0.0
    if type_str == "boolean":
        return False
    if type_str == "list":
        return []
    return "placeholder"  # string / 未知类型 → neutral 串


def _node_by_name(wf: Workflow) -> dict[str, AgentNode]:
    """node 名 → AgentNode（walk 合成产出时按当前节点查 output_schema 用）。"""
    out: dict[str, AgentNode] = {}
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            out[node.name] = node
    return out


# ── 能力 1: bootstrap ──────────────────────────────────────────────────────────


def bootstrap_run(
    wf_name: str,
    inputs: dict[str, Any] | None = None,
    *,
    orca_bin: str = "orca",
) -> BootstrapResult:
    """经 TARS 路径启动 workflow（``orca <wf> --inputs``）。

    ``inputs=None`` → 用 ``minimal_inputs`` 自动合成（[default]/[advanced] 省略）。
    返回 ``BootstrapResult``（含 run_id / 首节点 prompt）。**调用方负责 stop**（直接调
    ``tests.spike_ask_user.orca_cli.stop`` 或经 ``walk_dag`` / ``sentinel_e2e_run`` 自清）。
    """
    if inputs is None:
        inputs = minimal_inputs(wf_name)
    logger.info("bootstrap wf=%s inputs_keys=%s", wf_name, sorted(inputs.keys()))
    return bootstrap(wf_name, inputs, orca_bin=orca_bin)


# ── 能力 2: walk_dag ───────────────────────────────────────────────────────────


def walk_dag(
    wf_name: str,
    inputs: dict[str, Any] | None = None,
    *,
    max_steps: int = 30,
    orca_bin: str = "orca",
    orca_cli: WorkflowDriverProtocol | None = None,
) -> WalkResult:
    """headless TARS DAG walk：bootstrap → 逐节点喂 schema_faker 合成产出 → next → done/limit。

    - 单节点 workflow：走到 ``done:true``（``reached_done=True``）。
    - 多节点 workflow：走到 ``done:true`` 或 max_steps 或 next 报错（路由条件依赖真数据
      无法继续——此时 ``error`` 记原因，已访问的 ``node_sequence`` 仍证明链前段不破）。

    **经 TARS 路径**：用 mock 合成产出喂 ``orca next``，**不是** ``orca run`` 自驱。

    **DI（测试可注入）**：``orca_cli=None`` → 用 ``RealOrcaCLI``（真 orca CLI 子进程）；
    测试可注入 ``FakeOrcaCLI`` 钉死各分支（reached_done / error_kind / WalkLimitExceeded /
    DAGStallError），不必真 bootstrap。与 ``tars_loop.drive_workflow`` 同 DI 模式。
    """
    wf = load_parsed(wf_name)
    node_table = _node_by_name(wf)
    if inputs is None:
        inputs = minimal_inputs(wf_name)
    if orca_cli is None:
        orca_cli = RealOrcaCLI(orca_bin=orca_bin)

    boot = orca_cli.bootstrap(wf_name, inputs)
    result = WalkResult(run_id=boot.run_id)
    current_node = boot.node

    try:
        for _ in range(max_steps):
            node = node_table.get(current_node)
            schema = node.output_schema if isinstance(node, AgentNode) else None
            output = synthesize_for_schema(schema)
            _assert_not_fabricated(output, current_node)

            nxt = orca_cli.next_step(boot.run_id, output)
            step = WalkStep(
                node=current_node,
                output_preview=output[:160],
                done=nxt.done,
                next_node=nxt.node,
                raw_reason=nxt.raw.get("reason", ""),
            )
            result.steps.append(step)
            logger.info(
                "walk-dag[%s] node=%s → done=%s next=%s reason=%s",
                wf_name, current_node, nxt.done, nxt.node, step.raw_reason,
            )

            if nxt.done:
                # done:true 不一定是成功——引擎在 render_error / output_schema_mismatch
                # 等失败上也返 done:true + error_kind。reached_done 仅在「干净完成」时置 True。
                error_kind = nxt.raw.get("error_kind", "")
                if error_kind:
                    result.error = (
                        f"done:true 但带 error_kind={error_kind}；"
                        f"reason={nxt.raw.get('reason', '')}"
                    )
                    logger.warning("walk_dag[%s] %s", wf_name, result.error)
                else:
                    result.reached_done = True
                result.final = nxt
                return result
            if not nxt.node:
                # 引擎不变式违反（done=False 但无 next node）——绝不该发生。
                # 用独立类型 DAGStallError 让它冒出（不进下面的 except OrcaCLIError），
                # 区分于「多节点路由依赖真数据」的预期 next 非零退出。
                raise DAGStallError(
                    f"walk_dag[{wf_name}] next 既非 done 也无 next node（引擎状态机 bug）；"
                    f"raw={nxt.raw}"
                )
            current_node = nxt.node
        # 超 max_steps
        raise WalkLimitExceeded(
            f"walk_dag[{wf_name}] 超 max_steps={max_steps}；"
            f"已访问节点={result.node_sequence}（可能多节点 workflow 有循环）"
        )
    except OrcaCLIError as e:
        # 多节点 workflow 的路由条件依赖真数据时，next 会以 output_schema_mismatch /
        # route eval 失败等非零退出。记 error 但不算 harness 失败（前段链已证不破）。
        # 注意：DAGStallError 是 RuntimeError 子类（非 OrcaCLIError），不在此 catch，
        # 会原样冒出——引擎不变式违反必须 fail loud。
        result.error = f"{e.__class__.__name__}: {e}"
        logger.warning("walk_dag[%s] next 失败（可能路由依赖真数据）: %s", wf_name, e)
        return result
    except WalkLimitExceeded as e:
        result.error = str(e)
        logger.warning("walk_dag[%s] %s", wf_name, e)
        return result
    finally:
        # cleanup marker（best-effort；done:true 的 run 引擎已自清，stop 会 ok:false 容忍）。
        # DAGStallError 冒出前也先清 marker，防残留。
        try:
            orca_cli.stop(boot.run_id)
        except OrcaCLIError as e:
            logger.warning("walk_dag[%s] cleanup stop 失败: %s", wf_name, e)
        except Exception:
            logger.exception("walk_dag[%s] cleanup stop 异常", wf_name)


def _assert_not_fabricated(output: str, node: str) -> None:
    """合成产出/真实产出都不应含造假词（spike 同口径 sanity）。fail loud。"""
    if looks_fabricated(output):
        raise AssertionError(
            f"walk_dag 节点 {node!r} 产出含造假词；output_preview={output[:200]!r}"
        )


# ── 能力 3: sentinel E2E ───────────────────────────────────────────────────────


def sentinel_e2e_run(
    wf_name: str,
    *,
    sentinel_message: str,
    real_output: str | None = None,
    answer: str = "user-provided-dotted-path",
    scenario: list[str] | None = None,
    orca_bin: str = "orca",
) -> tuple[NextResult, WorkflowDriveLog]:
    """哨兵路径闭环 E2E（≥1 workflow）。

    用 ``MockSubagentBackend`` 剧本：spawn→哨兵→resume→真实 output。经
    ``tars_loop.drive_workflow`` 闭环到 ``done:true``。

    - ``scenario=None``（默认，闭环路径）：剧本 = ``[sentinel_message, real_output]``。
    - ``scenario`` 显式给（MAX_ASK 兜底路径用）：直接用，调用方负责给够条目
      （``MAX_ASK+1`` 条哨兵 = spawn + 3 次 resume，见 spike ``_mock_scenario_reentry_blocked``）。
    - ``real_output=None``（且 scenario=None）→ 用 ``schema_faker`` 据首节点 output_schema 合成
      （保证喂 ``orca next`` 能过 schema 校验）。
    - ``answer``：模拟用户答案（answer_provider 恒答之；None 表示「用户答不知道」）。

    断言（由测试层做）：
    - 哨兵未进 ``--output``（drive_workflow 已保证——只在退出哨兵循环后才喂 next）。
    - task_id 复用（MockSubagentBackend 同 task_id spawn+resume）。
    - final done:true（闭环路径）。
    """
    if scenario is None:
        if real_output is None:
            # 首节点 schema 合成（多数哨兵 E2E 是单节点 workflow）
            wf = load_parsed(wf_name)
            first_node = _first_agent_node(wf)
            real_output = synthesize_for_schema(
                first_node.output_schema if first_node else None
            )
        scenario = [sentinel_message, real_output]

    backend = MockSubagentBackend(scenario, backend_name="mock-sentinel-e2e")

    def _answer_provider(_question) -> str | None:
        return answer

    # drive_workflow 默认用 RealOrcaCLI（内部 orca CLI，bin="orca"），此处不注入 fake。
    return drive_workflow(
        backend=backend,
        wf=wf_name,
        inputs=minimal_inputs(wf_name),
        answer_provider=_answer_provider,
    )


def _first_agent_node(wf: Workflow) -> AgentNode | None:
    """workflow 的首个 AgentNode（sentinel_e2e 合成 real_output 时查 schema 用）。"""
    for node in wf.nodes:
        if isinstance(node, AgentNode):
            return node
    return None
