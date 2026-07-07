# 03 — NL：foreach 动态并行

- **场景**：1（从零描述）
- **输入**："给定一组 repo，对每个 repo 并发跑一个 assessor 评估质量，最后输出评估总数。"
- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - `set` 节点产数组 → `foreach` body(agent) 对每元素并发 → `$end`
  - 🔴 **语义关键**：set 的数组值必须是**字面 JSON 字符串**（双引号），不能用 `{{ inputs.repos }}`
    —— Jinja2 渲染真 list 产单引号 repr，`foreach` 的 JSON 解析会失败（`orca/run/foreach.py:123`）
  - `source` 首段是真实节点名（maker）；`max_concurrent` 限流
