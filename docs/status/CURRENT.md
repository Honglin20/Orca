# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

当前无进行中任务。

**最近完成**：prof-opt 反馈批（2026-09-08，commit `39f9214`，release note：`docs/releases/2026-09-08-prof-opt-feedback-batch.md`）；workflow 脚本 exec 位治本（2026-09-08，commit `c8f4807`+`4bc8a2c`，release note：`docs/releases/2026-09-08-workflow-scripts-exec-bit.md`）。

**挂账小项（非阻塞，下次顺手）**：
- HEAD 既有 pytest 失败 3 个：`test_gate_node_sh_parses_after_quote_fix`（陈旧断言）、`test_baseline_chain_*` ×2（状态机 failed≠running）
- run 卡片 chart_count 口径含 docs 清单
- prof-opt 首次真机 in-session 短跑时人工核验：第二次 docs 推送后面板内容仍可读、`rounds/<NNN>/<vid>/shadow` 存在（单测盲区，见 release note 披露）
