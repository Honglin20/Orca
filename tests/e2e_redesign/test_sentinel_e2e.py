"""test_sentinel_e2e.py —— headless TARS 哨兵路径闭环 E2E（ptq-sweeper 为主路径）。

**禁用模式**（任务硬约束）：经 ``tars_harness.sentinel_e2e_run`` = ``tars_loop.drive_workflow``
+ ``MockSubagentBackend`` 剧本（spawn→哨兵→resume→真实 output）+ ``orca next``。
**不**调 ``orca run``、**不**手搓 next 循环绕过 TARS。

**断言**（任务 §2 sentinel 路径 E2E）：
1. 子 agent 首返哨兵（缺 Tier B 必填项）→ TARS 层拦截（**不喂 orca next**）。
2. task_id 捕获 + 恢复**同一**子 agent（MockSubagentBackend calls_per_task 复用同 task_id）。
3. 恢复后拿真实 output → 才喂 ``orca next`` → workflow 推进。
4. 真实 output / 喂给 next 的产出**不含** ``_sentinel`` 字面量（哨兵绝不进引擎）。
5. MAX_ASK 兜底：连续哨兵 ≥3 次 → ``SentinelLoopExhausted`` fail loud（不无限循环）。
6. 真实 output 不含造假词（``looks_fabricated`` sanity）。

side effect 同 walk 测试：``conftest.recent_run_cleanup``（autouse）清近 600s 的 run dir。
"""

from __future__ import annotations

import json

import pytest

from tests.e2e_redesign.tars_harness import sentinel_e2e_run
from tests.spike_ask_user.sentinel import (
    MAX_ASK,
    SentinelLoopExhausted,
    build_sentinel_message,
)

# 全部测试创建真 orca run；opt-in conftest.recent_run_cleanup（mtime 退避，不碰用户老 run）。
pytestmark = pytest.mark.usefixtures("recent_run_cleanup")


# ── 主路径：ptq-sweeper 哨兵一次 → 真实 output → done ─────────────────────────


def test_sentinel_path_ptq_sweeper_closed_loop() -> None:
    """ptq-sweeper 缺 calib loader → 哨兵 → 恢复同一子 agent → 真实 output → done:true。

    这是任务 §2 的主示例（ptq-sweeper 缺 calib loader 场景）。
    """
    sentinel = build_sentinel_message(
        question="calib loader 在你项目的 dotted-path 是什么？",
        options=["myproj.data:load_calib", "myproj.dataset:make_loader"],
        context=(
            "我已 glob project_root 下 **/*.py 并 grep DataLoader，"
            "但 project_root 为空/无匹配；请直接给 dotted-path"
        ),
    )
    # real_output=None → harness 据 ptq_sweeper output_schema 合成（保证过 schema 校验）
    final_result, log = sentinel_e2e_run(
        "quant-ptq-sweep",
        sentinel_message=sentinel,
        answer="myproj.data:load_calib",
    )

    # (1) workflow done
    assert final_result.done is True, f"final raw={final_result.raw}"

    # (2) 只 1 个节点被驱动（单节点 workflow），且该节点哨兵触发 1 次
    assert len(log.nodes) == 1
    node_log = log.nodes[0]
    assert node_log.sentinel_triggered == 1, (
        f"哨兵应触发 1 次，实际 {node_log.sentinel_triggered}"
    )
    assert node_log.resumed_count == 1, f"resume 应调 1 次，实际 {node_log.resumed_count}"
    assert node_log.failed_at_max_ask is False

    # (3) task_id 复用：spawn + resume 的 task_id 相同（SPEC §2「同一子 agent」）
    node_log.assert_task_id_reused()

    # (4) 喂给 orca next 的真实 output 不含 _sentinel 字面量（哨兵绝不进引擎）
    real_output = node_log.final_output
    assert "_sentinel" not in real_output, (
        f"哨兵泄漏进 orca next 的 --output！final_output={real_output[:300]!r}"
    )
    assert "orca_ask_user_v1" not in real_output
    # 真实 output 是合法 JSON（ptq_sweeper output_schema 是 object）
    parsed = json.loads(real_output)
    assert isinstance(parsed, dict)
    # 关键 schema 字段在（证明喂的是真实 output 而非哨兵）
    assert "output_dir" in parsed, f"真实 output 缺 output_dir：{parsed}"


def test_sentinel_real_output_not_fabricated() -> None:
    """哨兵恢复后的真实 output 不含造假词（SPEC §3 sanity）。"""
    sentinel = build_sentinel_message(
        question="eval fn 的 dotted-path?",
        options=["m.eval:fn"],
        context="tier B 缺",
    )
    final_result, log = sentinel_e2e_run(
        "quant-ptq-sweep",
        sentinel_message=sentinel,
        answer="m.eval:fn",
    )
    assert final_result.done is True
    real_output = log.nodes[0].final_output
    # looks_fabricated 直接复用 spike 的同口径扫描
    from tests.spike_ask_user.sentinel import looks_fabricated
    assert not looks_fabricated(real_output), (
        f"真实 output 含造假词：{real_output[:300]!r}"
    )


# ── MAX_ASK 兜底：连续哨兵 → fail loud ────────────────────────────────────────


def test_sentinel_max_ask_exhaustion_fail_loud() -> None:
    """连续哨兵 ≥ MAX_ASK → ``SentinelLoopExhausted`` fail loud（不无限循环）。

    构造子 agent 永远返哨兵 + answer_provider 恒答「不知道」→ driver 必须 raise。
    scenario 需 ≥ MAX_ASK+1 条哨兵（spawn + 3 次 resume 都返哨兵），driver 在第
    MAX_ASK 次后 raise——与 spike ``_mock_scenario_reentry_blocked`` 同模式。
    """
    sentinel = build_sentinel_message(
        question="还是缺 X，再问一次？",
        options=["a"],
        context="reentry 测试——子 agent 永远哨兵",
    )
    # MAX_ASK+2 条哨兵：spawn + 3 次 resume = 4 次调用（MAX_ASK+1），多给 1 条保险
    # （driver 在 attempts=MAX_ASK 时 raise，不会读到 scenario[4]）。
    exhaustion_scenario = [sentinel] * (MAX_ASK + 2)
    with pytest.raises(SentinelLoopExhausted) as exc_info:
        sentinel_e2e_run(
            "quant-ptq-sweep",
            sentinel_message=sentinel,
            scenario=exhaustion_scenario,
            answer=None,  # 用户恒答「不知道」
        )
    # 错误消息含 MAX_ASK 兜底语义
    assert "MAX_ASK" in str(exc_info.value) or str(MAX_ASK) in str(exc_info.value)
