# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累、≤50 行**。

---

## 当前任务：无（prof-opt v6 已完成，2026-09-01 收尾归档）

上一任务：prof-opt v6 重设计（A 线机制 + B 线 web）——SPEC 终审、14 commits、逻辑验证 PASS。
见 CHANGELOG 索引 + `docs/releases/2026-09-01-prof-opt-v6.md`。

## 真机清单（归用户，v6 §16 / release note）

- npu/cuda 多卡真实分配、真实 mfu 评测链路、长训练流式早停实测
- web 文档面板 + live chart 推送真机联调
- playwright UI 套件（本机 chromium 缺 libnspr4）

## 遗留 backlog（非阻塞）

- playwright 9d 补用例（`tests/iface/web/test_playwright_9d.py`，环境修复后）
- file:// 图片破图降级（web 面板，已声明取舍）
- v5 用例面 44 个仍绿，若 v5 脚本后续彻底退役需同步清理

## 工作区遗留（非任务，不动）

- `.e2e_po/`、`.e2e_spe2e/`、`.e2e_perfver/`、`.e2e_scratch/` scratch（untracked，不提交）
- `orca/compile/catalog.py` + `tests/compile/test_catalog.py`（会话前遗留）
- `tests/e2e_phase14/` tape 产物（测试运行产物，不提交）
