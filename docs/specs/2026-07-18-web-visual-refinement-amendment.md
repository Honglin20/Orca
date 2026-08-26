# Amendment：web-shell-v2-spec.md（P3 视觉优化触发）

> 日期：2026-07-19
> 父 SPEC：[`web-shell-v2-spec.md`](./web-shell-v2-spec.md)
> 触发计划：[`docs/plans/2026-07-18-web-visual-refinement.md`](../plans/2026-07-18-web-visual-refinement.md) P3
> 性质：对父 SPEC 的**增量修订**（非重写），仅记录 P3 引入的两处契约扩展。

## 1. §7 暗色机制：单触发 → 双触发

**原 §7**：暗色仅由 `@media (prefers-color-scheme: dark)` 控制（跟随系统）。

**修订后**：暗色 = `@media (prefers-color-scheme: dark)` + `<html>.dark` / `<html>.light` class **双触发**。

- **system**（默认）：无 class，跟随 `@media`。
- **dark**（用户显式）：`<html>.dark` 强制暗，覆盖系统。
- **light**（用户显式）：`<html>.light` 强制亮，覆盖系统。

**实现约束**：`:root.dark` / `:root.light` specificity = (0,2,0) > `@media :root` = (0,1,0)，故显式 class 总是胜出（不依赖规则顺序，但 `index.css` 中 `:root.dark/.light` 仍定义在 `@media` 之后，双保险）。

**入口**：`src/hooks/use-theme.ts`（三态 toggle + localStorage 持久化），`App.tsx` 模块加载时 `initTheme()`。

## 2. §1.1 单一真相源：WS 连接态 sanctioned exception

**原 §1.1 铁律 1**：tape 事件 fold 是唯一真相源，前端无独立状态。

**修订（ sanctioned exception）**：WebSocket **连接状态**（`connected` / `reconnecting` / `disconnected`）是 **transport-only**（前端网络层派生），**非 tape 真相**，**不**违反铁律 1。

- 存储：`src/hooks/ws-connection-store.ts`（module-level zustand，独立于 `workflow-store`）。
- 写入：`src/hooks/use-websocket.ts` 内部 `onopen` / `onclose` / `onerror` 回调（不改对外 `void` 签名）。
- 读取：`src/hooks/use-ws-status.ts` → TopBar 连接指示点。
- **不进 tape / 不参与 reducer / 不影响幂等 / 后端 `ws_handler.py` 零改**。

## 3. 不涉及

- §5.7 DAG 常驻：**未修订**（P5 minimap 剥离为 follow-up，届时另开 amendment）。
- 其余父 SPEC 条款不变。
