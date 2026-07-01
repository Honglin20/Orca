# Orca

vendor-neutral、event-sourced、可视化的 coding-agent 编排控制平面——把 claude / codex / opencode 编进一个 DAG workflow，事件流（tape）是唯一真相源，支持人机决策门（gate）、时间旅行回放、CLI 与 Web 双入口。

> 设计决策见 [docs/TASK.md](docs/TASK.md)；各阶段契约见 [docs/specs/](docs/specs/)。

## 安装

```bash
uv sync                                            # Python 依赖
cd orca/iface/web/frontend && npm install && npm run build && cd -   # 仅 Web UI 需要（一次性）
```

## CLI

```bash
uv run orca run examples/demo_linear.yaml          # 跑 workflow（Textual TUI：DAG / 日志 / 答 gate）
uv run orca validate examples/nas.yaml             # 只校验，不跑
uv run orca list                                   # 列出可用 workflow
```

`orca run <yaml> [task] [-i key=value]... [--max-iter N]` —— 位置参数 `task` 是 `-i task="..."` 的语法糖；`-i key=value` 带类型推断（`true`/`false`/`null`/`[1,2]`/数字/字符串）。退出码：completed→0 / failed→1 / 参数或校验错→2。

## Web UI

```bash
uv run orca serve                # → http://127.0.0.1:7428
uv run orca serve --port 8000    # 自定义端口
```

左侧 run 列表 → 点 **+New** 填 yaml 路径启动 → 实时 DAG / 日志；gate 弹窗富交互作答；run 完成后点 **⏮ Replay** 时间旅行回放。多 run 真并发，事件按需懒加载。首次用需先构建前端（见安装）；hook 桥（claude 工具权限拦截）复用 serve 端口。

## Demo workflows（`examples/`）

| 文件 | 演示 | 节点 | 需 claude？ |
|---|---|---|---|
| `demo_linear.yaml` | 纯线性 a→b→c | script ×3 | 否（零 token）|
| `demo_loop.yaml` | 回环循环 + max_iter 终止 | set + script | 否 |
| `demo_foreach.yaml` | 数组分批并行 | set + foreach | 否 |
| `demo_parallel.yaml` | parallel 组并行汇聚 | script ×3 | 否 |
| `demo_failure.yaml` | 非零退出被记录（不 fail loud）| script | 否 |
| `demo_max_iter.yaml` | 循环不终止 → workflow_failed | set | 否 |
| `demo_conditional.yaml` | 条件分支 | set + agent | 是 |
| `demo_mixed.yaml` | 综合（script + agent + set + 回环）| 混合 | 是 |
| `demo_task.yaml` | task 位置参数注入 | agent | 是 |
| `nas.yaml` / `batch_assess.yaml` / `parallel_research.yaml` | 真实 workflow | 混合 | 是 |

script / set 驱动的 demo 不需要 claude 或 API key，秒级跑完，适合先体验编排。

## 测试

```bash
uv run pytest -q                              # 单元 + script demo（不含真 claude / 浏览器）
uv run pytest -q -m integration               # 真 claude + 浏览器 E2E（慢，需 claude CLI）
cd orca/iface/web/frontend && npm test        # 前端 vitest
```
