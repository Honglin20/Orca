"""test_spike.py —— P3:0-b ask-user spike 的 pytest 测试。

**分层**：
- ``TestSentinel``：哨兵识别 / 解析的纯单元测试（不依赖 backend / orca CLI）。
- ``TestMockBackend``：Mock 后端的行为契约（task_id 复用、scenario 用尽 fail loud）。
- ``TestDriverPure``：driver 单节点循环的纯逻辑测试（用 Mock backend + fake orca_cli）。
- ``TestEndToEndWithRealOrca``：真 ``orca`` CLI 的 2 节点闭环（mock backend）+ 重入 fail loud。
  这一组**会**真启动 orca run（落 tape），但**不**真 spawn claude（用 mock 子 agent）。

CI：默认全跑（只依赖 orca CLI + mock backend，无外部 API）；``-m integration`` 才跑真 claude。

依赖单向：仅依赖本目录模块 + pytest。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from tests.spike_ask_user.backend import SubagentBackend, SubagentResult
from tests.spike_ask_user.mock_backend import (
    MockSubagentBackend,
    ScenarioExhausted,
)
from tests.spike_ask_user.orca_cli import (
    BootstrapResult,
    NextResult,
    OrcaCLIError,
)
from tests.spike_ask_user.sentinel import (
    MAX_ASK,
    SENTINEL_VALUE,
    AskUserQuestion,
    SentinelError,
    SentinelLoopExhausted,
    build_sentinel_message,
    is_sentinel,
    looks_fabricated,
    parse_sentinel,
)
from tests.spike_ask_user.tars_loop import (
    AnswerProvider,
    FabricationDetected as LoopFabricationDetected,
    NodeDriveLog,
    OrcaBusyError,
    WorkflowDriveLog,
    drive_node,
    drive_workflow,
)


# ──────────────────────────────────────────────────────────────────────────────
# TestSentinel —— 纯单元测试（SPEC §1 strict 识别）
# ──────────────────────────────────────────────────────────────────────────────


class TestSentinel:
    """SPEC §1：``_sentinel:"orca_ask_user_v1"`` 的 strict 识别。"""

    def test_valid_sentinel_detected(self):
        msg = build_sentinel_message(
            question="calib?", options=["a:foo", "b:bar"], context="glob 无果"
        )
        assert is_sentinel(msg) is True
        q = parse_sentinel(msg)
        assert q == AskUserQuestion(
            question="calib?", options=("a:foo", "b:bar"), context="glob 无果"
        )

    def test_sentinel_in_code_fence_still_detected(self):
        """子 agent 常把 JSON 包在 ```json 围栏里，必须仍能识别。"""
        msg = (
            "我读代码没找到 loader，需要问用户：\n"
            "```json\n"
            + build_sentinel_message("calib?", ["a:foo"], "无果")
            + "\n```\n请恢复我。"
        )
        assert is_sentinel(msg) is True

    def test_sentinel_with_leading_text_detected(self):
        """哨兵前后有自然语言，最外层 JSON 对象仍应被抽出。"""
        msg = (
            "解释一段：以下是哨兵——"
            + build_sentinel_message("q", ["o1", "o2"], "c")
            + "——完毕"
        )
        assert is_sentinel(msg) is True

    def test_substring_match_rejected(self):
        """SPEC §1 末段：禁止 substring match。

        合法 agent 输出碰巧含 ``_orca_ask_user`` 字面量但 ``_sentinel`` 不符 → False。
        """
        # _sentinel 版本不符
        fake_version = json.dumps(
            {"_orca_ask_user": "q", "options": [], "context": "c", "_sentinel": "orca_ask_user_v2"}
        )
        assert is_sentinel(fake_version) is False
        # 完全没有 _sentinel 键
        no_sentinel = json.dumps(
            {"_orca_ask_user": "q", "options": [], "context": "c"}
        )
        assert is_sentinel(no_sentinel) is False
        # 纯字符串里出现 ``_orca_ask_user`` 字面量
        text_only = "I will never return _orca_ask_user, I promise"
        assert is_sentinel(text_only) is False
        # 非 JSON
        assert is_sentinel("not json at all") is False
        # 空
        assert is_sentinel("") is False

    def test_parse_rejects_unknown_keys(self):
        """schema 严格：unknown key fail loud。"""
        bad = json.dumps(
            {
                "_orca_ask_user": "q",
                "options": ["a"],
                "context": "c",
                "_sentinel": SENTINEL_VALUE,
                "extra_field": "should be rejected",
            }
        )
        with pytest.raises(SentinelError, match="未知字段"):
            parse_sentinel(bad)

    def test_parse_rejects_wrong_types(self):
        """schema 严格：类型错 fail loud。"""
        # _orca_ask_user 不是 str
        bad1 = json.dumps(
            {"_orca_ask_user": 42, "options": [], "context": "c", "_sentinel": SENTINEL_VALUE}
        )
        with pytest.raises(SentinelError, match="_orca_ask_user"):
            parse_sentinel(bad1)
        # options 不是 list[str]
        bad2 = json.dumps(
            {"_orca_ask_user": "q", "options": "not a list", "context": "c", "_sentinel": SENTINEL_VALUE}
        )
        with pytest.raises(SentinelError, match="options"):
            parse_sentinel(bad2)
        # context 不是 str
        bad3 = json.dumps(
            {"_orca_ask_user": "q", "options": [], "context": 99, "_sentinel": SENTINEL_VALUE}
        )
        with pytest.raises(SentinelError, match="context"):
            parse_sentinel(bad3)

    def test_parse_non_sentinel_raises(self):
        """非哨兵文本去 parse → fail loud（不是返回 None）。"""
        with pytest.raises(SentinelError, match="非哨兵文本"):
            parse_sentinel("totally normal agent output")

    def test_fabrication_detector(self):
        """SPEC §3：真实 output 不应含 torch.randn 等造假痕迹。"""
        assert looks_fabricated("calib = torch.randn(8, 3)") is True
        assert looks_fabricated("x = torch.rand(4)") is True
        assert looks_fabricated("loader = fake_data()") is True
        assert looks_fabricated("dummy_calib = ...") is True
        # 真实 output 不应触发
        assert looks_fabricated(
            json.dumps({"calib_loader": "myproj.data:load_calib", "source": "user"})
        ) is False

    def test_fabrication_word_boundary(self):
        """``\\b`` 词边界：下划线连接 / 字母后续**不应**误判为造假。

        守住「合法变量名碰巧含 torch_randn 字串不触发」的边界。
        """
        # 下划线连接的合法标识符——不应触发
        assert looks_fabricated("mymodule.torch_randn()") is False
        # 字母后接——不应触发（\b 边界）
        assert looks_fabricated("torch.randnX") is False
        # 但真正的 ``torch.randn(...)`` 仍要触发
        assert looks_fabricated("torch.randn(8, 3)") is True

    def test_is_sentinel_rejects_non_str(self):
        """defensive branch：非 str 输入返 False 而非 raise（driver 调用链上 output 恒 str）。"""
        assert is_sentinel(None) is False  # type: ignore[arg-type]
        assert is_sentinel(123) is False  # type: ignore[arg-type]
        assert is_sentinel(["not", "a", "str"]) is False  # type: ignore[arg-type]
        assert is_sentinel({"_sentinel": SENTINEL_VALUE}) is False  # type: ignore[arg-type]

    def test_parse_accepts_empty_options(self):
        """空 options 合法（SPEC §1 没禁止；子 agent 可能只问不给候选）。

        锁住此契约——防止未来某次重构悄悄加上 `if not options: raise`。
        """
        msg = build_sentinel_message("open question?", [], "no candidates")
        q = parse_sentinel(msg)
        assert q.options == ()

    def test_parse_accepts_multiline_question_with_quotes(self):
        """question / context 含换行、引号、中文混合：JSON 解码正确。"""
        msg = build_sentinel_message(
            question='calib loader 是 "myproj.data:load_calib"\n还是别的？',
            options=['"myproj.data:load_calib"', '其他'],
            context='我 grep 了 DataLoader\n看到多个候选',
        )
        q = parse_sentinel(msg)
        assert '"' in q.question
        assert "\n" in q.question
        assert "\n" in q.context

    def test_sentinel_with_nested_json_in_context(self):
        """context 字段含嵌套 JSON 文本时，``_extract_json_object`` 的括号配平状态机
        必须抽出**最外层**哨兵对象（而非被 context 里的 ``}`` 提前截断）。

        高回归风险测试——若有人把括号配平改正则或简化扫描，这条兜底。
        """
        # context 里嵌一个示例 JSON（合法——SPEC §1 说 context 是自由文本）
        nested = '{"example": {"inner": 1}, "list": [1, 2, 3]}'
        msg = build_sentinel_message(
            question="calib loader 在哪？",
            options=["a:foo"],
            context=f"看到的结构是 {nested}",
        )
        # is_sentinel 仍应识别
        assert is_sentinel(msg) is True
        # parse 出的 context 完整保留嵌套 JSON 文本（未被截断）
        q = parse_sentinel(msg)
        assert nested in q.context

    def test_extract_picks_first_balanced_json(self):
        """输出含两个 JSON 对象时，``_extract_json_object`` 取首个配平的。

        子 agent 可能回复「分析结果：{...合法输出...} 顺便提一下：{...}」——
        driver 必须只看首个完整 ``{...}`` 块。
        """
        from tests.spike_ask_user.sentinel import _extract_json_object

        text = '{"a": 1} 后面还有 {"b": 2}'
        extracted = _extract_json_object(text)
        assert extracted == '{"a": 1}'

    def test_max_ask_constant(self):
        """SPEC §4：MAX_ASK = 3（编译期常量）。"""
        assert MAX_ASK == 3
        """输出含两个 JSON 对象时，``_extract_json_object`` 取首个配平的。

        子 agent 可能回复「分析结果：{...合法输出...} 顺便提一下：{...}」——
        driver 必须只看首个完整 ``{...}`` 块。
        """
        from tests.spike_ask_user.sentinel import _extract_json_object

        text = '{"a": 1} 后面还有 {"b": 2}'
        extracted = _extract_json_object(text)
        assert extracted == '{"a": 1}'


# ──────────────────────────────────────────────────────────────────────────────
# TestOrcaCLIErrors —— orca_cli.py 5 处 fail-loud 路径（monkeypatch subprocess）
# ──────────────────────────────────────────────────────────────────────────────


class TestOrcaCLIErrors:
    """SPEC §fail-loud：``orca_cli._run_orca`` 的 5 处 raise（超时/非零退出/空 stdout/
    非 JSON / 缺关键字段）必须 fail loud。

    用 monkeypatch 替换 ``subprocess.run``，不真起 orca 子进程——纯控制流测试。
    """

    def test_timeout_raises(self, monkeypatch):
        import subprocess as sp
        from tests.spike_ask_user import orca_cli

        def _timeout(*a, **kw):
            raise sp.TimeoutExpired(cmd=["orca"], timeout=0.01)

        monkeypatch.setattr(orca_cli.subprocess, "run", _timeout)
        with pytest.raises(OrcaCLIError, match="超时"):
            orca_cli.bootstrap("spike_ask_user", {})

    def test_nonzero_exit_raises(self, monkeypatch):
        from tests.spike_ask_user import orca_cli

        def _fail(*a, **kw):
            return sp.CompletedProcess(args=a, returncode=1, stdout="", stderr="boom")

        import subprocess as sp
        monkeypatch.setattr(orca_cli.subprocess, "run", _fail)
        with pytest.raises(OrcaCLIError, match="非零退出"):
            orca_cli.bootstrap("spike_ask_user", {})

    def test_empty_stdout_raises(self, monkeypatch):
        import subprocess as sp
        from tests.spike_ask_user import orca_cli

        def _empty(*a, **kw):
            return sp.CompletedProcess(args=a, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(orca_cli.subprocess, "run", _empty)
        with pytest.raises(OrcaCLIError, match="stdout 空"):
            orca_cli.bootstrap("spike_ask_user", {})

    def test_non_json_stdout_raises(self, monkeypatch):
        import subprocess as sp
        from tests.spike_ask_user import orca_cli

        def _garbage(*a, **kw):
            return sp.CompletedProcess(args=a, returncode=0, stdout="not json", stderr="")

        monkeypatch.setattr(orca_cli.subprocess, "run", _garbage)
        with pytest.raises(OrcaCLIError, match="非 JSON"):
            orca_cli.bootstrap("spike_ask_user", {})

    def test_bootstrap_missing_required_fields_raises(self, monkeypatch):
        """bootstrap 结果缺 run_id / prompt / done → fail loud（防 KeyError 误判）。"""
        import subprocess as sp
        from tests.spike_ask_user import orca_cli

        def _missing(*a, **kw):
            # 返回 JSON 但缺关键字段
            return sp.CompletedProcess(
                args=a, returncode=0, stdout='{"name": "spike"}', stderr=""
            )

        monkeypatch.setattr(orca_cli.subprocess, "run", _missing)
        with pytest.raises(OrcaCLIError, match="缺字段"):
            orca_cli.bootstrap("spike_ask_user", {})


# 将 ``test_max_ask_constant`` 显式挪回 TestSentinel（之前因插入顺序被错放进
# TestOrcaCLIErrors）。下面会再 assert 一次——属于 sentinel 契约（SPEC §4）。


# ──────────────────────────────────────────────────────────────────────────────
# TestMockBackend —— Mock 后端契约
# ──────────────────────────────────────────────────────────────────────────────


class TestMockBackend:
    """Mock 后端：task_id 唯一性、scenario 用尽 fail loud、calls_per_task 准确。"""

    def test_spawn_returns_unique_task_ids(self):
        """全局时序：两次 spawn 各取 scenario 的下一个 output（不重置）。"""
        backend = MockSubagentBackend(["a", "b", "c"])
        r1 = backend.spawn("p1")
        r2 = backend.spawn("p2")
        assert r1.task_id != r2.task_id
        # 全局时序：spawn #1 → scenario[0]，spawn #2 → scenario[1]
        assert r1.output == "a"
        assert r2.output == "b"
        assert backend.spawn_count == 2

    def test_resume_reuses_same_task_id(self):
        backend = MockSubagentBackend(["spawn-out", "resume-out"])
        spawn_res = backend.spawn("prompt")
        resume_res = backend.resume(spawn_res.task_id, "answer")
        assert resume_res.task_id == spawn_res.task_id
        assert resume_res.call_index == 1
        assert resume_res.output == "resume-out"
        # calls_per_task 反映 spawn + resume = 2 次调用
        assert backend.calls_per_task() == {spawn_res.task_id: 2}

    def test_resume_unknown_task_fails_loud(self):
        backend = MockSubagentBackend(["a"])
        with pytest.raises(KeyError, match="unknown task_id"):
            backend.resume("nonexistent-task", "msg")

    def test_scenario_exhausted_fails_loud(self):
        """scenario 用尽 → ScenarioExhausted，不静默返回空。"""
        backend = MockSubagentBackend(["only-one-output"])
        backend.spawn("p")
        with pytest.raises(ScenarioExhausted):
            backend.resume(backend.spawned_task_ids[0], "msg")

    def test_empty_scenario_rejected(self):
        with pytest.raises(ValueError, match="scenario 不能为空"):
            MockSubagentBackend([])


# ──────────────────────────────────────────────────────────────────────────────
# TestDriverPure —— driver 单节点循环的纯逻辑（fake orca_cli）
# ──────────────────────────────────────────────────────────────────────────────


class _FakeOrcaCLI:
    """fake ``WorkflowDriverProtocol``：不真调 orca，按预设脚本返结果。

    让 driver 单元测试完全脱离真 orca CLI——只验证「哨兵检测 + task_id 捕获 +
    resume + 喂 next」的控制流。
    """

    def __init__(
        self,
        *,
        bootstrap_prompt: str = "<node-A prompt>",
        next_outputs: list[NextResult] | None = None,
    ) -> None:
        self._boot_prompt = bootstrap_prompt
        # next_outputs 按 driver 调用顺序消费；用尽 → fail loud
        self._next_outputs = list(next_outputs or [])
        self.bootstrap_calls: list[tuple[str, dict | None]] = []
        self.next_calls: list[tuple[str, str]] = []
        self.stop_calls: list[str] = []
        # public：测试断言「stop 被调用了 boot_run_id」用，无需 _ 前缀。
        self.boot_run_id = "fake-run-id-001"

    def bootstrap(self, wf: str, inputs: dict[str, Any] | None) -> BootstrapResult:
        self.bootstrap_calls.append((wf, inputs))
        return BootstrapResult(
            run_id=self.boot_run_id,
            tape="fake/tape.jsonl",
            node="data_finder",
            prompt=self._boot_prompt,
            prompt_file="fake/prompts/data_finder.md",
            done=False,
            raw={"run_id": self.boot_run_id},
        )

    def next_step(self, run_id: str, output: str) -> NextResult:
        self.next_calls.append((run_id, output))
        if not self._next_outputs:
            raise OrcaCLIError("fake next_outputs 用尽（测试构造不当）")
        return self._next_outputs.pop(0)

    def stop(self, run_id: str) -> dict[str, Any]:
        self.stop_calls.append(run_id)
        return {"ok": True, "run_id": run_id}


def _make_next_done() -> NextResult:
    return NextResult(done=True, prompt="", node="", busy=False, retry_after_ms=0, raw={"done": True})


def _make_next_prompt(text: str, node: str = "data_consumer") -> NextResult:
    return NextResult(done=False, prompt=text, node=node, busy=False, retry_after_ms=0, raw={})


class TestDriverPure:
    """driver 循环的纯逻辑测试（不依赖真 orca）。"""

    def test_drive_node_sentinel_once_then_real(self):
        """SPEC §2 主路径：spawn 哨兵 → 问用户 → resume → 真实 output。"""
        backend = MockSubagentBackend(
            [
                build_sentinel_message("calib?", ["a:foo"], "无果"),
                json.dumps({"calib_loader": "a:foo", "source": "user"}),
            ]
        )

        answers: list[str | None] = []

        def provider(q: AskUserQuestion) -> str | None:
            answers.append(q.options[0])
            return q.options[0]

        output, log = drive_node(
            backend, "<node A prompt>", provider, node_name="data_finder"
        )

        # 真实 output 拿到
        assert json.loads(output)["calib_loader"] == "a:foo"
        # 哨兵只触发 1 次
        assert log.sentinel_triggered == 1
        # resume 调用 1 次，task_id 与 spawn 相同
        assert log.resumed_count == 1
        log.assert_task_id_reused()  # 断言：所有 resume 都用 spawn 的 task_id
        # 答案被 provider 收到
        assert answers == ["a:foo"]

    def test_drive_node_no_sentinel_passes_through(self):
        """子 agent 不返回哨兵 → driver 直接拿真实 output，0 次 resume。"""
        backend = MockSubagentBackend(
            [json.dumps({"calib_loader": "a:foo", "source": "inferred"})]
        )
        output, log = drive_node(
            backend, "<prompt>", lambda _q: "should-not-be-called"
        )
        assert json.loads(output)["calib_loader"] == "a:foo"
        assert log.sentinel_triggered == 0
        assert log.resumed_count == 0
        assert log.final_task_id  # spawn 的 task_id 落地

    def test_drive_node_reentry_3x_fails_loud(self):
        """SPEC §4：连续哨兵 ≥ MAX_ASK → SentinelLoopExhausted。"""
        # spawn + MAX_ASK 次 resume 都返回哨兵 = MAX_ASK+1 个 scenario
        scenario = [build_sentinel_message("q", ["o"], "c")] * (MAX_ASK + 1)
        backend = MockSubagentBackend(scenario)
        with pytest.raises(SentinelLoopExhausted, match=str(MAX_ASK)):
            drive_node(backend, "<prompt>", lambda _q: None)  # 恒答「不知道」

        # 断言：driver 在第 MAX_ASK 次 resume 后中断，不会调到 scenario[MAX_ASK+1]
        calls = backend.calls_per_task()
        assert len(calls) == 1  # 一个 task_id
        only_task_id = list(calls.keys())[0]
        # spawn (1) + MAX_ASK 次 resume (3) = 4 次调用，scenario 第 5 个未读
        assert calls[only_task_id] == MAX_ASK + 1

    def test_drive_node_multiple_sentinel_resumes_then_real(self):
        """连续 2 次哨兵（前两次 provider 答 None）→ 第 3 次拿到真实 output。

        验证 driver 在**单节点内**经过多次 sentinel → resume 循环后仍能正确收尾。
        mock backend 不感知 answer 内容（scenario 按时序定义）；测试关注 driver 的
        控制流不变量（sentinel_triggered == 2 / resumed_count == 2 / 拿到真 output）。
        """
        backend = MockSubagentBackend(
            [
                build_sentinel_message("q1", ["o1"], "c1"),
                build_sentinel_message("q2", ["o2"], "c2"),
                json.dumps({"calib_loader": "o2", "source": "user"}),
            ]
        )
        gen = iter([None, None, "o2"])

        def provider(_q):
            return next(gen)

        output, log = drive_node(backend, "<prompt>", provider)
        assert log.sentinel_triggered == 2
        assert log.resumed_count == 2
        assert json.loads(output)["calib_loader"] == "o2"

    def test_drive_node_fabrication_in_real_output_detected(self):
        """SPEC §3：真实 output 含 torch.randn → FabricationDetected fail loud。"""
        backend = MockSubagentBackend(
            [
                build_sentinel_message("q?", ["o"], "c"),
                json.dumps({"calib_loader": "torch.randn(8,3)", "source": "user"}),
            ]
        )
        with pytest.raises(LoopFabricationDetected, match="造假痕迹"):
            drive_node(backend, "<prompt>", lambda _q: "o")

    def test_drive_workflow_two_node_closed_loop(self):
        """完整 2 节点闭环：A 哨兵→答→真 output → next → B 真接 → next → done。

        关键断言：
        - 哨兵被触发 1 次（只在 A）
        - A 的 resume task_id == A 的 spawn task_id（同一子 agent 恢复）
        - B 是新 spawn（新 task_id），没有 resume
        - 最终 done=True
        - 真实 output 喂给了 orca next（哨兵从未进 next）
        """
        backend = MockSubagentBackend(
            [
                # A.spawn = 哨兵
                build_sentinel_message("calib?", ["myproj.x:y"], "无果"),
                # A.resume = 真实 output
                json.dumps({"calib_loader": "myproj.x:y", "source": "user"}),
                # B.spawn = 真实 output（B 不缺数据）
                json.dumps({"summary": "拿到了"}),
            ]
        )
        fake_orca = _FakeOrcaCLI(
            bootstrap_prompt="<A prompt>",
            next_outputs=[
                _make_next_prompt("<B prompt>", node="data_consumer"),
                _make_next_done(),
            ],
        )
        result, log = drive_workflow(
            backend=backend,
            wf="spike_ask_user",
            inputs={},
            answer_provider=lambda q: q.options[0],
            orca_cli=fake_orca,
            stop_on_exit=False,
        )
        assert result.done is True
        assert len(log.nodes) == 2

        # 节点 A：哨兵触发 + resume 复用 task_id
        node_a = log.nodes[0]
        assert node_a.sentinel_triggered == 1
        assert node_a.resumed_count == 1
        node_a.assert_task_id_reused()

        # 节点 B：不哨兵、不 resume
        node_b = log.nodes[1]
        assert node_b.sentinel_triggered == 0
        assert node_b.resumed_count == 0

        # A 和 B 用的是不同 task_id（B 是新 spawn）
        assert log.nodes[0].final_task_id != log.nodes[1].final_task_id

        # 关键断言：哨兵 JSON 从未喂给 orca next（SPEC §0 核心不变量）。
        # **strict 调 is_sentinel**——不是 substring match（json.dumps 默认 separator
        # 是 `': '`（带空格），手写 needle 容易写错成 `':'`（无空格）导致断言 trivially 通过）。
        for _, output in fake_orca.next_calls:
            assert not is_sentinel(output), (
                f"哨兵泄漏进 orca next！output={output!r}"
            )
        # next 被调了 2 次（A 完成后 + B 完成后）
        assert len(fake_orca.next_calls) == 2

    def test_sentinel_leak_into_orca_next_would_be_caught(self):
        """反向断言：若 driver 真把哨兵喂给 next，上面的断言必须能 fail。

        防止「断言空操作」回归——之前用 substring 时 json.dumps 的 `': '` 与
        needle 的 `':'` 不匹配导致永远不触发（review 发现）。这里故意把哨兵塞进
        fake_orca.next_calls，验证断言**真的会 raise**。
        """
        # 构造一个「假装哨兵泄漏」的 orca_cli：在 next_step 里把哨兵追加到 calls
        backend = MockSubagentBackend(["whatever"])
        leaked_sentinel = build_sentinel_message("q?", ["o"], "c")

        class _LeakyFakeOrca(_FakeOrcaCLI):
            def next_step(self, run_id, output):  # type: ignore[override]
                # 故意把哨兵当 output 喂进去（模拟 driver bug）
                self.next_calls.append((run_id, leaked_sentinel))
                return _make_next_done()

        fake_orca = _LeakyFakeOrca(bootstrap_prompt="<A>", next_outputs=[])
        # drive_node 拿到 "whatever"（非哨兵，非造假）→ 喂 next → next 被 leaky 拦截
        result, log = drive_workflow(
            backend=backend,
            wf="spike_ask_user",
            inputs={},
            answer_provider=lambda _q: "x",
            orca_cli=fake_orca,
            stop_on_exit=False,
        )
        # 验证：哨兵确实进了 next_calls（leaky 注入成功）
        assert any(is_sentinel(out) for _, out in fake_orca.next_calls), (
            "leaky fake 应该把哨兵塞进 next_calls——前置条件"
        )
        # 如果我们用 strict 断言，**会**检测到哨兵泄漏（这是反向证明：断言非空操作）
        sentinel_leaked = [out for _, out in fake_orca.next_calls if is_sentinel(out)]
        assert sentinel_leaked, "断言失效：sentinel 泄漏未被检测到"

    def test_drive_workflow_sentinel_at_downstream_node(self):
        """节点 A 顺利，节点 B 触发哨兵 1 次 → 真实 output。

        SPEC §2「一个节点可连续返回多次哨兵」+ driver 对**每个**节点都跑哨兵循环，
        不只首节点。
        """
        backend = MockSubagentBackend(
            [
                # A.spawn → 真实 output（不哨兵）
                json.dumps(
                    {"calib_loader": "myproj.data:load_calib", "source": "inferred"}
                ),
                # B.spawn → 哨兵（B 也缺个东西）
                build_sentinel_message(
                    "summary 文案想要中文还是英文？",
                    ["中文", "English"],
                    "B 节点哨兵一次",
                ),
                # B.resume → 真实 output
                json.dumps({"summary": "已拿到 calib_loader，闭环成立。"}),
            ]
        )
        fake_orca = _FakeOrcaCLI(
            bootstrap_prompt="<A prompt>",
            next_outputs=[
                _make_next_prompt("<B prompt>", node="data_consumer"),
                _make_next_done(),
            ],
        )
        result, log = drive_workflow(
            backend=backend,
            wf="spike_ask_user",
            inputs={},
            answer_provider=lambda q: q.options[0],
            orca_cli=fake_orca,
            stop_on_exit=False,
        )
        assert result.done is True
        assert len(log.nodes) == 2

        # 节点 A：顺利，0 哨兵
        assert log.nodes[0].sentinel_triggered == 0
        # 节点 B：哨兵 1 次 + resume 1 次
        assert log.nodes[1].sentinel_triggered == 1
        assert log.nodes[1].resumed_count == 1
        log.nodes[1].assert_task_id_reused()
        # 哨兵不进 next
        for _, out in fake_orca.next_calls:
            assert not is_sentinel(out)

    def test_drive_workflow_orca_busy_raises_and_cleans_marker(self):
        """SPEC §2 edge case：``orca next`` 返 ``busy`` → OrcaBusyError + stop 清理。

        driver 不自动重试 busy（SPEC §2 把重试交给上层 / 主 session）。
        """
        backend = MockSubagentBackend(
            [json.dumps({"calib_loader": "ok", "source": "user"})]
        )
        busy_result = NextResult(
            done=False, prompt="", node="data_consumer",
            busy=True, retry_after_ms=500,
            raw={"reason": "busy", "retry_after_ms": 500},
        )
        fake_orca = _FakeOrcaCLI(
            bootstrap_prompt="<A>",
            next_outputs=[busy_result],  # 第一次 next 就 busy
        )
        with pytest.raises(OrcaBusyError, match="busy"):
            drive_workflow(
                backend=backend,
                wf="spike_ask_user",
                inputs={},
                answer_provider=lambda _q: "ok",
                orca_cli=fake_orca,
                stop_on_exit=True,
            )
        # busy 后 stop 被调（marker 清理）
        assert len(fake_orca.stop_calls) == 1

    def test_assert_task_id_reused_raises_on_mismatch(self):
        """``NodeDriveLog.assert_task_id_reused`` 在 task_id 不一致时 fail loud。

        直接构造一个 mismatch 的 log，验证断言 raise（这是 driver 正确性的关键保险）。
        """
        from tests.spike_ask_user.tars_loop import NodeAttemptLog

        log = NodeDriveLog(node_prompt_preview="<p>")
        log.attempts = [
            NodeAttemptLog(
                phase="spawn", task_id="task-A", call_index=0,
                is_sentinel=True, output_preview="...",
            ),
            NodeAttemptLog(
                phase="resume", task_id="task-B",  # 不同 task_id！
                call_index=1, is_sentinel=False, output_preview="...",
            ),
        ]
        with pytest.raises(AssertionError, match="task_id 未复用"):
            log.assert_task_id_reused()

    def test_assert_task_id_reused_silent_on_empty_attempts(self):
        """空 attempts → no-op（守护契约，不误报）。"""
        log = NodeDriveLog(node_prompt_preview="<p>")
        # 空 attempts 时不 raise
        log.assert_task_id_reused()
        backend = MockSubagentBackend(
            [build_sentinel_message("q", ["o"], "c")] * (MAX_ASK + 1)
        )
        fake_orca = _FakeOrcaCLI(
            bootstrap_prompt="<A prompt>",
            next_outputs=[],  # 节点 A fail，不会推进到 next
        )
        with pytest.raises(SentinelLoopExhausted):
            drive_workflow(
                backend=backend,
                wf="spike_ask_user",
                inputs={},
                answer_provider=lambda _q: None,  # 恒答「不知道」
                orca_cli=fake_orca,
                stop_on_exit=True,
            )
        # 清理：marker 被 stop 了
        assert fake_orca.stop_calls == [fake_orca.boot_run_id]
        # next 从未被调用（节点 A 未完成）
        assert fake_orca.next_calls == []


# ──────────────────────────────────────────────────────────────────────────────
# TestEndToEndWithRealOrca —— 真 orca CLI（mock backend，不真 spawn claude）
# ──────────────────────────────────────────────────────────────────────────────

# 这一组测试会真启动 orca run（落 tape / marker）。每个 test 结束自动 stop + 清文件。
# CI 默认跑（无 API 依赖）。如本机 orca CLI 异常可 ``-k "not RealOrca"`` 跳过。


def _spike_yaml_path() -> str:
    import pathlib
    return str(pathlib.Path(__file__).resolve().parent / "spike_ask_user.yaml")


@pytest.fixture
def orca_run_cleanup():
    """test 结束后扫描 runs/spike_ask_user-* 并清理（防 marker 残留）。

    Rule 12（fail loud）半步让：cleanup 失败不阻塞 test（marker 残留不是 test 失败），
    但**必须留诊断痕迹**（至少 logger.debug 记 run_id + 异常），不能完全静默吞。
    路径用 ``Path(__file__).resolve().parents[2]`` 推 Orca root，避免硬编码绝对路径。
    """
    import glob
    import logging
    import os
    import shutil
    import subprocess
    import pathlib

    orca_root = pathlib.Path(__file__).resolve().parents[2]
    runs_glob = str(orca_root / "runs" / "spike_ask_user-*")
    _log = logging.getLogger(__name__)

    yield
    # 清理：先 stop 活跃 marker（如有），再删 run 目录 + tape 文件
    for run_id_file in glob.glob(runs_glob + "/orca_env.sh"):
        run_dir = str(run_id_file).rsplit("/", 1)[0]
        run_id = run_dir.rsplit("/", 1)[-1]
        try:
            subprocess.run(
                ["orca", "stop", "--run-id", run_id],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as e:
            _log.debug("cleanup orca stop skip run_id=%s: %r", run_id, e)
    # 删 spike_ask_user-* 残留（只删目录；jsonl/jsonl.lock 是文件，单独 unlink）
    for path in glob.glob(runs_glob):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            _log.debug("cleanup unlink skip path=%s: %r", path, e)


@pytest.mark.usefixtures("orca_run_cleanup")
class TestEndToEndWithRealOrca:
    """真 ``orca bootstrap/next/stop`` 的端到端测试（mock backend）。"""

    def test_real_orca_two_node_closed_loop(self):
        """SPEC §5 主验收：真 orca run + mock backend 跑通 2 节点闭环。

        断言：
        - bootstrap 拿到 run_id + data_finder 节点
        - A 哨兵 → resume → 真实 output → orca next 推进到 B
        - B 拿到真实 output → orca next done:true
        - 全程无造假痕迹
        """
        from tests.spike_ask_user.tars_loop import RealOrcaCLI

        backend = MockSubagentBackend(
            [
                build_sentinel_message(
                    "calib loader dotted-path 是什么？",
                    ["myproj.data:load_calib"],
                    "project_root 为空",
                ),
                json.dumps(
                    {"calib_loader": "myproj.data:load_calib", "source": "user"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"summary": "已拿到 calib_loader，闭环成立。"},
                    ensure_ascii=False,
                ),
            ]
        )
        result, log = drive_workflow(
            backend=backend,
            wf=_spike_yaml_path(),
            inputs={},
            answer_provider=lambda q: q.options[0],
            orca_cli=RealOrcaCLI(),
            stop_on_exit=True,
        )
        assert result.done is True
        assert len(log.nodes) == 2

        # 节点 A 哨兵循环成立
        node_a = log.nodes[0]
        assert node_a.sentinel_triggered == 1
        assert node_a.resumed_count == 1
        node_a.assert_task_id_reused()
        # 真实 output 是合法 JSON
        real_a = json.loads(node_a.final_output)
        assert real_a["calib_loader"] == "myproj.data:load_calib"
        assert real_a["source"] == "user"

        # 节点 B 顺利
        node_b = log.nodes[1]
        assert node_b.sentinel_triggered == 0
        # 节点 A 和 B 用不同 task_id（B 是新 spawn）
        assert node_a.final_task_id != node_b.final_task_id

        # 全程无造假
        for node_log in log.nodes:
            assert not looks_fabricated(node_log.final_output)

    def test_real_orca_reentry_3x_fails_loud(self):
        """SPEC §4 重入：连续 3 次哨兵 → fail loud，不无限循环。"""
        from tests.spike_ask_user.tars_loop import RealOrcaCLI

        backend = MockSubagentBackend(
            [build_sentinel_message("q?", ["o"], "c")] * (MAX_ASK + 1)
        )
        with pytest.raises(SentinelLoopExhausted):
            drive_workflow(
                backend=backend,
                wf=_spike_yaml_path(),
                inputs={},
                answer_provider=lambda _q: None,  # 恒「不知道」
                orca_cli=RealOrcaCLI(),
                stop_on_exit=True,
            )
        # driver 在 MAX_ASK=3 后中断：backend 只被调了 spawn + 3 resume = 4 次
        assert backend.total_calls() == MAX_ASK + 1


# ──────────────────────────────────────────────────────────────────────────────
# Integration marker —— 真 spawn claude（CI skip，本地 -m integration 跑）
# ──────────────────────────────────────────────────────────────────────────────


def _claude_available() -> bool:
    import shutil
    return shutil.which("claude") is not None


@pytest.mark.integration
@pytest.mark.skipif(not _claude_available(), reason="claude CLI 不在 PATH")
@pytest.mark.usefixtures("orca_run_cleanup")
class TestRealClaudeBackendIntegration:
    """真 spawn ``claude -p`` 子 agent 的集成测试。

    **CI 默认 skip**（依赖 API key + 慢 + 非确定）。本地 ``pytest -m integration`` 可跑。

    这一组**不**断言 claude 输出内容（非确定），只断言：
    - 后端能 spawn / resume（``claude --session-id`` / ``--resume`` 子进程跑通）
    - driver 不崩（哨兵检测 / task_id 捕获逻辑 OK）
    """

    def test_claude_backend_spawn_smoke(self):
        """spawn 一个极简 prompt，断言 claude 真的回了对应内容。

        断言强度：prompt 要求 "Reply with exactly: OK"，断言 ``"OK" in output``
        —— 不留 ``or len > 0`` 这种近空操作 fallback（review 反馈）。
        """
        from tests.spike_ask_user.claude_backend import ClaudeCliBackend

        backend = ClaudeCliBackend(timeout_s=90, allowed_tools=("Read",))
        result = backend.spawn("Reply with exactly: OK")
        assert result.task_id
        assert "OK" in result.output

    def test_claude_backend_resume_smoke(self):
        """spawn + resume 续跑同一 session（验证 session 复用机制）。

        断言强度：spawn 里告诉它 secret word；resume 里问它 secret word——
        断言 response 含 ``banana``（上下文保持的证据）。若 claude 忘了，
        test fail loud 而非默默通过。
        """
        from tests.spike_ask_user.claude_backend import ClaudeCliBackend

        # resume 需要读 transcript + 处理新消息，给 150s（spawn 90 + resume 60 余量）
        backend = ClaudeCliBackend(timeout_s=150, allowed_tools=("Read",))
        first = backend.spawn("Remember the secret word: banana. Reply OK.")
        assert "OK" in first.output
        # resume：问它 secret word 是什么
        second = backend.resume(first.task_id, "What was the secret word I told you? Reply with only the word.")
        assert second.task_id == first.task_id  # session 复用
        assert second.call_index == 1
        # 上下文保持：claude 应记得 secret word
        assert "banana" in second.output.lower()
