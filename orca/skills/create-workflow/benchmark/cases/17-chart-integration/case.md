# 17 — NL 建带 web 图表的基准评测 workflow

- **场景**：NL 从零新建 + 运行时图表落地
- **输入**：NL（input.txt，含可视化意图词）
- **预期产物**：`expected/workflow.yaml` + `expected/agents/bench/agent.md` + `expected/agents/bench/scripts/bench_plot.py`
- **不变量**：
  - `setup` 是 **set-kind 节点名**（聚合评测配置向后传），不是顶层 `setup:` 段（schema 无该段，写了即拒）
  - workflow 必有 `outputs`；input `seed` 默认 0；每个 input `description` 以 `[ask]`/`[infer]`/`[default]`/`[advanced]` 标签起头
  - bench 是文件夹 agent：脚本在 `scripts/` 子目录 + body 用 `$ORCA_AGENT_RESOURCES` 引用
  - bench_plot.py：stdlib 内联 mock 评测（零外部依赖）→ 写 jsonl → 末尾 `render_chart` 推图
    （import 与每次调用均 try/except 包裹；label/title/chart_type 全字符串字面量；label+title 唯一）
