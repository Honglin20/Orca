# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前状态：`orca executor` 特性完成，无进行中任务

**持久化后端二进制配置 + 健康检查**完成：`orca executor set/show/unset/list/test` 命令组 +
`~/.orca/config.json` + 启动期 env 注入。复用既有 `resolve_cli_path()`，**exec/profile/registry
零核心改动**（OCP）。1031 passed 0 回归；终审 0 🔴 1 🟡（已修）/ 2 🟢（跳过）。

- **release note**：[`docs/releases/2026-07-02-executor-config.md`](../releases/2026-07-02-executor-config.md)
- **CHANGELOG**：顶部 `orca executor` 索引
- **核心交付**：① `orca/iface/cli/config.py`（config 持久化 + env 注入，`setdefault` 保 env>config>default）；
  ② `orca/iface/cli/executor_cmds.py`（sub-Typer + 纯函数 `classify` + `test` 复用 CLIRunner）；
  ③ `commands.py` `main()` 注入 + `add_typer`；④ ccr profile translator 接上 `claude_translator`。
- **测试**：35 单测（FakeRunner）+ 9 e2e（假脚本走完整 spawn 链路，不 mock CLIRunner）+ 2 integration（真 claude）。

## 待办（等用户指示方向）

1. **`orca executor` 真实端到端 manual 验证（待 ccr/claude + key 环境）**：
   `orca executor set claude "ccr code"` → `test` ✓ → `run` 实际 spawn ccr code →
   `ORCA_CLAUDE_CLI=claude run` 临时回 claude（证 env>config 优先级）→ `unset` 回 default。
2. **前序 4-bug-fix 的 TUI 端到端验证**（仍待 manual）：`demo_mixed` failed 时 TUI 停留「按 q 退出」。
3. **下一阶段（未规划）**：Web phase（前端 InterruptModal/DialogModal/cancel 端点 + terminate widget）；
   异协议 backend（codex/opencode）= 新 profile + translator（`executor set/test` 自动支持）。

## 必读文件（下一任务开工前按需）

- [`docs/releases/2026-07-02-executor-config.md`](../releases/2026-07-02-executor-config.md)（本次特性全貌 + 设计逻辑）
- [`docs/releases/2026-07-02-agent-observability-tui-fixes.md`](../releases/2026-07-02-agent-observability-tui-fixes.md)（前序 4 bug）
