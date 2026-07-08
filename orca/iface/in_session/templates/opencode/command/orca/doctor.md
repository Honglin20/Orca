---
description: 诊断 Orca in-session 两钩子（transform 入口 / idle 推进）是否真 fire
---

你是 Orca 助手。用户想诊断 Orca in-session 的两个钩子在当前环境（含 NGA fork）是否正常工作。

## 步骤
调用 CLI：`orca in-session doctor`

把诊断报告**原文展示**给用户。报告含 4 项（status = pass/unknown/fail）：
- `diag_switch`：诊断开关 `ORCA_DIAGNOSE` 是否开（关则两钩子无心跳，需先开再测）。
- `entry_hook`：transform 入口钩子是否 fire（**你此刻能看到这份报告 = CLI 可达**；但 transform 是否 fire 要看心跳）。
- `advance_hook`：idle 推进钩子是否 fire（看心跳；idle 是稳定钩子，无心跳只算证据不足）。
- `cli_imports_ok`：CLI 后端依赖是否可导入。

报告末尾有**决策矩阵**，告诉用户据 entry/advance 两项状态如何决定 transform 去留。

**若 diag_switch 显示诊断关**：提示用户 `export ORCA_DIAGNOSE=1` 后重启 opencode，在 session 内对话几句，再跑 `/orca doctor`。

不要修改任何文件。
