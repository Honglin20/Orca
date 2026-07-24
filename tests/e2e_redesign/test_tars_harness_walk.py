"""test_tars_harness_walk.py —— headless TARS DAG walk E2E（经 TARS 路径驱动真 workflow）。

**禁用模式**（任务硬约束）：不调 ``orca run``、不手搓 next 循环绕过 TARS。本文件的 walk
经 ``tars_harness.walk_dag`` = ``orca <wf> --inputs`` + 逐节点 ``orca next --run-id``，
用 ``schema_faker`` 合成的 mock 产出喂 next —— 这是 TARS skill 内部调的命令序列。

**分层**：
- 单节点 quant×4：走到 ``done:true``（完整链闭环）。
- 多节点（nas×2 / struct / kd）：bootstrap + 首跳（证明入口链不破；reached_done 可 False，
  因为多节点 workflow 的路由条件依赖真模型数据，合成 mock 无法满足——属预期设计边界，
  不是 harness 失败）。

**side effect**：每测试创建真 orca run（marker + tape）。``conftest.recent_run_cleanup``
（autouse）按 mtime 退避清近 600s 的 run 目录——**绝不**碰用户既有的活跃 run（如
kd-nas-20260720，2 天前）。
"""

from __future__ import annotations

import json
import subprocess

import pytest

from tests.e2e_redesign.contract import WORKFLOWS
from tests.e2e_redesign.tars_harness import bootstrap_run, minimal_inputs, walk_dag

# 全部测试创建真 orca run；opt-in conftest.recent_run_cleanup（mtime 退避，不碰用户老 run）。
pytestmark = pytest.mark.usefixtures("recent_run_cleanup")

# 单节点 quant 系（route → $end，必走到 done:true）。
SINGLE_NODE_WF = [
    "quant-ptq-sweep",
    "quant-sensitivity",
    "quant-qat",
    "quant-bit-curve",
]
# 多节点（nas×2 / struct / kd）。
MULTI_NODE_WF = [
    "nas-agent-pipeline",
    "nas-hp-search",
    "agent-struct-exploration",
    "kd-nas",
]

# orca 允许每 wf 仅一个活跃 run；elapsed > 此阈值的 run 视为「用户既有」（非本测试创建）——
# 测试无法也不应 stop 它（任务硬约束：不碰用户数据）。受其阻塞的 wf 跳过 bootstrap/walk。
_PREEXISTING_RUN_ELAPSED_S = 120.0


def _skip_if_preexisting_active_run(wf_name: str) -> None:
    """若 wf 有 elapsed > 120s 的活跃 run（用户既有，如 kd-nas-20260720），跳过测试。

    orca ``duplicate-active-run`` 会拒新 bootstrap；我们**不能** stop 用户的 run（任务硬约束），
    故只能 skip 并在报告登记为「环境约束，非契约违例」。
    """
    try:
        proc = subprocess.run(
            ["orca", "status", "--json"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except subprocess.SubprocessError:
        return  # status 查不到 → 不 skip（让测试自己暴露真问题）
    if proc.returncode != 0:
        return
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return
    for run in data.get("runs", []):
        rid = run.get("run_id", "")
        elapsed = float(run.get("elapsed", 0) or 0)
        if rid.startswith(f"{wf_name}-") and elapsed > _PREEXISTING_RUN_ELAPSED_S:
            pytest.skip(
                f"wf={wf_name} 有用户既有活跃 run {rid}（elapsed={elapsed:.0f}s）阻塞 bootstrap；"
                f"按任务硬约束不 stop 用户 run，此 wf 的 bootstrap/walk E2E 跳过"
            )


# ── 单节点 quant×4：完整 walk 到 done ─────────────────────────────────────────


@pytest.mark.parametrize("wf_name", SINGLE_NODE_WF)
def test_walk_single_node_reaches_done(wf_name: str) -> None:
    """单节点 quant workflow：walk 必走到 ``done:true``（output_schema 链全闭环）。

    证明：bootstrap + schema_faker 合成产出 + ``orca next`` 一气呵成——引擎接受合成产出，
    route → ``$end`` 终止。这是「output_schema 链不破」最强的动态证据。
    """
    result = walk_dag(wf_name)
    assert result.reached_done, (
        f"{wf_name} 单节点 workflow 未走到 done:true；"
        f"steps={result.node_sequence}; error={result.error!r}"
    )
    assert len(result.steps) == 1, (
        f"{wf_name} 应只 1 节点（route→$end），实际访问 {result.node_sequence}"
    )
    assert result.final is not None
    assert result.final.done is True


# ── 多节点：bootstrap + 首跳（链入口不破） ─────────────────────────────────────


@pytest.mark.parametrize("wf_name", MULTI_NODE_WF)
def test_walk_multi_node_first_step_progresses(wf_name: str) -> None:
    """多节点 workflow：bootstrap + 至少推进到首节点 next（证明**入口链**不破）。

    **本测试只证「链入口不破」，不证中段**：多节点 workflow 有路由条件 / 循环 / foreach，
    依赖真模型数据；合成 mock 走不到 done 属预期设计边界（任务 §「现实约束」：本环境无
    真模型）。断言：
    - 至少 1 步 walk 成功（首节点 output_schema 链接通，bootstrap + 首节点 next 过 schema）；
    - 若 reached_done，bonus（不强求）；
    - harness 有控制地退出（非 unhandled raise）——``result.error`` 记原因（路由依赖真数据）。

    中段链不破的保证由静态契约 ``check_output_schema_chain``（所有 ``{{ X.output.Y }}``
    引用逐字校验）覆盖；本动态测试是「入口链 + 引擎能推进」的运行时补充证据。
    """
    _skip_if_preexisting_active_run(wf_name)
    result = walk_dag(wf_name, max_steps=8)
    assert len(result.steps) >= 1, (
        f"{wf_name} 多节点 walk 未完成首步；error={result.error!r}"
    )
    # 首步的 output 喂 next 后，要么 done，要么给 next_node（链推进），要么因路由依赖
    # 真数据报错——都证明「bootstrap + 首节点 schema 链接通」。
    first_step = result.steps[0]
    assert first_step.node, f"{wf_name} 首步缺 node 名"


# ── bootstrap 冒烟（8 workflow 全量，快） ──────────────────────────────────────


@pytest.mark.parametrize("wf_name", sorted(WORKFLOWS.keys()))
def test_bootstrap_all_workflows(wf_name: str) -> None:
    """8 workflow 全量 bootstrap 冒烟：compile + inputs 解析 + 首节点 prompt 渲染无 Jinja 错。"""
    from tests.spike_ask_user.orca_cli import stop as orca_stop

    _skip_if_preexisting_active_run(wf_name)
    inputs = minimal_inputs(wf_name)
    boot = bootstrap_run(wf_name, inputs)
    try:
        assert boot.run_id, f"{wf_name} bootstrap 未返 run_id"
        assert boot.done is False, f"{wf_name} bootstrap 应有首节点（done=False）"
        assert boot.node, f"{wf_name} bootstrap 未给首节点名"
        assert boot.prompt, f"{wf_name} bootstrap 未给首节点 prompt"
        # 首节点 prompt 应含「Orca 节点执行」驱动协议头（TARS skill 投影的同一信封）
        assert "Orca 节点执行" in boot.prompt or "task 工具派" in boot.prompt, (
            f"{wf_name} 首节点 prompt 缺驱动协议头；prompt_head={boot.prompt[:200]!r}"
        )
    finally:
        try:
            orca_stop(run_id=boot.run_id)
        except Exception:
            pass
