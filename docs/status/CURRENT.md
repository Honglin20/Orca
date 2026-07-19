# CURRENT —— 当前任务快照

> 新 session 必读：本文件 + `CLAUDE.md`。任务完成移 CHANGELOG 并清空本文件，**不积累**。

---

## 状态（2026-07-19）

- **无进行中主线**。in-session 加固与性能 SPEC **P5（F1 resume）**刚 commit（详见 CHANGELOG）：`orca status` 无参加 `resumable` + SKILL 续跑段 + 占位 spec 建立，**零 marker 字段改动**。
- 近期完成（详见 CHANGELOG）：in-session **P5（F1 resume，7-19）** / P1（7-19）/ O1a tape fold（7-19，P3）/ Web 视觉优化 P0-P4（7-19）/ Node Memory（7-18）/ B2 子 agent 推 web（7-17）。
- **push 待用户手动**：本地领先 origin **88 commits**（WSL SSH 无 github key）。

## 候选下一步（in-session SPEC P2/P4/P6，待用户选定）

依据 SPEC [`2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1 §6 串行顺序（都碰 cli.py → 串行 P2→P4；P6 独立可任意时点；P5 已完成）：

1. **P2（D4 + D5 marker 三态 + doctor orphan）**：合并 commit。改 `marker.py` + doctor 加 orphan_markers check（glob 扫 + tail 50 行判 tape 状态）。SPEC §2 D4/D5。
2. **P4（D1 + D2 失败路径统一）**：D1 stop emit 失败留孤儿 → best-effort 落终态；D2 `apply_step_result` 异常裸崩 → `_safe_apply_or_fail` helper（DRY daemon+cli 两路）。SPEC §2 D1/D2。依赖 P2 先合并（read_marker 契约）。
3. **P6（S1 adapter contract-test 黄金集）**：独立，可任意时点。SPEC §5 S1。

**defer**（SPEC §8）：F2 retry / O1b wf 缓存 / O1c tape resume / O5 lock contention。

## follow-up / debt（预存·暂缓，全量见 CHANGELOG）

- `daemon.py:105` 裸 `sys.exit(128+signum)` 违 §3.3 grep 守门（baseline 即失败）
- 测试 baseline 失败：e2e `python3` 硬编码 / mcp 缺 `uv` / `test_bg_run_ps_logs` rot
- MCP 移除（用户暂缓）/ unified-backend 草稿推迟（含 teams 残留）/ catalog fallback 无测试 / `workflow_failed.data.kind` 字段 drift
- DAG compact minimap（`web-shell-v2-spec.md` §5.7 amendment 待开）

## 待办（用户侧真机，无代码）

- `tars install --target cc` 真生成 skill + `tars list/validate` 真工作
- §9#1 nga/cac 全套集成真机加载（Stop-hook / opencode.json plugin 是否真生效）
- **P1 + P5 改了 SKILL.md（P1: O4 busy + F3 inputs_validation_error；P5: F1 resume 续跑段）**：装了旧 TARS skill 副本的用户需重跑 `tars install` 同步

## 必读文件（开工前按需）

- [`docs/specs/2026-07-19-in-session-hardening-and-perf.md`](../specs/2026-07-19-in-session-hardening-and-perf.md) v4.1
- [`docs/specs/in-session-entry-and-simplification.md`](../specs/in-session-entry-and-simplification.md) v5
- [CHANGELOG](CHANGELOG.md)
