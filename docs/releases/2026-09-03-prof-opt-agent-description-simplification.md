# prof-opt agent description 精简

日期：2026-09-03  
状态：实现完成  
实现 commit：`389242f`

## 改动

- 将 prof-opt 六个顶层 agent 的 frontmatter `description` 压缩为两句话。
- 描述只保留节点主要目的，不再罗列执行步骤、参数、重试和失败语义。
- `po_propose` 明确以 source-level structural variant 为工作对象。

## 验证

- 六个 description 均为两句话，长度 120-173 字符。
- `git diff --check` 通过。
- `tars validate workflows/prof-opt/workflow.yaml` 通过。
