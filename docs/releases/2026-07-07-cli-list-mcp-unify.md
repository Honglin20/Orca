# Release Note —— CLI `list` 与 MCP `list_workflows` 统一

**日期**：2026-07-07
**背景**：CLI `list` 子命令与 MCP `list_workflows` 走两套不同逻辑（CLI 扫 `./examples` 按文件名；MCP 扫 `./workflows` + `~/.orca/workflows` 按 `wf.name`），看到的 workflow 列表对不上——违反接口统一性铁律。

## 改动点

### CLI `list` 委托 catalog（`orca/iface/cli/commands.py`）

`list_workflows` 命令重写：
- 删 `--dir` 选项 + 「扫 `./examples` 按文件名」旧逻辑（全量替换，不留兼容路径）。
- 改调 `catalog.list_workflows()`——与 MCP `list_workflows` **同一个函数**，single source of truth。
- 输出按 `wf.name` 列出，带 `⚙setup` 标记 + description。
- 空目录 → `（无可用 workflow；扫描了 ./workflows + ~/.orca/workflows）`。

### 一致性

| 维度 | 之前（CLI 旧） | 现在（统一） |
|---|---|---|
| 扫描目录 | 仅 `./examples` | `./workflows` + `~/.orca/workflows`（first-wins） |
| 匹配键 | 文件名 | `wf.name` 字段 |
| 数据源 | 自己 glob | `catalog.list_workflows()`（与 MCP 同源） |
| `--dir` | 有 | 删 |

CLI 与 MCP 现在看到完全一致的 workflow 列表。

## 测试

`tests/iface/cli/test_commands.py::TestListCommand` 重写（3 用例）：monkeypatch `catalog._workflow_dirs`，断言按 name 列出 + has_setup 标记 + 空提示。`tests/iface/` 全量 558 passed / 0 回归。

## 已知 follow-up

- 🟡 `iface/cli` lazy import `iface/mcp/catalog` 是跨子包耦合（架构味道，非硬违规——catalog 是纯函数，只依赖 compile+schema）。code-reviewer 建议择期把 `catalog.py` 迁到 `orca/compile/catalog.py`，三壳各自从 compile 包取用。本次保留 lazy import + 注释，已登记。
