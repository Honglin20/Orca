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
import type { WorkflowTopology } from "@/types/topology";
import {
  enterReplay as replayEnter,
  exitReplay as replayExit,
  resetDerived,
  resetReplayBuffer,
  setReplayTarget as replaySetTarget,
} from "./replay-actions";

// ── store state 形状（业务派生 + UI 交互态 + actions，SPEC §3.1）──────────────
export interface WorkflowState {
  // === 业务真相派生物（从 events fold）===
  /** 当前 run 的事件缓存（懒加载填，切走清；非真相源，铁律 2）。按 seq 去重保证幂等。 */
  events: WorkflowEvent[];
  /** 派生：节点状态（fold 产出，last-writer-wins 幂等）。 */
  nodes: Record<string, NodeState>;
  /** 派生：当前 gate（human_decision_requested 设，resolved 清）。null 表示无活跃 gate。 */
  gate: GateState | null;
  /**
   * 派生：最近一次抢答/已答信息（human_decision_resolved 设）。用于 ResolvedToast
   * 显示「已被 [source] 答」——三通道竞速广播（SPEC §1.5）。null = 尚无已解决 gate。
   * 注：本字段是「最近一次」快照而非真相源（真相在 tape），仅驱动 toast 短暂显示。
   */
  lastResolved: { by: string; answer: string } | null;
  workflowName: string;
  status: WorkflowStatus; // workflow 级；"idle" = 未加载任何 run（前端派生态）
  cost: number; // 派生：累计 cost_usd（agent_usage fold）
  /**
   * 派生：静态 DAG 拓扑（phase 9c）。来自 workflow_started.data.topology（单一真相源 = tape，
   * SPEC §0.1 铁律）。workflow_started handler 提取；workflowDef 出现后 DAG 即可布局（无需
   * 等 route_taken 增量拼边）。null = 尚未收到 workflow_started。
   */
  workflowDef: WorkflowTopology | null;

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
  /**
   * phase 9c tape replay 增量定位（SPEC §2.3，反 Conductor 全量重放）。
   * 前进：apply events[current+1..pos]；后退：从最近 checkpoint 恢复再 apply 到 pos。
   * 详见 stores/replay-actions.ts。
   */
  setReplayTarget: (pos: number) => void;
  /** 进入 replay 模式：缓存 live 末态（live==replay 断言基线）+ 初始化 events 缓存。 */
  enterReplay: () => void;
  /** 退出 replay：恢复 live 末态（replayPosition 拨回末尾）。 */
  exitReplay: () => void;
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
  lastResolved: { by: string; answer: string } | null;
  workflowName: string;
  status: WorkflowStatus;
  cost: number;
  events: WorkflowEvent[];
  selectedNode: string | null;
  replayMode: boolean;
  replayPosition: number;
  activeRunId: string | null;
  workflowDef: WorkflowTopology | null;
};

const eventHandlers: Record<string, Handler> = {
  // ── workflow 生命周期 ──
  workflow_started: (s, d) => {
    s.status = "running";
    s.workflowName = String(d.workflow_name ?? "");
    // phase 9c：提取静态拓扑到 workflowDef（单一真相源 = tape，SPEC §0.1 铁律）。
    // workflow_started 只发一次，幂等无虞。topology 缺失（旧后端兼容）→ 留 null，DAG 不渲染。
    const topo = d.topology;
    if (topo && typeof topo === "object" && Array.isArray((topo as Record<string, unknown>).nodes)) {
      s.workflowDef = topo as unknown as import("@/types/topology").WorkflowTopology;
    }
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

  // ── 并发（foreach；progress 派生给 9c DAG widget，SPEC §1.2）──
  foreach_started: (s, d, e) => {
    if (!e.node) return;
    const total = Number(d.item_count ?? 0);
    // 起始：0/total；foreach_item_completed 递增 done 计数
    s.nodes[e.node] = { status: "running", progress: `0/${total}` };
  },
  foreach_item_started: () => {},
  foreach_item_completed: (s, _d, e) => {
    if (!e.node) return;
    const cur = s.nodes[e.node];
    if (!cur || !cur.progress) return;
    const [done, total] = cur.progress.split("/").map(Number);
    if (Number.isFinite(done) && Number.isFinite(total)) {
      cur.progress = `${done + 1}/${total}`;
    }
  },
  foreach_completed: (s, _d, e) => {
    if (!e.node) return;
    // 完成后保持 progress（显示满进度），status 走 done
    const cur = s.nodes[e.node];
    s.nodes[e.node] = { status: "done", progress: cur?.progress };
  },

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
  human_decision_resolved: (s, d) => {
    // 三通道竞速广播（SPEC §1.5）：收到 resolved → 清 gate + 记 lastResolved
    // （驱动 ResolvedToast「已被 [source] 答：[answer]」，2s 后 toast 自清）。
    s.gate = null; // 清（无论赢家；幂等）
    s.lastResolved = {
      by: String(d.resolved_by ?? ""),
      answer: String(d.answer ?? ""),
    };
  },

  // ── 自定义（9d chart/table 渲染读 events；fold 无派生）──
  custom: () => {},

  // ── 错误 ──
  error: () => {},
};

// ── fold 核心（handler dispatch，无 dedup/无 events.push）──────────────────────
// 提取出来让 replay 复用：replay 重新 apply 同 seq 事件是预期行为（从头 fold），故走
// applyOneRaw（= foldEvent）绕过 processEvent 的 seq 去重。**handler 表只有一份**（铁律 1），
// 区别仅在外壳（live: dedup+push；replay: 不 dedup/不 push，events 缓存已冻结）。
function foldEvent(state: ImmerDraft, event: WorkflowEvent): void {
  const handler = eventHandlers[event.type];
  if (handler) {
    try {
      handler(state, event.data ?? {}, event);
    } catch (err) {
      // fail loud（记 console，但不 crash store —— 单条坏事件不该毁整个 fold）
      console.error(
        `[orca] event handler 抛异常 type=${event.type} seq=${event.seq}`,
        err
      );
    }
  } else {
    // 未知 type：fail loud（warn，但不 crash）。后端新增 event type 但前端 handler 表未同步时
    // 能被发现，而非静默丢失派生。processEvent 外壳仍负责 events.push（让 9c/9d 可读）。
    console.warn(`[orca] 未知的 event type="${event.type}" (seq=${event.seq})，无 handler`);
  }
}

// ── 单 store（铁律 4：全前端唯一 create()）──────────────────────────────────────
// immer middleware：set 回调内直接 mutate draft，immer 生成新引用（不可变 + 可读性双赢）。
export const useWorkflowStore = create<WorkflowState>()(
  immer((set, get) => ({
    events: [],
    nodes: {},
    gate: null,
    lastResolved: null,
    workflowName: "",
    status: "idle",
    cost: 0,
    workflowDef: null,

    selectedNode: null,
    replayMode: false,
    replayPosition: 0,
    activeRunId: null,

    processEvent: (event) => {
      set((state) => {
        // ── 幂等 guard：同 seq 已存在则跳过（live 重连重放安全）──
        // 见 store.test.ts「fold idempotent」断言。replay 的 apply 走 applyOneRaw（绕过此 guard）。
        if (state.events.some((e) => e.seq === event.seq)) {
          return; // 不变（immer：不 mutate 即返回原 state）
        }

        // ── fold 核心（handler dispatch；replay 复用同一份，铁律 1）──
        foldEvent(state, event);

        // events append（已在 seq guard 排除重复）
        state.events.push(event);
      });
    },

    replayState: (events) => {
      // 重置派生态 → 逐条 processEvent（live/replay 共用同一路径，铁律 4）
      set((state) => {
        state.events = [];
        state.selectedNode = null;
        state.replayPosition = 0;
      });
      resetDerived(set);
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
      resetReplayBuffer(); // replay buffer 也清（跟随 run 生命周期）
      set((state) => {
        state.activeRunId = null;
        state.events = [];
        state.selectedNode = null;
        state.replayMode = false;
        state.replayPosition = 0;
      });
      resetDerived(set);
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
        // 仅设 UI 滑块位置标记（不触发 fold）。增量 fold 走 setReplayTarget。
        state.replayPosition = pos;
      }),

    // ── phase 9c replay actions（增量 apply + checkpoint，SPEC §2.3）──
    setReplayTarget: (pos) => {
      // applyOne：复用 foldEvent（同一 handler 表，铁律 1），但绕过 seq 去重 + 不 push
      // events（replay 时 events 缓存已冻结在 buffer，不该被 replay 的 apply 污染）。
      const applyOne = (event: WorkflowEvent) => {
        set((state) => {
          foldEvent(state, event);
        });
      };
      replaySetTarget(pos, get, set, applyOne);
    },
    enterReplay: () => {
      replayEnter(get, set);
    },
    exitReplay: () => {
      replayExit(set);
    },
  }))
);

// 导出 eventHandlers 的 type keys 给测试断言（覆盖全部 EventType）
export const HANDLED_EVENT_TYPES = Object.keys(eventHandlers);
