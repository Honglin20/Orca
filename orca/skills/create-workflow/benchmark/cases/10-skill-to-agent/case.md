# 10 — 转换：CC/opencode skill（无脚本）→ agent

- **场景**：4（agent 封成 skill + 编排 md）
- **输入**：用户把两个 agent 封成了 CC skill，外加一份编排说明：

```markdown
# assets/complexity/SKILL.md
---
name: complexity
description: 分析代码复杂度
---
分析给定代码的圈复杂度与维护风险，输出结论。
```
```markdown
# assets/refactor/SKILL.md
---
name: refactor
description: 生成重构建议
---
据复杂度分析结果给出重构建议。
```
```markdown
# assets/orchestration.md
先跑 complexity，再跑 refactor。
```

用户："转成 Orca workflow。"

- **预期产物**：`expected/workflow.yaml` + `expected/agents/{complexity,refactor}.md`
- **不变量**：
  - 每个 skill → 一个 agent md（SKILL.md body 剥 frontmatter 后作 prompt；无脚本 → 单 MD 形态）
  - 编排 md 的顺序 → linear routes complexity→refactor→`$end`
  - refactor 要用 complexity 的结果 → prompt 加 `{{ complexity.output }}`
