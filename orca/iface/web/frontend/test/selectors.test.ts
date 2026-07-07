// test/selectors.test.ts —— 纯函数 selector 验收（SPEC §3.1 / §0 D2 / D3 / D7 / §10）。
//
// 断言：
//   1. **selectConversation**：fixture tape T 含 reasoning / step_start / 乱序 tool_result /
//      orphan result / retry / foreach / unknown_event / pending tool / chart / gate /
//      failed node → 按 node 分组（D2）+ 只含 conversation-相关事件
//   2. **selectCharts**：custom(kind=chart) → group/identity 去重 upsert；D7 序无关：
//      selectCharts(T) == selectCharts(sort(T)) == selectCharts(reverse(T))
//   3. **selectLog**：每事件一行；每 EventType 有 readable 摘要（无 no-op fallback）
//   4. **selectConversation** D7 序无关同样成立

import { beforeEach, describe, expect, it } from "vitest";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectAgents,
  selectCharts,
  selectConversation,
  selectLog,
  summarizeEvent,
} from "@/selectors";
import type { EventType, WebEvent } from "@/types/events";

let _seq = 0;
function ev(
  type: EventType,
  overrides: Partial<WebEvent> = {}
): WebEvent {
  _seq += 1;
  return {
    seq: _seq,
    type,
    timestamp: 1700000000 + _seq,
    node: null,
    session_id: null,
    data: {},
    ...overrides,
  };
}

function resetStore() {
  _seq = 0;
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
    selectedNode: null,
    activeRunId: null,
  });
}

/**
 * 构造 SPEC §10 fixture tape T：含 reasoning、step_start、乱序 tool_result、orphan result、
 * retry、foreach、unknown_event、pending tool、chart、gate、failed node。
 */
function buildFixtureT(): WebEvent[] {
  return [
    ev("workflow_started", { data: { workflow_name: "demo" } }),
    ev("node_started", { node: "code_gen", session_id: "s1" }),
    ev("prompt_rendered", {
      node: "code_gen",
      session_id: "s1",
      data: { preview: "Write a fn" },
    }),
    ev("agent_step_started", {
      node: "code_gen",
      session_id: "s1",
      data: { step_reason: "next" },
    }),
    ev("agent_thinking", {
      node: "code_gen",
      session_id: "s1",
      data: { text: "Let me think..." },
    }),
    ev("agent_message", {
      node: "code_gen",
      session_id: "s1",
      data: { text: "Hello" },
    }),
    // 乱序 tool_result（在同 session 内将与下面的 call 配对）
    ev("agent_tool_result", {
      node: "code_gen",
      session_id: "s1",
      data: { tool_call_id: "tc1", result: "ok" },
    }),
    ev("agent_tool_call", {
      node: "code_gen",
      session_id: "s1",
      data: { tool: "bash", args: { cmd: "ls" }, tool_call_id: "tc1" },
    }),
    // orphan tool_result（无对应 call）：仍进 LogStream，但 selectConversation 也保留
    // （conversation 内 orphan 判定是渲染层关注点）
    ev("agent_tool_result", {
      node: "code_gen",
      session_id: "s1",
      data: { tool_call_id: "orphan", result: "?" },
    }),
    // retry
    ev("retry_started", {
      node: "code_gen",
      session_id: "s1",
      data: { attempt: 1, max_attempts: 3, kind: "transient" },
    }),
    ev("retry_succeeded", {
      node: "code_gen",
      session_id: "s1",
      data: { attempt_total: 1 },
    }),
    // foreach
    ev("foreach_started", {
      node: "fan",
      data: { item_count: 3, max_concurrent: 2 },
    }),
    ev("foreach_item_started", { node: "fan", data: { index: 0 } }),
    ev("foreach_item_completed", { node: "fan", data: { index: 0 } }),
    ev("foreach_completed", { node: "fan", data: { count: 1, succeeded: 1 } }),
    // unknown_event（D8）
    ev("unknown_event", {
      node: "code_gen",
      data: { raw: { x: 1 }, source: "opencode" },
    }),
    // pending tool（无 result）
    ev("agent_tool_call", {
      node: "code_gen",
      session_id: "s1",
      data: { tool: "read", args: { path: "/etc" }, tool_call_id: "tc2" },
    }),
    // custom chart
    ev("custom", {
      node: "code_gen",
      session_id: "s1",
      data: {
        kind: "chart",
        chart: {
          chart_type: "line",
          data: [{ x: 1, y: 2 }],
          label: "metrics",
          title: "loss",
        },
      },
    }),
    // 第二张 chart（同 label 不同 title：测 group/identity）
    ev("custom", {
      node: "code_gen",
      session_id: "s1",
      data: {
        kind: "chart",
        chart: {
          chart_type: "bar",
          data: [{ x: "a", y: 3 }],
          label: "metrics",
          title: "acc",
        },
      },
    }),
    // 同 label + 同 title（identity upsert：后到胜，D7）
    ev("custom", {
      node: "code_gen",
      session_id: "s1",
      data: {
        kind: "chart",
        chart: {
          chart_type: "line",
          data: [{ x: 2, y: 4 }],
          label: "metrics",
          title: "loss",
        },
      },
    }),
    // gate
    ev("human_decision_requested", {
      data: { gate_id: "g1", prompt: "继续？" },
    }),
    ev("human_decision_resolved", {
      data: { gate_id: "g1", answer: "yes" },
    }),
    // failed node
    ev("node_failed", {
      node: "code_gen",
      session_id: "s1",
      data: { kind: "exec_error", message: "boom" },
    }),
  ];
}

describe("selectors", () => {
  beforeEach(() => resetStore());

  // ── 1. selectConversation：D2 按 node 分组 ──
  it("selectConversation 按 node 分组，只含 conversation-相关事件", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);

    const convCodeGen = selectConversation(
      useWorkflowStore.getState(),
      "code_gen"
    );
    expect(convCodeGen.node).toBe("code_gen");
    // 所有 events 都属于 code_gen（fixture 故意如此）
    expect(convCodeGen.events.length).toBeGreaterThan(0);
    // 不该包含 human_decision_requested（无 node 字段，进 gate 模态不进 conversation）
    const types = convCodeGen.events.map((e) => e.type);
    expect(types).not.toContain("human_decision_requested");
    expect(types).not.toContain("human_decision_resolved");
    // 不该包含 foreach_*（属于 fan node）
    expect(types).not.toContain("foreach_started");
    // 应包含 thinking / message / tool_call / step / chart / unknown / retry / failed
    expect(types).toContain("agent_thinking");
    expect(types).toContain("agent_message");
    expect(types).toContain("agent_tool_call");
    expect(types).toContain("agent_step_started");
    expect(types).toContain("custom");
    expect(types).toContain("unknown_event");
    expect(types).toContain("retry_started");
    expect(types).toContain("node_failed");

    // fan node 的 conversation：仅 foreach_* 是 conversation 相关（其余 fan 事件无 session）
    const convFan = selectConversation(useWorkflowStore.getState(), "fan");
    const fanTypes = convFan.events.map((e) => e.type);
    expect(fanTypes).toContain("foreach_started");
    expect(fanTypes).toContain("foreach_completed");
  });

  // ── 2. selectConversation D7 序无关 ──
  it("selectConversation D7：sort(T) 与 reverse(T) 产同 snapshot", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);
    const forward = JSON.stringify(
      selectConversation(useWorkflowStore.getState(), "code_gen").events.map(
        (e) => e.seq
      )
    );

    resetStore();
    useWorkflowStore.getState().loadFromEvents([...events].reverse());
    const backward = JSON.stringify(
      selectConversation(useWorkflowStore.getState(), "code_gen").events.map(
        (e) => e.seq
      )
    );

    // events 数组最终都按 seq 升序排列（store 内 sort），所以 snapshot 应相等
    expect(backward).toEqual(forward);
  });

  // ── 3. selectCharts：D3 group/identity 去重 upsert + D7 序无关 ──
  it("selectCharts：group/identity 去重 upsert（同 label+title 后到胜）", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);
    const { groups } = selectCharts(useWorkflowStore.getState());

    // 一个 group "metrics"
    expect(groups.length).toBe(1);
    expect(groups[0].group).toBe("metrics");
    // 两个 identity：loss / acc（loss 被 upsert，不堆积）
    const identities = groups[0].entries.map((e) => e.identity).sort();
    expect(identities).toEqual(["acc", "loss"]);
    // loss 的最后 payload 应是后到（data:[{x:2,y:4}]）
    const loss = groups[0].entries.find((e) => e.identity === "loss");
    const payload = loss?.payload as { data: Array<{ x: number; y: number }> };
    expect(payload.data).toEqual([{ x: 2, y: 4 }]);
  });

  it("selectCharts D7：reverse(T) 产同集（序无关）", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);
    const forwardIds = JSON.stringify(
      selectCharts(useWorkflowStore.getState()).groups.map((g) =>
        g.entries.map((e) => ({ id: e.identity, seq: e.seq }))
      )
    );

    resetStore();
    useWorkflowStore.getState().loadFromEvents([...events].reverse());
    const backwardIds = JSON.stringify(
      selectCharts(useWorkflowStore.getState()).groups.map((g) =>
        g.entries.map((e) => ({ id: e.identity, seq: e.seq }))
      )
    );

    // D7：identity → seq 应相等（同 identity upsert 后 seq 应一致——max(seq) 胜，
    // store 内 sort 后无论到达顺序，遍历顺序一致 → upsert 终态一致）
    expect(backwardIds).toEqual(forwardIds);
  });

  // ── 4. selectLog：每事件一行，每 EventType 有 readable 摘要 ──
  it("selectLog：行数 == tape 事件数；每行有 readable 摘要（无 no-op fallback）", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);
    const lines = selectLog(useWorkflowStore.getState());

    expect(lines.length).toBe(events.length);
    // 每行 text 非空（无 no-op fallback）
    for (const line of lines) {
      expect(line.text.length).toBeGreaterThan(0);
      expect(line.text.length).toBeLessThanOrEqual(80);
    }
    // 错误事件标记 isError
    const failedLine = lines.find((l) => l.type === "node_failed");
    expect(failedLine?.isError).toBe(true);
    const wfFailLine = lines.find((l) => l.type === "workflow_failed");
    // workflow_failed 不在 fixture（只有 node_failed），跳过
    expect(wfFailLine).toBeUndefined();
  });

  // ── 5. summarizeEvent 穷尽性：每个 EventType 都有分支（无 default fallthrough）──
  it("summarizeEvent：全部 39 个 EventType 都有 readable 摘要（穷尽）", () => {
    const allTypes: EventType[] = [
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
    for (const type of allTypes) {
      const e: WebEvent = {
        seq: 1,
        type,
        timestamp: 1,
        node: "n",
        session_id: "s",
        data: {},
      };
      const line = summarizeEvent(e);
      expect(line.length, `summarizeEvent(${type}) 应产出非空 readable 行`).toBeGreaterThan(
        "n [s] ".length // 至少有 node + session 前缀；detail 不能为空字符串
      );
      // 不能落入未映射分支
      expect(line).not.toContain("unmapped");
    }
  });

  // ── 6. selectAgents：fold 后 agents 行模型 ──
  it("selectAgents：fold 后输出 agent 行（status / elapsed）", () => {
    useWorkflowStore.getState().loadFromEvents([
      ev("workflow_started", { data: { workflow_name: "demo" } }),
      ev("node_started", { node: "A" }),
      ev("node_completed", {
        node: "A",
        data: { output: "ok", elapsed: 1.5 },
      }),
      ev("node_started", { node: "B" }),
    ]);
    const agents = selectAgents(useWorkflowStore.getState());
    const a = agents.find((x) => x.node === "A");
    const b = agents.find((x) => x.node === "B");
    expect(a?.status).toBe("done");
    expect(a?.elapsed).toBeCloseTo(1.5, 5);
    expect(b?.status).toBe("running");
  });
});
