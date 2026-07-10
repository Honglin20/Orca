# 02 — NL：并行 diamond

- **场景**：1（从零描述）
- **输入**："一个 entry agent 把查询拆成两个可并行的子问题，两个 researcher 并行调研，完事一个 synthesizer 把两路结果合并。"
- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - starter → parallel 组 [researcher_a, researcher_b] → synthesizer(set 汇聚) → `$end`
  - 组聚合输出经 `<组名>.output.outputs.<branch>` 取
  - synthesizer 用 `set` 节点（纯汇聚，无 token）
