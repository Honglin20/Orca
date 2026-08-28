# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前任务：Web 界面性能优化（SDD-LOOP 编排）

- **Phase 0 完成 → Phase 1 进行中**（spec 评审环，轮 1）
- SPEC：`docs/specs/2026-08-28-web-perf-optimization.md`
- 计划：`docs/plans/2026-08-28-web-perf-optimization.md`（用户已批准；Plan agent 对抗审查已过）
- 范围：批 1 传输层（gzip / Cache-Control / 产物清理）+ 批 2 markdown chunk 瘦身（rehypePrismCommon + katex CSS 移位）+ 批 3+4 fold 索引 / 细粒度订阅 / memo / 虚拟化（10 步）；**不做** huge-mode tail-first
- 外环轮数：spec 0 / plan 0 / e2e 0；全循环回退 0
- 基线工作树：prof-opt prompt 清理 15 个 M 文件 + untracked scratch（**既有无关改动，commit 时只提交本次文件**）

## 已完成（勿重复）

- workflows per-workflow 目录隔离改造（2026-08-28，E2E **PASS**，报告 `LAYOUT_MIGRATION_REPORT.md`）
- prof-opt v5 收口（2026-08-27）；create-workflow skill v2（`a379375`）
- prof-opt prompt 洁净清理 + 轮末结论闭环（2026-08-28，commit `94378e8`）：15 violations 闭环 / 内联抽脚本 ×12 / lint 部署豁免 + `rounds/<NNN>/analysis.md` 双节落盘回流（propose 时延节 + probe 精度节 + report Round Conclusions 节）；**镜像预置规则经用户否决（洁净原则）不写入**；E2E 由用户替换 mfu-benchmark 后自跑；CHANGELOG [2026-08-28] + release note `docs/releases/2026-08-28-prof-opt-prompt-cleanliness-and-round-conclusions.md`

## 工作区遗留（非任务，不动）

- `.e2e_po/`、`.e2e_spe2e/` scratch（untracked，不提交）
- web-perf 两份 docs（`docs/plans|specs/2026-08-28-web-perf-optimization.md`）+ `orca/iface/web/static/assets/` 构建产物——**非本任务产物，未提交未动**
