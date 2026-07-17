// selectors.ts —— 纯函数 selector：state → view model（SPEC §3.1 / §0 D2 / D7）。
//
// 铁律：selector 是 **唯一** view 输入。组件不直接读 store.events 做派生（避免 N 处
// fold 漂移）。所有视图真相从这里出。
//
// D2 conversation 分组键 = node；retry/foreach 多 session_id 在同 node 内合并（细分隔符）。
// D7 seq 升序 fold：selectCharts(T)==selectCharts(sort(T))==selectCharts(reverse(T))。
//
// 这些 selector 输出不可变快照（每次调用新建结构），调用方应 useCallback/useMemo
// 避免每 render 重算。

import type { WebEvent } from "@/types/events";
import type { NodeState } from "@/types/store-types";
import type { WorkflowState } from "@/stores/workflow-store";
import { CONVERSATION_TYPES, eventMatchesNode } from "@/conversation-types";

// ── selectAgents：DAG nodes → AgentsRail 行模型 ─────────────────────────────────
// P3：sessionCount（子 agent session 数，不含 "main" 哨兵）+ iteration（Loop 组迭代号）
// 依赖 P2 nodesIndex 倒排索引。见 selectAgentGroups。
export interface AgentRow {
  node: string;
  status: NodeState["status"];
  progress?: string;
  elapsed?: number;
  startedAt?: number;
  inputTokens?: number;
  outputTokens?: number;
  reasoningTokens?: number;
  /** P3：子 agent session 数（不含 "main"；依赖 nodesIndex）。> 1 → UI 折叠子 session。 */
  sessionCount?: number;
  /** P3：循环节点迭代号（= sessionCount；仅 Loop 组派生，selectAgentGroups 设）。 */
  iteration?: number;
}

/** "main" session 哨兵字面量（与 workflow-store.MAIN_SESSION 同义；不跨层 import store 内部常量）。 */
const MAIN_SESSION = "main";

/**
 * 格式化 token 小字（ AgentsRail / TopBar 用，DRY）。
 * 优先 ``in/out`` 折叠为 ``1.2k`` 风格；无 token → undefined。
 */
export function formatTokens(input?: number, output?: number): string | undefined {
  if (input === undefined && output === undefined) return undefined;
  const fmt = (n: number | undefined): string => {
    if (n === undefined) return "0";
    if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
    return String(n);
  };
  return `${fmt(input)}/${fmt(output)}`;
}

/**
 * 格式化秒数为 ``Ns`` / ``Nm Ns``（AgentsRail / TopBar 共用，DRY）。
 *
 * @param seconds 秒数（float 可）
 * @param precision ``"tenths"`` → ``30.4s``（TopBar 高精度）；``"seconds"`` → ``30s``（AgentsRail 紧凑）
 */
export function formatElapsed(
  seconds: number,
  precision: "tenths" | "seconds" = "seconds"
): string {
  if (seconds < 60) {
    return precision === "tenths"
      ? `${seconds.toFixed(1)}s`
      : `${seconds.toFixed(0)}s`;
  }
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

export function selectAgents(state: WorkflowState): AgentRow[] {
  // SPEC web-attach §3 / M3：huge 模式下若 ``serverOverview`` 在（尚未 ``load full``），
  // overview 优先（信任服务端 fold）；否则 client-fold（同 v2）。``loadFull`` 清
  // ``serverOverview`` → 此分支自然回退到 client-fold（M4 可验）。
  if (state.huge && state.serverOverview && !state.hugeFullyLoaded) {
    return state.serverOverview.agents.map((a) => ({
      node: a.name,
      status: (a.status as NodeState["status"]) ?? "pending",
      elapsed: a.elapsed,
    }));
  }
  // 先从拓扑拿到全部 DAG 节点名（保持拓扑顺序；无拓扑时空），保证未启动的节点也以
  // pending 出现在 AgentsRail（SPEC §5.2：「每 agent」含未启动）。运行态 node 覆盖
  // pending 占位（同 node 名只保留 state.nodes 的实际状态）。
  const orderedNames: string[] = [];
  const seen = new Set<string>();
  if (state.workflowDef) {
    for (const n of state.workflowDef.nodes) {
      const name = typeof n.name === "string" ? n.name : "";
      if (!name || seen.has(name)) continue;
      orderedNames.push(name);
      seen.add(name);
    }
  }
  // state.nodes 内的运行态节点（拓扑未列的也补上 —— 防御 topology 缺漏）
  for (const name of Object.keys(state.nodes)) {
    if (!seen.has(name)) {
      orderedNames.push(name);
      seen.add(name);
    }
  }
  return orderedNames.map((node) => {
    const ns: NodeState | undefined = state.nodes[node];
    // P3 方案 6：子 agent session 数（不含 "main" 哨兵）。> 1 → UI 折叠子 session 列表。
    // `state.nodesIndex` 在 WorkflowState 必填，但本 selector 可被 partial-state cast 调用
    // （AgentsRail / 测试），故 optional chaining 兜底，缺索引 → undefined（不显折叠）。
    const idx = state.nodesIndex?.[node];
    const subSessionCount = idx
      ? idx.sessions.filter((s) => s !== MAIN_SESSION).length
      : 0;
    return {
      node,
      status: ns?.status ?? "pending",
      progress: ns?.progress,
      elapsed: ns?.elapsed,
      startedAt: ns?.startedAt,
      inputTokens: ns?.inputTokens,
      outputTokens: ns?.outputTokens,
      reasoningTokens: ns?.reasoningTokens,
      sessionCount: subSessionCount > 0 ? subSessionCount : undefined,
    };
  });
}

// ── selectAgentGroups：阶段分组（SPEC web-presentation-refinement §P3 方案 4 + P2-3）──
// 遍历 workflowDef.nodes 声明顺序，按 back-route 切分 Setup / Loop / Finalize 三组。
//
// **算法**（P2-3 闭环）：
//   - back-route = route 的 to 节点声明早于 from（to ∈ 已访问集 → 形成回边循环）。
//   - 收集所有 back-route → loop 区间 [min(to idx) .. max(from idx)]（声明 index）。
//   - Setup = nodes[0 .. loopStart-1]；Loop = nodes[loopStart .. loopEnd]；
//     Finalize = nodes[loopEnd+1 ..]。
//   - 无 back-route → 全平铺（单组 "Agents"，fallback 不破功能）。
//
// **e3b8ad oracle**（workflows/agent-struct-exploration.yaml）：
//   唯一 back-route = viz_round → hypothesizer →
//     Setup = [family_detect, baseline_measure]
//     Loop  = [hypothesizer, engineer, structure_gate, evaluator, analyst, curator, viz_round]
//     Finalize = [finalize, viz_finalize]
//
// **iteration**（P3 方案 5）：Loop 组 agent 设 iteration = sessionCount（UI 显示 R{N}，
// 从 selectNodeSessions distinct session 数派生，依赖 P2 nodesIndex）。
export interface AgentGroup {
  /** "Setup" | "Loop" | "Finalize" | "Agents"（无 back-route fallback 单组）。 */
  group: string;
  agents: AgentRow[];
}

export function selectAgentGroups(state: WorkflowState): AgentGroup[] {
  const agents = selectAgents(state);
  if (agents.length === 0) return [];
  const def = state.workflowDef;
  // 无拓扑 / 空拓扑 → 单组平铺（fallback，DRY：不重复 selectAgents 的 orderedNames 逻辑）
  if (!def || def.nodes.length === 0) {
    return [{ group: "Agents", agents }];
  }
  // 声明顺序 node 名 → index（与 selectAgents.orderedNames 同构；去重 + 跳过空名）
  const declIdx = new Map<string, number>();
  let n = 0;
  for (const node of def.nodes) {
    const name = typeof node.name === "string" ? node.name : "";
    if (!name || declIdx.has(name)) continue;
    declIdx.set(name, n++);
  }
  // 找 back-route：route.to 声明 index < route.from 声明 index（to 先声明 = 已访问）。
  // $end / 未知名不在 declIdx → 跳过（防御）。多个 back-route → 取最左 to / 最右 from 区间。
  let loopStart = Infinity;
  let loopEnd = -1;
  for (const r of def.routes) {
    const fi = declIdx.get(r.from);
    const ti = declIdx.get(r.to);
    if (fi === undefined || ti === undefined) continue;
    if (ti < fi) {
      if (ti < loopStart) loopStart = ti;
      if (fi > loopEnd) loopEnd = fi;
    }
  }
  // 无 back-route → 单组平铺（fallback）
  if (loopEnd === -1) {
    return [{ group: "Agents", agents }];
  }
  // 按声明 index 分桶（selectAgents 顺序 = 声明顺序 + nodes 兜底；兜底节点无 declIdx → Finalize 末尾）
  const setup: AgentRow[] = [];
  const loop: AgentRow[] = [];
  const finalize: AgentRow[] = [];
  for (const a of agents) {
    const idx = declIdx.get(a.node);
    if (idx === undefined) {
      finalize.push(a);
    } else if (idx < loopStart) {
      setup.push(a);
    } else if (idx <= loopEnd) {
      loop.push(a);
    } else {
      finalize.push(a);
    }
  }
  const groups: AgentGroup[] = [];
  if (setup.length) groups.push({ group: "Setup", agents: setup });
  if (loop.length) {
    // P3 方案 5：Loop 组 agent 派生 iteration = sessionCount（UI 显示 R{N}）
    groups.push({
      group: "Loop",
      agents: loop.map((a) =>
        a.iteration === undefined && a.sessionCount
          ? { ...a, iteration: a.sessionCount }
          : a
      ),
    });
  }
  if (finalize.length) groups.push({ group: "Finalize", agents: finalize });
  return groups;
}

// ── selectWorkflowElapsed / selectNodeElapsed / selectStall（SPEC §0 D5 / §6 D9）─
// D5：running 时 wall-clock tick（``now - startedAt``）；完成 snap 到 tape 的 elapsed
// 字段（``workflowElapsed`` / NodeState.elapsed），停 tick（防 wall-clock 成前端真相）。
// D9：current step 无新事件 > threshold（默认 5s）→ 琥珀「思考中 Ns」。

/**
 * workflow 级 elapsed（秒）。
 *
 * - completed：snap 到 ``workflowElapsed``（``workflow_completed.data.elapsed``，D5 权威）
 * - running：``now - workflowStartedAt``（live tick）
 * - failed/cancelled：tape 的终态事件 timestamp − workflowStartedAt
 *   （SPEC §0 D5 字面只点名 ``workflow_completed``，但 §5.1 TopBar AC 是 elapsed 语义，
 *    closed review 同意此扩展：failed/cancelled 也是终态需 snap。``workflowElapsed`` 只
 *    有 completed 写入；failed/cancelled 通过读 state.events 末条 workflow_* 事件 ts 推算，
 *    纯 tape 读不重派生 —— 符合铁律 1）
 * - idle/未启动：null
 */
export function selectWorkflowElapsed(
  state: WorkflowState,
  now: number
): number | null {
  // D5 权威 snap：workflow_completed.data.elapsed
  if (state.workflowElapsed !== null) return state.workflowElapsed;
  if (state.workflowStartedAt === null) return null;
  if (state.status === "running") {
    return Math.max(0, now - state.workflowStartedAt);
  }
  // failed/cancelled 终态：从 tape 末条 workflow_* 事件读 ts snap（无 wall-clock 漂移）
  if (state.status === "failed" || state.status === "cancelled") {
    const terminalTs = findWorkflowTerminalTs(state);
    if (terminalTs !== null) {
      return Math.max(0, terminalTs - state.workflowStartedAt);
    }
    // 理论不可达（failed/cancelled 必有对应事件）；fail loud 不静默返回 null
    console.warn(
      "[orca] selectWorkflowElapsed: status=" +
        state.status +
        " 但 tape 无 workflow_failed/cancelled 事件"
    );
    return null;
  }
  return null;
}

/** 在 events 里倒序找 workflow_failed/cancelled 的 timestamp（state.events 已 seq 升序）。 */
function findWorkflowTerminalTs(state: WorkflowState): number | null {
  for (let i = state.events.length - 1; i >= 0; i--) {
    const t = state.events[i].type;
    if (t === "workflow_failed" || t === "workflow_cancelled") {
      return state.events[i].timestamp;
    }
  }
  return null;
}

/**
 * 节点级 elapsed（秒）。
 * - 已完成（done/failed/skipped）：snap 到 NodeState.elapsed（D5）
 * - running：``now - startedAt``（live tick）
 * - 未启动：null
 */
export function selectNodeElapsed(
  state: WorkflowState,
  node: string,
  now: number
): number | null {
  const ns = state.nodes[node];
  if (!ns) return null;
  if (ns.status !== "running") return ns.elapsed ?? null;
  if (ns.startedAt == null) return null;
  return Math.max(0, now - ns.startedAt);
}

/** Stall 阈值默认值（ms）；可经 ``WEB_STALL_THRESHOLD_MS`` env 覆盖（SPEC §0 D9 / §6）。 */
export const DEFAULT_STALL_THRESHOLD_MS = 5000;

/**
 * 节点 stall 检测（SPEC §0 D9 / §6）。
 *
 * 当前 node 状态 running 且（``now`` − 该 node 最后事件 timestamp）> threshold → stalled。
 * 当 ``agent_thinking`` 事件在流（即该 node 最后事件是 agent_thinking）→ 更准确
 * （明确「思考中」）；否则仅 wall-clock 静默（opencode 块级，SPEC §6 诚实呈现）。
 *
 * **单位对齐**：``WebEvent.timestamp`` 是 Unix **秒**（``Date.now()/1000``）；本 selector
 * 的 ``now`` 入参也是秒。``thresholdMs`` / 返回的 ``sinceMs`` 是 **毫秒**（与 SPEC
 * ``WEB_STALL_THRESHOLD_MS`` 命名一致）；内部 ``(now - lastTs) * 1000`` 把秒差转 ms。
 *
 * 返回 null = 非 running / 无最后事件 / 未超阈值；否则返回 ``{ sinceMs, thinking }``。
 */
export function selectStall(
  state: WorkflowState,
  node: string,
  now: number,
  thresholdMs: number = DEFAULT_STALL_THRESHOLD_MS
): { sinceMs: number; thinking: boolean } | null {
  const ns = state.nodes[node];
  if (!ns || ns.status !== "running") return null;
  // 找该 node 最后一个事件的 timestamp（state.events 已 seq 升序 → 倒序找首条）
  let lastTs: number | null = null;
  let lastType: string | null = null;
  for (let i = state.events.length - 1; i >= 0; i--) {
    const e = state.events[i];
    if (e.node === node) {
      lastTs = e.timestamp;
      lastType = e.type;
      break;
    }
  }
  if (lastTs === null) return null;
  // now / lastTs 都是秒；转 ms 与 thresholdMs 比较。
  const sinceMs = Math.max(0, now - lastTs) * 1000;
  if (sinceMs <= thresholdMs) return null;
  return { sinceMs, thinking: lastType === "agent_thinking" };
}

// ── selectConversation：events → per-node 对话模型（D2 按 node 分组；P2 加 session 维度）─
// 输出按 seq 升序的事件分组（每 node 一个数组）。
//
// **P2 sessionId 参数**（SPEC web-presentation-refinement §P2 方案 1）：
//   - 省略 / ``"all"`` → 全 node 聚合（旧行为，零回归；用于"All"标签）
//   - 指定 sessionId → 仅该 session 事件（一次 buildEntries 处理 ~208 而非 4224，
//     缓解症状 #3/#5）
//
// **null session_id → "main"**（与 nodesIndex 一致）。
//
// **SPEC §P2 AC#2 偏离说明**：AC#2 prose 写「selectConversation 不再全量 filter（读 nodesIndex）」，
// 但同节接口契约 ``NodeSessionIndex`` 只存 count（无 sessionSeqs），且性能主因是 buildEntries
// 输入缩量（4224→208）而非 selectConversation 自身。本实现遵循接口契约：selectConversation
// 仍 filter state.events（O(N)，但 N=该 node 事件数，非全 tape），nodesIndex 只供
// selectNodeSessions 渲染会话选择器（O(sessions)）。如真机测发现 ~208 事件 filter 仍是瓶颈，
// 再扩 NodeSessionIndex 加 sessionSeqs（SPEC 方案 2 原始设计）。
//
// 「orphan tool_result」（无对应 call）在本 selector 不剔除——保留全部 conversation 相关
// 事件供视图分类；orphan 判定 + 剔除在视图层 ``buildEntries`` 内做（warn + 跳过）。
export interface ConversationGroup {
  node: string;
  events: WebEvent[];
}

/**
 * 选择所有应进 conversation 的事件，按 node（+ 可选 session）过滤，按 seq 升序。
 *
 * @param state 含 events + nodesIndex（sessionId 指定时仅作过滤参数，不读 nodesIndex）
 * @param nodeId 节点名（null/undefined → 空组）
 * @param sessionId 省略 / ``"all"`` → 全 node 聚合（旧行为）；具体 sessionId → 仅该 session
 */
export function selectConversation(
  state: WorkflowState,
  nodeId: string | null | undefined,
  sessionId?: string | "all" | null
): ConversationGroup {
  if (nodeId === undefined || nodeId === null) {
    return { node: "", events: [] };
  }
  // 仅取该 node 的 conversation-相关事件，按 seq 升序（state.events 已是 seq-sorted）。
  // SPEC §5.3：foreach_* / retry_* / interrupt_* / validator_* / wait_* 在 conversation 内
  // dim 渲染——故纳入 conversation 事件集（dim 是渲染层决定，本 selector 只输出事件流）。
  //
  // **workflow_failed** 特例：make_workflow_failed 在编排层把责任 node 写入 ``data.node``
  // （top-level ``e.node`` 仍为 null，对齐 schema/event.py 注释）。SPEC §5.3 要求它进
  // conversation 红 block —— 故同时按 top-level ``e.node`` 或 ``data.node`` 匹配。
  // 这是 tape 字段的合法读取（不是视图层重派生），符合铁律 1。
  const filterBySession = sessionId !== undefined && sessionId !== null && sessionId !== "all";
  const events = state.events.filter((e) => {
    if (!CONVERSATION_TYPES.has(e.type)) return false;
    // workflow_failed 可能以 data.node 关联（见上方注释 + eventMatchesNode 类型守门）
    if (!eventMatchesNode(e, nodeId)) return false;
    if (!filterBySession) return true;
    const eSid = e.session_id ?? "main";
    return eSid === sessionId;
  });
  return { node: nodeId, events };
}

// ── selectNodeSessions：从 nodesIndex 派生会话选择器行（SPEC §P2 / P0-6）──────────────
// 读 state.nodesIndex（store 维护的倒排索引），**不全量 filter state.events**——这是 P2
// 性能主入口之一（family_detect 4226 事件 → 65 session 索引查表 O(sessions)）。
//
// 输出按 sessions 数组顺序（= 首事件 seq 升序，store 维护此不变量）。
// label：``main`` 显式；其他 sessionId 截断前 10 字符（``ses_090e74…``）。
export interface NodeSessionRow {
  sessionId: string;
  /** 显示标签（main 或 ses_xxx 截断前 10 字符 + "…"）。 */
  label: string;
  /** 该 session 的 conversation 类事件数。 */
  eventCount: number;
  /** 该 session 首事件 timestamp（排序 / 调试用）。 */
  firstTs: number;
}

/** sessionId → 显示 label（main 哨兵 / 截断前 10 字符）。DRY：selector + 测试共用。 */
export function formatSessionLabel(sessionId: string): string {
  if (sessionId === "main") return "main";
  if (sessionId.length <= 10) return sessionId;
  return sessionId.slice(0, 10) + "…";
}

export function selectNodeSessions(
  state: WorkflowState,
  nodeId: string | null | undefined
): NodeSessionRow[] {
  if (!nodeId) return [];
  const idx = state.nodesIndex[nodeId];
  if (!idx) return [];
  return idx.sessions.map((sid) => ({
    sessionId: sid,
    label: formatSessionLabel(sid),
    eventCount: idx.sessionEventCounts[sid] ?? 0,
    firstTs: idx.sessionFirstTs[sid] ?? 0,
  }));
}

/**
 * 选择当前节点是否应显示 ▎ 流式光标（SPEC §5.3 闭 review #4）。
 *
 * IFF：
 *   1. ``state.status == "running"``（非 running 终态——completed/failed/cancelled/idle——
 *      都不显）
 *   2. 该 node 最后一个 conversation 事件是 ``agent_message`` / ``agent_thinking``
 *      （隐含：其后无 ``agent_tool_call`` / ``agent_tool_result`` / ``node_completed``
 *      ——若有，那些事件 seq 更大、会出现在末尾，从而取代 message/thinking 成为 last）
 *
 * 实现只看 last event：state.events 已 seq 升序，filter 后末尾就是 max-seq 事件。
 * 若末尾是 message/thinking → 其后必无 tool/result/node_completed（它们 seq 更大但
 * 没出现，说明未发生）。
 */
export function selectStreamingCursor(
  state: WorkflowState,
  nodeId: string | null | undefined
): boolean {
  if (state.status !== "running") return false;
  if (!nodeId) return false;
  const events = state.events;
  // 从末尾向前找该 node 的最后一条 conversation 事件（O(k)，k 通常小）
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    // workflow_failed 可能以 data.node 关联（eventMatchesNode 含类型守门，DRY）
    if (!eventMatchesNode(e, nodeId)) continue;
    if (!CONVERSATION_TYPES.has(e.type)) continue;
    return e.type === "agent_message" || e.type === "agent_thinking";
  }
  return false;
}

/** 进 conversation 的事件集合（DRY：selectConversation / selectStreamingCursor / store nodesIndex 共用）。 */
// CONVERSATION_TYPES 已移到 src/conversation-types.ts（P2：workflow-store 也需引用，避免 cycle）

// ── selectCharts：custom(kind=chart) → ChartsView（D3 / D7）──────────────────────
export interface ChartEntry {
  seq: number;
  node: string | null;
  /** 分组键 = data.label ?? "misc"。 */
  group: string;
  /** 组内身份 = data.title ?? chart_type+seq（同 identity upsert）。 */
  identity: string;
  /** 原始 chart payload（ChartPayload shape 由 chart/types.ts 定义）。 */
  payload: unknown;
  /** 原始事件 seq，用于 D7 sort 后的稳定身份消歧。 */
}

/** 提取单条 custom 事件的 chart payload（data.kind==="chart"）。非 chart → null。 */
function extractChartPayload(e: WebEvent): {
  chart: Record<string, unknown>;
} | null {
  const d = e.data;
  if (!d || d.kind !== "chart") return null;
  const chart = d.chart;
  if (!chart || typeof chart !== "object") return null;
  return { chart: chart as Record<string, unknown> };
}

/**
 * 选择所有 chart 事件 → 按 group 分组 + identity 去重（D7 seq 升序 fold）。
 *
 * 同 identity upsert：后到（更大 seq）覆盖前到。state.events 已 seq-sorted，故遍历即得
 * D7 序无关结果（selectCharts(T)==selectCharts(sort(T))==selectCharts(reverse(T))）。
 */
export function selectCharts(state: WorkflowState): {
  groups: { group: string; entries: ChartEntry[] }[];
} {
  // SPEC web-attach §3 / M3：huge 模式 + serverOverview → 信任服务端 fold（仅 label/title/
  // chart_type 清单，无完整 payload）→ 渲染为占位 entry（点击触发 ``loadFull`` 拉真实 payload）。
  // ``loadFull`` 后 serverOverview 清，回退 client-fold（M4 可验：与展开后一致）。
  if (state.huge && state.serverOverview && !state.hugeFullyLoaded) {
    const entries: ChartEntry[] = state.serverOverview.charts.map((c, i) => ({
      seq: -i - 1, // 负 seq 占位（避免与真实 seq 冲突；loadFull 后清）
      node: null,
      group: c.label || "misc",
      identity: c.title || `${c.chart_type}#${i}`,
      payload: {
        label: c.label,
        title: c.title,
        chart_type: c.chart_type,
      },
    }));
    const groupMap = new Map<string, ChartEntry[]>();
    for (const e of entries) {
      const arr = groupMap.get(e.group);
      if (arr) arr.push(e);
      else groupMap.set(e.group, [e]);
    }
    return {
      groups: Array.from(groupMap.entries()).map(([group, es]) => ({
        group,
        entries: es,
      })),
    };
  }
  // identity → entry（同 identity upsert，后到胜）
  const byIdentity = new Map<string, ChartEntry>();
  for (const e of state.events) {
    if (e.type !== "custom") continue;
    const extracted = extractChartPayload(e);
    if (!extracted) continue;
    const chart = extracted.chart;
    const label = typeof chart.label === "string" ? chart.label : "misc";
    const chartType = typeof chart.chart_type === "string" ? chart.chart_type : "chart";
    const title = typeof chart.title === "string" ? chart.title : "";
    const identity = title || `${chartType}#${e.seq}`;
    // D7 upsert：直接覆盖（后到 seq 更大胜；seq 升序遍历 → 最后写入 = max seq）
    byIdentity.set(identity, {
      seq: e.seq,
      node: e.node,
      group: label,
      identity,
      payload: chart,
    });
  }
  // 按 group 分组，保持首次插入顺序
  const groupMap = new Map<string, ChartEntry[]>();
  for (const entry of byIdentity.values()) {
    const arr = groupMap.get(entry.group);
    if (arr) arr.push(entry);
    else groupMap.set(entry.group, [entry]);
  }
  return { groups: Array.from(groupMap.entries()).map(([group, entries]) => ({ group, entries })) };
}

// ── selectLog：events → LogStream 行模型（仅生命周期/routing/gate/失败进 Log）─────
// SPEC web-presentation-refinement §P1：LogStream 装分级 classifier，过程事件（agent_*/
// foreach_item_*/prompt_rendered/agent_usage/custom/dialog_message/unknown_event）归
// ConversationView，不进 Log。debug 级（route_taken）默认隐藏，可用 setLogShowDebug(true) 展开。
export type LogLevel = "info" | "success" | "error" | "warning" | "debug";

export interface LogLine {
  seq: number;
  type: WebEvent["type"];
  text: string; // 单行摘要 ≤80 字符
  level: LogLevel; // 取代旧 isError：分级粒度更细，配色按 level
}

/**
 * 事件类型 → Log 级别分类器（纯函数，SPEC web-presentation-refinement §P1 分级表）。
 *
 * - 非 null：进 LogStream，按 level 配色（info/success/error/warning/debug）
 * - null：不进 Log（过程事件归 ConversationView，零回归）
 *
 * **穷尽守门**：switch 覆盖全 39 EventType，default 分支靠 TS ``never`` 编译期
 * 拦截（events.ts 加 type 没补这里 → 编译失败）；运行时兜底 console.warn + null
 * （不应触达，防御 unknown 运行时值）。
 *
 * 分级表（逐字对齐 SPEC §P1）：
 * | level    | EventType                                                                  |
 * |----------|----------------------------------------------------------------------------|
 * | info     | workflow_started / node_started / foreach_started / retry_started /         |
 * |          | validator_started / wait_started / human_decision_requested /              |
 * |          | interrupt_requested / dialog_started                                       |
 * | success  | workflow_completed / workflow_resumed / node_completed / foreach_completed |
 * |          | retry_succeeded / validator_passed / wait_completed /                      |
 * |          | human_decision_resolved / interrupt_resolved / dialog_ended                |
 * | error    | workflow_failed / workflow_cancelled / node_failed / retry_exhausted /     |
 * |          | validator_failed / error                                                   |
 * | warning  | node_skipped                                                               |
 * | debug    | route_taken（默认隐藏，可 setLogShowDebug(true) 展开）                     |
 * | null     | agent_message / agent_thinking / agent_tool_call / agent_tool_result /     |
 * |          | agent_step_started / foreach_item_started / foreach_item_completed /       |
 * |          | prompt_rendered / agent_usage / custom / dialog_message / unknown_event    |
 */
export function classifyLogLevel(type: WebEvent["type"]): LogLevel | null {
  switch (type) {
    // info：开始类生命周期
    case "workflow_started":
    case "node_started":
    case "foreach_started":
    case "retry_started":
    case "validator_started":
    case "wait_started":
    case "human_decision_requested":
    case "interrupt_requested":
    case "dialog_started":
      return "info";
    // success：完成类生命周期
    case "workflow_completed":
    case "workflow_resumed":
    case "node_completed":
    case "foreach_completed":
    case "retry_succeeded":
    case "validator_passed":
    case "wait_completed":
    case "human_decision_resolved":
    case "interrupt_resolved":
    case "dialog_ended":
      return "success";
    // error：失败类
    case "workflow_failed":
    case "workflow_cancelled":
    case "node_failed":
    case "retry_exhausted":
    case "validator_failed":
    case "error":
      return "error";
    // warning：跳过
    case "node_skipped":
      return "warning";
    // debug：路由（默认隐藏，SPEC 决策 3）
    case "route_taken":
      return "debug";
    // null：过程事件归 ConversationView，不进 Log
    case "agent_message":
    case "agent_thinking":
    case "agent_tool_call":
    case "agent_tool_result":
    case "agent_step_started":
    case "foreach_item_started":
    case "foreach_item_completed":
    case "prompt_rendered":
    case "agent_usage":
    case "custom":
    case "dialog_message":
    case "unknown_event":
      return null;
    default: {
      // TS 编译期穷尽守门：events.ts 加新 type 没补上面 case → 编译失败。
      const _exhaustive: never = type;
      // 运行时兜底（理论不可达；防御 unknown 运行时值）：fail loud，不静默吞。
      console.warn(
        `[orca] classifyLogLevel: unmapped event type ${String(_exhaustive)} → 降级为不进 Log`
      );
      return null;
    }
  }
}

/**
 * debug 级（route_taken）是否在 LogStream 显示。模块级状态 + setter（SPEC §P1：
 * 默认隐藏；保留可恢复开关，YAGNI 不接 UI，供未来调试/开关调用）。
 */
let showDebug = false;

/** 切换 debug 级可见性（默认 false 隐藏 route_taken）。供未来 UI 开关或调试调用。 */
export function setLogShowDebug(v: boolean): void {
  showDebug = v;
}

/** 单行摘要：每个 EventType 均有 readable 摘要，无 no-op fallback（SPEC §5.5 / §9 AC3）。 */
export function summarizeEvent(e: WebEvent): string {
  const d = e.data ?? {};
  const node = e.node ?? "-";
  const sess = e.session_id ? e.session_id.slice(0, 6) : "------";
  const detail = eventDetail(e.type, d);
  return `${node} [${sess}] ${detail}`.slice(0, 80);
}

function eventDetail(
  type: WebEvent["type"],
  d: Record<string, unknown>
): string {
  switch (type) {
    case "workflow_started":
      return `workflow ${str(d.workflow_name)} started`;
    case "workflow_completed":
      return `workflow completed (${num(d.elapsed)}s)`;
    case "workflow_failed":
      return `workflow FAILED: ${str(d.message)}`;
    case "workflow_cancelled":
      return `workflow cancelled (${str(d.reason)})`;
    case "workflow_resumed":
      return `workflow resumed (replayed ${num(d.replayed_events)})`;
    case "node_started":
      return `node started`;
    case "node_completed":
      return `node completed (${num(d.elapsed)}s)`;
    case "node_failed":
      return `node FAILED: ${str(d.message)}`;
    case "node_skipped":
      return `node skipped (${str(d.reason)})`;
    case "agent_message":
      return `msg: ${str(d.text).slice(0, 60)}`;
    case "agent_thinking":
      return `thinking: ${str(d.text).slice(0, 60)}`;
    case "agent_tool_call":
      return `tool_call: ${str(d.tool)}`;
    case "agent_tool_result":
      return `tool_result: ${str(d.tool_call_id)}`;
    case "agent_usage":
      return `usage: in=${num(d.input_tokens)} out=${num(d.output_tokens)} rt=${num(d.reasoning_tokens ?? 0)} $${num(d.cost_usd)}`;
    case "agent_step_started":
      return `step: ${str(d.step_reason)}`;
    case "route_taken":
      return `route: ${str(d.from)} → ${str(d.to)}`;
    case "foreach_started":
      return `foreach: ${num(d.item_count)} items`;
    case "foreach_item_started":
      return `foreach item[${num(d.index)}]`;
    case "foreach_item_completed":
      return `foreach item[${num(d.index)}] done`;
    case "foreach_completed":
      return `foreach done (${num(d.count)})`;
    case "human_decision_requested":
      return `GATE: ${str(d.prompt)}`;
    case "human_decision_resolved":
      return `gate resolved: ${str(d.answer)}`;
    case "interrupt_requested":
      return `interrupt requested (${str(d.source)})`;
    case "interrupt_resolved":
      return `interrupt resolved: ${str(d.action)}`;
    case "prompt_rendered":
      return `prompt rendered`;
    case "retry_started":
      return `retry ${num(d.attempt)}/${num(d.max_attempts)} (${str(d.kind)})`;
    case "retry_succeeded":
      return `retry succeeded (total ${num(d.attempt_total)})`;
    case "retry_exhausted":
      return `retry exhausted (${num(d.attempts)})`;
    case "wait_started":
      return `wait ${num(d.duration_seconds)}s (${str(d.reason)})`;
    case "wait_completed":
      return `wait done (${num(d.elapsed_seconds)}s)`;
    case "validator_started":
      return `validator started`;
    case "validator_passed":
      return `validator passed`;
    case "validator_failed":
      return `validator FAILED`;
    case "dialog_started":
      return `dialog started (${str(d.node)})`;
    case "dialog_message":
      return `dialog[${str(d.role)}]: ${str(d.text).slice(0, 50)}`;
    case "dialog_ended":
      return `dialog ended (${num(d.total_turns)} turns)`;
    case "custom":
      return `custom[${str(d.kind)}]`;
    case "error":
      return `ERROR: ${str(d.message)}`;
    case "unknown_event":
      return `? unknown (${str(d.source)})`;
    default: {
      // 穷尽性检查：TS 编译期保证所有 EventType 都有分支。运行时若到这里是 events.ts
      // 与本 switch drift——codegen drift guard 应已拦。fail loud：返回可读标识，不静默。
      const _exhaustive: never = type;
      return `? unmapped ${String(_exhaustive)}`;
    }
  }
}

function str(v: unknown): string {
  if (v === undefined || v === null) return "";
  return String(v);
}

function num(v: unknown): number {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
}

export function selectLog(state: WorkflowState): LogLine[] {
  // SPEC §P1：filter（classifyLogLevel 非 null）+ 默认隐藏 debug 级（route_taken）。
  // 一次遍历完成 filter + map，保留可恢复 debug 的能力（setLogShowDebug）。
  const out: LogLine[] = [];
  for (const e of state.events) {
    const level = classifyLogLevel(e.type);
    if (level === null) continue;
    if (level === "debug" && !showDebug) continue;
    out.push({
      seq: e.seq,
      type: e.type,
      text: summarizeEvent(e),
      level,
    });
  }
  return out;
}
