# 01 — NL：线性三 agent

- **场景**：1（从零描述）
- **输入**："我要一个文本处理 pipeline：先 ingest 清洗输入文本去噪，再 analyze 提取要点，最后 report 写汇总报告。三步串行。"
- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - 3 个 agent 节点，全部内联 prompt（短，不复用 → 内联）
  - 边：ingest→analyze→report→`$end`
  - `inputs.task`（string）；`outputs` 取自 report
  - executor=opencode + deepseek-v4-flash
