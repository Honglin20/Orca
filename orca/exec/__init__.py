"""orca.exec —— 单 node 执行内核（后端无关契约 + 各 kind 实现）。

只回答「给定一个 node + context，怎么把它跑出来、吐出事件流？」（SPEC phase-4 §0）。

双层抽象（SPEC §1）：
  - ``Executor`` 接口（Layer 1，后端无关）：``async exec(node, ctx) -> AsyncIterator[Event]``
  - 每个实现（Layer 2）= 共享基础设施 + 协议特化：
      ``ClaudeExecutor`` = ``CLIRunner``（通用子进程）+ claude ``translator``（profiles 层）
      ``ScriptExecutor`` = subprocess（无 translator，shell 即协议）
      ``SetExecutor``    = Jinja2 求值（无子进程）

铁律（SPEC §7.0）：
  1. **依赖单向**：exec → schema + events(只类型) + profiles；exec **不依赖 run/compile**；
     profiles **不依赖 exec**。
  2. **executor 不写 tape**：产出 ``AsyncIterator[Event]``，写 tape + bus.emit 归 phase 5
     orchestrator。本包只 ``from orca.schema import Event``（类型），不 import events.bus/tape。
  3. **Translator 纯函数**：``(line, session_id) -> list[Event]``，fixture 驱动测试，不 spawn。
  4. **fail loud**：6 类错误（timeout/spawn/stream/result_parse/schema/render）全 emit
     ``node_failed`` + ``error``；json_decode 例外（debug log + 跳过，claude 偶发非 JSON 行）。
  5. **session_id 一致性**：单次 ``exec()`` 入口生成一个 ``uuid4().hex``，全程复用。
"""

from orca.exec.context import RunContext
from orca.exec.error import ExecError, phase_to_error_type
from orca.exec.error_kinds import ErrorKind
from orca.exec.factory import make_executor
from orca.exec.interface import Executor
from orca.exec.result import Error, Result

# ClaudeExecutor / ScriptExecutor / SetExecutor 经 factory 惰性导入（避免 import 时
# 拉起 claude 子包的 jinja2/jsonschema 依赖链；也切断 __init__ 对 claude 的硬依赖，
# 便于将来按需扩展）。SPEC §7.1 要求 ``from orca.exec import ClaudeExecutor`` 可用 →
# 用 __getattr__ 惰性解析。


def __getattr__(name: str):
    if name == "ClaudeExecutor":
        from orca.exec.claude.executor import ClaudeExecutor as _CE

        return _CE
    if name == "ScriptExecutor":
        from orca.exec.script import ScriptExecutor as _SE

        return _SE
    if name == "SetExecutor":
        from orca.exec.set_node import SetExecutor as _SetE

        return _SetE
    raise AttributeError(f"module 'orca.exec' has no attribute {name!r}")


__all__ = [
    "Executor",
    "make_executor",
    "RunContext",
    "ExecError",
    "phase_to_error_type",
    "ErrorKind",
    "Error",
    "Result",
    "ClaudeExecutor",
    "ScriptExecutor",
    "SetExecutor",
]
