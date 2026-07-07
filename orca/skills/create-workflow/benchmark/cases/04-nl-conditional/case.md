# 04 — NL：条件分支

- **场景**：1（从零描述）
- **输入**："classifier 判断输入类别；类别是 A 走 handler 处理后正常结束；不是 A 就直接判失败终止。"
- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - classifier 用 `output_schema` 产结构化 `{kind}`，路由判 `output.json.kind == "A"`
  - 两条 route：when=A → handler；catch-all（无 when，放最后）→ terminate(failed)
  - terminate 节点 `routes` 必空、不作 entry
