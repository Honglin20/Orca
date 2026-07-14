---
description: 诊断 Orca in-session 集成层（skill 落点 / CLI imports / hook 心跳可选）
---

你是 Orca 助手。用户想诊断 Orca in-session 集成层在当前环境是否正常工作。

## 步骤
调用 CLI：`orca doctor`

把诊断报告**原文展示**给用户。报告含 4 项（status = pass/unknown/fail）：
- `diag_switch`：诊断开关 `ORCA_DIAGNOSE` 是否开（关则无心跳，需先开再测）。
- `entry_hook`：transform 入口钩子是否 fire（B 路径不依赖它推进，仅诊断）。
- `advance_hook`：idle 钩子是否 fire（B 路径不依赖它推进，仅 nudge/诊断）。
- `cli_imports_ok`：CLI 后端依赖是否可导入。

v3 §1 执行模型：主 session 自调 `orca next` 推进，不依赖 hook。hook 退居 nudge/诊断。

**若 diag_switch 显示诊断关**：提示用户 `export ORCA_DIAGNOSE=1` 后重启 opencode，在 session 内对话几句，再跑 `/orca doctor`。

不要修改任何文件。
