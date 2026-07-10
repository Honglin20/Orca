# 08 — 组装：散 agent md → 线性

- **场景**：3（只有 agent 池，无编排）
- **输入**：用户给了 3 个 agent md（`a.md` / `b.md` / `c.md`，各自独立角色 prompt），说"按 a→b→c 顺序串起来"。
- **预期产物**：`expected/workflow.yaml` + `expected/agents/{a,b,c}.md`
- **不变量**：
  - skill 补编排：linear routes a→b→c→`$end`
  - 每节点 `agent: <name>` 引用（复用角色 → 单 MD，非内联）
  - agent md 原样落到 `agents/`（用户素材保留）
  - 用户只给顺序、没说数据传递 → 各 agent 独立，不强行加 `{{ a.output }}`
