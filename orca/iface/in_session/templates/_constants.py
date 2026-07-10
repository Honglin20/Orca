"""templates/_constants.py —— 与 plugin TS 模板共享的契约常量（SPEC §2.6.1）。

单一真相源：``MARKER_REGEX`` 同时被本模块（Python 测试用）与 opencode plugin TS 模板
（运行时用）引用。改 regex 时**两处必须同步**——本模块的测试 ``test_marker_regex`` 守
契约；plugin TS 文件经 grep 测试 ``test_plugin_embeds_canonical_marker_regex`` 守同步。
"""

from __future__ import annotations

# SPEC §2.6.1 marker 规范 —— 行首/行尾锚定 + 子命令名 \w+ + args 非贪婪 [^>\n]*?
# （args 禁 `>` 与换行，marker 单行）。opencode plugin TS 模板必须嵌入**同一字符串**。
MARKER_REGEX = r"^<!--\s*orca:cmd\s+(\w+)(?:\s+([^>\n]*?))?\s*-->$"

# 改写后 user 消息文本**不得**含本字面（一次性消费保证，SPEC §2.6.1）。
MARKER_LITERAL = "<!--orca:cmd"
