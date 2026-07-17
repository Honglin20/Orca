// stores/workflow-store.ts —— Zustand 单 store = fold(tape)（SPEC §3.1 / §0 D7）。
//
// 六条铁律对应：
//   - **单 store + 单 fold**（铁律 4 / SPEC §3.1）：全前端唯一 ``create()``。state = reducer(events)
//     在 **seq 升序**应用（D7 seq-indexed sorted map，非 append-list）；tiebreaker max(seq) 胜
//     （保 ChartsView(T)==ChartsView(sort(T))==ChartsView(reverse(T))）。
//   - **fold 幂等**（铁律 4 / §3.2.3）：seq-indexed Map 去重——同事件应用 N 次状态一致。
//   - **events 是缓存非真相**（铁律 2）：真相在后端 tape，前端 events 只是当前 run 的缓存，
//     切走（unloadRun）就清。
//   - **D8 unknown_event/agent_usage reducer no-op**：unknown_event/agent_step_started/agent_usage
//     不投影 RunState/视图真相（agent_usage 仅聚合 cost）。
//   - **reasoning_tokens 读 data.get('reasoning_tokens', 0)**：旧 tape 默认 0。
//   - **无 Replay 功能**（SPEC §3.1）：state 永远 = fold(全量 events)；删除 replay 全部。
//
// 不可变性：用 immer middleware（同前）。

import { create } from "zustand";
import { immer } from "zustand/middleware/immer";
import type { EventType, WebEvent } from "@/types/events";
import type {
  GateState,
  LastResolved,
  NodeSessionIndex,
  NodeState,
  RunMetaExtended,
  ServerOverview,
  WorkflowStatus,
} from "@/types/store-types";
import type { WorkflowTopology } from "@/types/topology";
import { CONVERSATION_TYPES } from "@/conversation-types";

// ── store state 形状（业务派生 + UI 交互态 + actions，SPEC §3.1）──────────────
export interface WorkflowState {
  // === 业务真相派生物（从 events fold）===
  /**
   * seq-indexed sorted event map（D7）。key = seq（全局唯一）。**非 append-list**：
   * 插入即排序（ Map 维持插入顺序，但本 store 在 fold 前 sort by seq 保证升序 apply）。
   * 这是 D7 的核心——fold 不依赖事件到达顺序，只依赖 seq 顺序。
   */
  events: WebEvent[];
  /** 派生：节点状态（fold 产出，last-writer-wins 幂等）。 */
  nodes: Record<string, NodeState>;
  /** 派生：当前 gate（human_decision_requested 设，resolved 清）。null 表示无活跃 gate。 */
  gate: GateState | null;
  /** 派生：最近一次已解决 gate（驱动 ResolvedToast）。null = 尚无。 */
  lastResolved: LastResolved | null;
  workflowName: string;
  status: WorkflowStatus;
  cost: number;
  /** 派生：静态 DAG 拓扑（来自 workflow_started.data.topology）。null = 未收到。 */
  workflowDef: WorkflowTopology | null;
  /** 派生：workflow_started.timestamp（D5 elapsed tick 起点）。 */
  workflowStartedAt: number | null;
  /** 派生：workflow_completed/failed/cancelled.data.elapsed（D5 snap）。null = 未完成。 */
  workflowElapsed: number | null;
  /** 派生：累计 reasoning_tokens（agent_usage fold）。 */
  reasoningTokens: number;
  /** 派生：最后已见 seq（D6 WS resume 用）。 */
  lastSeqSeen: number;
  /**
   * 派生：node × session 倒排索引（SPEC web-presentation-refinement §P2 / P0-6）。
   * 四路径（refold / loadFromEvents / loadEarlierChunk / loadFull）+ in-order 增量 fold 路径
   * 都维护，保一致（selectNodeSessions 直接读此索引渲染会话选择器，不全量 filter events）。
   */
  nodesIndex: Record<string, NodeSessionIndex>;

  // === UI 交互态（非业务真相，铁律 2）===
  selectedNode: string | null;
  /**
   * 当前选中会话（SPEC §P2 P1-3 联动）：``"all"`` = 聚合该 node 全 session（旧行为零回归）；
   * 具体 sessionId = 仅该 session；``null`` = 未选（初始 / unloadRun）。
   * ``setSelectedNode`` 同步设为该 node 第一个 sub session（无 sub → ``"all"``）。
   */
  selectedSession: string | "all" | null;
  /** 当前懒加载的 run（loadRun 设，unloadRun 清；null = 未持有任何 run）。 */
  activeRunId: string | null;

  // === Huge-mode + writable 状态（SPEC web-attach §3 / M3）===
  /**
   * 服务端派生 overview（huge=true 时由 /meta 返回）。null = 非 huge 模式 / 已 ``load full``。
   * selectors 在 huge 模式优先读此字段；``loadFull`` 时清此字段 → 全量 client-fold（M4 可验）。
   */
  serverOverview: ServerOverview | null;
  /** writable=false（attached run，read-only）→ gate 模态禁提交（SPEC §8 AC11）。 */
  writable: boolean;
  /** huge 模式（/meta 判定）：tail + 增量 prepend + ``load full`` 按钮。 */
  huge: boolean;
  /** huge 模式下当前窗口的最旧 seq（用于 ``?since=oldest-M`` 增量 prepend）。 */
  oldestSeqInWindow: number;
  /** huge 模式下最新的 seq（用于 WS resume since=newest_seq）。 */
  newestSeqInWindow: number;
  /** huge 模式下是否已 ``load full``（全量 client-fold，clear serverOverview）。 */
  hugeFullyLoaded: boolean;

  // === actions ===
  /** 统一 fold 入口（live + WS 增量）。幂等（seq 去重）。 */
  processEvent: (event: WebEvent) => void;
  /** 全量 fold：重置派生态 → 逐条 processEvent（seq 升序）。用于初始加载 + WS 全量重拉。 */
  loadFromEvents: (events: WebEvent[]) => void;
  /** 懒加载：GET /api/runs/<id>/events → loadFromEvents。失败 fail loud。 */
  loadRun: (runId: string) => Promise<void>;
  /**
   * SPEC web-attach §3 huge-mode 入口：先 GET /meta → 判 huge 分支。
   * - huge=false：GET /events 全量 → loadFromEvents（同 loadRun 行为）。
   * - huge=true：GET /events?tail=500 → loadFromEvents（tail fold）+ 设 serverOverview。
   *   Conversation/Log 读 tail；上滚 IntersectionObserver → ``loadEarlierChunk`` 增量 prepend。
   */
  loadRunWithMeta: (runId: string) => Promise<void>;
  /** huge 模式增量 prepend：fetch ``?since=oldest-M&limit=M`` → 与既有 events 合并 fold。 */
  loadEarlierChunk: (runId: string, chunkSize: number) => Promise<boolean>;
  /** huge 模式 ``load full``：拉全量 + clear serverOverview（M4：可 client-fold 校验）。 */
  loadFull: (runId: string) => Promise<void>;
  /** 卸载当前 run 的派生态（懒加载红线：切走清，不累积）。 */
  unloadRun: () => void;
  /**
   * UI 交互态 setter（非业务真相）。SPEC §P2 P1-3：同步联动设 selectedSession = 该 node
   * 第一个 sub session（依赖 nodesIndex；无 sub → ``"all"``；node=null → selectedSession=null）。
   */
  setSelectedNode: (node: string | null) => void;
  /** SPEC §P2：切当前 node 的会话（"all"=聚合；具体 sessionId=仅该 session）。 */
  setSelectedSession: (sid: string | "all" | null) => void;
}

// ── eventHandlers 表（唯一状态计算路径，SPEC §3.1）──────────────────────────────
// 覆盖全部 39 个 EventType（对齐 orca/schema/event.py EventType Literal）。每条只做派生：
// 改 status/nodes/gate/cost——不拼接（保证幂等：同事件 N 次应用结果一致）。
//
// PRECONDITION: handler 只由 foldEvent 调用——幂等靠 store 顶层 seq 去重保证，handler
// 自身不做去重（cost 累加依赖此前提）。
//
// D8：unknown_event / agent_step_started 在 reducer 层 MUST no-op（绝不投影 RunState/视图）。
type Handler = (
  state: FoldDraft,
  data: Record<string, unknown>,
  event: WebEvent
) => void;

type FoldDraft = {
  nodes: Record<string, NodeState>;
  gate: GateState | null;
  lastResolved: LastResolved | null;
  workflowName: string;
  status: WorkflowStatus;
  cost: number;
  workflowDef: WorkflowTopology | null;
  workflowStartedAt: number | null;
  workflowElapsed: number | null;
  reasoningTokens: number;
  lastSeqSeen: number;
  // 注：nodesIndex 不在 FoldDraft —— 它由 indexConversationEvent 维护（refold /
  // processEvent 调用），不进 handler 表（handlers 只算 nodes/gate/cost 等核心派生）。
};

// node-level helper：确保 node 槽存在并 merge patch（last-writer-wins 幂等）。
function patchNode(
  nodes: Record<string, NodeState>,
  name: string,
  patch: Partial<NodeState>
): void {
  const cur = nodes[name];
  nodes[name] = cur ? { ...cur, ...patch } : { status: "pending", ...patch };
}

// ── nodesIndex 维护（SPEC §P2 / P0-6）──────────────────────────────────────────
// 倒排索引：每 node → { sessions, sessionEventCounts, sessionFirstTs }。仅统计
// CONVERSATION_TYPES 事件（与 selectConversation 输出集对齐；过程事件 count 一致）。
//
// **null session_id → "main"**（SPEC §P2 接口契约）。workflow_failed 特例：top-level
// e.node 为 null，但 data.node 是责任 node → 索引到 data.node（与 selectConversation 一致）。
const MAIN_SESSION = "main";

/**
 * 增量 patch nodesIndex：把单条 conversation 事件计入索引（refold / 增量 fold 共用）。
 *
 * 幂等性靠上层 seq 去重保证（同 seq 事件不会被 fold 两次）；本函数本身是「+1 计数」非幂等。
 *
 * @param index mutable nodesIndex（immer draft 或 fresh object）
 * @param event 必须是 CONVERSATION_TYPES 事件；非此集合应跳过（调用方判断）
 */
function indexConversationEvent(
  index: Record<string, NodeSessionIndex>,
  event: WebEvent
): void {
  // 目标 node：优先 e.node；workflow_failed 以 data.node 关联（eventMatchesNode 同语义，
  // 但此处需取出 targetNode 字符串做索引 key，不能直接用 boolean helper）
  let targetNode = event.node;
  if (!targetNode && event.type === "workflow_failed") {
    const dn = event.data?.node;
    if (typeof dn === "string") targetNode = dn;
  }
  if (!targetNode) return; // workflow 级无 node 事件不索引（不属于任何 agent）
  const sid = event.session_id ?? MAIN_SESSION;
  let entry = index[targetNode];
  if (!entry) {
    entry = {
      sessions: [],
      sessionEventCounts: {},
      sessionFirstTs: {},
    };
    index[targetNode] = entry;
  }
  if (!(sid in entry.sessionEventCounts)) {
    entry.sessions.push(sid);
    entry.sessionEventCounts[sid] = 0;
    entry.sessionFirstTs[sid] = event.timestamp;
  }
  entry.sessionEventCounts[sid] += 1;
  // firstTs 保持首次写入（refold 按 seq 升序 fold → 首次 = 最早；增量 in-order 也最早）
}

const eventHandlers: Record<EventType, Handler> = {
  // ── workflow 生命周期 ──
  workflow_started: (s, d, e) => {
    s.status = "running";
    s.workflowName = String(d.workflow_name ?? "");
    s.workflowStartedAt = e.timestamp;
    s.workflowElapsed = null;
    const topo = d.topology;
    if (topo && typeof topo === "object" && Array.isArray((topo as Record<string, unknown>).nodes)) {
      s.workflowDef = topo as unknown as WorkflowTopology;
    } else if (topo !== undefined) {
      // fail loud：topology 字段存在但 shape 异常 = 后端契约违背，warn（不静默吞）
      console.warn(
        `[orca] workflow_started.data.topology shape 异常 (seq=${e.seq})`,
        topo
      );
    }
  },
  workflow_completed: (s, d) => {
    s.status = "completed";
    const elapsed = Number(d.elapsed);
    if (Number.isFinite(elapsed)) s.workflowElapsed = elapsed;
  },
  workflow_failed: (s) => {
    s.status = "failed";
  },
  workflow_cancelled: (s) => {
    s.status = "cancelled";
  },
  workflow_resumed: (s) => {
    s.status = "running";
  },

  // ── node 生命周期（last-writer-wins，幂等）──
  node_started: (s, _d, e) => {
    if (!e.node) return;
    patchNode(s.nodes, e.node, { status: "running", startedAt: e.timestamp });
  },
  node_completed: (s, d, e) => {
    if (!e.node) return;
    const elapsed = Number(d.elapsed);
    patchNode(s.nodes, e.node, {
      status: "done",
      output: d.output,
      elapsed: Number.isFinite(elapsed) ? elapsed : undefined,
    });
  },
  node_failed: (s, _d, e) => {
    if (!e.node) return;
    patchNode(s.nodes, e.node, { status: "failed" });
  },
  node_skipped: (s, _d, e) => {
    if (!e.node) return;
    patchNode(s.nodes, e.node, { status: "skipped" });
  },

  // ── agent 流式 ──
  agent_usage: (s, d, e) => {
    // D8: usage 仅聚合 cost + reasoning_tokens（不进 conversation）。幂等靠 seq 去重保证。
    const c = Number(d.cost_usd ?? 0);
    if (Number.isFinite(c)) s.cost += c;
    const rt = Number(d.reasoning_tokens ?? 0);
    if (Number.isFinite(rt)) s.reasoningTokens += rt;
    // per-node tokens 累计（用于 AgentsRail token 小字，SPEC §5.2）。**单一真相 = tape**：
    // 此处只是 fold 派生，不在 selector 内二次重算（避免双重真相）。
    if (e.node) {
      const cur = s.nodes[e.node];
      const inT = Number(d.input_tokens ?? 0);
      const outT = Number(d.output_tokens ?? 0);
      const rtT = Number(d.reasoning_tokens ?? 0);
      patchNode(s.nodes, e.node, {
        inputTokens: (cur?.inputTokens ?? 0) + (Number.isFinite(inT) ? inT : 0),
        outputTokens: (cur?.outputTokens ?? 0) + (Number.isFinite(outT) ? outT : 0),
        reasoningTokens:
          (cur?.reasoningTokens ?? 0) + (Number.isFinite(rtT) ? rtT : 0),
      });
    }
  },
  agent_message: () => {},
  agent_thinking: () => {},
  agent_tool_call: () => {},
  agent_tool_result: () => {},
  agent_step_started: () => {
    // D8: reducer no-op（仅 liveness 心跳，LogStream 渲染）。
  },

  // ── 路由 ──
  route_taken: () => {},

  // ── 并发（foreach）──
  foreach_started: (s, d, e) => {
    if (!e.node) return;
    const total = Number(d.item_count ?? 0);
    patchNode(s.nodes, e.node, { status: "running", progress: `0/${total}` });
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
    const cur = s.nodes[e.node];
    patchNode(s.nodes, e.node, { status: "done", progress: cur?.progress });
  },

  // ── HMIL gate ──
  human_decision_requested: (s, d, e) => {
    const gate_id = String(d.gate_id ?? "");
    if (!gate_id) {
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
  human_decision_resolved: (s, d, e) => {
    // gate_id 匹配检查（三通道竞速防误关新 gate）：迟到的 resolved（gate_id 不匹配当前
    // 活跃 gate）→ warn 不关；仅当 gate_id 匹配或当前无活跃 gate（容忍）时才清。
    const resolvedId = String(d.gate_id ?? "");
    if (
      s.gate &&
      resolvedId &&
      s.gate.gate_id !== resolvedId &&
      s.gate.gate_id !== ""
    ) {
      console.warn(
        `[orca] human_decision_resolved gate_id=${resolvedId} 不匹配当前活跃 gate=${s.gate.gate_id} (seq=${e.seq})，忽略`
      );
      return;
    }
    s.gate = null;
    s.lastResolved = {
      by: String(d.resolved_by ?? ""),
      answer: String(d.answer ?? ""),
    };
  },

  // ── interrupt / prompt / retry / wait / validator / dialog / custom / error ──
  // 这些 type 不进 store 派生（LogStream / selectConversation 渲染时直接读 events）。
  // handler 表显式 no-op 而非缺失——fail loud：未知 type 在 foldEvent 走 warn 分支。
  interrupt_requested: () => {},
  interrupt_resolved: () => {},
  prompt_rendered: () => {},
  retry_started: () => {},
  retry_succeeded: () => {},
  retry_exhausted: () => {},
  wait_started: () => {},
  wait_completed: () => {},
  validator_started: () => {},
  validator_passed: () => {},
  validator_failed: () => {},
  dialog_started: () => {},
  dialog_message: () => {},
  dialog_ended: () => {},
  custom: () => {},
  error: () => {},
  unknown_event: () => {
    // D8: reducer MUST no-op（仅 LogStream 渲染，绝不投影 RunState/视图真相）。
  },
};

// ── fold 核心 ──────────────────────────────────────────────────────────────
// 唯一状态计算路径；reducer 幂等（依赖顶层 seq 去重）。
function foldEvent(state: FoldDraft, event: WebEvent): void {
  const handler = eventHandlers[event.type];
  if (!handler) {
    // 不可达：eventHandlers 是 Record<EventType, Handler>，TS 编译期穷尽性已保证。
    // 运行时若到这里说明 events.ts 与 handler 表 drift（codegen 应已拦）。
    console.warn(
      `[orca] event handler 表缺 type="${event.type}" (seq=${event.seq})——events.ts 与 store drift？`
    );
    return;
  }
  try {
    handler(state, event.data ?? {}, event);
  } catch (err) {
    console.error(
      `[orca] event handler 抛异常 type=${event.type} seq=${event.seq}`,
      err
    );
  }
}

/**
 * 全量 refold：从 events 数组（按 seq 升序）重算全部派生字段。
 *
 * D7 核心保证：state 永远 = ``fold(sorted(events))``。无论 events 到达顺序如何，
 * 最终派生态一致（ChartsView(T)==ChartsView(sort(T))==ChartsView(reverse(T))）。
 *
 * 实现策略：handlers **必须**在 seq 升序上应用（node_started 之前不能跑 node_completed），
 * 故 out-of-order 到达时不能增量 fold——必须从 sorted events 全量重 fold。
 *
 * 性能：每次 processEvent 触发 refold → O(N) 派生 + O(N log N) sort（仅 out-of-order 时）。
 * 1000 事件下 ~10k ops/事件，可接受；P2 引入 in-order 增量 fold 后，WS 常态 in-order
 * 到达的事件不再触发 refold（仅 out-of-order / loadEarlierChunk 触发）。
 */
function refold(state: WorkflowState): void {
  // 重置派生（保留 UI 交互态 selectedNode / selectedSession / activeRunId / events 数组本身）
  state.nodes = {};
  state.gate = null;
  state.lastResolved = null;
  state.workflowName = "";
  state.status = "idle";
  state.cost = 0;
  state.workflowDef = null;
  state.workflowStartedAt = null;
  state.workflowElapsed = null;
  state.reasoningTokens = 0;
  state.lastSeqSeen = 0;
  state.nodesIndex = {};
  // 在 draft 上逐条 fold（events 已 sort，故按数组顺序 apply 即 seq 升序）
  for (const e of state.events) {
    foldEvent(state, e);
    if (e.seq > state.lastSeqSeen) state.lastSeqSeen = e.seq;
    // nodesIndex 维护（P0-6 四路径之一：refold 全量重建）
    if (CONVERSATION_TYPES.has(e.type)) {
      indexConversationEvent(state.nodesIndex, e);
    }
  }
}

/** 把派生字段重置到初始（DRY：loadFromEvents / unloadRun 共用）。 */
function resetDerived(s: WorkflowState): void {
  s.nodes = {};
  s.gate = null;
  s.lastResolved = null;
  s.workflowName = "";
  s.status = "idle";
  s.cost = 0;
  s.workflowDef = null;
  s.workflowStartedAt = null;
  s.workflowElapsed = null;
  s.reasoningTokens = 0;
  s.lastSeqSeen = 0;
  s.nodesIndex = {};
}

/** 单 store（铁律 4：全前端唯一 create()）。 */
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
    workflowStartedAt: null,
    workflowElapsed: null,
    reasoningTokens: 0,
    lastSeqSeen: 0,
    nodesIndex: {},

    selectedNode: null,
    selectedSession: null,
    activeRunId: null,

    // huge-mode + writable（SPEC web-attach §3 / M3）
    serverOverview: null,
    writable: true,
    huge: false,
    oldestSeqInWindow: 0,
    newestSeqInWindow: 0,
    hugeFullyLoaded: true, // 非 huge 模式视同已 full load

    processEvent: (event) => {
      set((state) => {
        // ── 幂等 guard：同 seq 已存在则跳过 ──
        if (state.events.some((e) => e.seq === event.seq)) {
          return;
        }

        // P0-5 轻量增量 fold（SPEC §P2 方案 3）：WS 常态 in-order 到达 → 只 fold 新事件，
        // patch nodes/nodesIndex，不全量 refold。out-of-order（WS resume 重放乱序 /
        // loadEarlierChunk prepend 历史）→ 既有全量 refold。
        //
        // D7 幂等不变：in-order 增量是 seq 升序 fold 的特例（handlers 在升序上 apply），
        // 单测证等价（test/store.test.ts）。
        //
        // 注：``state.events`` 已是 seq 升序数组（refold/loadFromEvents 后保持）。
        // 若 ``event.seq > lastSeqSeen`` → event 是新最大值 → push 到末尾仍保升序，无需 sort。
        if (event.seq > state.lastSeqSeen) {
          state.events.push(event);
          foldEvent(state, event);
          state.lastSeqSeen = event.seq;
          // nodesIndex 增量 patch（P0-6 四路径之一：in-order 增量）
          if (CONVERSATION_TYPES.has(event.type)) {
            indexConversationEvent(state.nodesIndex, event);
          }
        } else {
          // out-of-order：插入 + sort + 全量 refold（含 nodesIndex 重建）
          state.events.push(event);
          state.events.sort((a, b) => a.seq - b.seq);
          refold(state);
        }
      });
    },

    loadFromEvents: (events) => {
      // 重置 events 数组 → sort + refold（D7：序无关）。
      set((state) => {
        state.events = [...events].sort((a, b) => a.seq - b.seq);
        refold(state);
      });
    },

    loadRun: async (runId) => {
      try {
        const resp = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`);
        if (!resp.ok) {
          console.error(`[orca] loadRun ${runId} 失败 HTTP ${resp.status}`);
          return;
        }
        const events = (await resp.json()) as WebEvent[];
        set((state) => {
          state.activeRunId = runId;
        });
        get().loadFromEvents(events);
      } catch (err) {
        console.error(`[orca] loadRun ${runId} 网络错误`, err);
      }
    },

    /**
     * SPEC web-attach §3 huge-mode 入口。先 GET /meta → 判 huge 分支。
     *
     * - **非 huge**：GET /events 全量 → loadFromEvents（同 loadRun）。
     * - **huge**：GET /events?tail=500 → loadFromEvents（tail fold 进 Conversation/Log）+
     *   设 serverOverview（overview selectors 信任服务端 fold，M3/M4）+ 记 oldest/newest
     *   窗口边界（供 IntersectionObserver ``loadEarlierChunk`` 增量 prepend 用）。
     * - ``writable=false``（attached run）：selectors 仍按 fold 工作，gate 模态据此禁提交。
     */
    loadRunWithMeta: async (runId) => {
      let meta: RunMetaExtended | null = null;
      try {
        const mresp = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/meta`
        );
        if (mresp.ok) meta = (await mresp.json()) as RunMetaExtended;
      } catch (err) {
        console.warn(`[orca] loadRunWithMeta ${runId} /meta 失败，回退 full`, err);
      }
      if (meta && meta.huge) {
        // huge 模式：拉 tail=500 + 设 serverOverview
        try {
          const resp = await fetch(
            `/api/runs/${encodeURIComponent(runId)}/events?tail=500`
          );
          if (!resp.ok) {
            console.error(
              `[orca] loadRunWithMeta huge ${runId} tail 拉取失败 HTTP ${resp.status}`
            );
            return;
          }
          const tail = (await resp.json()) as WebEvent[];
          set((state) => {
            state.activeRunId = runId;
            state.huge = true;
            state.hugeFullyLoaded = false;
            state.serverOverview = meta!.overview ?? null;
            state.writable = meta!.writable;
            state.oldestSeqInWindow =
              tail.length > 0 ? tail[0].seq : meta!.oldest_seq;
            state.newestSeqInWindow =
              tail.length > 0 ? tail[tail.length - 1].seq : meta!.newest_seq;
          });
          get().loadFromEvents(tail);
        } catch (err) {
          console.error(`[orca] loadRunWithMeta huge ${runId} 网络错误`, err);
        }
        return;
      }
      // 非 huge（或 /meta 失败回退）：原有 loadRun 全量路径
      set((state) => {
        state.huge = false;
        state.hugeFullyLoaded = true;
        state.serverOverview = null;
        state.writable = meta?.writable ?? true;
      });
      await get().loadRun(runId);
    },

    /**
     * huge 模式增量 prepend：fetch ``?since=max(0, oldest-M)&limit=M`` → 与既有 events
     * 合并 fold（O(window)，不重算全 tape）。返回 true 表示拉到新事件（窗口向上扩展）。
     *
     * 到达 ``oldest_seq == 1`` 时返回 false（已到顶，无更多历史）。
     */
    loadEarlierChunk: async (runId, chunkSize) => {
      const state0 = get();
      if (!state0.huge) return false;
      if (state0.oldestSeqInWindow <= 1) return false; // 已到顶
      const since = Math.max(0, state0.oldestSeqInWindow - 1 - chunkSize);
      try {
        const resp = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/events?since=${since}&limit=${chunkSize}`
        );
        if (!resp.ok) {
          console.warn(
            `[orca] loadEarlierChunk ${runId} HTTP ${resp.status}（忽略）`
          );
          return false;
        }
        const chunk = (await resp.json()) as WebEvent[];
        if (chunk.length === 0) return false;
        set((state) => {
          // 合并：旧 events + chunk（processEvent 内部 seq 去重，安全）
          const merged = [...state.events, ...chunk];
          merged.sort((a, b) => a.seq - b.seq);
          state.events = merged;
          refold(state);
          state.oldestSeqInWindow = Math.min(
            state.oldestSeqInWindow,
            chunk[0].seq
          );
        });
        return true;
      } catch (err) {
        console.warn(`[orca] loadEarlierChunk ${runId} 网络错误`, err);
        return false;
      }
    },

    /**
     * huge 模式 ``load full``：拉全量 events → client-fold + clear serverOverview（M4：
     * 客户端可经此校验服务端 overview 派生与 client-fold 一致）。``hugeFullyLoaded=true``。
     */
    loadFull: async (runId) => {
      try {
        const resp = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/events`
        );
        if (!resp.ok) {
          console.error(`[orca] loadFull ${runId} HTTP ${resp.status}`);
          return;
        }
        const events = (await resp.json()) as WebEvent[];
        set((state) => {
          state.hugeFullyLoaded = true;
          state.serverOverview = null; // clear → selectors 回退 client-fold（M4 可验）
        });
        get().loadFromEvents(events);
      } catch (err) {
        console.error(`[orca] loadFull ${runId} 网络错误`, err);
      }
    },

    unloadRun: () => {
      set((state) => {
        state.activeRunId = null;
        state.selectedNode = null;
        state.selectedSession = null;
        resetDerived(state);
        state.events = [];
        // huge-mode 状态清空（避免下一 run 残留）
        state.serverOverview = null;
        state.writable = true;
        state.huge = false;
        state.oldestSeqInWindow = 0;
        state.newestSeqInWindow = 0;
        state.hugeFullyLoaded = true;
      });
    },

    setSelectedNode: (node) =>
      set((state) => {
        state.selectedNode = node;
        // SPEC §P2 P1-3 联动：selectedSession = 该 node 第一个 sub session（依赖 nodesIndex）；
        // 无 sub → "all"；node=null → null。让 ConversationView 默认显示单个 sub（症状 #3/#5
        // 缓解：buildEntries 输入 ~208 而非 4224）。
        if (node === null) {
          state.selectedSession = null;
          return;
        }
        const idx = state.nodesIndex[node];
        if (!idx) {
          state.selectedSession = "all";
          return;
        }
        // sessions 已按首事件 seq 升序；跳过 "main"，第一个非 main 即最旧 sub（稳定默认）
        const firstSub = idx.sessions.find((s) => s !== MAIN_SESSION);
        state.selectedSession = firstSub ?? "all";
      }),

    setSelectedSession: (sid) =>
      set((state) => {
        state.selectedSession = sid;
      }),
  }))
);

// 导出 handler 表 keys 给测试断言（覆盖全部 EventType）
export const HANDLED_EVENT_TYPES = Object.keys(eventHandlers);
