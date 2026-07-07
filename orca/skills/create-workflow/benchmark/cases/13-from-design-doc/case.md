# 13 — 从设计文档生成

- **场景**：C（PRD / 设计文档 → workflow）
- **输入**：用户给一份 PRD：

```markdown
# assets/PRD.md
## 内容生产 pipeline
目标：把一个主题变成一篇润色完的文章。
步骤：
1. **outline**：根据主题产出大纲
2. **draft**：据大纲写初稿
3. **review**：审阅初稿、标记问题、给出定稿
数据流：每步用上一步输出。
```

用户："按这个 PRD 生成 Orca workflow。"

- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - 从 prose 抽 3 步 + 顺序 + 数据流 → 3 个内联 agent，prompt 带 `{{ <上一步>.output }}`
  - inputs.subject（PRD 提到"主题"）
