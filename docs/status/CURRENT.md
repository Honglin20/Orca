# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**phase 9a（iface/web 后端）已完成** —— 在 **`phase9-web` 分支** 上（非 master）。
phase 9 全部子阶段（9a/9b/9c/9d）在此分支开发，**勿切回 master**。

- **状态**：✅ 9a 后端完成（37 web 单测全绿，0 RuntimeWarning 0 ResourceWarning；594 全量
  全绿零回归；5 条铁律 grep 全过；review 全修复）
- **release note**：[`docs/releases/2026-06-30-phase9a-web-backend.md`](../releases/2026-06-30-phase9a-web-backend.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)
- **commit 规范**：`feat(web):` 前缀（web 是纯渲染/转发层）

## 下一步（phase 9b 前端骨架）

phase 9b：React+Vite+Zustand 前端骨架（路由导航栈 + Zustand 单 store + 懒加载 + WS hook）。
参考 [`docs/specs/phase-9b-web-frontend-core.md`](../specs/phase-9b-web-frontend-core.md) +
[`docs/plans/2026-06-30-phase9b-web-frontend-core.md`](../plans/2026-06-30-phase9b-web-frontend-core.md)。

phase 9a 提供给 9b 的契约：
- `GET /api/runs`（元数据列表，无 events）+ `GET /api/runs/<id>/events`（懒加载全量）+
  `GET /api/runs/<id>`（meta+state 快照）+ `POST /api/run`（启动）。
- `/ws` 单通道：subscribe(run_id) → 推该 run 事件（带 run_id 标签）；gate_response 反向。
- 复用 phase-6 `/gate` + `/gate/respond`（web 层多 run 分发已就位）。

## 必读文件（开工前）

1. [`docs/specs/phase-9b-web-frontend-core.md`](../specs/phase-9b-web-frontend-core.md)
2. [`docs/plans/2026-06-30-phase9b-web-frontend-core.md`](../plans/2026-06-30-phase9b-web-frontend-core.md)
3. [`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md) §4.3（唯一真相源铁律）
4. [`docs/releases/2026-06-30-phase9a-web-backend.md`](../releases/2026-06-30-phase9a-web-backend.md)（9a 后端契约）
