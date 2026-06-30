// test/fixtures/events.ts —— 测试用事件流工厂（phase 9c）。
//
// 提供：构造 workflow_started（含 topology）+ node_* + route_taken 事件流的 helper，
// 让 graph/replay/log 测试共享一致的事件构造逻辑（DRY）。

import type { WorkflowEvent, WorkflowStatus } from "@/types/events";
import type { WorkflowTopology } from "@/types/topology";

let _seq = 0;
/** 重置 seq 计数器（每个测试独立）。 */
export function resetSeq(): void {
  _seq = 0;
}

/** 线性 demo 拓扑：start(set) → decide(set) → $end，含回环边 decide→decide。 */
export const LINEAR_TOPOLOGY: WorkflowTopology = {
  entry: "start",
  nodes: [
    { name: "start", kind: "set" },
    { name: "decide", kind: "set" },
  ],
  routes: [
    { from: "start", to: "decide" },
    { from: "decide", to: "decide", when: "retry" },
    { from: "decide", to: "$end" },
  ],
  parallel: [],
};

/** 含 parallel 组的拓扑：fan → [grp: a,b] → $end。 */
export const PARALLEL_TOPOLOGY: WorkflowTopology = {
  entry: "fan",
  nodes: [
    { name: "fan", kind: "set" },
    { name: "a", kind: "script" },
    { name: "b", kind: "script" },
  ],
  routes: [
    { from: "fan", to: "grp" },
    { from: "grp", to: "$end" },
  ],
  parallel: [{ name: "grp", branches: ["a", "b"] }],
};

/** 含 foreach 节点的拓扑（测 progress 派生透传到 widget）。 */
export const FOR_EACH_TOPOLOGY: WorkflowTopology = {
  entry: "fan",
  nodes: [
    { name: "fan", kind: "foreach" },
    { name: "worker", kind: "agent" },
  ],
  routes: [{ from: "fan", to: "$end" }],
  parallel: [],
};

export interface MakeEventOpts {
  seq?: number;
  type?: WorkflowEvent["type"];
  timestamp?: number;
  node?: string | null;
  session_id?: string | null;
  data?: Record<string, unknown>;
}

/** 构造单条事件（seq 自增，可覆盖）。 */
export function mkEvent(opts: MakeEventOpts = {}): WorkflowEvent {
  _seq += 1;
  return {
    seq: opts.seq ?? _seq,
    type: opts.type ?? "node_started",
    timestamp: opts.timestamp ?? 1_000_000 + _seq,
    node: opts.node !== undefined ? opts.node : null,
    session_id: opts.session_id !== undefined ? opts.session_id : null,
    data: opts.data ?? {},
  };
}

/** workflow_started 事件（含 topology）。 */
export function mkWorkflowStarted(topology: WorkflowTopology): WorkflowEvent {
  return mkEvent({
    type: "workflow_started",
    data: { workflow_name: "demo", node_count: topology.nodes.length, entry: topology.entry, topology },
  });
}

/**
 * 构造一条完整的 demo 事件流：workflow_started → 2 个 node 各 started/completed →
 * workflow_completed。每个 node 用独立 session_id（测 session 分组）。
 */
export function buildDemoStream(topology: WorkflowTopology = LINEAR_TOPOLOGY): WorkflowEvent[] {
  const events: WorkflowEvent[] = [];
  events.push(mkWorkflowStarted(topology));
  for (const n of topology.nodes) {
    events.push(mkEvent({ type: "node_started", node: n.name, session_id: `s-${n.name}` }));
    events.push(
      mkEvent({
        type: "node_completed",
        node: n.name,
        session_id: `s-${n.name}`,
        data: { output: { result: n.name }, elapsed: 0.1 },
      })
    );
    events.push(mkEvent({ type: "route_taken", data: { from: n.name, to: "$end" } }));
  }
  events.push(mkEvent({ type: "workflow_completed", data: { elapsed: 1.0, outputs: {} } }));
  return events;
}

/** 含 foreach 拓扑（测 progress 派生）。 */
export const FOREACH_TOPOLOGY: WorkflowTopology = {
  entry: "fan",
  nodes: [
    { name: "fan", kind: "foreach" },
    { name: "worker", kind: "agent" },
  ],
  routes: [
    { from: "fan", to: "$end" },
  ],
  parallel: [],
};

/**
 * 构造覆盖全部有派生逻辑 handler 的「富」事件流（agent_usage→cost / foreach→progress /
 * human_decision→gate），让 live==replay 断言真正压到所有有副作用的 handler。
 */
export function buildRichStream(topology: WorkflowTopology = LINEAR_TOPOLOGY): WorkflowEvent[] {
  const events: WorkflowEvent[] = [];
  events.push(mkWorkflowStarted(topology));
  // 第一个 node：agent，带 usage（cost 派生）+ message（无派生但有 session）
  const n0 = topology.nodes[0];
  events.push(mkEvent({ type: "node_started", node: n0.name, session_id: "s-agent" }));
  events.push(
    mkEvent({
      type: "agent_message",
      node: n0.name,
      session_id: "s-agent",
      data: { text: "hello" },
    })
  );
  events.push(
    mkEvent({
      type: "agent_usage",
      node: n0.name,
      session_id: "s-agent",
      data: { input_tokens: 100, output_tokens: 50, cache_tokens: 0, cost_usd: 0.05 },
    })
  );
  events.push(
    mkEvent({
      type: "node_completed",
      node: n0.name,
      session_id: "s-agent",
      data: { output: { done: true }, elapsed: 0.2 },
    })
  );
  events.push(mkEvent({ type: "route_taken", data: { from: n0.name, to: "$end" } }));

  // gate（human_decision → gate 派生 + resolved 清空）
  events.push(
    mkEvent({
      type: "human_decision_requested",
      data: { gate_id: "g1", prompt: "继续？", options: ["yes", "no"], source: "test" },
    })
  );
  events.push(
    mkEvent({
      type: "human_decision_resolved",
      data: { gate_id: "g1", answer: "yes" },
    })
  );

  // foreach（如果拓扑含 foreach 节点 → progress 派生）
  const feNode = topology.nodes.find((n) => n.kind === "foreach");
  if (feNode) {
    events.push(
      mkEvent({
        type: "foreach_started",
        node: feNode.name,
        data: { item_count: 3, max_concurrent: 2 },
      })
    );
    for (let i = 0; i < 3; i++) {
      events.push(
        mkEvent({ type: "foreach_item_started", node: feNode.name, data: { index: i, item_key: `k${i}` } })
      );
      events.push(
        mkEvent({ type: "foreach_item_completed", node: feNode.name, data: { index: i, output: { i } } })
      );
    }
    events.push(
      mkEvent({ type: "foreach_completed", node: feNode.name, data: { count: 3, succeeded: 3 } })
    );
  }

  events.push(mkEvent({ type: "workflow_completed", data: { elapsed: 2.0, outputs: {} } }));
  return events;
}

/** 构造 N 个 node_completed 事件的长流（测 checkpoint / 增量 apply）。 */
export function buildLongStream(nodeCount: number): WorkflowEvent[] {
  const events: WorkflowEvent[] = [];
  events.push(mkWorkflowStarted(LINEAR_TOPOLOGY));
  for (let i = 0; i < nodeCount; i++) {
    events.push(
      mkEvent({
        type: "node_started",
        node: `n${i}`,
        session_id: `s-${i}`,
      })
    );
    events.push(
      mkEvent({
        type: "node_completed",
        node: `n${i}`,
        session_id: `s-${i}`,
        data: { output: { i }, elapsed: 0.01 },
      })
    );
  }
  return events;
}

/** 读 store 当前派生态快照（live==replay 断言用）。 */
export function snapshotNodes(
  nodes: Record<string, unknown>
): Record<string, unknown> {
  return JSON.parse(JSON.stringify(nodes));
}

export function snapshotState(
  state: Readonly<{
    nodes: Record<string, unknown>;
    gate: unknown;
    cost: number;
    workflowName: string;
    status: WorkflowStatus;
    workflowDef: unknown;
  }>
): Record<string, unknown> {
  // 全部业务派生态字段（live==replay byte-identical 断言用）。
  // **有意排除**：events（replay 不 push，是 replay 内部机制）、replayMode/replayPosition/
  // selectedNode（UI 交互态，非业务真相）、activeRunId（懒加载标记）。
  return JSON.parse(
    JSON.stringify({
      nodes: state.nodes,
      gate: state.gate,
      cost: state.cost,
      workflowName: state.workflowName,
      status: state.status,
      workflowDef: state.workflowDef,
    })
  );
}
