# CURRENT —— 当前任务快照

> 新 session 开工前**必读**此文件 + `CLAUDE.md` + 对应阶段 SPEC。
> 完成任务后清空本文件（移到 release note），**不积累**。

---

## 当前任务

**无活跃任务** —— 阶段 7（iface/cli CLI 壳）已完成。

- **状态**：✅ 已完成（557 测试全绿 = 478 phase 1-6 + 79 phase 7 净增，零回归；5 条铁律
  grep 验证全过；`orca --help` 显示 run/validate/list）
- **里程碑**：🎉 **Orca 已是可用 CLI 工具**（单 backend + 单 shell + 完整 user journey）
- **release note**：[`docs/releases/2026-06-30-phase7-cli.md`](../releases/2026-06-30-phase7-cli.md)
- **CHANGELOG**：[`docs/status/CHANGELOG.md`](CHANGELOG.md)

## 下一步（待启动新 session）

阶段 9：Web 壳（FastAPI + WebSocket + React+Vite+ReactFlow+Zustand SPA）。
参考 [`docs/specs/phase-9-web.md`](../specs/phase-9-web.md) +
[`docs/specs/shells-design-draft.md`](../specs/shells-design-draft.md) §4。

phase 7 提供给 phase 9 的契约：
- CLI 壳验证了「壳订阅事件流 + gate 走 handler.resolve」范式，Web 壳照搬（渲染层换 React）。
- `_GateHttpBridge` 的双线程隔离模式：web server 是单进程 uvicorn（同引擎事件循环），
  比 CLI 更简单（不需 TUI loop 隔离），但 `register_gate_routes` + `HumanGateHandler` +
  `SessionContextRegistry` 共享对象的模式直接复用。
- GateModal 的 source 分支渲染（tool_permission/agent_ask）是 Web 弹窗的 UI 对照。

## phase 7 遗留（非阻断，后续可优化）

- `_GateHttpBridge` 优雅退出有 1 条 gc RuntimeWarning（broadcaster task 在 loop close 时
  未完全 await 完）——非致命、可见（fail loud 满足），多线程 asyncio loop 生命周期清理
  的已知边缘，可后续用 lifespan shutdown hook 进一步收敛。
- parallel 组进度（`DagTree.set_group_progress`）已实现且有单测，但 `_dispatch_to_widgets`
  未接 reducer 事件（无 foreach/parallel 进度事件驱动入口）——后续 phase 补 reducer
  事件接线即可。
