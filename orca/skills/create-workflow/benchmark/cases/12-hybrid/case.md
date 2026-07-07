# 12 — 混合：NL + 既有 agent md

- **场景**：B（描述意图 + 部分既有素材）
- **输入**：用户给了一个 `researcher.md`，说："用这个 researcher 跑主题调研，然后一个 writer 出报告——writer 你帮我写。"
- **预期产物**：`expected/workflow.yaml` + `expected/agents/researcher.md`
- **不变量**：
  - researcher → `agent:` 引用（既有素材保留）；writer → 内联 prompt（skill 起草）
  - 混用 agent-ref + inline 两态（同一 workflow 内）
  - writer 引用 `{{ researcher.output }}`
