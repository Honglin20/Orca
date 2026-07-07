# 05 — NL：结构化输出 + validator + retry

- **场景**：1（从零描述）
- **输入**："generator 产出一个 JSON：{model_class, weights_path}。要校验 model_class 是合法 Python 标识符、weights_path 是绝对路径；不合规就重跑，最多重试 1 次。"
- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - agent 带 `output_schema`（结构化）+ `validator`（criteria + max_retries=1）
  - `RetryPolicy`：max_attempts≥1（瞬时失败重试）
  - 单节点 → `$end`；outputs 取 generator.output
