// stores/workflow-store.ts —— Zustand 单 store = fold(tape)（SPEC §3.1 / §0 D7）。
//
// 六条铁律对应：
//   - **单 store + 单 fold**（铁律 4 / SPEC §3.1）：全前端唯一 ``create()``。state = reducer(events)
//     在 **seq 升序**应用（D7 seq-sorted events array + ``seenSeqs: Set<number>`` O(1) 索引，
//     非 append-list）；tiebreaker max(seq) 胜（保 ChartsView(T)==ChartsView(sort(T))==ChartsView(reverse(T))）。
//   - **fold 幂等**（铁律 4 / §3.2.3）：seq 升序 apply + ``seenSeqs: Set<number>`` O(1)
//     去重（SPEC audit-c C4；**dev-mode ``__foldTwiceForInvariantCheck`` canary** 检测
//     累加型 handler 非幂等漂移，INV-2 显式承认当前 reducer 幂等靠调用方 seq 去重）。
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
import { enableMapSet } from "immer";
import type { EventType, WebEvent } from "@/types/events";
import type {
  GateState,
  LastResolved,
  LoadError,
  NodeSessionIndex,
  NodeState,
  RunMetaExtended,
  ServerOverview,
  WorkflowStatus,
} from "@/types/store-types";
import type { WorkflowTopology } from "@/types/topology";
import { CONVERSATION_TYPES, conversationTargetNode } from "@/conversation-types";
import { routeEdgeKey } from "@/route-edge";

// SPEC audit-c B1：immer 对 Set<number> draft 的支持必须在模块加载时启用一次。
// 否则 seenSeqs Set 的 add/has/new Set 反映不到 next state（静默失效）。
enableMapSet();

// ── 模块级 abort registry（SPEC audit-c §4.1 重试取消；Map 非 WeakMap，C3）──────────
// runId 是 string 不可能是 WeakMap 键；故 Map + 显式 delete（unloadRun / load 覆盖）。
interface InflightEntry {
  abort: AbortController;
  timer: ReturnType<typeof setTimeout> | null;
  epoch: number;
}
const inflightLoads = new Map<string, InflightEntry>();

// ── 模块级 moduleEpoch counter（SPEC audit-c C4，**非 store 字段**）──────────────────
// 防 A→B→A 同 runId 不同实例串话：第一次 load(A) 的迟到 fetch 在切回 A 后 resolve，
// 仅靠 activeRunId===A 校验会通过（同 runId），moduleEpoch 区分同 runId 不同实例。
let moduleEpoch = 0;

// ── 模块级 INV-7 warn-once Set（SPEC audit-c §3 INV-7 E6）──────────────────────────
// 记 `runId::seq`，drop 时 add + warn-once（同 seq 第二次 drop 不重复 warn）；unloadRun 清。
const droppedSeqs = new Set<string>();

// ── 模块级 untitled-chart warn-once Set（SPEC audit-c §4.2 MINOR-5）─────────────────
// partition 对无 title chart 跨多次 re-render 仅 warn 一次（防 spam）；unloadRun/unmount 清。
// 放在 store 模块以与 droppedSeqs 一致管理生命周期，ChartRenderer import 共享。
export const untitledChartWarned = new Set<string>();
export function _resetUntitledChartWarnings(): void {
  untitledChartWarned.clear();
}

/** iterate 整个 Map abort + clearTimeout + delete（SPEC audit-c C2/E9 abort-all-entries）。 */
function abortAllInflight(): void {
  for (const [, entry] of inflightLoads) {
    entry.abort.abort();
    if (entry.timer !== null) clearTimeout(entry.timer);
  }
  inflightLoads.clear();
}

/**
 * SPEC audit-c §4.1 fetchWithBackoff：3 次指数退避（1s/2s/4s）+ retryCount 进 store
 * 驱动 reactive banner（E4）+ 退避期 loadStatus 保持 loading（BLOCKER-3）。
 *
 * 每次 attempt（含 retry fetch）必带 `{signal}`（G2/C12），AbortController 来自当前
 * entry；切 run / unloadRun → abortAllInflight → in-flight fetch 立即 reject AbortError。
 *
 * @returns parsed JSON（array）或 throw LoadError（caller 写错误态）
 */
async function fetchEventsWithBackoff(
  runId: string,
  entry: InflightEntry,
  url: string,
  opts: { expectArray: boolean } = { expectArray: true }
): Promise<unknown> {
  const MAX_ATTEMPTS = 3;
  const backoffs = [1000, 2000, 4000];
  let lastError: LoadError | null = null;

  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      // 退避 timer 同步绑到 entry（C1），让 abort-all 能 clearTimeout pending 退避；
      // **监听 abort signal**——clearTimeout 不唤醒 await，必须 signal reject 才能让
      // loader 闭包释放（否则 loader 永久悬挂 + 闭包内存泄漏）。SPEC audit-c C1 round-5
      // 「entry.timer = timer」要求字面已落实，此 listener 补「await 必须能被 abort 唤醒」。
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, backoffs[attempt - 1]);
        entry.timer = t;
        entry.abort.signal.addEventListener(
          "abort",
          () => {
            clearTimeout(t);
            reject(new DOMException("aborted", "AbortError"));
          },
          { once: true }
        );
      });
      entry.timer = null;
      if (entry.abort.signal.aborted) {
        throw { kind: "network", message: "aborted" } as LoadError;
      }
      // retryCount 进 store 驱动 reactive banner（E4）
      useWorkflowStore.setState({ retryCount: attempt });
    }
    try {
      const resp = await fetch(url, { signal: entry.abort.signal });
      if (!resp.ok) {
        lastError = {
          kind: "http",
          status: resp.status,
          message: `HTTP ${resp.status}`,
        };
        if (attempt < MAX_ATTEMPTS - 1) {
          console.warn(
            `[orca] loadEvents ${runId} 失败 HTTP ${resp.status}，重试中 第 ${attempt + 1} 次`
          );
          continue;
        }
        throw lastError;
      }
      let parsed: unknown;
      try {
        parsed = await resp.json();
      } catch (err) {
        lastError = {
          kind: "parse",
          message: `resp.json() 解析失败: ${(err as Error).message ?? String(err)}`,
        };
        if (attempt < MAX_ATTEMPTS - 1) {
          console.warn(
            `[orca] loadEvents ${runId} parse 失败，重试中 第 ${attempt + 1} 次`
          );
          continue;
        }
        throw lastError;
      }
      if (opts.expectArray && !Array.isArray(parsed)) {
        lastError = {
          kind: "parse",
          message: `响应非 array (got ${typeof parsed})`,
        };
        if (attempt < MAX_ATTEMPTS - 1) {
          console.warn(
            `[orca] loadEvents ${runId} 响应非 array，重试中 第 ${attempt + 1} 次`
          );
          continue;
        }
        throw lastError;
      }
      return parsed;
    } catch (err) {
      // AbortError（来自 abort signal）→ 直接抛，不重试
      if ((err as Error)?.name === "AbortError") {
        throw { kind: "network", message: "aborted" } as LoadError;
      }
      // 已是 LoadError（http/parse）→ continue 或 throw
      if (
        err &&
        typeof err === "object" &&
        "kind" in err &&
        (err.kind === "http" || err.kind === "parse")
      ) {
        lastError = err as LoadError;
        if (attempt < MAX_ATTEMPTS - 1) continue;
        throw lastError;
      }
      // 真·network 错误（fetch reject）→ 退避重试
      lastError = {
        kind: "network",
        message: (err as Error)?.message ?? String(err),
      };
      if (attempt < MAX_ATTEMPTS - 1) {
        console.warn(
          `[orca] loadEvents ${runId} 网络错误，重试中 第 ${attempt + 1} 次：${lastError.message}`
        );
        continue;
      }
      throw lastError;
    }
  }
  // 不可达（for 循环必 throw 或 return），TS 收敛
  throw lastError ?? { kind: "network", message: "unreachable" };
}

// ── store state 形状（业务派生 + UI 交互态 + actions，SPEC §3.1）──────────────
export interface WorkflowState {
  // === 业务真相派生物（从 events fold）===
  /**
   * events 数组（升序）+ ``seenSeqs: Set<number>`` 索引（SPEC audit-c C4：O(1) 去重，
   * 替代旧 ``events.some`` O(N) 扫描；序无关不变式 D7 仍由 sort + refold 保证）。
   * **非 append-list**：插入即排序（fold 前 sort by seq 保证升序 apply）。fold 不依赖
   * 事件到达顺序，只依赖 seq 顺序。
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
  /**
   * 派生：route_taken 已走边 key 集合（SPEC 2026-08-28 C3.5；key 走 routeEdgeKey helper）。
   * refold / resetDerived / processEvent in-order 三路径一致维护；同 nodesIndex 模式
   * （索引维护不进 handler 表，route_taken handler 保持 no-op）。requires enableMapSet。
   */
  takenEdgeKeys: Set<string>;
  /**
   * SPEC audit-c §4.4：seq 去重 Set（O(1) 替代 events.some O(N) 扫描，INV-3 长 run 不卡）。
   * **重建收进 refold 末尾**（N1）：所有走 refold 路径（loadFromEvents / loadEarlierChunk /
   * loadFull）自动一致；in-order 增量分支显式 add。requires immer ``enableMapSet()``。
   */
  seenSeqs: Set<number>;

  // === Loader 错误态（SPEC audit-c §4.1；UI 交互态，非 fold 派生）===
  /**
   * ``idle``（首 mount，M18）/ ``loading``（fetch + 退避期）/ ``loaded`` / ``error``。
   * **退避期保持 loading**（BLOCKER-3）——不加 retrying，retryCount>0 叠加非阻塞 banner。
   */
  loadStatus: "idle" | "loading" | "loaded" | "error";
  /** loader 终态失败时写入（INV-1 fail loud，不再 console.error+return）。 */
  loadError: LoadError | null;
  /** 退避重试计数（进 store，E4：驱动 reactive retry-banner）。 */
  retryCount: number;
  /** loadEarlierChunk 失败单开关（M14；非 N 个 chunk 计数）。 */
  historyLoadError: boolean;

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
  /** 懒加载：GET /api/runs/<id>/events → _refoldAndCommit。SPEC audit-c：失败 fail loud（写 loadError + 退避重试）。 */
  loadRun: (runId: string) => Promise<void>;
  /**
   * SPEC web-attach §3 huge-mode 入口：先 GET /meta → 据 huge 置信息位。
   * - huge 与否都 GET /events 全量 → loadFromEvents（用户偏好：huge 不弹 gate，直接加载）。
   *   huge 标记仍置位（"大 run" 信息位），但 hugeFullyLoaded 恒 true → 占位/按钮分支不触发。
   *   loadEarlierChunk/loadFull 留作 huge-tail 场景预留能力（当前未接 UI）。
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
// PRECONDITION: handler 只由 foldEvent 调用——幂等靠 store 顶层 ``seenSeqs`` seq 去重保证，
// handler 自身不做去重（cost 累加依赖此前提）。SPEC audit-c §4.3 加 dev-mode canary
// （``__foldTwiceForInvariantCheck``）：对同一 event apply 两次比较派生快照，任何变化
// = handler 非幂等 = warn。**漂移 canary 非证明全幂等**（G3）；prod no-op（N3）。
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

// ── nodesIndex 维护（SPEC §P2 / P0-6 + 2026-08-28 C3.1 ev 索引）──────────────────
// 倒排索引：每 node → { sessions, sessionEventCounts, sessionFirstTs, ev }。仅统计
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
  // 目标 node：conversationTargetNode 唯一化（C3.2——与 selectConversation 严格同源，
  // e.node 优先、workflow_failed 按 data.node、双字段只归 e.node）
  const targetNode = conversationTargetNode(event);
  if (!targetNode) return; // workflow 级无 node 事件不索引（不属于任何 agent）
  const sid = event.session_id ?? MAIN_SESSION;
  let entry = index[targetNode];
  if (!entry) {
    entry = {
      sessions: [],
      sessionEventCounts: {},
      sessionFirstTs: {},
      ev: { all: [], bySession: {}, last: null },
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

  // C3.1 ev 索引：事件引用按 seq 升序 append（refold sort 序 / in-order 增量天然升序；
  // out-of-order 走 refold 全量重建）。last = 最新一条。
  entry.ev.all.push(event);
  const bucket = entry.ev.bySession[sid];
  if (bucket) bucket.push(event);
  else entry.ev.bySession[sid] = [event];
  entry.ev.last = event;
}

/**
 * 增量维护 takenEdgeKeys（SPEC 2026-08-28 C3.5）：route_taken 事件 → 边 key 入 Set。
 *
 * **同 nodesIndex 模式：索引维护不进 handler 表 / FoldDraft**（route_taken handler 保持
 * no-op）。派生语义与旧 WorkflowGraph 全量扫描逐字符等价：`String(e.data?.from ?? "")` +
 * `String(e.data?.to ?? "")` + `from && to` 守卫（缺任一 / 非 string → 不入集合）。
 *
 * 幂等性靠上层 seq 去重保证（Set.add 本身幂等，畸形事件重复也不变）。
 */
function indexRouteEvent(taken: Set<string>, event: WebEvent): void {
  const from = String(event.data?.from ?? "");
  const to = String(event.data?.to ?? "");
  if (from && to) taken.add(routeEdgeKey(from, to));
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
    const dataElapsed = Number(d.elapsed);
    const hasDataElapsed = d.elapsed != null && Number.isFinite(dataElapsed);
    // in-session 路径 node_completed.data 只含 output（无 elapsed）→ 用事件 timestamp
    // 差补算（node_completed.ts − node_started.ts，均 epoch 秒 → 差为真实耗时秒数）。
    // 标准 executor 路径优先取 data.elapsed（executor 用 time.monotonic 实测，更精确）。
    // 两路都无（缺 startedAt，如老 tape 重放）→ undefined，AgentsRail 不显示（fail loud 不撒谎）。
    const startedAt = s.nodes[e.node]?.startedAt;
    const tsElapsed =
      startedAt != null && e.timestamp != null
        ? Math.max(0, e.timestamp - startedAt)
        : undefined;
    patchNode(s.nodes, e.node, {
      status: "done",
      output: d.output,
      elapsed: hasDataElapsed ? dataElapsed : tsElapsed,
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
    } else {
      // SPEC audit-c M4：silent skip → warn（progress 形异常开发者可见）+ 保留原值
      console.warn(
        `[orca] foreach_item_completed progress 形异常 seq=${e.seq} progress="${cur.progress}"`
      );
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
  state.takenEdgeKeys = new Set();
  // 在 draft 上逐条 fold（events 已 sort，故按数组顺序 apply 即 seq 升序）
  for (const e of state.events) {
    foldEvent(state, e);
    if (e.seq > state.lastSeqSeen) state.lastSeqSeen = e.seq;
    // nodesIndex 维护（P0-6 四路径之一：refold 全量重建）
    if (CONVERSATION_TYPES.has(e.type)) {
      indexConversationEvent(state.nodesIndex, e);
    }
    // takenEdgeKeys 重建（C3.5：route_taken 不在 CONVERSATION_TYPES——独立判断，不并入上面分支）
    if (e.type === "route_taken") {
      indexRouteEvent(state.takenEdgeKeys, e);
    }
  }
  // SPEC audit-c N1：seenSeqs 重建收进 refold 末尾——所有走 refold 路径
  // （loadFromEvents / loadEarlierChunk / loadFull）自动一致。否则 loadEarlierChunk
  // 走 set+refold 但 seenSeqs 不动 → 后续 WS resume 推 chunk 区间重复事件 has 返 false
  // → events 数组重复。
  state.seenSeqs = new Set(state.events.map((e) => e.seq));
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
  s.takenEdgeKeys = new Set();
  s.seenSeqs = new Set();
}

// ── SPEC audit-c §4.5 _refoldAndCommit（E7 私有 helper，loader 共用）──────────────────
// loaders 把 fold 累积到单次 set 同时提交（activeRunId + events + 派生 + loadStatus="loaded"），
// **不**调 public loadFromEvents（E7：public loadFromEvents 是 triggerResumeFallback 专用，
// 不翻 loadStatus）。loadStatus 翻转与 events 写入在同一 set 内（INV-7 互补，N5/C12 原子性）。
//
// **caller 责任**：写时双重校验（``activeRunId===runId && moduleEpoch===myEpoch``）已在
// caller 端完成；本 helper 只负责单次原子提交。
function _refoldAndCommit(
  state: WorkflowState,
  runId: string,
  events: WebEvent[],
  patch: Partial<WorkflowState>
): void {
  state.events = [...events].sort((a, b) => a.seq - b.seq);
  refold(state); // 内部已重建 seenSeqs（N1）
  state.activeRunId = runId;
  for (const [k, v] of Object.entries(patch)) {
    // immer draft 上设任意字段（patch 仅含 huge/serverOverview/窗口边界/loadStatus 等）
    (state as unknown as Record<string, unknown>)[k] = v;
  }
  state.loadStatus = "loaded";
  state.loadError = null;
  state.retryCount = 0;
}

// ── SPEC audit-c §4.3 dev-mode 自检 helper（B3，C3 handler 表不变式 canary）──────────
// 仅 ``import.meta.env.DEV`` 启用，prod no-op（N3）。
// 语义：对同一 event 连续 apply 两次（强制绕过 seq 去重），比较前后 state 派生快照——
// 任何派生字段变化 = handler 非幂等 = warn。**漂移 canary，非证明全幂等**（G3）。
// C6.1①：snapshot 的 nodesIndex 是投影复制值（无 ev），非 store 原生 NodeSessionIndex。
// 入参兼容两种来源：store 原生 state（baseline）与投影 clone（s1）——投影只读
// sessions/sessionEventCounts/sessionFirstTs 三字段，两种输入都满足。
type CanarySnapshotState = Omit<WorkflowState, "nodesIndex"> & {
  nodesIndex: Record<
    string,
    Pick<
      NodeSessionIndex,
      "sessions" | "sessionEventCounts" | "sessionFirstTs"
    >
  >;
};

type FoldableSnapshot = Pick<
  CanarySnapshotState,
  | "nodes"
  | "gate"
  | "lastResolved"
  | "workflowName"
  | "status"
  | "cost"
  | "workflowDef"
  | "workflowStartedAt"
  | "workflowElapsed"
  | "reasoningTokens"
  | "lastSeqSeen"
  | "nodesIndex"
> & { nodesIndex: Record<string, Omit<NodeSessionIndex, "ev">> };

export function __foldTwiceForInvariantCheck(event: WebEvent): void {
  if (!import.meta.env.DEV) return; // prod no-op（N3）
  setImmediateSnapshot(__foldTwiceRun.bind(null, event));
}

function __foldTwiceRun(event: WebEvent): void {
  // 在临时 draft state 上 apply 两次，比较快照
  const baseline = useWorkflowStore.getState();
  const before = snapshot(baseline);
  // ②（SPEC 2026-08-28 C6.1）baseline clone 投影化：nodes 深拷贝（patchNode / foreach
  // progress 会写穿共享引用——**禁**共享引用剥离变体，会 mutate immer 冻结的 baseline）
  // + nodesIndex 复用①投影（不含 ev——否则 ev 引入后每事件把事件集序列化两份）。
  // 其余顶层字段 handler 只整体赋值，spread 浅拷贝即可；seenSeqs/takenEdgeKeys（Set）
  // 不进 handler 表，clone 不影响 fold 结果。
  const s1: CanarySnapshotState = {
    ...baseline,
    nodes: JSON.parse(JSON.stringify(baseline.nodes)),
    nodesIndex: projectNodesIndex(baseline.nodesIndex),
  };
  foldEvent(s1 as unknown as FoldDraft, event);
  // ③（C6.1）：删除两处 indexConversationEvent——其「+1 计数」非幂等属已知契约，
  // 索引等价性由 D7 测试守护（store.test.ts C3 describe）；此处保留调用反而使负例红灯。
  const after1 = snapshot(s1);
  // apply 第二次（绕过 seq 去重）
  foldEvent(s1 as unknown as FoldDraft, event);
  const after2 = snapshot(s1);
  if (!shallowEqSnapshot(after1, after2)) {
    console.warn(
      `[orca] handler 非幂等 canary：type=${event.type} seq=${event.seq} 两次 apply 派生不一致`,
      { before, after1, after2 }
    );
  }
  void before; // 抑制 unused
}

/**
 * ①（SPEC 2026-08-28 C6.1）nodesIndex **投影复制值**：sessions 数组拷贝 +
 * sessionEventCounts/sessionFirstTs 浅拷贝对象。必须复制值：snapshot 持引用时，二次
 * apply 后的原地 mutate 型漂移结构性不可见（检测力零增益）；**投影不含 ev**
 * （杜绝 ev.all 事件引用数组进 JSON 双快照 + 防未来接线回退，非「修复检测缺陷」）。
 */
function projectNodesIndex(
  index: Record<
    string,
    Pick<
      NodeSessionIndex,
      "sessions" | "sessionEventCounts" | "sessionFirstTs"
    >
  >
): Record<string, Omit<NodeSessionIndex, "ev">> {
  const out: Record<string, Omit<NodeSessionIndex, "ev">> = {};
  for (const [node, entry] of Object.entries(index)) {
    out[node] = {
      sessions: [...entry.sessions],
      sessionEventCounts: { ...entry.sessionEventCounts },
      sessionFirstTs: { ...entry.sessionFirstTs },
    };
  }
  return out;
}

function snapshot(s: CanarySnapshotState): FoldableSnapshot {
  return {
    nodes: s.nodes,
    gate: s.gate,
    lastResolved: s.lastResolved,
    workflowName: s.workflowName,
    status: s.status,
    cost: s.cost,
    workflowDef: s.workflowDef,
    workflowStartedAt: s.workflowStartedAt,
    workflowElapsed: s.workflowElapsed,
    reasoningTokens: s.reasoningTokens,
    lastSeqSeen: s.lastSeqSeen,
    nodesIndex: projectNodesIndex(s.nodesIndex),
  };
}

function shallowEqSnapshot(a: FoldableSnapshot, b: FoldableSnapshot): boolean {
  // nodesIndex / nodes 是嵌套对象——deep 比较用 JSON（canary 用，性能非关键）
  return JSON.stringify(a) === JSON.stringify(b);
}

// microtask 排程（避免同步调用污染当前 set 上下文）
function setImmediateSnapshot(fn: () => void): void {
  Promise.resolve().then(fn);
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
    takenEdgeKeys: new Set<string>(),
    seenSeqs: new Set<number>(),

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

    // SPEC audit-c §4.1：loader 错误态（UI 交互态，非 fold 派生，M19）
    loadStatus: "idle", // 初始 idle（M18）
    loadError: null,
    retryCount: 0,
    historyLoadError: false,

    processEvent: (event) => {
      set((state) => {
        // ── SPEC audit-c §3 INV-7：loadStatus !== "loaded" 拒收 WS 增量 ──
        // 防 loading/idle/error 期 WS 推事件污染部分 fold；drop + warn-once（E6）。
        // **defer-RESUME（E1 BLOCKER）**：loaded 后发 sendResume since=lastSeqSeen 由
        // server 重放补全窗口事件（不依赖 plain subscribe，subscribe forward-only）。
        if (state.loadStatus !== "loaded") {
          const key = `${state.activeRunId ?? "<null>"}::${event.seq}`;
          if (!droppedSeqs.has(key)) {
            droppedSeqs.add(key);
            console.warn(
              `[orca] INV-7 drop event seq=${event.seq} loadStatus=${state.loadStatus} run=${state.activeRunId}`
            );
          }
          return;
        }
        // ── 幂等 guard：seenSeqs O(1) 查（SPEC audit-c C4 替代 events.some O(N) 扫）──
        if (state.seenSeqs.has(event.seq)) {
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
          state.seenSeqs.add(event.seq); // O(1)（C4）
          // nodesIndex 增量 patch（P0-6 四路径之一：in-order 增量）
          if (CONVERSATION_TYPES.has(event.type)) {
            indexConversationEvent(state.nodesIndex, event);
          }
          // takenEdgeKeys 增量维护（C3.5：route_taken 独立判断，handler 保持 no-op）
          if (event.type === "route_taken") {
            indexRouteEvent(state.takenEdgeKeys, event);
          }
        } else {
          // out-of-order：插入 + sort + 全量 refold（含 nodesIndex + seenSeqs 重建）
          state.events.push(event);
          state.events.sort((a, b) => a.seq - b.seq);
          refold(state);
        }
      });
    },

    /**
     * 全量 refold 公共 action（D7：序无关）。
     *
     * **SPEC audit-c E7 显式不变量**：本 action 是 WS resume-fallback
     * （use-websocket ``triggerResumeFallback``）专用，**签名与 loadStatus 行为保持不变
     * （不 touch loadStatus）**——loaders 走私有 ``_refoldAndCommit`` helper，不调本 action。
     */
    loadFromEvents: (events) => {
      // 重置 events 数组 → sort + refold（D7：序无关）。
      set((state) => {
        state.events = [...events].sort((a, b) => a.seq - b.seq);
        refold(state);
      });
    },

    /**
     * SPEC audit-c §4.1：懒加载入口（fail loud）。
     *
     * - HTTP 非 200 / 网络错误 / parse 失败 → 3 次指数退避（1s/2s/4s）+ retryCount 进 store
     *   驱动 reactive banner；3 次失败翻 ``error`` 终态 + 写 loadError（INV-1/INV-4）。
     * - 退避期 loadStatus 保持 ``loading``（BLOCKER-3），retryCount>0 叠加 retry-banner。
     * - AbortController + moduleEpoch 双校验防 A→B→A 陈旧 fetch 污染（N2）。
     * - 原子提交：所有状态（activeRunId + events + 派生 + loadStatus）单 set 同时写（INV-4）。
     */
    loadRun: async (runId) => {
      // moduleEpoch++ BEFORE abortAllInflight（E9 钉死顺序，evaluator 已证顺序无关）
      moduleEpoch++;
      const myEpoch = moduleEpoch;
      abortAllInflight();
      const entry: InflightEntry = { abort: new AbortController(), timer: null, epoch: myEpoch };
      inflightLoads.set(runId, entry);
      // 入口 reset retryCount=0（E4，防 B 继承 A）+ historyLoadError（DRY，m2 对齐 loadRunWithMeta）
      set({ loadStatus: "loading", loadError: null, retryCount: 0, historyLoadError: false });

      try {
        const events = (await fetchEventsWithBackoff(
          runId,
          entry,
          `/api/runs/${encodeURIComponent(runId)}/events`
        )) as WebEvent[];
        // 写时双重校验（N2/C2）：activeRunId + moduleEpoch
        if (get().activeRunId !== null && get().activeRunId !== runId) return;
        if (moduleEpoch !== myEpoch) return;
        set((state) => {
          _refoldAndCommit(state, runId, events, {});
        });
      } catch (err) {
        writeLoadError(get, set, runId, myEpoch, err as LoadError);
      } finally {
        if (inflightLoads.get(runId)?.epoch === myEpoch) inflightLoads.delete(runId);
      }
    },

    /**
     * SPEC web-attach §3 huge-mode 入口。先 GET /meta → 据 meta.huge 置信息位。
     *
     * - **huge 与否皆全量**：GET /events → loadFromEvents + hugeFullyLoaded=true（用户偏好：
     *   huge 不弹「加载全部」gate，直接全量加载）。huge 标记据 meta 置位仅作"大 run"信息。
     * - ``/meta`` 失败 silent fallback（INV-1 qualifier M9）；full 也失败 → 错误态。
     * - ``writable=false``（attached run）：gate 模态禁提交。
     */
    loadRunWithMeta: async (runId) => {
      moduleEpoch++;
      const myEpoch = moduleEpoch;
      abortAllInflight();
      const entry: InflightEntry = { abort: new AbortController(), timer: null, epoch: myEpoch };
      inflightLoads.set(runId, entry);
      set({ loadStatus: "loading", loadError: null, retryCount: 0, historyLoadError: false });

      // /meta silent fallback（INV-1 qualifier M9）：失败仅 console.warn + meta=null
      let meta: RunMetaExtended | null = null;
      try {
        const mresp = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/meta`,
          { signal: entry.abort.signal }
        );
        if (mresp.ok) meta = (await mresp.json()) as RunMetaExtended;
      } catch (err) {
        if ((err as Error)?.name !== "AbortError") {
          console.warn(`[orca] loadRunWithMeta ${runId} /meta 失败，回退 full`, err);
        }
      }

      // 全量路径（huge run 亦直接全量加载——不弹「加载全部」gate；用户偏好直接加载，
      // 接受大 run 较慢的代价）。huge 标记仍据 meta 置位（信息位），但 hugeFullyLoaded
      // 恒 true → ChartRenderer 占位/按钮分支（huge && !hugeFullyLoaded）永不触发。
      try {
        const events = (await fetchEventsWithBackoff(
          runId,
          entry,
          `/api/runs/${encodeURIComponent(runId)}/events`
        )) as WebEvent[];
        if (get().activeRunId !== null && get().activeRunId !== runId) return;
        if (moduleEpoch !== myEpoch) return;
        set((state) => {
          _refoldAndCommit(state, runId, events, {
            huge: meta?.huge ?? false,
            hugeFullyLoaded: true,
            serverOverview: null,
            writable: meta?.writable ?? true,
          });
        });
      } catch (err) {
        writeLoadError(get, set, runId, myEpoch, err as LoadError);
      } finally {
        if (inflightLoads.get(runId)?.epoch === myEpoch) inflightLoads.delete(runId);
      }
    },

    /**
     * huge 模式增量 prepend：fetch ``?since=max(0, oldest-M)&limit=M`` → 与既有 events
     * 合并 fold（O(window)，不重算全 tape）。返回 true 表示拉到新事件（窗口向上扩展）。
     *
     * SPEC audit-c §4.1：免退避重试（C6：历史拉取非关键 load），失败仅 set
     * ``historyLoadError=true`` + UI banner；同窗口节流（M14）；下次成功自动清。
     * **写时双重校验**（C2 BLOCKER）：loadEarlierChunk(A) 在飞 + 切 B + A chunk
     * late-resolve 时丢弃，不污染 B。
     */
    loadEarlierChunk: async (runId, chunkSize) => {
      const state0 = get();
      if (!state0.huge) return false;
      if (state0.oldestSeqInWindow <= 1) return false; // 已到顶
      const since = Math.max(0, state0.oldestSeqInWindow - 1 - chunkSize);
      const originatingEpoch = moduleEpoch; // 持当前 epoch（C2 写时校验）
      // 复用同 runId 的 inflight entry（若无则新建临时）——loadEarlierChunk 不重置 retryCount
      let entry = inflightLoads.get(runId);
      if (!entry) {
        entry = { abort: new AbortController(), timer: null, epoch: originatingEpoch };
        inflightLoads.set(runId, entry);
      }
      try {
        const resp = await fetch(
          `/api/runs/${encodeURIComponent(runId)}/events?since=${since}&limit=${chunkSize}`,
          { signal: entry.abort.signal }
        );
        if (!resp.ok) {
          // 免退避，banner-only（M14/C6）；节流：同窗口已 true 则不重复 set
          if (!get().historyLoadError) {
            set({ historyLoadError: true });
          }
          console.warn(`[orca] loadEarlierChunk ${runId} HTTP ${resp.status}（忽略）`);
          return false;
        }
        const chunk = (await resp.json()) as WebEvent[];
        if (!Array.isArray(chunk)) {
          if (!get().historyLoadError) set({ historyLoadError: true });
          return false;
        }
        if (chunk.length === 0) return false;
        // 写时双重校验（C2 BLOCKER）：activeRunId + moduleEpoch 都一致才写
        if (get().activeRunId !== runId) return false;
        if (moduleEpoch !== originatingEpoch) return false;
        set((state) => {
          // 合并：旧 events + chunk（seenSeqs/refold 内部 seq 去重，安全）
          const merged = [...state.events, ...chunk];
          merged.sort((a, b) => a.seq - b.seq);
          state.events = merged;
          refold(state); // 末尾重建 seenSeqs（N1）
          state.oldestSeqInWindow = Math.min(
            state.oldestSeqInWindow,
            chunk[0].seq
          );
          state.historyLoadError = false; // 下次成功自动清（M14）
        });
        return true;
      } catch (err) {
        if ((err as Error)?.name === "AbortError") return false;
        if (!get().historyLoadError) set({ historyLoadError: true });
        console.warn(`[orca] loadEarlierChunk ${runId} 网络错误（忽略）`, err);
        return false;
      }
    },

    /**
     * huge 模式 ``load full``：拉全量 events → client-fold + clear serverOverview（M4：
     * 客户端可经此校验服务端 overview 派生与 client-fold 一致）。``hugeFullyLoaded=true``。
     *
     * SPEC audit-c §4.1：失败**不清 serverOverview**（保留原 huge 状态）+ 写错误态；
     * 原子提交同 loadRun/loadRunWithMeta。
     */
    loadFull: async (runId) => {
      moduleEpoch++;
      const myEpoch = moduleEpoch;
      abortAllInflight();
      const entry: InflightEntry = { abort: new AbortController(), timer: null, epoch: myEpoch };
      inflightLoads.set(runId, entry);
      set({ loadStatus: "loading", loadError: null, retryCount: 0, historyLoadError: false });

      try {
        const events = (await fetchEventsWithBackoff(
          runId,
          entry,
          `/api/runs/${encodeURIComponent(runId)}/events`
        )) as WebEvent[];
        if (get().activeRunId !== null && get().activeRunId !== runId) return;
        if (moduleEpoch !== myEpoch) return;
        set((state) => {
          // 保留 huge=true（loadFull 在 huge 模式触发）；只清 serverOverview + hugeFullyLoaded=true
          _refoldAndCommit(state, runId, events, {
            hugeFullyLoaded: true,
            serverOverview: null,
          });
        });
      } catch (err) {
        writeLoadError(get, set, runId, myEpoch, err as LoadError);
      } finally {
        if (inflightLoads.get(runId)?.epoch === myEpoch) inflightLoads.delete(runId);
      }
    },

    unloadRun: () => {
      // SPEC audit-c §4.1：abort-all + Map.delete + 复位错误态 + 清 warn-once Sets（E6/MINOR-5）
      abortAllInflight();
      droppedSeqs.clear();
      untitledChartWarned.clear();
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
        // loader 错误态复位（M18）
        state.loadStatus = "idle";
        state.loadError = null;
        state.retryCount = 0;
        state.historyLoadError = false;
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

// ── SPEC audit-c §4.1 writeLoadError：loader 终态失败时写错误态（fail loud，INV-1）──────
// 写时双重校验（activeRunId + moduleEpoch）：切到别的 run 或同 runId 不同实例 → 不写错误态
// （保留当前 run 的状态，不被陈旧失败覆盖）。
// 注：set 参数类型用 immer StoreApi['setState'] 的宽松联合（避免与 zustand-immer 重载冲突）。
type GetState = () => WorkflowState;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type SetFn = (...args: any[]) => void;

function writeLoadError(
  get: GetState,
  set: SetFn,
  expectedRunId: string,
  expectedEpoch: number,
  err: LoadError
): void {
  // 切到别的 run / 同 runId 不同实例 → 不写错误态（保留当前 run 的状态）
  const cur = get();
  if (cur.activeRunId !== null && cur.activeRunId !== expectedRunId) return;
  if (moduleEpoch !== expectedEpoch) return;
  if (err.kind === "network" && err.message === "aborted") return; // abort 不算失败
  console.warn(`[orca] loader 终态失败 run=${expectedRunId}`, err);
  set({
    loadStatus: "error",
    loadError: err,
    // retryCount 保留终态值（drives 最终错误信息「重试 N 次后失败」），下次 invocation reset=0
  });
}
