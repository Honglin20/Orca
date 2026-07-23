# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-24）

### ✅ 单端口 + 多 Run 监控「遗留清项」（SPEC §13 v4 carry-over）—— 已提交

**状态**：SPEC §13 v4「遗留（非阻塞）」清单七项全清（AC14 contract test / P0 持久缓存 /
`tars project rebuild` / Stale projects 折叠区 / 统一 open 列表语义 / scripts 归位 /
2 pre-existing quick fix）。27 新测全绿；463 passed / 1 pre-existing fail
（`test_web_does_not_import_cli`，架构问题 `apply_kb_requirement` web→cli 反向依赖，
**不动**留后续 PR）。code-reviewer 3 🟡 全闭环 + 3 🟢 采纳。详见 release note：
[2026-07-24-single-port-multi-run-cleanup.md](../releases/2026-07-24-single-port-multi-run-cleanup.md)。

### ✅ 单端口 + 多 Run 监控（Phase A + B' + C 生产 + 单测）—— 已提交

**状态**：commit `1788cea`（实现）+ `c5cf298`（E2E 回归修复）。code-reviewer 2 blocker + 4 major + 3 minor 全闭环。**test-agent 真机 E2E：AC1/3/5/8/16/17/18 七项功能契约全 PASS**。详见 release note：[2026-07-24-single-port-multi-run-monitoring.md](../releases/2026-07-24-single-port-multi-run-monitoring.md)。

---

## 历史状态（已完成，详见 CHANGELOG）

- ✅ **In-Session Chart 鲁棒出图**（commit `003acc3`）
- ✅ **KB 可移植 + struct-exploration**（commit `6e0f167` + `0be8c6d`）
- ✅ **Workflow 全面重设计 P1-P9 + P4b + Stage3/4**（系列 commits，2026-07-21~22）
- ✅ **`orca open` 跨项目端口占用修复**（commit `7d9b7eb` + `9677c1e`）
- ✅ **Workflow 可视化全量优化**（commits `b820ef1`…`f516223`）
- ✅ **P8 引擎 artifacts dir + `orca gc`**（commit `b1eaf43`）

## 待确认（收尾，非阻塞）

- ts_quant 正式进 orca pyproject 依赖
- 各 workflow 真机 in-session E2E + 截真图替换文档占位
- 量化 workflow sidecar 脚本永久单测

## 必读文件（开工前按需）

- [SPEC §13：单端口 + 多 Run 监控 v4 修订](../specs/2026-07-23-single-port-multi-run-monitoring.md)
- [CHANGELOG](CHANGELOG.md)
- [shells-design-draft.md](../specs/shells-design-draft.md)（Phase 6.3 三通道竞速，已在定稿）
