// stores/workflow-store.ts —— Zustand 单 store（事件溯源 fold，SPEC §3）。
//
// 六条铁律对应：
//   - **单 store + 单 fold**（铁律 4）：全前端唯一的 `create()`；eventHandlers 表是唯一
//     状态计算路径，live（WS）和 replay（REST /events）都调 processEvent（反双路径）。
//   - **fold 幂等**（铁律 4 / §3.2.3）：node 状态 last-writer-wins，events 按 seq 去重 ——
//     同事件应用 N 次状态一致（store.test.ts 显式断言）。
//   - **events 是缓存非真相**（铁律 2 / §3.2.2）：真相在后端 tape，前端 events 只是当前
//     run 的懒加载缓存，切走（unloadRun）就清，不累积。
//   - **前端无编排逻辑**（铁律 6）：store 只 fold + 存少量 UI 交互态，不决策。
//
// 不可变性：用 immer 中间件（Zustand 官方推荐）—— handler 可直接 mutate draft，immer
// 保证生成新引用，避免手写浅拷贝的越界 mutate 风险（9c/9d 加复杂 handler 的安全锁）。

import { create } from "zustand";
import { immer } from "zustand/middleware/immer";
import type {
  GateState,
  NodeState,
  WorkflowEvent,
  WorkflowStatus,
} from "@/types/events";

// ── store state 形状（业务派生 + UI 交互态 + actions，SPEC §3.1）──────────────
export interface WorkflowState {
  // === 业务真相派生物（从 events fold）===
  /** 当前 run 的事件缓存（懒加载填，切走清；非真相源，铁律 2）。按 seq 去重保证幂等。 */
  events: WorkflowEvent[];
  /** 派生：节点状态（fold 产出，last-writer-wins 幂等）。 */
  nodes: Record<string, NodeState>;
  /** 派生：当前 gate（human_decision_requested 设，resolved 清）。null 表示无活跃 gate。 */
  gate: GateState | null;
  workflowName: string;
  status: WorkflowStatus; // workflow 级；"idle" = 未加载任何 run（前端派生态）
  cost: number; // 派生：累计 cost_usd（agent_usage fold）

  // === UI 交互态（非业务真相，铁律 2）===
  selectedNode: string | null;
  replayMode: boolean;
  replayPosition: number;
  /** 当前懒加载的 run（loadRun 设，unloadRun 清；null = 未持有任何 run）。 */
  activeRunId: string | null;

  // === actions ===
  /** 统一 fold 入口（live + replay 共用，反双路径）。幂等（seq 去重）。 */
  processEvent: (event: WorkflowEvent) => void;
  /** 全量 replay：重置派生态 → 逐条 processEvent。用于 WS 重连（初始加载走 useRunEvents）。 */
  replayState: (events: WorkflowEvent[]) => void;
  /** 懒加载：GET /api/runs/<id>/events → replayState。失败 fail loud（记 console + 留 idle）。 */
  loadRun: (runId: string) => Promise<void>;
  /** 卸载当前 run 的派生态（懒加载红线：切走清，不累积）。 */
  unloadRun: () => void;
  /** UI 交互态 setter（非业务真相）。 */
  setSelectedNode: (node: string | null) => void;
  setReplayMode: (on: boolean) => void;
  setReplayPosition: (pos: number) => void;
}

// ── eventHandlers 表（唯一状态计算路径，SPEC §3.1）──────────────────────────────
// 覆盖全部 21 个 EventType（对齐 orca/schema/event.py EventType Literal）。每条只做派生：
// 改 status / nodes / gate / cost —— **不拼接**（保证幂等：同事件 N 次应用结果一致）。
// 未知 type 走 fallback（processEvent 内 `?.`），不 crash（SPEC §3.2 + 反 AgentHarness 静默崩）。
//
// PRECONDITION: 所有 handler 只能由 processEvent 调用 —— 幂等靠 processEvent 的 seq 去重保证，
// handler 自身不做去重（agent_usage 累加 cost 依赖此前提）。
type Handler = (
  state: ImmerDraft, // immer draft：可直接 mutate（immer 保证生成新引用）
  data: Record<string, unknown>,
  event: WorkflowEvent
) => void;

// handler 接收的 draft 子集（immer middleware 下 set((s) => ...) 的 s 就是 draft）
type ImmerDraft = {
  nodes: Record<string, NodeState>;
  gate: GateState | null;
  workflowName: string;
  status: WorkflowStatus;
  cost: number;
  events: WorkflowEvent[];
  selectedNode: string | null;
  replayMode: boolean;
  replayPosition: number;
  activeRunId: string | null;
};

const eventHandlers: Record<string, Handler> = {
  // ── workflow 生命周期 ──
  workflow_started: (s, d) => {
    s.status = "running";
    s.workflowName = String(d.workflow_name ?? "");
  },
  workflow_completed: (s) => {
    // workflow 级终态（对齐 RunState.status "completed"；与 node 的 "done" 有意区分）
    s.status = "completed";
  },
  workflow_failed: (s) => {
    s.status = "failed";
  },

  // ── node 生命周期（last-writer-wins，幂等）──
  node_started: (s, _d, e) => {
    if (e.node) s.nodes[e.node] = { status: "running" };
  },
  node_completed: (s, d, e) => {
    if (e.node) s.nodes[e.node] = { status: "done", output: d.output };
  },
  node_failed: (s, _d, e) => {
    if (e.node) s.nodes[e.node] = { status: "failed" };
  },
  node_skipped: (s, _d, e) => {
    if (e.node) s.nodes[e.node] = { status: "skipped" };
  },

  // ── agent 流式（只对 cost/gate 派生有贡献的 fold；message/thinking 9c 渲染读 events）──
  agent_usage: (s, d) => {
    const c = Number(d.cost_usd ?? 0);
    if (!Number.isNaN(c)) s.cost += c; // 累加 usage 事件；幂等由 seq 去重保证（同事件不重复加）
  },
  agent_message: () => {},
  agent_thinking: () => {},
  agent_tool_call: () => {},
  agent_tool_result: () => {},

  // ── 路由（9c DAG 渲染读 events，fold 无派生）──
  route_taken: () => {},

  // ── 并发（foreach；9c 读 events）──
  foreach_started: () => {},
  foreach_item_started: () => {},
  foreach_item_completed: () => {},
  foreach_completed: () => {},

  // ── HMIL gate（9d 弹窗读 store.gate）──
  human_decision_requested: (s, d, e) => {
    const gate_id = String(d.gate_id ?? "");
    if (!gate_id) {
      // fail loud：缺 gate_id 是后端契约违规，记 warning（不静默）
      console.warn(
        `[orca] human_decision_requested 缺 gate_id (seq=${e.seq})`,
        e
      );
      return;
    }
    s.gate = {
      gate_id,
      prompt: String(d.prompt ?? ""),
      options: Array.isArray(d.options)
        ? (d.options as unknown[]).map(String)
        : undefined,
      source: d.source != null ? String(d.source) : undefined,
      context:
        d.context && typeof d.context === "object"
          ? (d.context as Record<string, unknown>)
          : undefined,
    };
  },
  human_decision_resolved: (s) => {
    s.gate = null; // 清（无论赢家；幂等）
  },

  // ── 自定义（9d chart/table 渲染读 events；fold 无派生）──
  custom: () => {},

  // ── 错误 ──
  error: () => {},
};

// ── 单 store（铁律 4：全前端唯一 create()）──────────────────────────────────────
// immer middleware：set 回调内直接 mutate draft，immer 生成新引用（不可变 + 可读性双赢）。
export const useWorkflowStore = create<WorkflowState>()(
  immer((set, get) => ({
    events: [],
    nodes: {},
    gate: null,
    workflowName: "",
    status: "idle",
    cost: 0,

    selectedNode: null,
    replayMode: false,
    replayPosition: 0,
    activeRunId: null,

    processEvent: (event) => {
      set((state) => {
        // ── 幂等 guard：同 seq 已存在则跳过（live 重连重放 + replay 都安全）──
        // 见 store.test.ts「fold idempotent」断言。
        if (state.events.some((e) => e.seq === event.seq)) {
          return; // 不变（immer：不 mutate 即返回原 state）
        }

        // ── 派生：调对应 handler（未知 type 静默忽略，不 crash，SPEC §3.2）──
        const handler = eventHandlers[event.type];
        if (handler) {
          try {
            // immer draft 直接传给 handler —— handler 内 mutate 即可
            handler(state, event.data ?? {}, event);
          } catch (err) {
            // fail loud（记 console，但不 crash store —— 单条坏事件不该毁整个 fold）
            console.error(
              `[orca] event handler 抛异常 type=${event.type} seq=${event.seq}`,
              err
            );
          }
        }
        // 未知 type：仅缓存 event（让 9c/9d 仍可读），不改派生态

        // events append（已在 seq guard 排除重复）
        state.events.push(event);
      });
    },

    replayState: (events) => {
      // 重置派生态 → 逐条 processEvent（live/replay 共用同一路径，铁律 4）
      set((state) => {
        state.events = [];
        state.nodes = {};
        state.gate = null;
        state.workflowName = "";
        state.status = "idle";
        state.cost = 0;
        state.selectedNode = null;
        state.replayPosition = 0;
      });
      events.forEach((e) => get().processEvent(e));
    },

    loadRun: async (runId) => {
      // 懒加载：GET /api/runs/<id>/events → replayState（铁律 1，SPEC §4.1）
      try {
        const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`);
        if (!resp.ok) {
          console.error(`[orca] loadRun ${runId} 失败 HTTP ${resp.status}`);
          return;
        }
        const events = (await resp.json()) as WorkflowEvent[];
        set((state) => {
          state.activeRunId = runId;
        });
        get().replayState(events);
      } catch (err) {
        // fail loud（记 console；保持 idle，UI 显示加载失败）
        console.error(`[orca] loadRun ${runId} 网络错误`, err);
      }
    },

    unloadRun: () => {
      // 切走 → 清当前 run 的派生态（懒加载红线：不累积，铁律 1）
      set((state) => {
        state.activeRunId = null;
        state.events = [];
        state.nodes = {};
        state.gate = null;
        state.workflowName = "";
        state.status = "idle";
        state.cost = 0;
        state.selectedNode = null;
        state.replayMode = false;
        state.replayPosition = 0;
      });
    },

    setSelectedNode: (node) =>
      set((state) => {
        state.selectedNode = node;
      }),
    setReplayMode: (on) =>
      set((state) => {
        state.replayMode = on;
      }),
    setReplayPosition: (pos) =>
      set((state) => {
        state.replayPosition = pos;
      }),
  }))
);

// 导出 eventHandlers 的 type keys 给测试断言（覆盖全部 EventType）
export const HANDLED_EVENT_TYPES = Object.keys(eventHandlers);
