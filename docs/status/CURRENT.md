# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 当前任务（2026-07-24）

### ✅ 单端口 + 多 Run 监控（Phase A + B' + C 生产 + 单测）—— 已提交

**状态**：commit `1788cea`（实现）+ `c5cf298`（E2E 回归修复）。code-reviewer 2 blocker + 4 major + 3 minor 全闭环。**test-agent 真机 E2E：AC1/3/5/8/16/17/18 七项功能契约全 PASS**（单端口复用 / 跨项目 discovery / 懒挂载 / DELETE 四态 / WS 控制帧同步 / open 深链复用）；AC20 零回归——E2E 抓到 1 个 1788cea 引入的回归（push_probe H6 mock 未跟 B-4 `_pump` 签名变更），`c5cf298` 修复，三套件 1129 passed / 仅 3 pre-existing 失败（均与 §13 无关）。

**完成项**：
- Phase A：`orca_home_fingerprint` + 端口登记上移 `~/.orca/.orca-web.json` + `exclusive_port_decision` 临界区（B-6）。
- Phase B'：`orca/runtime/_project.py` 注册表 + `start_run(project_path=)` + `POST /api/run` body 必填 + allowlist。
- Phase C：`GET /api/runs?scope=all` discovery + `ensure_attached` + `DELETE`（M-3 四态）+ WS 控制帧（B-4 queue+writer）+ AuthMiddleware no-op（M-1）+ 前端 RunListPage + run-list-store。
- 单测：runtime（20）+ multi-run-phase-c（15）+ phase-a-registry-auth（10）= 45 新测；既有套件全绿（1 pre-existing apply_kb_requirement import 非 §13 引入）。

**遗留（非阻塞，待后续 PR）**：
- 前端 `out/` 未构建（WSL/Win 混合环境 rollup native binary 缺失，需 Windows-native shell `npm run build`）。
- `_scan_meta_overview` contract test（AC14）未补。
- 持久层 `.orca-meta-cache.json`（P0）未实现。
- `orca project rebuild` 命令未实现。
- pre-existing `apply_kb_requirement` web→cli import（非 §13 引入）。

详见 release note：[2026-07-24-single-port-multi-run-monitoring.md](../releases/2026-07-24-single-port-multi-run-monitoring.md)

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
