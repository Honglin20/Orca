"""validator.py —— Semantic Output Validator（phase 11 §9.6，LLM 二次校验 agent output）。

回答「agent output 形状对但语义错怎么办？」：``output_schema`` 只校 shape/type（结构化提取），
校不出「model_class 是合法标识符但其实是 ``123abc``」「weights_path 是字符串但不是绝对路径」
这类**语义**错。``validate_output`` spawn 第二个 claude -p，喂 agent output + 自然语言 criteria，
让它判断是否合格，返回 ``{passed, issues}``。

执行流程（SPEC §9.6.4）：
  1. 组 prompt：agent output（JSON dump）+ criteria + 「返回 {passed, issues} JSON」指令
  2. spawn 第二个 claude -p（**复用 SpawnConfig / CLIRunner / profile**，DRY —— 不重写 spawn）：
     - ``cli_path = profile.resolve_cli_path()``（review C5：兼容 ccr 中转，不硬编码 "claude"）
     - ``flags = profile.flags``（同主 agent 的 ``-p --output-format stream-json ...``）
     - ``--allowed-tools ""``（validator 无需工具，纯文本判断）
     - ``--model <m>`` 仅当 ``config.model`` 显式指定（省 token：用 haiku 校验 sonnet 产出）
  3. ``result_text`` 经 ``extract_and_validate`` 提 ``{passed: bool, issues: [str]}`` JSON
  4. **fail-safe（SPEC §9.6.6）**：validator LLM 自己崩 / 输出不可解析 → 记 warning + 当作 passed
     （不阻塞 workflow —— validator 是辅助校验，自身故障不应让 agent 合格的 output 失败）
  5. 返回 ``(passed, issues)`` —— **不 emit 任何事件**

事件归属裁定（Rule 7，铁律 2 化解）：
  SPEC §9.6.4 示例签名含 ``bus`` 参数且让 ``validate_output`` 自己 emit validator_started /
  validator_passed。但铁律 2（``tests/exec/test_contract.py::test_dependency_no_events_bus_no_tape``
  守护）：exec/ 不 import 事件总线 / 不持 bus —— executor 产 ``AsyncIterator[Event]``，
  写 tape / emit 归 orchestrator（run/ 层）。两条原则冲突。

  **裁定（Rule 7，选 B）**：``validate_output`` **不持 bus、不 emit**，只返回 ``(passed, issues)``
  （纯计算 + 一次子进程 spawn，无副作用）。三类 validator_* 事件（started / passed / failed）
  **全部由 orchestrator 的 ``_dispatch_with_validator`` loop emit** —— 单一 emitter，职责清晰，
  与 retry_*（也由 retry loop 在 run/ 层 emit）模式一致。这比「split emit」（validate_output 发
  started/passed、orchestrator 发 failed）更内聚：emit 逻辑集中在一处，validator 函数纯返回值。

  偏离 SPEC §9.6.4 签名（``bus`` 参数移除）+ §9.6.5 事件归属，记入 SPEC §11.6 + release note。

依赖单向（铁律 2 保留）：本模块依赖 ``orca.exec.{runner, error}`` +
``orca.exec.claude.result_extractor`` + ``orca.schema``（ValidatorConfig 类型）+
``orca.profiles.base``（CliProfile 类型）。**不**依赖 ``orca.events``（不 emit）、**不**依赖
``orca.run``（orchestrator 调本模块，反向不行）、**不**依赖 ``orca.iface``。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from orca.exec.claude.result_extractor import extract_and_validate
from orca.exec.env import build_env_overlay
from orca.exec.runner import CLIRunner, SpawnConfig

if TYPE_CHECKING:
    from orca.profiles.base import CliProfile
    from orca.schema import ValidatorConfig

logger = logging.getLogger(__name__)

# validator claude 返回的 JSON schema（SPEC §9.6.4：返回 {passed, issues}）。
# 用 jsonschema 校验形状，防 validator LLM 返半结构化乱七八糟的输出。
_VERDICT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "issues": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["passed", "issues"],
    # 允许 validator 返回额外字段（如 reasoning）—— 只校验我们关心的两个，宽容。
    "additionalProperties": True,
}

# validator prompt 模板（SPEC §9.6.4）。output JSON dump + criteria + 输出格式指令。
# 用 ``{output_json}`` / ``{criteria}`` 占位（非 .format 的 {}，避免 output 自身含 {} 冲突），
# 改用 str.replace 注入 —— output 可能含任意字符，format 会撞 KeyError / 格式错。
_PROMPT_TEMPLATE = """你是 output validator（语义校验员）。判断下面的 agent output 是否满足校验标准。

## Agent output（JSON）
```json
{output_json}
```

## 校验标准（自然语言）
{criteria}

## 你的任务
判断上述 agent output 是否**语义上**满足校验标准（不只看字段类型 / shape，看含义是否正确）。
例如「model_class 是合法 Python 标识符」「weights_path 是绝对路径」这类语义约束。

## 输出格式（必须严格遵守）
只返回一个 JSON 对象，不要加任何额外解释或前后缀文字：

{{
  "passed": <true 或 false>,
  "issues": ["<如果不合格，列出每个具体问题>"]
}}

passed=true 时 issues 可为空数组 []。passed=false 时 issues 必须列出每个具体问题（每条一句话）。
"""


async def validate_output(
    output: Any,
    config: ValidatorConfig,
    profile: CliProfile,
    *,
    model: str | None = None,
) -> tuple[bool, list[str]]:
    """spawn 第二个 claude -p 做 LLM 二次语义校验（SPEC §9.6.4，纯函数无 emit 副作用）。

    Args:
        output: agent 的产出（任意可 JSON 序列化对象；自由文本则原样喂入）。
        config: ValidatorConfig（含 criteria / max_retries / model）。``max_retries`` 不在本
            函数消费（orchestrator loop 用），仅 ``criteria`` 在此用。
        profile: CliProfile（**必须**经 ``profile.resolve_cli_path()`` 拿 cli_path，review C5：
            兼容 ccr 中转 ``ORCA_CLAUDE_CLI=ccr code``，不硬编码 "claude"）。
        model: 可选模型覆盖（默认走 ``config.model``；orchestrator 显式透传）。

    Returns:
        ``(passed, issues)``：passed=True 时 issues 恒为 []（SPEC §9.6.3 payload 契约）；
        passed=False 时 issues 是 validator claude 列出的具体问题（每条一句话）。

    **不 emit 任何事件**（Rule 7 裁定：事件由 orchestrator 的 validator loop 统一发，见模块
    docstring 的「事件归属裁定」）。调用方（orchestrator）负责 emit validator_started（调本
    函数前）+ validator_passed（passed=True 时调本函数后）+ validator_failed（passed=False 时）。

    **fail-safe（SPEC §9.6.6）**：validator claude 自身崩（spawn 失败 / 非零退出 / 输出不可解析）
    → 记 warning + 返回 ``(True, [])``。理由：validator 是辅助校验，它的故障不应让 agent
    合格的 output 失败阻塞 workflow。**唯二不 fail loud 的场景之一**（另一个是 json_decode
    非 JSON 心跳行），SPEC 明确允许。
    """
    # 1. 组 prompt（output JSON dump；用 replace 注入避免 .format 与 output 内 {} 冲突）
    try:
        output_json = json.dumps(output, ensure_ascii=False, indent=2)
    except (TypeError, ValueError) as e:
        # output 不可 JSON 序列化（罕见，agent output 通常可序列化）→ fail-safe：
        # 把 repr 喂给 validator（它仍能判断语义），不阻塞 workflow。
        logger.warning(
            "validator: agent output 不可 JSON 序列化（%s），改用 repr 喂入", e,
        )
        output_json = repr(output)
    # model 默认走 config.model（caller 也可显式覆盖；None 时 _build 用 config.model）。
    effective_model = model if model is not None else config.model
    prompt = _PROMPT_TEMPLATE.replace("{output_json}", output_json).replace(
        "{criteria}", config.criteria
    )

    # 2. 构造 SpawnConfig（复用 profile 的 cli_path / flags / env_overlay，DRY —— review C5）
    cfg = _build_validator_spawn_config(profile, prompt, effective_model)

    # 3. CLIRunner 跑子进程，捕获 result_text（流式丢弃 —— validator 不需 token 级显示）
    result_holder: dict[str, Any] = {
        "result_text": None, "is_error": False, "api_error_status": None,
    }

    def on_result(
        raw_result: str, usage: dict, cost: float, is_error: bool,
        api_error_status: int | None = None,
    ) -> None:
        result_holder["result_text"] = raw_result
        result_holder["is_error"] = is_error
        result_holder["api_error_status"] = api_error_status

    runner = CLIRunner(cfg, on_result=on_result)
    try:
        async for _line in runner.stream():
            pass  # 流式丢弃（validator 的事件不进 tape，只取最终 result_text）
    except Exception as e:
        # validator claude spawn 自身崩（如 binary 不存在 / timeout）→ fail-safe
        logger.warning(
            "validator claude spawn 异常（fail-safe → 当作 passed）：%s", e,
        )
        return True, []

    # 4. validator LLM 自身报错（result.is_error=true）或 exit 非零 → fail-safe
    if runner.exit_code != 0 or result_holder["is_error"]:
        logger.warning(
            "validator claude 自身失败（fail-safe → 当作 passed）：exit_code=%s, "
            "is_error=%s, api_error_status=%s, result=%s, stderr 末尾=%s",
            runner.exit_code,
            result_holder["is_error"],
            result_holder["api_error_status"],
            (result_holder["result_text"] or "")[:300],
            runner.stderr[-300:],
        )
        return True, []

    result_text = result_holder["result_text"]
    if result_text is None:
        # exit 0 但无 result 事件（claude 流异常）→ fail-safe
        logger.warning(
            "validator claude exit 0 但无 result 事件（fail-safe → 当作 passed）；stderr=%s",
            runner.stderr[-300:],
        )
        return True, []

    # 5. 提取 + 校验 verdict JSON（复用 extract_and_validate，DRY）
    try:
        verdict = extract_and_validate(result_text, _VERDICT_SCHEMA)
    except Exception as e:  # noqa: BLE001 — validator LLM 输出不可控，任何提取/校验失败都 fail-safe
        logger.warning(
            "validator verdict 不可解析（fail-safe → 当作 passed）：%s；result_text=%s",
            e,
            (result_text or "")[:200],
        )
        return True, []

    passed = bool(verdict["passed"])
    issues = list(verdict.get("issues", []))

    if passed:
        return True, []
    return False, issues


def _build_validator_spawn_config(
    profile: CliProfile, prompt: str, model: str | None,
) -> SpawnConfig:
    """构造 validator 的 SpawnConfig（SPEC §9.6.4 + review C5）。

    与 ``ClaudeExecutor._build_spawn_config`` 共享 profile 来源（cli_path / flags / env_overlay），
    但 validator 的 argv 更简：
      - ``--allowed-tools ""``：validator 无需任何工具（纯文本判断，不调 Bash/Read/MCP）。
      - ``--model <m>``：仅当 ``model`` 显式指定（省 token：用 haiku 校验 sonnet 产出）。

    DRY：cli_path / flags / env_overlay 全部从 profile 取（不硬编码 "claude"），保证 ccr 中转
    （``ORCA_CLAUDE_CLI=ccr code``）对 validator 同样生效 —— 主 agent 走 ccr，validator 也走 ccr。
    """
    extra_args: list[str] = ["--allowed-tools", ""]  # 无工具
    if model is not None:
        extra_args.extend(["--model", model])

    return SpawnConfig(
        cli_path=profile.resolve_cli_path(),  # review C5：env > default，不硬编码
        flags=profile.resolve_flags(),  # env > config > default（2026-07-07 executor CLI 扩展）
        extra_args=extra_args,
        mcp_flag_args=[],  # validator 不挂 MCP（无 ask_user 需求）
        prompt=prompt,
        prompt_channel=profile.resolve_prompt_channel(),  # env > config > default
        env_overlay=build_env_overlay(profile.env_overlay_prefixes),
        timeout=None,  # validator 是短 query，理论上很快；timeout 归 retry/interrupt 外层管
    )
