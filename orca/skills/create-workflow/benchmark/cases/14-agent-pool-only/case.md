# 14 — 只造 agent 池（无 workflow）

- **场景**：E（只 author agent，暂不编排）
- **输入**："帮我建 3 个可复用 agent：coder（写代码）/ reviewer（审代码）/ summarizer（写总结）。先不编 workflow，以后引用。"
- **预期产物**：`expected/agents/{coder,reviewer,summarizer}.md`（**无 workflow.yaml**）
- **不变量**：
  - 3 个独立 agent md，各自 frontmatter（description/model/tools）+ body prompt
  - 不产出 workflow（用户明说"先不编"）
  - 🔴 benchmark 守门：此 case 无 `expected/workflow.yaml`，校验只查 agent md 存在性
