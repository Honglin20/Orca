# 06 — 转换：异构 workflow YAML（如 AgentHarness 风格）

- **场景**：2（已有 workflow 文件夹，整体翻译）
- **输入**：用户给了一份别家框架的 workflow 定义（`steps` + `depends_on` + 内联 prompt）：

```yaml
# assets/foreign_workflow.yaml（AgentHarness 风格，仅示例形态）
workflow:
  name: data_pipeline
  steps:
    - id: fetch
      agent: true
      prompt: "拉取指定数据源"
    - id: clean
      depends_on: [fetch]
      agent: true
      prompt: "清洗拉取到的数据"
    - id: export
      depends_on: [clean]
      agent: true
      prompt: "导出为 json"
```

用户："把这个转成 Orca workflow。"

- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - `depends_on` 链 → Orca 线性 routes（fetch→clean→export→`$end`）
  - 每步 `agent: true` + prompt → 内联 AgentNode（短 prompt → 内联）
  - 不假定源格式有专门 importer——通用"读出节点 + 读出顺序"抽取
