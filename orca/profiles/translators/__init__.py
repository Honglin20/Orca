"""profiles/translators/ —— per-backend 协议适配器（纯函数）。

translator 是「这个 backend 的 stream-json 行 → Orca Event」的协议知识。它**归属 profiles 层**
（与对应 backend 的 profile 同居），不归 exec（决策 1，见 docs/releases/2026-06-30-phase4-exec.md）。

为什么放 profiles：
  - 内聚：每个 backend 的协议知识与其 profile 描述同处一文件树。
  - OCP：加 backend = 丢一个 translator 文件 + 一个 profile 文件，零 exec/factory 改动。
  - 依赖铁律：profiles 不能 import exec；translator 只依赖 schema（Event），profiles 持有它零新增依赖。

铁律（SPEC §7.0 第 3 条）：translator 是**纯函数** ``(line, session_id) -> list[Event]``，
无 self / 无 I/O / 无全局状态 / 无副作用。fixture 驱动测试，不 spawn claude。
"""

from orca.profiles.translators.claude import claude_translator

__all__ = ["claude_translator"]
