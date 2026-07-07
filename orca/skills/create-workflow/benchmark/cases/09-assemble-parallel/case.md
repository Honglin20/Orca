# 09 — 组装：散 agent md → 并行 diamond

- **场景**：3（只有 agent 池，无编排）
- **输入**：用户给 `researcher_a.md` / `researcher_b.md` 两个研究员 agent，说"哥俩并行调研主题，完事合并"。
- **预期产物**：`expected/workflow.yaml` + `expected/agents/{researcher_a,researcher_b}.md`
- **不变量**：
  - entry=researcher_a → parallel 组 [researcher_a, researcher_b]（a 已跑则幂等跳过）→ synthesizer → `$end`
  - 两个 researcher 用 `agent:` 引用（复用）；**synthesizer 是 skill 补写的内联 merge 节点**（用户没给合成 agent → skill 起草）
  - synthesizer 经 `<组名>.output.outputs.<branch>` 取两路结果 → 混合 inline + agent-ref 两态
