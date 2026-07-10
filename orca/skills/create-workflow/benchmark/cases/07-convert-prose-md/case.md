# 07 — 转换：prose workflow.md + 散 prompt

- **场景**：2（已有文件夹，异构素材）
- **输入**：一个文件夹，含 `workflow.md`（散文描述步骤）+ 两个 prompt 片段文件：

```markdown
# assets/workflow.md
## 流程
1. **researcher** 调研主题（见 researcher.prompt）
2. **writer** 撰写初稿（见 writer.prompt）
3. **editor** 润色定稿
```
```text
# assets/researcher.prompt
调研主题 {{ inputs.topic }}，输出 3 条要点。
```
```text
# assets/writer.prompt
据调研结果写初稿。
```

用户："转成 Orca workflow。"

- **预期产物**：`expected/workflow.yaml`
- **不变量**：
  - 从 md 抽出 3 步顺序；prompt 文件内容 → 内联 prompt（researcher / writer）
  - editor md 里没单独 prompt 文件 → skill 据职责起草一句
  - researcher 引用 `{{ inputs.topic }}` → workflow 声明 `inputs.topic`
