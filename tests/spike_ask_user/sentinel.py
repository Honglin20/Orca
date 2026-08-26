"""sentinel.py —— ask-user 哨兵契约的纯 Python 实现（SPEC 逐字映射）。

**SPEC 来源**: ``docs/specs/agent-ask-user-sentinel.md`` §1 / §4。

子 agent 缺 Tier B 必填项、且读代码无果时，以其**最终消息**返回**严格**如下 JSON：

```
{"_orca_ask_user": "<一句话问题>",
 "options": ["<候选1>", "<候选2>"],
 "context": "<agent 已查过哪里、看到了什么、为什么歧义>",
 "_sentinel": "orca_ask_user_v1"}
```

本模块提供：

- ``AskUserQuestion``：哨兵负载的 dataclass。
- ``is_sentinel(text)``：**strict** 识别（不是 substring match）。先尝试把 ``text`` 当 JSON
  解析；解析成功且顶层 dict 含 ``_sentinel == "orca_ask_user_v1"`` 才返回 True。任何非法 JSON、
  缺字段、版本不符、含其他类型 → False（agent 合法输出碰巧含 ``_orca_ask_user`` 不会误判）。
- ``parse_sentinel(text)``：解析并校验 schema（字段齐全、类型正确），失败 fail loud。
- ``MAX_ASK = 3``：SPEC §4 重入上限；driver 据此中断。
- ``SentinelError`` / ``SentinelLoopExhausted``：失败路径分类，不静默吞错。

依赖单向：零外部依赖（仅 stdlib）。可被 driver、test、未来 TARS skill 复用。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final


# SPEC §1：版本化魔键。driver 用它做 strict 识别（不是 substring match）。
SENTINEL_KEY: Final[str] = "_sentinel"
SENTINEL_VALUE: Final[str] = "orca_ask_user_v1"

# SPEC §4：连续哨兵 ≥ MAX_ASK → driver fail loud（不无限循环）。
MAX_ASK: Final[int] = 3

# 哨兵负载的合法字段集合（parse 时拒 unknown key，schema 严格）。
_SENTINEL_FIELDS: Final[frozenset[str]] = frozenset(
    {"_orca_ask_user", "options", "context", "_sentinel"}
)


class SentinelError(Exception):
    """哨兵解析 / 校验失败的基类。fail loud 时由 driver 抛出。"""


class SentinelLoopExhausted(SentinelError):
    """SPEC §4：连续哨兵 ≥ ``MAX_ASK`` 次仍未拿到真实 output。"""


@dataclass(frozen=True)
class AskUserQuestion:
    """哨兵负载（SPEC §1）。

    - ``question``：一句话问题（``_orca_ask_user``）。
    - ``options``：候选答案列表（自由文本；opencode 无原生 AskUserQuestion，结构化靠 prompt）。
    - ``context``：agent 已查过哪里、看到了什么、为什么歧义。
    """

    question: str
    options: tuple[str, ...]
    context: str


def _extract_json_object(text: str) -> str | None:
    """从子 agent 最终消息里抽出最外层 JSON 对象字面量。

    子 agent 经常把哨兵 JSON 包在 ```json ... ``` 围栏或前后带解释文字里。我们关心的是
    **最外层**的 ``{ ... }`` 块——用括号配平扫描，避免正则误匹配嵌套字符串内的括号。

    找不到任何 ``{`` → 返回 None（``is_sentinel`` 据此返回 False）。
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # 括号不配平 → 不是合法 JSON 对象
    return None


def is_sentinel(text: str) -> bool:
    """Strict 哨兵识别（SPEC §1）。

    判定规则（必须全部满足）：
    1. 抽出最外层 JSON 对象字面量；
    2. ``json.loads`` 成功且为 ``dict``；
    3. ``dict[_sentinel] == "orca_ask_user_v1"``。

    任何一步失败 → False。**不是 substring match**——避免 agent 合法输出里碰巧含
    ``_orca_ask_user`` 字面量造成 false positive（SPEC §1 末段强调）。
    """
    if not isinstance(text, str) or not text:
        return False
    payload = _extract_json_object(text)
    if payload is None:
        return False
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(obj, dict):
        return False
    return obj.get(SENTINEL_KEY) == SENTINEL_VALUE


def parse_sentinel(text: str) -> AskUserQuestion:
    """解析哨兵负载并校验 schema（SPEC §1）。

    先 ``is_sentinel`` 闸门（不是哨兵 → ``SentinelError`` fail loud），再校验字段类型与
    必填性。**unknown key 拒收**（schema 严格，避免子 agent 漂移契约）。

    失败一律 ``SentinelError``（fail loud）；调用方不应吞错。
    """
    if not is_sentinel(text):
        raise SentinelError(
            f"非哨兵文本（缺 _sentinel={SENTINEL_VALUE!r} 魔键或 JSON 非法）；preview={text[:120]!r}"
        )

    payload = _extract_json_object(text)
    assert payload is not None  # is_sentinel 已通过，必然抽得出
    obj = json.loads(payload)

    # unknown key 拒收
    extra = set(obj.keys()) - _SENTINEL_FIELDS
    if extra:
        raise SentinelError(f"哨兵含未知字段 {sorted(extra)}；合法字段={sorted(_SENTINEL_FIELDS)}")

    question = obj.get("_orca_ask_user")
    options = obj.get("options")
    context = obj.get("context")

    if not isinstance(question, str) or not question.strip():
        raise SentinelError(f"_orca_ask_user 必为非空 str，got {type(question).__name__}")
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise SentinelError("options 必为 list[str]")
    if not isinstance(context, str):
        raise SentinelError(f"context 必为 str，got {type(context).__name__}")

    return AskUserQuestion(
        question=question,
        options=tuple(options),
        context=context,
    )


# 便捷构造（mock backend / 测试用），避免每次手写 JSON。
def build_sentinel_message(
    question: str, options: list[str], context: str
) -> str:
    """构造一个合规的哨兵最终消息（仅 mock / 测试用；真实子 agent 自行 JSON）。"""
    return json.dumps(
        {
            "_orca_ask_user": question,
            "options": list(options),
            "context": context,
            "_sentinel": SENTINEL_VALUE,
        },
        ensure_ascii=False,
    )


# 用于断言「最终 output 不是造假」的快速扫描（SPEC §3 严禁 torch.randn 等造假）。
_FABRICATION_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\btorch\.randn\b"),
    re.compile(r"\btorch\.rand\b"),
    re.compile(r"\bfake_data\b"),
    re.compile(r"\bdummy_calib\b"),
)


def looks_fabricated(text: str) -> bool:
    """启发式检测子 agent 是否在真实 output 里造假（SPEC §3）。

    返回 True 时，output 里出现了 ``torch.randn`` / ``torch.rand`` / ``fake_data`` /
    ``dummy_calib`` 等典型造假痕迹。哨兵路径里不应出现这些（哨兵是问用户而非造假），
    真实 output 里更不应出现（节点 A 是问用户拿真 dotted-path，不该自己造数据）。

    仅作断言用，非硬性 schema；driver 用它做最后一道 sanity check。
    """
    return any(p.search(text) for p in _FABRICATION_PATTERNS)
