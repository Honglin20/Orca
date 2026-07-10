"""tests/e2e_phase14/test_opencode_agent_pool.py —— phase-14 agent 一等化 opencode 真跑 e2e。

SPEC §8.2 E2E-2 / E2E-3b：agent 引用解析 + 文件夹化 resources，用 opencode+deepseek-v4-flash
**真跑**（不 mock 编排 / 不 mock opencode / 不 mock resolver）。

  - E2E-1：``agent: greeter`` 显式引用（单文件 ``agents/greeter.md``）→ opencode 真跑，
    agent_message 含期望答复（验证 agent 池解析物化的 prompt 真能被 opencode 执行）。
  - E2E-2：文件夹化 agent（``agents/filebot/agent.md`` + ``agents/filebot/scripts/flag.txt``）
    + frontmatter（model/tools）→ opencode 真跑，agent 经 ``$ORCA_AGENT_RESOURCES`` 读到 flag
    （验证 resources_root env 注入链：executor spawn → opencode → Bash → cat）。

无 opencode 二进制 / 无 deepseek auth → skip（不阻断 CI）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

_ARTIFACTS = Path(__file__).parent / "_artifacts"


def _opencode_available() -> bool:
    if os.environ.get("ORCA_E2E_SKIP_OPENCODE") == "1":
        return False
    return shutil.which("opencode") is not None


def _deepseek_auth_present() -> bool:
    p = Path.home() / ".local/share/opencode/auth.json"
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return isinstance(d, dict) and "deepseek" in d
    except Exception:
        return False


def _short_tape_root(tmp_path: Path) -> Path:
    """短 /tmp 路径（macOS SOCK_PATH_MAX=90，与 e2e_phase13 同约束）。"""
    h = hashlib.md5(str(tmp_path).encode()).hexdigest()[:8]
    root = Path(f"/tmp/orca-p14-e2e-{h}")
    root.mkdir(parents=True, exist_ok=True)
    return root


class TestOpencodeAgentPoolE2E:
    """phase-14 agent 一等化 opencode 真跑（每功能点完整 E2E，不 mock）。"""

    def test_e2e1_explicit_agent_ref_runs(self, tmp_path):
        """E2E-1：``agent: greeter`` 显式引用 → opencode 真跑，agent_message 含期望答复。"""
        if not _opencode_available() or not _deepseek_auth_present():
            pytest.skip("opencode / deepseek auth 不可用")

        # workflow 同目录建 agents/greeter.md（resolver 查 workflow_dir/agents/）
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "greeter.md").write_text(
            "Reply with exactly this token and nothing else: GREETER_OK", encoding="utf-8"
        )
        (tmp_path / "wf.yaml").write_text(
            """
name: p14_e2e1
description: phase-14 E2E-1 显式 agent 引用 opencode 真跑
entry: g
nodes:
  - name: g
    kind: agent
    agent: greeter
    executor: opencode
    model: "deepseek/deepseek-v4-flash"
    routes:
      - to: $end
outputs:
  reply: "{{ g.output }}"
""",
            encoding="utf-8",
        )

        tape_root = _short_tape_root(tmp_path)
        tape_path = tape_root / "tape.jsonl"
        try:
            asyncio.run(self._drive(tmp_path / "wf.yaml", tape_path))
        finally:
            _ARTIFACTS.mkdir(exist_ok=True)
            if tape_path.exists():
                shutil.copy(tape_path, _ARTIFACTS / "phase14_e2e1_tape.jsonl")
            shutil.rmtree(tape_root, ignore_errors=True)

    async def _drive_e2e1_assert(self, app) -> None:
        from orca.iface.cli.app import OrcaApp  # noqa: F401（类型提示）

        events = list(app.bus.tape.replay())
        assert app.terminal_state is not None, "E2E-1：编排未到终态"
        assert app.terminal_state.status == "completed", (
            f"E2E-1：编排应 completed；got {app.terminal_state.status}"
        )
        agent_msgs = [
            e for e in events if e.type == "agent_message" and e.node == "g"
        ]
        assert agent_msgs, f"E2E-1：g 应有 agent_message；types={[e.type for e in events]}"
        joined = "".join(e.data.get("text", "") for e in agent_msgs).upper()
        assert "GREETER_OK" in joined, (
            f"E2E-1：agent_message 应含 GREETER_OK；joined={joined!r}"
        )

    def test_e2e2_folder_agent_resources_accessible(self, tmp_path):
        """E2E-2：文件夹化 agent + scripts/flag.txt → opencode 经 $ORCA_AGENT_RESOURCES 读 flag。"""
        if not _opencode_available() or not _deepseek_auth_present():
            pytest.skip("opencode / deepseek auth 不可用")

        bot_dir = tmp_path / "agents" / "filebot"
        (bot_dir / "scripts").mkdir(parents=True)
        # 文件夹 agent 入口 agent.md + frontmatter（model/tools）
        (bot_dir / "agent.md").write_text(
            "---\n"
            "description: 读资源 flag 的 bot\n"
            'model: "deepseek/deepseek-v4-flash"\n'
            "tools: [Bash]\n"
            "---\n"
            "Run this shell command exactly once, then reply with the command's stdout "
            "as your only output (no commentary):\n"
            "  cat $ORCA_AGENT_RESOURCES/scripts/flag.txt",
            encoding="utf-8",
        )
        (bot_dir / "scripts" / "flag.txt").write_text(
            "SECRET_FLAG_42", encoding="utf-8"
        )
        (tmp_path / "wf.yaml").write_text(
            """
name: p14_e2e2
description: phase-14 E2E-2 文件夹化 agent + resources opencode 真跑
entry: f
nodes:
  - name: f
    kind: agent
    agent: filebot
    executor: opencode
    routes:
      - to: $end
outputs:
  reply: "{{ f.output }}"
""",
            encoding="utf-8",
        )

        tape_root = _short_tape_root(tmp_path)
        tape_path = tape_root / "tape.jsonl"
        try:
            asyncio.run(self._drive(tmp_path / "wf.yaml", tape_path, e2e="e2e2"))
        finally:
            _ARTIFACTS.mkdir(exist_ok=True)
            if tape_path.exists():
                shutil.copy(tape_path, _ARTIFACTS / "phase14_e2e2_tape.jsonl")
            shutil.rmtree(tape_root, ignore_errors=True)

    async def _drive_e2e2_assert(self, app) -> None:
        events = list(app.bus.tape.replay())
        assert app.terminal_state is not None, "E2E-2：编排未到终态"
        assert app.terminal_state.status == "completed", (
            f"E2E-2：编排应 completed；got {app.terminal_state.status}"
        )
        # agent 经 Bash cat $ORCA_AGENT_RESOURCES/scripts/flag.txt → 输出含 flag
        agent_msgs = [
            e for e in events if e.type == "agent_message" and e.node == "f"
        ]
        joined = "".join(e.data.get("text", "") for e in agent_msgs)
        assert "SECRET_FLAG_42" in joined, (
            f"E2E-2：agent 应读到 $ORCA_AGENT_RESOURCES/scripts/flag.txt（SECRET_FLAG_42）；"
            f"joined={joined!r}"
        )

    # ── 共享驱动：起 OrcaApp（headless TUI），poll 终态，断言 ────────────────────

    async def _drive(self, wf_yaml: Path, tape_path: Path, *, e2e: str = "e2e1") -> None:
        from orca.compile import load_workflow
        from orca.iface.cli.app import OrcaApp

        wf = load_workflow(wf_yaml)
        app = OrcaApp(wf=wf, tape_path=tape_path)

        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.3)
            # poll 终态（opencode 单轮 ~10-40s；预留 180s 余量）
            for _ in range(900):
                if app.terminal_state is not None:
                    break
                await pilot.pause(0.2)
            else:
                pytest.fail(f"opencode+deepseek 编排 180s 未到终态（{e2e}）")
            await pilot.pause(0.5)

            if e2e == "e2e1":
                await self._drive_e2e1_assert(app)
            else:
                await self._drive_e2e2_assert(app)
