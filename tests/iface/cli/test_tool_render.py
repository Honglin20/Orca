"""test_tool_render.py —— render layer v1 测试（render-layer-design-draft §10 / §14.1）。

覆盖：
  - normalizer snapshot（fixtures-driven：相同输入 → 相同 RenderItem，跨端契约）
  - reducer：tool_call/result 配对 + thinking/message 累积 + seq 排序
  - claude-code 对齐 acceptance（§14.1）：
      * thinking 不渲染 markdown（dim+italic 纯文本）
      * message 走 markdown
  - fail loud（§6.2 / §13）：args 非 dict → NormalizeError；opencode read 目录 XML
    解析失败 → 降级 is_dir=False + warning（不 raise）
  - DRY 一致性守卫：log_stream + node_detail 共享 describe_tool_event（§7.3 第 1 步）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from orca.iface.cli.widgets.tool_render import (
    NormalizeError,
    describe_tool_event,
    normalize_tool,
    render_tool,
    RenderState,
    reduce_event,
)
from orca.iface.cli.widgets.tool_render.kinds import render_message, render_thinking
from orca.schema import Event, RenderItem

# ── fixtures 路径（spec §10.1 anchor）─────────────────────────────────────────
# tests/iface/cli/test_tool_render.py → tests/e2e_phase15/_artifacts/render_tool_cases.json
FIXTURES = Path(__file__).resolve().parents[2] / "e2e_phase15/_artifacts/render_tool_cases.json"


def _load_cases() -> list[dict]:
    """加载 fixtures（spec §10 共享 anchor）。"""
    return json.loads(FIXTURES.read_text())["cases"]


# ── normalizer snapshot（spec §10.1 / §6.2）──────────────────────────────────


class TestNormalizeSnapshot:
    """fixtures-driven snapshot：相同 (executor, tool, args, result) → 相同 RenderItem。

    spec §10.2：跨端一致性 anchor —— 任何一端实现偏离 spec 立即测试失败。
    """

    @pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
    def test_normalize_matches_expected(self, case: dict):
        """每个 case：normalize → 验证 kind/status/title/subtitle/payload 满足 fixture 契约。"""
        item = normalize_tool(
            executor=case["executor"],
            tool_name=case["tool_name"],
            args=case["args"],
            result=case["result"],
            status=case["status"],
        )
        expected = case["expected"]
        assert item.kind == expected["kind"], f"kind mismatch: {item.kind} != {expected['kind']}"
        assert item.status == expected["status"]
        assert item.title == expected["title"]
        assert item.subtitle == expected["subtitle"]
        # payload keys 必须严格匹配（spec §5.2 schema）
        assert set(item.payload.keys()) == set(expected["payload_keys"]), (
            f"payload keys mismatch: got {set(item.payload.keys())} != {set(expected['payload_keys'])}"
        )

        # payload 不变量（结构性断言，避免 snapshot 因字面值漂移）
        invs = expected.get("payload_invariants", {})
        for key, expected_val in invs.items():
            if key == "content_len":
                assert len(item.payload["content"]) == expected_val
            elif key == "content_first_line_n":
                assert item.payload["content"][0]["n"] == expected_val
            elif key == "content_first_line_text":
                assert item.payload["content"][0]["text"] == expected_val
            elif key == "entries_len":
                assert len(item.payload["entries"]) == expected_val
            elif key == "entries_first":
                assert item.payload["entries"][0] == expected_val
            elif key == "matches_len":
                assert len(item.payload["matches"]) == expected_val
            elif key == "matches_len_min":
                assert len(item.payload["matches"]) >= expected_val
            elif key == "matches_first":
                assert item.payload["matches"][0] == expected_val
            elif key == "matches_first_path_in":
                # grep: 第一个 group 的 path 在候选列表内
                paths = [g["path"] for g in item.payload["matches"]]
                assert any(p in expected_val for p in paths), \
                    f"no path in {expected_val}; got paths={paths}"
            elif key == "hunks_len_min":
                assert len(item.payload["hunks"]) >= expected_val
            elif key == "args_preview_contains":
                assert expected_val in item.payload["args_preview"]
            elif key == "result_preview_len_max":
                assert len(item.payload["result_preview"]) <= expected_val
            elif key == "bytes_min":
                assert item.payload["bytes"] >= expected_val
            elif key == "_note":
                pass  # 注释，跳过
            else:
                # 默认：值相等
                assert item.payload.get(key) == expected_val, \
                    f"payload[{key!r}] mismatch: {item.payload.get(key)!r} != {expected_val!r}"

        # raw 字段含原始 args/result（spec §5.1：永不参与渲染决策）
        assert item.raw["args"] == case["args"]
        # result：None 时 raw["result"]=None；非 None 时 == 输入
        assert item.raw["result"] == case["result"]
        # raw 还含 tool_name（reducer 配对用）
        assert item.raw["tool_name"] == case["tool_name"]


# ── fail loud（spec §6.2 / §13）───────────────────────────────────────────────


class TestFailLoud:
    """spec §6.2：args 非 dict → NormalizeError（不静默吞）。

    spec §13：opencode read 目录 XML 解析失败 → 降级 + warning（不 raise）。
    """

    def test_args_non_dict_raises(self):
        """args 非 dict → NormalizeError（translator 层契约破裂显式化）。"""
        with pytest.raises(NormalizeError) as exc_info:
            normalize_tool(
                executor="claude",
                tool_name="Bash",
                args="not a dict",  # type: ignore[arg-type]
                result=None,
                status="running",
            )
        # 错误信息含类型 + 值（debug 友好）
        msg = str(exc_info.value)
        assert "args must be dict" in msg
        assert "str" in msg

    def test_args_none_raises(self):
        """args=None 同样 fail loud（None 不该到 normalizer）。"""
        with pytest.raises(NormalizeError):
            normalize_tool(
                executor="opencode",
                tool_name="read",
                args=None,  # type: ignore[arg-type]
                result=None,
                status="running",
            )

    def test_opencode_dir_xml_malformed_falls_back(self, caplog):
        """spec §13：opencode read 目录 XML 解析失败 → 降级 + warning（不 raise）。"""
        malformed = "<path>/bad</path>\n<type>directory</type>\n<entries>not closed"
        item = normalize_tool(
            executor="opencode",
            tool_name="read",
            args={"filePath": "/bad"},
            result=malformed,
            status="completed",
        )
        # 降级：is_dir=False（不走目录树）+ content 原样文本
        assert item.payload["is_dir"] is False
        assert "content" in item.payload
        # warning log 应被记录（fail visible）
        assert any("XML" in r.message or "目录" in r.message for r in caplog.records), (
            "opencode 目录 XML 解析失败应记 warning log（fail visible，spec §13）"
        )


# ── GAP #1 闭环：opencode read 文件 XML envelope（spec §6.3）─────────────────


# 真实 tape 证据样本（取自 ``runs/demo_task-20260704-085641-f15c8d.jsonl`` seq=5，
# 截短为 7 行 + 末尾 marker）。GAP #1：opencode ``read`` 一个**文件**也返回 XML
# envelope，原实现只检测 ``<type>directory</type>``，file 走兜底 → 双重行号 +
# tag 泄漏 + EOF marker 漏出。修复后应剥 envelope 还原干净文本。
_OPENCODE_READ_FILE_ENVELOPE = (
    "<path>/abs/pyproject.toml</path>\n"
    "<type>file</type>\n"
    "<content>\n"
    "1: [build-system]\n"
    "2: requires = [\"hatchling\"]\n"
    "3: build-backend = \"hatchling.build\"\n"
    "4: \n"
    "5: [project]\n"
    "6: name = \"orca\"\n"
    "(End of file - total 6 lines)\n"
    "</content>"
)


class TestOpencodeReadFileEnvelope:
    """GAP #1：opencode ``read`` 文件 XML envelope 解析（spec §6.3 订正）。"""

    def test_opencode_read_file_strips_envelope(self):
        """剥 envelope：payload.content 不含 ``<path>`` / ``<type>file</type>`` /
        ``<content>`` tag、不含 ``N:`` 行号前缀、不含 ``(End of file`` marker。"""
        item = normalize_tool(
            executor="opencode",
            tool_name="read",
            args={"filePath": "/abs/pyproject.toml"},
            result=_OPENCODE_READ_FILE_ENVELOPE,
            status="completed",
        )
        assert item.payload["is_dir"] is False
        content = item.payload["content"]
        # 还原成纯文本，逐项断言
        rendered_text = "\n".join(line["text"] for line in content)
        # 不含 envelope tag（spec §6.3 file envelope 应被剥）
        assert "<path>" not in rendered_text
        assert "<type>file</type>" not in rendered_text
        assert "<content>" not in rendered_text
        assert "</content>" not in rendered_text
        # 不含 opencode 自加的 ``N:`` 行号前缀（避免与 Rich Syntax 双重行号）
        for line in content:
            # ``N: `` 仅当整行以数字开头 + 冒号才视作残留前缀；正常 TOML 行首无此模式
            assert not (line["text"] and line["text"][0].isdigit() and ":" in line["text"][:6]), (
                f"行 {line['n']} 仍含 ``N:`` 行号前缀：{line['text']!r}"
            )
        # 不含尾部 EOF marker
        assert "(End of file" not in rendered_text

    def test_opencode_read_file_preserves_line_count(self):
        """剥 envelope 后行数 == 原文件行数（fixture 6 行；EOF marker 不算）。"""
        item = normalize_tool(
            executor="opencode",
            tool_name="read",
            args={"filePath": "/abs/pyproject.toml"},
            result=_OPENCODE_READ_FILE_ENVELOPE,
            status="completed",
        )
        content = item.payload["content"]
        # fixture 含 6 行原文（含 1 个空行）+ EOF marker（被剥）
        assert len(content) == 6, f"应剥到 6 行，实际 {len(content)}"
        # 行号连续 1..6（_line_numbered 契约）
        assert [c["n"] for c in content] == list(range(1, 7))
        # 关键行字面值（剥 ``N: `` 前缀后）
        assert content[0]["text"] == "[build-system]"
        assert content[1]["text"] == "requires = [\"hatchling\"]"
        # 空行（fixture 第 4 行）：opencode 给 ``4: ``（带尾空格），剥后应是空串
        assert content[3]["text"] == "", f"空行应剥为空串，实际 {content[3]['text']!r}"

    def test_opencode_read_file_tape_evidence(self):
        """真实 tape 证据回归（``runs/demo_task-20260704-085641-f15c8d.jsonl`` seq=5）。

        读取 tape 提取 seq=5 的 result → normalize → 验证 payload.content 干净。
        这是 GAP #1 的真跑证据 anchor，防 opencode 升级 envelope shape 时静默漂移。
        """
        tape_path = Path(__file__).resolve().parents[3] / "runs/demo_task-20260704-085641-f15c8d.jsonl"
        if not tape_path.exists():
            pytest.skip(f"真实 tape 不存在：{tape_path}（CI 环境无 runs/ 时跳过）")
        result_text = None
        for line in tape_path.read_text().splitlines():
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if ev.get("seq") == 5 and ev.get("type") == "agent_tool_result":
                result_text = ev["data"]["result"]
                break
        assert result_text is not None, "tape seq=5 agent_tool_result 不存在"
        # 必须是 file envelope（不是 directory）—— 这是 GAP #1 的前提
        assert "<type>file</type>" in result_text

        item = normalize_tool(
            executor="opencode",
            tool_name="read",
            args={"filePath": "/abs/pyproject.toml"},
            result=result_text,
            status="completed",
        )
        content = item.payload["content"]
        rendered_text = "\n".join(line["text"] for line in content)
        # 不变量：envelope tag 全被剥
        assert "<path>" not in rendered_text
        assert "<type>file</type>" not in rendered_text
        assert "<content>" not in rendered_text
        # EOF marker 不出现
        assert "(End of file" not in rendered_text
        # 行数 == 原 tape 末尾 marker 声明的 total（72 行）
        assert len(content) == 72, (
            f"剥 envelope 后应得 72 行（与 opencode 声明一致），实际 {len(content)}"
        )


class TestFileWriteSubtitle:
    """GAP #2：file_write subtitle（spec §8.1 ``✏ <path> (new, NB)``）。"""

    def test_file_write_subtitle_new_bytes(self):
        """file_write payload → subtitle == ``new, NB``。"""
        item = normalize_tool(
            executor="claude",
            tool_name="Write",
            args={"file_path": "new.py", "content": "import os\n\nprint(os.getcwd())\n"},
            result="",
            status="completed",
        )
        assert item.kind == "file_write"
        # content 是 ``import os\n\nprint(os.getcwd())\n`` → 30 bytes (utf-8)
        assert item.payload["bytes"] == 30
        assert item.subtitle == "new, 30B"

    def test_file_write_subtitle_zero_bytes(self):
        """空文件：subtitle == ``new, 0B``（边界：bytes=0 仍渲染 new marker）。"""
        item = normalize_tool(
            executor="opencode",
            tool_name="write",
            args={"filePath": "empty.txt", "content": ""},
            result="",
            status="completed",
        )
        assert item.subtitle == "new, 0B"


# ── reducer（spec §9）─────────────────────────────────────────────────────────


def _ev(seq: int, etype: str, data: dict, *, node: str = "n1", session: str = "s1") -> Event:
    """构造 Event helper。"""
    return Event(seq=seq, type=etype, timestamp=0.0, node=node, session_id=session, data=data)


class TestReducer:
    """spec §9：Event 流累积 reducer 规则。"""

    def test_message_accumulates_by_session_node(self):
        """spec §9.2：agent_message → messages[key] += text。"""
        state = RenderState()
        reduce_event(state, _ev(1, "agent_message", {"text": "hello "}), executor="claude")
        reduce_event(state, _ev(2, "agent_message", {"text": "world"}), executor="claude")
        key = "s1|n1"
        assert state.messages[key] == "hello world"
        assert (1, "message", key) in state.order
        assert (2, "message", key) in state.order

    def test_thinking_accumulates_even_when_hidden(self):
        """spec §9.2：thinking_visible=False 时仍累积（保可重建性）。"""
        state = RenderState()
        state.thinking_visible = False
        reduce_event(state, _ev(1, "agent_thinking", {"text": "think..."}), executor="claude")
        key = "s1|n1"
        # 累积不丢
        assert state.thinking[key] == "think..."
        # order 也记录（重渲时仍按 seq 排序，切回可见立即出现）
        assert (1, "thinking", key) in state.order

    def test_tool_call_creates_running_card(self):
        """spec §9.2：agent_tool_call → normalize(status=running) → tool_cards[id]。"""
        state = RenderState()
        reduce_event(
            state,
            _ev(1, "agent_tool_call", {"tool": "Bash", "args": {"command": "ls"}, "tool_call_id": "tc1"}),
            executor="claude",
        )
        assert "tc1" in state.tool_cards
        item = state.tool_cards["tc1"]
        assert item.kind == "shell"
        assert item.status == "running"
        # order 按 call 时的 seq
        assert (1, "tool", "tc1") in state.order

    def test_tool_result_pairs_with_call(self):
        """spec §9.2：agent_tool_result → 重新 normalize(status=completed) 覆盖。"""
        state = RenderState()
        reduce_event(
            state,
            _ev(1, "agent_tool_call", {"tool": "Bash", "args": {"command": "ls"}, "tool_call_id": "tc1"}),
            executor="claude",
        )
        reduce_event(
            state,
            _ev(2, "agent_tool_result", {"tool_call_id": "tc1", "result": "out"}),
            executor="claude",
        )
        item = state.tool_cards["tc1"]
        assert item.status == "completed"
        # payload 已填充 result（shell.output）
        assert item.payload["output"] == "out"
        # order 不变（位置由 call 时的 seq 决定）
        assert (1, "tool", "tc1") in state.order
        # result 不入 order（避免重复）
        assert all(o[0] != 2 for o in state.order)

    def test_tool_result_without_call_logs_warning(self, caplog):
        """spec §9.2 防御：tool_result 无对应 call → 跳过 + warning（fail visible）。"""
        state = RenderState()
        reduce_event(
            state,
            _ev(1, "agent_tool_result", {"tool_call_id": "orphan", "result": "x"}),
            executor="claude",
        )
        # 不崩 + warning log（不静默丢）
        assert "orphan" not in state.tool_cards
        assert any("tool_result" in r.message for r in caplog.records)

    def test_seq_ordering_is_monotonic(self):
        """spec §9.3 / §12.11：order 按 seq 单调递增（Tape 不变量保证）。"""
        state = RenderState()
        reduce_event(state, _ev(5, "agent_message", {"text": "a"}), executor="claude")
        reduce_event(state, _ev(2, "agent_thinking", {"text": "b"}), executor="claude")
        reduce_event(state, _ev(8, "agent_tool_call", {"tool": "Bash", "args": {}, "tool_call_id": "x"}), executor="claude")
        ordered = state.ordered_entries()
        seqs = [seq for seq, _, _ in ordered]
        assert seqs == sorted(seqs), "order 应按 seq 单调递增（spec §12.11）"


# ── claude-code 对齐 acceptance（spec §14.1）─────────────────────────────────


class TestClaudeCodeAlignment:
    """spec §14.1：thinking 不渲染 markdown；message 渲染 markdown。"""

    def test_thinking_no_markdown(self):
        """§14.1 正例：thinking 文本含 markdown 语法 → snapshot 与 raw text 字符级一致
        （仅允许 dim+italic 文本样式包裹）。

        验证 claude-code 对齐（§12.8）：thinking 不渲染 markdown。
        """
        raw_text = "# 标题\n\n**bold** 和 `code` 与 - 列表"
        rendered = render_thinking(raw_text)
        # 必须是 Text（不是 Markdown）—— claude-code 对齐
        assert isinstance(rendered, Text), "thinking 应渲染为 Text（非 Markdown），spec §12.8"
        # 文本内容字符级一致（不丢字符 / 不解析 markdown）
        assert str(rendered) == raw_text, "thinking 文本应字符级一致（不渲染 markdown）"
        # 样式：dim + italic
        assert "dim" in str(rendered.style)
        assert "italic" in str(rendered.style)

    def test_message_renders_markdown(self):
        """§14.1 反例：相同文本走 agent_message → 应渲染为 markdown（不是 raw text）。

        验证 Markdown 路径走通（Rich Markdown 默认开 Syntax，§12.12）。
        """
        markdown_text = "# 标题\n\n**bold** 和 `code`"
        rendered = render_message(markdown_text)
        # 必须是 Markdown（不是 Text）—— 与 thinking 区别
        from rich.markdown import Markdown
        assert isinstance(rendered, Markdown), "message 应渲染为 Markdown（spec §8.3）"

        # 用 Console 渲染到纯文本，应包含 markdown 处理痕迹（如 H1 的下划线或加粗 ESC 序列）
        console = Console(record=True, width=80)
        console.print(rendered)
        rendered_str = console.export_text()
        # 不应字符级 == raw_text（已过 markdown 处理）
        assert rendered_str != markdown_text, \
            "message 应被 markdown 处理（不应原样输出）"


# ── DRY 一致性守卫（spec §7.3 第 1 步）────────────────────────────────────────


class TestDRYConsistency:
    """spec §7.3：log_stream + node_detail 工具事件摘要共享 describe_tool_event。"""

    def test_describe_tool_event_call_format(self):
        """工具事件单行摘要：``tool: Bash({args})``。"""
        desc = describe_tool_event(
            "agent_tool_call",
            {"tool": "Bash", "args": {"command": "ls"}},
            detail="log",
        )
        assert desc == "tool: Bash({'command': 'ls'})" or "tool: Bash(" in desc

    def test_describe_tool_event_result_format(self):
        """工具结果单行摘要：``→ <result>``。"""
        desc = describe_tool_event(
            "agent_tool_result",
            {"tool_call_id": "x", "result": "ok output"},
            detail="log",
        )
        assert desc == "→ ok output"

    def test_describe_tool_event_unknown_etype_returns_empty(self):
        """非工具事件 etype → 空串（caller 不该调，但防御性兜底）。"""
        desc = describe_tool_event("agent_message", {"text": "hi"}, detail="log")
        assert desc == ""


# ── render_tool 派发（spec §3.1 第四层）───────────────────────────────────────


class TestRegistryDispatch:
    """spec §3.1：kind → renderer 派发表。"""

    @pytest.mark.parametrize("kind,expected_renderer_name", [
        ("file_read", "Panel"),
        ("file_write", "Panel"),
        ("file_edit", "Panel"),
        ("shell", "Panel"),
        ("glob", "Panel"),
        ("grep", "Panel"),
        ("unknown", "Panel"),
    ])
    def test_each_kind_renders_to_panel(self, kind, expected_renderer_name):
        """每个 kind 都能渲染（不抛），且返回 Rich Panel（共性规则 §8.2）。"""
        # 构造最小 RenderItem（手工填充，绕过 normalizer）
        item = RenderItem(
            kind=kind,  # type: ignore[arg-type]
            status="completed",
            title="t",
            subtitle="",
            payload=self._min_payload(kind),  # type: ignore[attr-defined]
            raw={"args": {}, "result": None, "tool_name": "X"},
        )
        rendered = render_tool(item)
        from rich.panel import Panel
        assert isinstance(rendered, Panel), f"{kind} 应渲染为 Panel（共性规则 §8.2）"

    @staticmethod
    def _min_payload(kind: str) -> dict:
        """per-kind 最小 payload（让 renderer 不抛）。"""
        if kind == "file_read":
            return {"path": "p", "is_dir": False, "content": [], "truncated": False}
        if kind == "file_write":
            return {"path": "p", "content": [], "bytes": 0}
        if kind == "file_edit":
            return {"path": "p", "hunks": [], "added": 0, "deleted": 0}
        if kind == "shell":
            return {"command": "c", "output": ""}
        if kind == "glob":
            return {"pattern": "*", "matches": []}
        if kind == "grep":
            return {"pattern": "*", "matches": []}
        if kind == "unknown":
            return {"tool_name": "", "args_preview": "{}", "result_preview": ""}
        return {}
