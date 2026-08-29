// test/_helpers.ts —— 共享测试 helper（DRY：resetStore + ALL_EVENT_TYPES + makeEvent）。
//
// 提取自 store.test.ts / selectors.test.ts / ws-resume.test.ts 三份重复实现。

import type { EventType, WebEvent } from "@/types/events";
import type { NodeSessionIndex } from "@/types/store-types";
import { useWorkflowStore } from "@/stores/workflow-store";

/** 后端 EventType 全集（codegen 同步；测试 backstop）——39 个（SPEC §3.2）。 */
export const ALL_EVENT_TYPES: EventType[] = [
  "workflow_started",
  "workflow_completed",
  "workflow_failed",
  "workflow_cancelled",
  "node_started",
  "node_completed",
  "node_failed",
  "node_skipped",
  "agent_message",
  "agent_thinking",
  "agent_tool_call",
  "agent_tool_result",
  "agent_usage",
  "agent_step_started",
  "route_taken",
  "foreach_started",
  "foreach_item_started",
  "foreach_item_completed",
  "foreach_completed",
  "human_decision_requested",
  "human_decision_resolved",
  "interrupt_requested",
  "interrupt_resolved",
  "prompt_rendered",
  "workflow_resumed",
  "retry_started",
  "retry_succeeded",
  "retry_exhausted",
  "wait_started",
  "wait_completed",
  "validator_started",
  "validator_passed",
  "validator_failed",
  "dialog_started",
  "dialog_message",
  "dialog_ended",
  "custom",
  "error",
  "unknown_event",
];

/** 构造事件（seq 自动 random；调用方按需覆盖）。 */
export function makeEvent(
  type: EventType,
  overrides: Partial<WebEvent> = {}
): WebEvent {
  return {
    seq: Math.floor(Math.random() * 1_000_000),
    type,
    timestamp: Date.now() / 1000,
    node: overrides.node ?? null,
    session_id: null,
    data: overrides.data ?? {},
    ...overrides,
  };
}

/**
 * 重置 store 到初始（每个测试独立）。
 *
 * SPEC audit-c：默认 ``loadStatus="loaded"`` 让 ``processEvent`` 可直接驱动（多数 store /
 * selector / chart 测试只需 fold 行为，不需测试 loadStatus 状态机）。测试 INV-7 拒收 /
 * loader 状态机时显式 setState 到 idle/loading/error。
 */
export function resetStore(): void {
  useWorkflowStore.setState({
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
    // SPEC audit-c：默认 loaded 让 processEvent 可驱动（INV-7 不拦）
    loadStatus: "loaded",
    loadError: null,
    retryCount: 0,
    historyLoadError: false,
    huge: false,
    hugeFullyLoaded: true,
    serverOverview: null,
    writable: true,
    oldestSeqInWindow: 0,
    newestSeqInWindow: 0,
  });
}

/**
 * 构造 NodeSessionIndex（ev 空索引）——组件测试 setState 注入用（agents-rail
 * sessionCount / iteration 用例）。NodeSessionIndex 加 ev 必填字段后（SPEC 2026-08-28
 * C3.1），测试字面量缺 ev 会 tsc 报错——收敛到本 helper（DRY）。
 *
 * @param sessions session 列表（首事件 seq 升序语义）
 * @param sessionEventCounts 每 session 事件数（缺省每 session 计 1）
 */
export function makeNodeIndex(
  sessions: string[],
  sessionEventCounts?: Record<string, number>
): NodeSessionIndex {
  return {
    sessions,
    sessionEventCounts:
      sessionEventCounts ?? Object.fromEntries(sessions.map((s) => [s, 1])),
    sessionFirstTs: Object.fromEntries(sessions.map((s, i) => [s, i + 1])),
    ev: { all: [], bySession: {}, last: null },
  };
}
