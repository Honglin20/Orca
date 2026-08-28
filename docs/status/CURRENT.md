# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前无进行中任务

## 已完成（勿重复）

- workflows per-workflow 目录隔离改造（2026-08-28，迁移链 `a7cb0a5..37b4295` + 收尾 commit；E2E **PASS**，验证报告 `LAYOUT_MIGRATION_REPORT.md`）。旧布局遗留裁决（puzzle verifier 断链 / struct yaml 描述文本 / KB kd 卡修剪）**待用户决策**——见报告「待用户决策区」。
- prof-opt v5 时延先行顺序门控完成（2026-08-27，`fdd7a52..d46e9d5` + `b03d8fb`；真机 §11.4 清单归用户）
- create-workflow skill v2 固化（commit `a379375`，2026-08-27）
- prof-opt v4 重构完成（2026-08-26，13 commits）

## 工作区遗留（非任务，不动）

- `.e2e_po/`、`.e2e_spe2e/` scratch（untracked，不提交）；`.layout_baseline_list.txt`（迁移基线，已入 .gitignore，`LAYOUT_MIGRATION_REPORT.md` 引用其路径）
- prof-opt prompt 洁净审查完成（2026-08-28）：15 violations / 11 borderlines，findings + 清理批次草案见 `docs/plans/2026-08-28-prof-opt-prompt-cleanliness-findings.md`；迁移 loop 已收口（2026-08-28），workflows/ 写权已释放，**清理可开工**
