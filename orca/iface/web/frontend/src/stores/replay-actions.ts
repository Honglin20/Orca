// stores/replay-actions.ts —— phase 9c tape replay 增量 apply + checkpoint（SPEC §2.3）。
//
// 反模式（SPEC §0.2）：Conductor 的 setReplayPosition 每次拖滑块全量重置 + 全量重放，长
// workflow 卡顿。本模块用**增量 apply**：
//   - 前进（pos 增大）：apply events[current+1 .. pos]
//   - 后退（pos 减小）：找最近 checkpoint（< pos）→ 恢复 → apply 到 pos
//   - checkpoint：每 CHECKPOINT_INTERVAL 个事件存一次 state snapshot
//
// 单路径 fold（铁律 1）：replay 的 apply **复用 store 的 processEvent**（同一 handler 表），
// 不写第二套 fold。这是 live==replay 一致性的根本保证。
//
// 关键不变量：``replayPosition`` 始终 = 当前已 apply 到的 events 索引（-1 = 未开始）。

import type { WorkflowEvent } from "@/types/events";
import type { WorkflowState } from "./workflow-store";

/** checkpoint 间隔（每 N 个事件存一次 snapshot）。SPEC §2.3 示例值 20。 */
export const CHECKPOINT_INTERVAL = 20;

/** replay checkpoints + live 末态缓存（模块级单例，跟随 active run 生命周期）。 */
interface ReplayBuffer {
  /** position → 该位置的派生态 snapshot（nodes/gate/cost/workflowDef）。key = position。 */
  checkpoints: Map<number, ReplaySnapshot>;
  /** live 末态快照（enterReplay 时存；exitReplay/live==replay 断言用）。 */
  liveFinal: ReplaySnapshot | null;
  /** 进入 replay 时缓存的全量 events（replay apply 读，不重复 fetch）。 */
  events: WorkflowEvent[];
}

export interface ReplaySnapshot {
  nodes: Record<string, { status: string; output?: unknown; progress?: string }>;
  gate: WorkflowState["gate"];
  cost: number;
  workflowName: string;
  status: WorkflowState["status"];
  workflowDef: WorkflowState["workflowDef"];
}

// 模块级单例：replay buffer 跟随当前 run（unloadRun/exitReplay 清）。不进 store state
// （非业务真相，是 replay 的内部缓存；放进 store 反而每次 set 触发重渲染）。
let buffer: ReplayBuffer = {
  checkpoints: new Map(),
  liveFinal: null,
  events: [],
};

/** 测试/卸载用：重置 replay buffer（unloadRun 调）。 */
export function resetReplayBuffer(): void {
  buffer = { checkpoints: new Map(), liveFinal: null, events: [] };
}

/** 测试用：读 buffer 状态（断言 checkpoint 数）。 */
export function __getReplayBufferForTest(): Readonly<ReplayBuffer> {
  return buffer;
}

/** 拍快照：从当前 store state 提取派生态（深拷贝，避免后续 mutate 污染 checkpoint）。 */
function snapshot(state: WorkflowState): ReplaySnapshot {
  return {
    nodes: JSON.parse(JSON.stringify(state.nodes)) as ReplaySnapshot["nodes"],
    gate: state.gate ? JSON.parse(JSON.stringify(state.gate)) : null,
    cost: state.cost,
    workflowName: state.workflowName,
    status: state.status,
    workflowDef: state.workflowDef
      ? JSON.parse(JSON.stringify(state.workflowDef))
      : null,
  };
}

/**
 * 重置全部业务派生态到初始（DRY：replayState / unloadRun / enterReplay 共用）。
 *
 * 排除 UI 交互态（selectedNode/replayMode/replayPosition/activeRunId）和 events 缓存 ——
 * 调用方按需单独处理这些。只重置从 events fold 出来的派生态。
 */
export function resetDerived(set: (fn: (s: WorkflowState) => void) => void): void {
  set((s) => {
    s.nodes = {};
    s.gate = null;
    s.cost = 0;
    s.workflowName = "";
    s.status = "idle";
    s.workflowDef = null;
  });
}

/** 恢复 snapshot 到 store（immer set）。 */
function restore(
  set: (fn: (s: WorkflowState) => void) => void,
  snap: ReplaySnapshot
): void {
  set((state) => {
    state.nodes = JSON.parse(JSON.stringify(snap.nodes)) as WorkflowState["nodes"];
    state.gate = snap.gate ? JSON.parse(JSON.stringify(snap.gate)) : null;
    state.cost = snap.cost;
    state.workflowName = snap.workflowName;
    state.status = snap.status;
    state.workflowDef = snap.workflowDef
      ? JSON.parse(JSON.stringify(snap.workflowDef))
      : null;
  });
}

/**
 * 进入 replay 模式（SPEC §2.5）。
 *
 * 1. 缓存当前 live 末态（live==replay 断言基线）。
 * 2. 缓存全量 events（replay apply 读）。
 * 3. 重置派生态到初始 → apply events[0..0]（position = 0）。
 * 4. 标记 replayMode = true。
 *
 * 注：replay 重置派生态后逐条 apply 同一份 events —— 用的是 store.processEvent（同一
 * handler 表），所以 fold 路径与 live 完全一致（铁律 1）。
 */
export function enterReplay(
  get: () => WorkflowState,
  set: (fn: (s: WorkflowState) => void) => void
): void {
  const state = get();
  buffer = {
    checkpoints: new Map(),
    liveFinal: snapshot(state), // 缓存 live 末态
    events: [...state.events],
  };
  // 重置派生态到初始（DRY：复用 resetDerived）
  resetDerived(set);
  set((s) => {
    s.replayMode = true;
    s.replayPosition = -1; // 尚未 apply 任何事件
  });
  // 建一个 position=-1 的空态 checkpoint：让任何后退（含 < 首个 checkpoint）都走
  // checkpoint 路径，消除 setReplayTarget 的「无 checkpoint 全量重置」特殊分支（铁律 3
  // 精神：永远增量，不全量重置）。空态 snapshot 直接构造（与 resetDerived 后的派生态一致）。
  buffer.checkpoints.set(-1, {
    nodes: {},
    gate: null,
    cost: 0,
    workflowName: "",
    status: "idle",
    workflowDef: null,
  });
}

/**
 * 退出 replay：恢复 live 末态（replayPosition 拨回末尾，状态归位）。
 * replayMode 关闭后，WS live 事件继续走 processEvent（基于 liveFinal 末态续）。
 */
export function exitReplay(
  set: (fn: (s: WorkflowState) => void) => void
): void {
  const liveFinal = buffer.liveFinal;
  set((s) => {
    s.replayMode = false;
  });
  if (liveFinal) {
    restore(set, liveFinal);
    set((s) => {
      s.events = buffer.events; // 恢复全量 events 缓存
      s.replayPosition = buffer.events.length - 1;
    });
  }
  resetReplayBuffer();
}

/**
 * 增量定位到 pos（SPEC §2.3，反全量重放）。
 *
 * 前进：apply events[current+1 .. pos]（每跨 CHECKPOINT_INTERVAL 存 checkpoint）。
 * 后退：找 ≤ pos 的最近 checkpoint → 恢复 → apply (checkpoint..pos]。
 *
 * 关键：apply 复用 store.processEvent（同一 handler 表，铁律 1）。processEvent 内的 seq
 * 去重 guard 在 replay 模式需绕过（replay 重新 apply 同 seq 事件是预期行为）—— 故 replay
 * 的 apply 走 ``applyEventBypassingDedup``（见 store），而非直接 processEvent。
 *
 * @param applyOne 应用单个事件的函数（store 注入：绕过 seq 去重的 apply）
 */
export function setReplayTarget(
  pos: number,
  get: () => WorkflowState,
  set: (fn: (s: WorkflowState) => void) => void,
  applyOne: (event: WorkflowEvent) => void
): void {
  const events = buffer.events;
  if (events.length === 0) return;
  const clamped = Math.max(-1, Math.min(pos, events.length - 1));
  const current = get().replayPosition;

  if (clamped === current) return;

  if (clamped > current) {
    // ── 前进：apply events[current+1 .. clamped] ──
    for (let i = current + 1; i <= clamped; i++) {
      applyOne(events[i]);
      // checkpoint：每 CHECKPOINT_INTERVAL 个存一次（在 apply 之后，position = i）
      if ((i + 1) % CHECKPOINT_INTERVAL === 0) {
        buffer.checkpoints.set(i, snapshot(get()));
      }
    }
  } else {
    // ── 后退：找 ≤ clamped 的最近 checkpoint（含 -1 空态，永不为 null）──
    // enterReplay 已建 checkpoints.set(-1, empty)，故任何 clamped 都能命中一个 checkpoint，
    // 消除「无 checkpoint 全量重置」特殊分支（铁律 3：永远增量）。
    let cpPos = -1;
    for (const k of buffer.checkpoints.keys()) {
      if (k <= clamped && k > cpPos) cpPos = k;
    }
    const snap = buffer.checkpoints.get(cpPos);
    if (snap) {
      restore(set, snap);
    }
    // 理论不可达（-1 checkpoint 总在）：防御性兜底，保证不静默错
    // 从 checkpoint+1 apply 到 clamped
    const start = snap ? cpPos + 1 : 0;
    for (let i = start; i <= clamped; i++) {
      applyOne(events[i]);
    }
  }
  set((s) => {
    s.replayPosition = clamped;
  });
}
