"""test_ask_user.py —— agent 主动问（HumanGate source=agent_ask，SPEC §5 / 计划 G4.4）。"""

from __future__ import annotations

import asyncio

from orca.gates.ask_user import ask_user
from orca.gates.handler import HumanGateHandler

from tests.gates.conftest import make_bus, run_async


def test_ask_user_triggers_agent_ask_gate(tmp_path):
    """ask_user 触发 source=agent_ask 的 gate；壳 resolve 后返回 answer。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            task = asyncio.create_task(
                ask_user(
                    handler,
                    prompt="需要数据库连接串？",
                    options=["postgres", "mysql"],
                    context={"suggested": ["postgres"]},
                    run_id="r1",
                    node="db_init",
                )
            )
            await asyncio.sleep(0.02)  # 等 ask_user 跑到 handler.request

            # 模拟壳答了
            gate_id = next(iter(handler._pending))
            ok = handler.resolve(gate_id, "postgres", "cli")
            assert ok is True

            answer = await asyncio.wait_for(task, timeout=1.0)
            assert answer == "postgres"

            # 验证 Tape 落了 requested 事件，且 source=agent_ask
            events = list(bus.tape.replay())
            requested = next(
                e for e in events if e.type == "human_decision_requested"
            )
            assert requested.data["source"] == "agent_ask"
            assert requested.data["prompt"] == "需要数据库连接串？"
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())


def test_ask_user_free_text_no_options(tmp_path):
    """options=None（自由文本）：壳 resolve 任意字符串都返回。"""

    async def scenario():
        bus, _ = make_bus(tmp_path)
        handler = HumanGateHandler(bus)
        await handler.start()
        try:
            task = asyncio.create_task(
                ask_user(handler, prompt="密码？", run_id="r", node="n")
            )
            await asyncio.sleep(0.02)
            gate_id = next(iter(handler._pending))
            handler.resolve(gate_id, "hunter2", "web")
            assert await asyncio.wait_for(task, timeout=1.0) == "hunter2"
        finally:
            await handler.stop()
            bus.close()

    run_async(scenario())
