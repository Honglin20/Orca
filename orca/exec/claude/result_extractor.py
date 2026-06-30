"""result_extractor.py —— 通用 JSON 提取 + schema 校验（纯函数）。

回答「claude result 文本怎么变成结构化输出？」：SPEC §2.7 两层实现，**backend 无关**
（决策 3：通用能力留在 ``exec/claude/``，ClaudeExecutor 直接 import 用）。

两层（SPEC §2.7）：
  1. **JSON 提取**（``extract_json_text``）：从 ``result.result`` 文本提 JSON
     - 整个 text 是合法 JSON → 直接返回
     - ```json fence``` / ```fence``` → 取围栏内
     - 第一个平衡 ``{...}`` 或 ``[...]`` 块（处理嵌套 + 字符串内括号 + 转义）
     - 都失败 → raise
  2. **schema 校验**（``extract_and_validate``）：
     - ``schema is None`` → 返回原 text（自由文本，SPEC §2.7）
     - 非 None → ``extract_json_text`` → ``jsonschema.validate`` → 失败 raise ``ExecError(phase="schema")``

为什么 claude ``-p --output-format stream-json`` 不配合 JSON schema：claude 的结构化输出
靠 prompt 约束 + 自提自校，不走原生 schema（与 AgentHarness 实测一致）。

依赖单向：本模块依赖 ``orca.exec.error``（ExecError）+ 第三方 ``jsonschema``；
不依赖 schema/events/profiles/run/compile。纯函数，可独立单测（fixture 驱动，不 spawn）。
"""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema
from jsonschema import ValidationError as JsonSchemaValidationError

from orca.exec.error import ExecError

# ```json ... ``` / ``` ... ``` 围栏正则（DOTALL 让 . 跨行）。
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def extract_json_text(text: str) -> Any:
    """从 ``text`` 提 JSON，按优先级（SPEC §2.7）：

    1. 整个 text 是合法 JSON → 直接返回
    2. ```json fence``` / ```fence``` → 取第一个围栏内再 parse
    3. 第一个平衡 ``{...}`` 或 ``[...]`` 块（处理嵌套/字符串/转义）
    4. 都失败 → raise ``ValueError``（由 ``extract_and_validate`` 包成 ``ExecError``）

    返回解析后的 Python 对象（dict / list / 标量）。
    """
    # 1. 整段合法 JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. ```json fence``` / ```fence```
    m = _FENCE_RE.search(text)
    if m:
        fenced = m.group(1).strip()
        try:
            return json.loads(fenced)
        except json.JSONDecodeError:
            pass  # 围栏内不合法，继续尝试平衡块

    # 3. 第一个平衡 {...} 或 [...] 块
    extracted = _first_balanced_block(text)
    if extracted is not None:
        return json.loads(extracted)  # 此处不合法会抛 ValueError（无更多 fallback）

    raise ValueError(f"文本中未找到合法 JSON：{text[:200]!r}")


def _first_balanced_block(text: str) -> str | None:
    """返回 text 中第一个平衡的 ``{...}`` / ``[...]`` 块（含外层括号），无则 None。

    处理：嵌套括号 / 字符串内的括号 / 转义引号（``\\"``）。从第一个 ``{`` 或 ``[``` 开始，
    数深度，遇配对闭合且深度归零即返回。
    """
    start_idx = -1
    open_ch = ""
    close_ch = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start_idx = i
            open_ch = ch
            close_ch = "}" if ch == "{" else "]"
            break
    if start_idx < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start_idx : i + 1]
    return None  # 不平衡（无闭合）


def extract_and_validate(text: str, schema: dict | None) -> Any:
    """提取 + 校验（SPEC §2.7）。

    - ``schema is None`` → 返回原 text（自由文本场景，output_schema 未声明）
    - 非 None → ``extract_json_text`` 提 JSON → ``jsonschema.validate`` 校验 →
      提取失败 / 校验失败 raise ``ExecError(phase="schema")``（fail loud，SPEC §6）

    返回值：schema=None 时为原 str；非 None 时为解析后的 Python 对象（dict/list）。
    """
    if schema is None:
        return text  # 自由文本：output_schema 未声明，取整段 result

    try:
        extracted = extract_json_text(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise ExecError(
            phase="schema",
            message=f"result 文本无法提取为合法 JSON：{e}（text 前 200 字符：{text[:200]!r}）",
        ) from e

    try:
        jsonschema.validate(extracted, schema)
    except JsonSchemaValidationError as e:
        raise ExecError(
            phase="schema",
            message=f"result JSON 不符合 node.output_schema：{e.message}（path={list(e.absolute_path)}）",
        ) from e

    return extracted
