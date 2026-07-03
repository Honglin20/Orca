# examples/

Orca workflow 示例集。按节点类型分两类（agent example 用 opencode + deepseek-v4-flash 真跑，不 mock）。

## 纯 script demo（零 token，不烧 API，确定性）

控制流 demo，用 script/set/wait 节点驱动，不 spawn agent。秒级跑完。

| 示例 | 演示 |
|---|---|
| `demo_linear.yaml` | 线性推进 a→b→c→$end（最简控制流） |
| `demo_loop.yaml` | 循环路由（node 路由回自身，set 累积状态） |
| `demo_parallel.yaml` | parallel 组静态并行（多分支 asyncio.gather） |
| `demo_foreach.yaml` | foreach 动态并行（运行时数组 → 多分支并发） |
| `demo_failure.yaml` | script 节点失败冒泡到 workflow_failed（fail loud） |
| `demo_max_iter.yaml` | max_iterations 上限保护（超迭代 → workflow_failed） |
| `terminate.yaml` | terminate 节点（显式业务终止，status=success/failed） |
| `with_wait.yaml` | wait 节点（可中断的 sleep，rate-limit 退避/节奏控制） |

## agent workflow（opencode + deepseek-v4-flash 真跑，烧 API）

真 spawn agent（`executor: opencode` + `model: "deepseek/deepseek-v4-flash"`，固化在每个 agent node）。需 opencode 二进制 + deepseek auth。

| 示例 | 演示 |
|---|---|
| `demo_conditional.yaml` | 条件分支（set 决策值驱动路由） |
| `demo_interrupt.yaml` | 运行中中断（Ctrl+G → continue/skip/abort） |
| `demo_mixed.yaml` | 混合编排（script 准备 → agent 分析 → set 累积） |
| `demo_skip.yaml` | 中断后 skip 节点（Ctrl+G → skip 跳过当前 node 继续下游） |
| `demo_task.yaml` | task 位置参数（`orca run <yaml> <task>` → inputs.task） |
| `batch_assess.yaml` | foreach 批量评估（多 agent 并发评估候选并聚合） |
| `parallel_research.yaml` | DAG 分叉+合并（diamond）并行范式；agent 引用 `agents/` 池（phase-14 显式 `agent:` 引用） |
| `nas.yaml` | 迭代式神经结构搜索（agent + script + set 混合） |
| `mxint_analysis.yaml` | mxint 项目分析 workflow（多 agent 流水线，引用 `agents/` 池） |
| `render_chart.yaml` | render_chart 推图（**文件夹化 agent** plotter + scripts 资源；agent→script→`orca.chart.render_chart`→tape custom(chart)→TUI 图表 tab） |
| `with_retry.yaml` | Retry Policy（节点级自动重试 transient 失败） |
| `with_validator.yaml` | Semantic Output Validator（LLM 二次语义校验） |
| `with_dialog.yaml` | Dialog（agent 跑完后多轮追问） |

## claude-only 例外

| 示例 | 说明 |
|---|---|
| `with_ask_user.yaml` | 演示 `ask_user` MCP 工具，需 `mcp_tools=True`（claude）。**opencode 不支持 ask_user**（`mcp_tools=False`），故此示例保留 claude 后端，测试 skip（无 ANTHROPIC_API_KEY）。 |

## agent 池（`agents/`）

agent md 文件，被 workflow 用 `agent: <name>` 引用（phase-14 agent 一等化）。resolver 查 `<workflow_dir>/agents/` → `<cwd>/agents/`。

- `researcher_a.md` / `researcher_b.md` —— parallel_research 用
- `analyzer.md` / `configurator.md` / `optimizer.md` / `report_painter.md` / `diagnostic_saver.md` / `runner.md` —— nas / mxint_analysis 用
- `plotter/` —— **文件夹化 agent**（`agent.md` + `scripts/chart_demo.py` 资源，render_chart 用；spawn 时 env 注入 `ORCA_AGENT_RESOURCES` 让 agent 的 Bash 工具访问自带脚本）

## 运行

```bash
# 项目根目录下
orca validate examples/<name>.yaml   # 校验（不跑）
orca run examples/<name>.yaml        # 跑（agent 类起 TUI；script 类秒级完成）
orca list                            # 列出 examples/ 下所有 workflow
```

agent 类需 opencode + deepseek auth（`~/.local/share/opencode/auth.json` 含 deepseek provider）。
