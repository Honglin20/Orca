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

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  classifyLogLevel,
  selectAgents,
  selectCharts,
  selectConversation,
  selectLog,
  selectNodeSessions,
  setLogShowDebug,
  summarizeEvent,
  type LogLevel,
} from "@/selectors";
import type { EventType, WebEvent } from "@/types/events";
import { ALL_EVENT_TYPES } from "./_helpers";

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
    nodesIndex: {},
    selectedNode: null,
    selectedSession: null,
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
  beforeEach(() => {
    resetStore();
    // 测试隔离：debug 级恢复默认隐藏（防 setLogShowDebug(true) 跨用例泄漏）
    setLogShowDebug(false);
  });

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

  // ── 4. selectLog：filter（classifyLogLevel 非 null）+ 默认隐藏 debug ──
  it("selectLog：仅含生命周期/routing/gate/失败；过程事件不进 Log（SPEC §P1）", () => {
    const events = buildFixtureT();
    useWorkflowStore.getState().loadFromEvents(events);
    const lines = selectLog(useWorkflowStore.getState());
    const lineTypes = lines.map((l) => l.type);

    // 过程事件不进 Log（零回归 ConversationView 仍含）
    expect(lineTypes).not.toContain("agent_message");
    expect(lineTypes).not.toContain("agent_thinking");
    expect(lineTypes).not.toContain("agent_tool_call");
    expect(lineTypes).not.toContain("agent_tool_result");
    expect(lineTypes).not.toContain("agent_step_started");
    expect(lineTypes).not.toContain("foreach_item_started");
    expect(lineTypes).not.toContain("foreach_item_completed");
    expect(lineTypes).not.toContain("prompt_rendered");
    expect(lineTypes).not.toContain("custom");
    expect(lineTypes).not.toContain("unknown_event");
    // route_taken 默认隐藏（debug 级）
    expect(lineTypes).not.toContain("route_taken");

    // 生命周期事件进 Log（fixture 含的子集；无 node_completed/workflow_completed）
    expect(lineTypes).toContain("workflow_started");
    expect(lineTypes).toContain("node_started");
    expect(lineTypes).toContain("node_failed");
    expect(lineTypes).toContain("foreach_started");
    expect(lineTypes).toContain("foreach_completed");
    expect(lineTypes).toContain("retry_started");
    expect(lineTypes).toContain("retry_succeeded");
    expect(lineTypes).toContain("human_decision_requested");
    expect(lineTypes).toContain("human_decision_resolved");

    // 每行 text 非空（无 no-op fallback）+ level 已分级
    for (const line of lines) {
      expect(line.text.length).toBeGreaterThan(0);
      expect(line.text.length).toBeLessThanOrEqual(80);
      expect(line.level).toBeDefined();
    }
    // 错误事件 level === "error"（取代旧 isError）
    const failedLine = lines.find((l) => l.type === "node_failed");
    expect(failedLine?.level).toBe("error");
    // workflow_failed 不在 fixture（只有 node_failed）
    expect(lines.find((l) => l.type === "workflow_failed")).toBeUndefined();
  });

  // ── 4b. classifyLogLevel：全 39 EventType oracle 表（TS never 编译期穷尽守门）──
  it("classifyLogLevel：全 39 EventType oracle 表（缺一编译失败 + 运行时逐条对齐 SPEC §P1）", () => {
    // Record<EventType,_> 强制全 39 key 写齐 —— 缺一个 type TS 编译失败（never 守门）
    const ORACLE: Record<EventType, LogLevel | null> = {
      // info：开始类生命周期
      workflow_started: "info",
      node_started: "info",
      foreach_started: "info",
      retry_started: "info",
      validator_started: "info",
      wait_started: "info",
      human_decision_requested: "info",
      interrupt_requested: "info",
      dialog_started: "info",
      // success：完成类生命周期
      workflow_completed: "success",
      workflow_resumed: "success",
      node_completed: "success",
      foreach_completed: "success",
      retry_succeeded: "success",
      validator_passed: "success",
      wait_completed: "success",
      human_decision_resolved: "success",
      interrupt_resolved: "success",
      dialog_ended: "success",
      // error：失败类
      workflow_failed: "error",
      workflow_cancelled: "error",
      node_failed: "error",
      retry_exhausted: "error",
      validator_failed: "error",
      error: "error",
      // warning：跳过
      node_skipped: "warning",
      // debug：路由（默认隐藏）
      route_taken: "debug",
      // null：过程事件归 ConversationView，不进 Log
      agent_message: null,
      agent_thinking: null,
      agent_tool_call: null,
      agent_tool_result: null,
      agent_step_started: null,
      foreach_item_started: null,
      foreach_item_completed: null,
      prompt_rendered: null,
      agent_usage: null,
      custom: null,
      dialog_message: null,
      unknown_event: null,
    };

    // 运行时逐条断言（与 compile-time Record 互补 —— 防运行时映射 drift）
    for (const type of ALL_EVENT_TYPES) {
      expect(
        classifyLogLevel(type),
        `classifyLogLevel(${type}) 应与 SPEC §P1 分级表一致`
      ).toBe(ORACLE[type]);
    }

    // SPEC §P1 关键边界点单独钉一遍（防后续误改）
    expect(classifyLogLevel("workflow_resumed")).toBe("success"); // P0-1 补
    expect(classifyLogLevel("dialog_started")).toBe("info");
    expect(classifyLogLevel("dialog_ended")).toBe("success");
    expect(classifyLogLevel("dialog_message")).toBeNull(); // agent 级过程
    expect(classifyLogLevel("route_taken")).toBe("debug"); // 默认隐藏
    expect(classifyLogLevel("agent_tool_call")).toBeNull();
    expect(classifyLogLevel("agent_thinking")).toBeNull();

    // fail-loud 兜底：编译期 never 已封死合法路径，但运行时若有 unknown 值
    // （e.g. 跨版本/脏数据），需 console.warn + 返 null（SPEC §全局约束 3）
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect(classifyLogLevel("__not_a_real_type__" as EventType)).toBeNull();
      expect(warnSpy).toHaveBeenCalledTimes(1);
      expect(warnSpy.mock.calls[0]?.[0]).toContain("unmapped event type");
    } finally {
      warnSpy.mockRestore();
    }
  });

  // ── 4c. selectLog debug 开关：setLogShowDebug(true) → route_taken 进 Log ──
  it("selectLog：setLogShowDebug(true) 展开后 route_taken 进 Log（默认隐藏可恢复）", () => {
    useWorkflowStore.getState().loadFromEvents([
      ev("workflow_started"),
      ev("route_taken", { data: { from: "A", to: "B" } }),
      ev("node_started", { node: "A" }),
    ]);
    try {
      // 默认隐藏
      let lines = selectLog(useWorkflowStore.getState());
      expect(lines.map((l) => l.type)).not.toContain("route_taken");
      expect(lines.map((l) => l.type)).toEqual([
        "workflow_started",
        "node_started",
      ]);
      // 展开 debug
      setLogShowDebug(true);
      lines = selectLog(useWorkflowStore.getState());
      expect(lines.map((l) => l.type)).toEqual([
        "workflow_started",
        "route_taken",
        "node_started",
      ]);
      const route = lines.find((l) => l.type === "route_taken");
      expect(route?.level).toBe("debug");
    } finally {
      // 测试隔离：恢复默认
      setLogShowDebug(false);
    }
  });

  // ── 5. summarizeEvent 穷尽性：每个 EventType 都有分支（无 default fallthrough）──
  it("summarizeEvent：全部 39 个 EventType 都有 readable 摘要（穷尽）", () => {
    // 用 _helpers.ALL_EVENT_TYPES（与 codegen 同步），避免硬编码 list 与 events.ts drift。
    // summarizeEvent 内部 switch 自带 never 编译期守门（缺 type → 编译失败）；
    // 本测试再加运行时遍历断言每个 type 都产出非空 readable 行（防 drift 双保险）。
    for (const type of ALL_EVENT_TYPES) {
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

// ── P2：selectNodeSessions（nodesIndex 派生）+ selectConversation sessionId 维度 ──
// SPEC web-presentation-refinement §P2。fixture = e3b8ad family_detect 缩影（main + 多 sub）。
describe("selectors P2 — selectNodeSessions + selectConversation sessionId", () => {
  beforeEach(() => resetStore());

  /** 构造 family_detect 缩影 fixture：main(2) + subA(3) + subB(2)。sessionId 用 30 字符（仿真）。 */
  function buildFamilyFixture() {
    return [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "family_detect", session_id: null }),
      ev("agent_message", { node: "family_detect", session_id: "ses_AAA111222333xxx", data: { text: "a1" } }),
      ev("agent_thinking", { node: "family_detect", session_id: "ses_AAA111222333xxx", data: { text: "a2" } }),
      ev("agent_tool_call", { node: "family_detect", session_id: "ses_AAA111222333xxx", data: { tool: "bash", tool_call_id: "ta" } }),
      ev("agent_message", { node: "family_detect", session_id: "ses_BBB222333444yyy", data: { text: "b1" } }),
      ev("agent_thinking", { node: "family_detect", session_id: "ses_BBB222333444yyy", data: { text: "b2" } }),
      ev("node_completed", { node: "family_detect", session_id: null }),
      // 另一 node（验证 selector 不混淆跨 node 索引）
      ev("node_started", { node: "other", session_id: null }),
      ev("agent_message", { node: "other", session_id: "ses_CCC", data: { text: "c1" } }),
    ];
  }

  it("selectNodeSessions：从 nodesIndex 派生会话行（main + sub label/eventCount/firstTs）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    const rows = selectNodeSessions(useWorkflowStore.getState(), "family_detect");
    // 3 session: main + subA + subB（按首事件 seq 升序）
    expect(rows.map((r) => r.sessionId)).toEqual([
      "main",
      "ses_AAA111222333xxx",
      "ses_BBB222333444yyy",
    ]);
    // label：main 显式；其他截断前 10 字符 + "…"（仿真 18 字符 sessionId）
    expect(rows[0].label).toBe("main");
    expect(rows[1].label).toBe("ses_AAA111…"); // 前 10 字符 + …
    expect(rows[2].label).toBe("ses_BBB222…");
    // eventCount
    expect(rows[0].eventCount).toBe(2); // main: node_started + node_completed
    expect(rows[1].eventCount).toBe(3); // subA
    expect(rows[2].eventCount).toBe(2); // subB
    // firstTs 单调（seq 升序 fold → 首事件 ts 升序）
    expect(rows[0].firstTs).toBeLessThan(rows[1].firstTs);
    expect(rows[1].firstTs).toBeLessThan(rows[2].firstTs);
  });

  it("selectNodeSessions：nodeId=null/无索引 → []（fail-safe，不 throw）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    expect(selectNodeSessions(useWorkflowStore.getState(), null)).toEqual([]);
    expect(selectNodeSessions(useWorkflowStore.getState(), "no_such_node")).toEqual([]);
  });

  // ── selectConversation sessionId 维度（零回归 + 切 session）──
  it("selectConversation 省略 sessionId → 全 node 聚合（旧行为零回归）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    const all = selectConversation(useWorkflowStore.getState(), "family_detect");
    // 全 7 conversation 事件（main 2 + subA 3 + subB 2；不含 route_taken / workflow_started）
    expect(all.events.length).toBe(7);
    // 按 seq 升序
    for (let i = 1; i < all.events.length; i++) {
      expect(all.events[i].seq).toBeGreaterThan(all.events[i - 1].seq);
    }
  });

  it("selectConversation sessionId='all' → 与省略等价（零回归）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    const omitted = selectConversation(useWorkflowStore.getState(), "family_detect");
    const allKw = selectConversation(useWorkflowStore.getState(), "family_detect", "all");
    expect(allKw.events.map((e) => e.seq)).toEqual(omitted.events.map((e) => e.seq));
  });

  it("selectConversation sessionId=具体 → 仅该 session 事件（症状 #2/#5 缓解：缩量）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    const subA = selectConversation(
      useWorkflowStore.getState(),
      "family_detect",
      "ses_AAA111222333xxx"
    );
    // 仅 ses_A 的 3 事件
    expect(subA.events.length).toBe(3);
    expect(subA.events.every((e) => e.session_id === "ses_AAA111222333xxx")).toBe(true);

    const main = selectConversation(
      useWorkflowStore.getState(),
      "family_detect",
      "main"
    );
    // main = null session_id 的 lifecycle 事件（2 条）
    expect(main.events.length).toBe(2);
    expect(main.events.every((e) => e.session_id === null)).toBe(true);
  });

  it("selectConversation workflow_failed data.node 特例：跨 session 聚合仍含 wf_failed", () => {
    // workflow_failed 的 top-level node=null，但 data.node=nodeId 应被 selectConversation 拾取。
    useWorkflowStore.getState().loadFromEvents([
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: null }),
      ev("agent_message", { node: "n", session_id: "s1", data: { text: "x" } }),
      ev("workflow_failed", { data: { node: "n", message: "boom" } }),
    ]);
    // sessionId="s1" 过滤下，workflow_failed（session_id=null 归 main）不应出现
    const s1 = selectConversation(useWorkflowStore.getState(), "n", "s1");
    expect(s1.events.map((e) => e.type)).not.toContain("workflow_failed");
    // 全聚合（"all"）含 workflow_failed
    const all = selectConversation(useWorkflowStore.getState(), "n", "all");
    expect(all.events.map((e) => e.type)).toContain("workflow_failed");
  });
});
