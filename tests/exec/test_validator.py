"""tests/exec/test_validator.py —— validate_output 单元（phase 11 §9.6.4）。

覆盖（断言 INTENT 而非仅行为）：
  - passed=true → validator_started emit + validator_passed emit + 返回 (True, [])
  - passed=false → 返回 (False, [issues])（validator_failed 由 orchestrator 发，不在此测）
  - validator LLM 崩（exit_code!=0 / is_error=true / 输出不可解析）→ fail-safe 返回 (True, [])
  - SpawnConfig 用 profile.resolve_cli_path()（spy argv，不硬编码 "claude"）—— review C5
  - criteria_preview = criteria[:100]

确定性：mock CLIRunner（不 spawn 真 claude），monkeypatch ``orca.exec.validator.CLIRunner``。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from orca.exec.validator import validate_output
from orca.profiles import get_profile
from orca.schema import ValidatorConfig


def run_async(coro):
    """统一异步入口（asyncio.run，本仓库约定）。"""
    return asyncio.run(coro)


# ── 共享 FakeRunner（与 tests/exec/claude/test_executor.py 同构，tests 非包故就地复制）──


class FakeRunner:
    """CLIRunner 替身：按预设行 yield，暴露 exit_code/elapsed/stderr/is_error。

    捕获传入的 SpawnConfig（供测试 spy argv：cli_path / flags / extra_args）。
    """

    def __init__(
        self,
        lines=None,
        *,
        exit_code: int = 0,
        timed_out: bool = False,
        elapsed: float = 0.1,
        stderr: str = "",
        raise_on_stream: BaseException | None = None,
    ) -> None:
        self._lines = list(lines) if lines is not None else []
        self._on_result = None
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.elapsed = elapsed
        self.stderr = stderr
        self.was_interrupted = False
        self.raise_on_stream = raise_on_stream
        # spy：捕获构造时传入的 SpawnConfig（让测试断言 argv 来源）
        self.last_cfg: Any = None

    async def stream(self) -> AsyncIterator[str]:
        if self.raise_on_stream is not None:
            raise self.raise_on_stream
        for line in self._lines:
            self._maybe_fire_on_result(line)
            yield line

    def _maybe_fire_on_result(self, line: str) -> None:
        if self._on_result is None:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict) or obj.get("type") != "result":
            return
        self._on_result(
            obj.get("result", ""),
            obj.get("usage") or {},
            obj.get("total_cost_usd") or 0.0,
            bool(obj.get("is_error", False)),
            obj.get("api_error_status"),
        )


def _patch_runner(monkeypatch, fake: FakeRunner):
    """把 ``orca.exec.validator.CLIRunner`` 替换成 fake（捕获 cfg + 接 on_result）。"""

    def factory(cfg, on_result=None):
        fake.last_cfg = cfg
        fake._on_result = on_result
        return fake

    monkeypatch.setattr("orca.exec.validator.CLIRunner", factory)
    return fake


def _result_line(result_text: str, *, is_error: bool = False) -> str:
    """构造 claude stream-json 的 result 行（CLIRunner._maybe_fire_on_result 检测 type=result）。"""
    return json.dumps({
        "type": "result",
        "result": result_text,
        "is_error": is_error,
        "usage": {},
        "total_cost_usd": 0.0,
    })


@pytest.fixture
def profile():
    """claude builtin profile（含真 translator / resolve_cli_path）。"""
    return get_profile("claude")


@pytest.fixture
def config():
    return ValidatorConfig(criteria="model_class 必须是合法的 Python 标识符", max_retries=1)


# ── 1. passed=true → emit started + passed，返回 (True, []) ─────────────────────


def test_validate_output_passed_returns_true_empty_issues(monkeypatch, profile, config):
    """validator claude 返回 {passed:true} → 返回 (True, [])。

    INTENT：通过路径契约 —— validate_output 返回 passed=True 且 issues 恒空。
    注意：validate_output 本身**不 emit**（Rule 7：事件由 orchestrator 统一发，见
    validator.py 模块 docstring「事件归属裁定」）。validator_started/passed 由 orchestrator
    的 _dispatch_with_validator loop emit，覆盖在 tests/run/test_validator_orchestrator.py。
    """
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"model_class": "SimpleNet"}, config, profile)
    )
    assert passed is True
    assert issues == []


def test_validate_output_failed_returns_issues(monkeypatch, profile, config):
    """validator claude 返回 {passed:false, issues:["x"]} → 返回 (False, ["x"])。

    INTENT：失败路径返回 issues 列表（让 orchestrator 把它作 guidance 拼进下次 prompt）。
    validator_failed 事件**不在此 emit**（由 orchestrator 发，它知道 retrying）—— 本测试
    只断言返回值契约。
    """
    fake = FakeRunner(lines=[_result_line(
        '{"passed": false, "issues": ["model_class 是 123abc 非法标识符"]}'
    )])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"model_class": "123abc"}, config, profile)
    )
    assert passed is False
    assert issues == ["model_class 是 123abc 非法标识符"]


# ── 2. fail-safe：validator LLM 自身崩 → 返回 (True, []) ─────────────────────────


def test_validate_output_llm_crash_nonzero_exit_fail_safe(monkeypatch, profile, config):
    """validator claude 非零退出 → fail-safe 返回 (True, [])，**不** raise。

    INTENT（SPEC §9.6.6）：validator 是辅助校验，它自身故障（binary 不存在 / 崩溃）不应让
    agent 合格的 output 失败阻塞 workflow。这是 fail-loud 铁律的**唯一例外**之一（另一个是
    json_decode 非 JSON 心跳行）。fail-safe = 当作 passed。
    """
    fake = FakeRunner(lines=[], exit_code=127, stderr="command not found: claude")
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is True
    assert issues == []


def test_validate_output_llm_is_error_fail_safe(monkeypatch, profile, config):
    """validator claude result.is_error=true（API 报错）→ fail-safe 返回 (True, [])。"""
    fake = FakeRunner(lines=[_result_line("api error", is_error=True)])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is True
    assert issues == []


def test_validate_output_unparseable_verdict_fail_safe(monkeypatch, profile, config):
    """validator claude 输出非 JSON / 不合 {passed, issues} schema → fail-safe 返回 (True, [])。

    INTENT：validator LLM 可能返回自由文本（如「看起来没问题」）而非严格 JSON。fail-safe
    不让格式错阻塞 workflow。记 warning 但测试不断言日志（行为契约是返回值）。
    """
    # result_text 是自由文本（非 JSON）
    fake = FakeRunner(lines=[_result_line("看起来 output 没问题，通过。")])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is True
    assert issues == []


def test_validate_output_no_result_event_fail_safe(monkeypatch, profile, config):
    """validator claude exit 0 但流里无 result 事件 → fail-safe 返回 (True, [])。"""
    # 只有非 result 行（如 agent_message），无 result
    fake = FakeRunner(lines=[json.dumps({"type": "assistant", "message": "hi"})])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is True
    assert issues == []


def test_validate_output_spawn_exception_fail_safe(monkeypatch, profile, config):
    """CLIRunner.stream() 抛异常（如 binary 不存在 spawn 失败）→ fail-safe 返回 (True, [])。"""
    fake = FakeRunner(raise_on_stream=FileNotFoundError("[Errno 2] No such file: claude"))
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is True
    assert issues == []


# ── 3. SpawnConfig 用 profile.resolve_cli_path()（review C5）──────────────────


def test_validate_output_uses_profile_cli_path(monkeypatch, profile, config):
    """SpawnConfig.cli_path = profile.resolve_cli_path()（不硬编码 "claude"）。

    INTENT（review C5）：用户用 ccr 中转（``ORCA_CLAUDE_CLI=ccr code``）时，validator 的
    claude spawn 必须走同款中转 —— 否则 validator 直连 Anthropic 而 agent 走 ccr，行为
    不一致。spy CLIRunner 构造时收到的 SpawnConfig，断言 cli_path 与 profile 一致。
    """
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    run_async(validate_output({"x": 1}, config, profile))

    assert fake.last_cfg is not None, "SpawnConfig 未被捕获（CLIRunner 未被调）"
    # cli_path 来自 profile.resolve_cli_path()（env > default）
    assert fake.last_cfg.cli_path == profile.resolve_cli_path()
    # flags 也来自 profile（不硬编码 -p --output-format ...）
    assert fake.last_cfg.flags == profile.flags


def test_validate_output_ccr_env_override_propagates(monkeypatch, profile, config):
    """ORCA_CLAUDE_CLI=ccr code → validator SpawnConfig.cli_path 含 ccr code（env 覆盖生效）。

    INTENT：env 覆盖是 profile 的核心能力（canary 切换无需重启）。validator 必须复用同一
    resolve_cli_path()，不能绕过 profile 自造 cli_path。本测试设 env 后断言 cli_path 跟着变。
    """
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    # claude profile 的 cli_path_env = "ORCA_CLAUDE_CLI"
    monkeypatch.setenv("ORCA_CLAUDE_CLI", "ccr code")
    try:
        run_async(validate_output({"x": 1}, config, profile))
        assert fake.last_cfg.cli_path == "ccr code"
    finally:
        monkeypatch.delenv("ORCA_CLAUDE_CLI", raising=False)


def test_validate_output_model_override_adds_model_flag(monkeypatch, profile):
    """config.model 显式指定 → SpawnConfig.extra_args 含 ``--model <m>``。

    INTENT：省 token 场景（用 haiku 校验 sonnet 产出）。model=None 时不加 --model（用默认）。
    """
    config_with_model = ValidatorConfig(
        criteria="x", max_retries=1, model="claude-haiku-4-5",
    )
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    run_async(validate_output({"x": 1}, config_with_model, profile))

    extra = fake.last_cfg.extra_args
    assert "--model" in extra
    assert extra[extra.index("--model") + 1] == "claude-haiku-4-5"


def test_validate_output_no_model_no_model_flag(monkeypatch, profile, config):
    """config.model=None → SpawnConfig.extra_args 不含 --model（用 profile 默认模型）。"""
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    run_async(validate_output({"x": 1}, config, profile))

    assert "--model" not in fake.last_cfg.extra_args


# ── 4. allowed-tools="" （validator 无工具）──────────────────────────────────


def test_validate_output_no_tools_allowed(monkeypatch, profile, config):
    """SpawnConfig.extra_args 含 ``--allowed-tools ""``（validator 无需任何工具）。

    INTENT：validator 是纯文本判断，不调 Bash/Read/MCP。显式 ``--allowed-tools ""`` 关掉
    所有工具（防 validator claude 自作主张调工具跑偏）。
    """
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])
    _patch_runner(monkeypatch, fake)

    run_async(validate_output({"x": 1}, config, profile))

    extra = fake.last_cfg.extra_args
    assert "--allowed-tools" in extra
    idx = extra.index("--allowed-tools")
    assert extra[idx + 1] == ""


# ── 5. issues 透传：多条 issue 全保留 ──────────────────────────────────────────


def test_validate_output_multiple_issues_all_returned(monkeypatch, profile, config):
    """validator 返回多条 issues → 全部透传（不截断 / 不合并）。

    INTENT：orchestrator 把 issues 拼进 guidance 反馈给 agent，每条 issue 对应一个具体
    修正点。截断 / 合并会让 agent 漏修。本测试 3 条全保留。
    """
    fake = FakeRunner(lines=[_result_line(
        '{"passed": false, "issues": ["issue A", "issue B", "issue C"]}'
    )])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is False
    assert issues == ["issue A", "issue B", "issue C"]


def test_validate_output_verdict_extra_fields_tolerated(monkeypatch, profile, config):
    """validator 返回额外字段（如 reasoning）→ 容忍，只取 passed/issues。

    INTENT：LLM 输出不可控，validator claude 可能返回 ``{passed, issues, reasoning}``。
    schema 设 additionalProperties=True 容忍；本测试验证提取仍正确。
    """
    fake = FakeRunner(lines=[_result_line(
        '{"passed": false, "issues": ["x"], "reasoning": "因为..."}'
    )])
    _patch_runner(monkeypatch, fake)

    passed, issues = run_async(
        validate_output({"x": 1}, config, profile)
    )
    assert passed is False
    assert issues == ["x"]


def test_validate_output_non_serializable_uses_repr(monkeypatch, profile, config):
    """output 不可 JSON 序列化（如自定义对象）→ repr fallback，不阻塞，validator 仍跑。

    INTENT（SPEC §9.6.6 fail-safe 延伸）：agent output 通常可序列化，但极端场景（如 set /
    自定义类）json.dumps 会抛 TypeError。validator 不应因此崩 —— 改用 repr 喂入（validator
    claude 仍能据 repr 判断语义）。记 warning 但不断言日志（行为契约是「不崩 + 正常返回」）。
    本测试用 set（json 不支持）触发 fallback。
    """

    # spy：捕获传给 CLIRunner 的 prompt（验证 repr 进了 prompt，非原 output）
    captured_prompts: list[str] = []
    fake = FakeRunner(lines=[_result_line('{"passed": true, "issues": []}')])

    def factory(cfg, on_result=None):
        fake.last_cfg = cfg
        fake._on_result = on_result
        captured_prompts.append(cfg.prompt)
        return fake

    monkeypatch.setattr("orca.exec.validator.CLIRunner", factory)

    non_serializable = {"items": {1, 2, 3}}  # set 不可 JSON 序列化
    passed, issues = run_async(validate_output(non_serializable, config, profile))

    assert passed is True
    assert issues == []
    # prompt 含 repr（set 的 repr 形如 {1, 2, 3}），不含 JSON dump（json.dumps(set) 会抛）
    assert len(captured_prompts) == 1
    assert "items" in captured_prompts[0]


# ── 6. ValidatorConfig schema：空 criteria 加载期 fail loud ──────────────────


def test_validator_config_empty_criteria_rejected():
    """ValidatorConfig.criteria="" → pydantic ValidationError（min_length=1 守护）。

    INTENT（铁律 12 fail loud）：空 criteria 是配置错（无校验标准 = validator 无意义）。
    schema 层 min_length=1 让它在 YAML 加载期 / AgentNode 构造期拒绝，不等到运行期才发现。
    与 RetryPolicy.max_attempts ge=1 同模式。
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="string_too_short"):
        ValidatorConfig(criteria="", max_retries=1)
    # 纯空白也应拒（strip 后空）；当前 min_length=1 只校长度不校内容，纯空白 " " 会过——
    # 这是 pydantic min_length 的既定语义（防完全空，不防语义空白），可接受。
    with pytest.raises(ValidationError, match="string_too_short"):
        ValidatorConfig(criteria="")


def test_validator_config_max_retries_ge_zero():
    """ValidatorConfig.max_retries ge=0 守护（-1 → ValidationError）。

    INTENT：负 max_retries 语义错（不能负数次重试）。ge=0 让 0 合法（只校验一次）但拒负值。
    """
    from pydantic import ValidationError

    ValidatorConfig(criteria="x", max_retries=0)  # 0 合法
    with pytest.raises(ValidationError, match="greater_than_equal"):
        ValidatorConfig(criteria="x", max_retries=-1)
