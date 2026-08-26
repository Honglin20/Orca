"""orca.events.adapters —— 子 agent 过程读 adapter（SPEC-B v4 §3.3/§4/§5）。

每个 adapter 实现 ``ReadAdapter`` 协议，产 ``RawAgentEvent``（统一 IR）。Backend 差异封装在
各自的实现里；ingestor / daemon 主体 / 前端零 backend 感知（SPEC §0 接口同一性 + grep 守门）。

  - ``cc_jsonl``        —— Claude Code sidechain jsonl（``~/.claude/projects/.../subagents/``）。
  - ``opencode_sqlite`` —— opencode sqlite ``event`` 表 seq 游标。

**grep 守门（SPEC §9 AC5）**：本目录内禁任何 backend 名条件分支或字面赋值；backend 选择
只在 ``sidechain_daemon.py`` 启动参数（``--backend cc|opencode``），其它文件零 backend 感知。
"""
