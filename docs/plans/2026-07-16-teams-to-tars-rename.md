# Plan: teams 命令 → tars（后端/运维 CLI 改名）

> 用户（2026-07-16）：将 `teams` 命令改为 `tars`。`generate.yaml` 不挪（已确认，仅 pipeline 在 workflows/）。
> 状态：草稿 | 分支 `in-session-unified-backend`
> 背景：`teams` SPEC §3.2 本就是变量化命令名（`ORCA_BACKEND_CMD`，默认 teams）。改名 = 改入口 + 默认 + 同步引用 + 重装。

## 0. 目标

用户输入的后端/运维命令 `teams`（install/run/serve/ps/validate/mcp/executor/list/logs/wait/resume）→ `tars`。`tars install` / `tars run` 等。**`orca` in-session 命令（list/next/status/...）不动**（用户未要求）。skill 已是 tars（上一步）。

## 1. ref map（已扫）+ 改动

### 1.1 入口名（关键）—— `pyproject.toml`
- L46 `teams = "orca.iface.cli.commands:main"` → `tars = "orca.iface.cli.commands:main"`。
- 注释 L42-44「teams = 后端命令…env ORCA_BACKEND_CMD 变量化（默认 teams）」→ tars。
- **改完须重装**（`pip install -e .`）让 `tars` 上 PATH、`teams` 退场。

### 1.2 默认命令名 —— `orca/iface/cli/commands.py`
- L1602 `DEFAULT_BACKEND_CMD = "teams"` → `"tars"`。
- help/docstring（L337/344/380/389/1579/1581/1594-1607/1606）`teams` → `tars`（显示名一致，`tars --help` 别再说 teams）。
- L1612 `teams_app = app` → `tars_app = app`（**保 `teams_app` 作 deprecated 别名** 向后兼容，避免陡断——coder 定，倾向保别名）。

### 1.3 validator 保留字 —— `orca/compile/validator.py`
- L55/64 保留字名单含「teams 后端命令名 + ORCA_BACKEND_CMD 默认值（teams）」→ tars（wf name 禁取 teams → 改 tars）。核实保留字集合里的字面量 "teams" → "tars"。

### 1.4 测试（19 处 `teams` 活引用）
- 全量 grep `tests/` 的 `"teams"` / `teams ` → `tars`。重点：test_install_cmds / test_skill_cmds / test_commands 等里 invoke `teams` 或断言 help 含 teams 的 → tars。
- `teams_app` 引用 → `tars_app`（或经别名仍过，但改干净）。

### 1.5 docs / SPEC
- SPEC §3.1（`teams run/serve/...`）/ §3.2（默认 teams）→ tars。
- docs/plans + releases 里 `teams` = 历史记录，**不改**（历史）。

### 1.6 不动
- `orca` in-session CLI（iface/in_session/cli.py + 其 pyproject 入口）。
- skill（已是 tars，不提 teams）。
- `ORCA_BACKEND_CMD` env 名（机制名不变，只改默认值）。
- orca.ts / cc_nudge（引用 orca = in-session，不提 teams——核实）。

## 2. 架构审视

- 单接口：`tars` 是后端命令单一入口（rename，非新增第二入口）。`orca` 是 in-session 单一入口。两入口职责清（后端 vs in-session），不重叠。
- 变量机制保留：`ORCA_BACKEND_CMD` 仍可覆盖（默认 tars）。
- 改后清理：help/docstring/测试 teams 残留清零（活引用），历史 doc 保留。

## 3. 测试 / E2E（纯 CLI，禁 MCP）

- **单测**：全量 `pytest tests/`（重点 iface/cli + compile）0 回归；`teams` 活引用清零（grep）。
- **重装 + 命令验证**：`pip install -e .` → `which tars` + `tars --help`（显示 tars，列出 install/run/serve/…）+ `teams`（应 not found 或 deprecated）。`tars install --target cc` 真跑（部署 skill + workflow）。
- **test-agent 真机**：`tars install` / `tars --help` / `tars list` 真工作；`orca` 命令不受影响。

## 4. 风险 / scope

- **R1（重装）**：改 pyproject 入口须 `pip install -e .` 才生效；旧 `teams` 脚本残留至重装。coder 必须重装 + 验 `tars` 上 PATH。
- **R2（陡断）**：去 `teams` 会断既有 `teams xxx` 调用——保 `teams_app` 别名 + （可选）pyproject 同时留 `teams` deprecated 入口？用户要「改为 tars」（替换）——倾向纯替换 `tars`，保 `teams_app` 模块别名足够（代码层兼容），命令层 `teams` 退场。
- **R3（validator 保留字）**：wf name 禁取 teams→tars，别漏（否则 wf 能叫 teams 撞命令）。
- **scope**：rename teams→tars（入口+默认+保留字+help+测试+SPEC）+ 重装。不动 orca/skill/env 名/templates。

## 流程闭环
本计划 → **coder-agent**（rename 全 refs + 重装 + code-reviewer 查漏 teams 残留/陡断 + 单测 + commit + 状态文档）→ **test-agent** 真机（`tars` 命令工作 + orca 不受影响）。
