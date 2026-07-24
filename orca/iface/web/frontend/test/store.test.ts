// test/store.test.ts —— Zustand 单 store = fold(tape) 验收（web-shell-v2 SPEC §3.1 / §9）。
//
// 断言意图（Rule 9）：
//   1. **单 store**（铁律 4）：全 src 只一个 create()
//   2. **eventHandlers 覆盖全部 EventType**（39 个，对齐 event.py Literal）
//   3. **fold 幂等**（铁律 4 / §3.2.3）：同事件 N 次应用 = 状态一致
//   4. **loadRun**：fetch /events → loadFromEvents → nodes 派生正确
//   5. **unloadRun**：清 events/nodes/gate（懒加载红线）
//   6. **D7 seq 升序 fold**：loadFromEvents 内部 sort，序无关
//   7. **D8 unknown_event/agent_step_started reducer no-op**

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  HANDLED_EVENT_TYPES,
  useWorkflowStore,
} from "@/stores/workflow-store";
import type { WebEvent } from "@/types/events";
import {
  ALL_EVENT_TYPES,
  makeEvent,
  resetStore,
} from "./_helpers";

describe("workflow-store", () => {
  beforeEach(() => resetStore());

  // ── 1. 单 store（铁律 4）──
  it("源码中只存在一个 zustand create()（单 store 铁律）", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        "..",
        "src",
        "stores",
        "workflow-store.ts"
      ),
      "utf8"
    );
    const createMatches = src.match(/\bcreate</g) ?? [];
    expect(createMatches.length).toBe(1);
  });

  // ── 2. eventHandlers 覆盖全部 39 个 EventType ──
  it("eventHandlers 覆盖全部 39 个 EventType（codegen 对齐）", () => {
    for (const t of ALL_EVENT_TYPES) {
      expect(HANDLED_EVENT_TYPES, `handler 缺 ${t}`).toContain(t);
    }
    for (const t of HANDLED_EVENT_TYPES) {
      expect(ALL_EVENT_TYPES, `handler 多了未知 type ${t}`).toContain(t);
    }
    expect(HANDLED_EVENT_TYPES.length).toBe(ALL_EVENT_TYPES.length);
  });

  // ── 3. fold 幂等 ──
  it("同事件应用两次：nodes 一致 + events 不重复 + cost 不翻倍（fold 幂等）", () => {
    const store = useWorkflowStore.getState();
    const ev = makeEvent("node_completed", {
      seq: 1,
      node: "A",
      data: { output: { x: 1 }, elapsed: 0.1 },
    });
    store.processEvent(ev);
    store.processEvent(ev);

    const { nodes, events } = useWorkflowStore.getState();
    expect(Object.keys(nodes).length).toBe(1);
    expect(nodes.A.status).toBe("done");
    expect(nodes.A.output).toEqual({ x: 1 });
    expect(events.length).toBe(1);
  });

  it("agent_usage 重复应用不翻倍 cost（seq 去重）", () => {
    const store = useWorkflowStore.getState();
    const usage = makeEvent("agent_usage", { seq: 5, data: { cost_usd: 0.1 } });
    store.processEvent(usage);
    store.processEvent(usage);
    expect(useWorkflowStore.getState().cost).toBeCloseTo(0.1, 5);
  });

  it("不同 seq 的同类事件累积（非去重误伤）", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("agent_usage", { seq: 1, data: { cost_usd: 0.1 } })
    );
    store.processEvent(
      makeEvent("agent_usage", { seq: 2, data: { cost_usd: 0.2 } })
    );
    expect(useWorkflowStore.getState().cost).toBeCloseTo(0.3, 5);
  });

  it("agent_usage per-node tokens 累计（SPEC §5.2 AgentsRail token 小字）", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } })
    );
    store.processEvent(makeEvent("node_started", { seq: 2, node: "A" }));
    store.processEvent(
      makeEvent("agent_usage", {
        seq: 3,
        node: "A",
        data: { input_tokens: 100, output_tokens: 50, reasoning_tokens: 30 },
      })
    );
    store.processEvent(
      makeEvent("agent_usage", {
        seq: 4,
        node: "A",
        data: { input_tokens: 20, output_tokens: 10 },
      })
    );
    const nodeA = useWorkflowStore.getState().nodes.A;
    expect(nodeA.inputTokens).toBe(120);
    expect(nodeA.outputTokens).toBe(60);
    expect(nodeA.reasoningTokens).toBe(30);
    expect(useWorkflowStore.getState().reasoningTokens).toBe(30);
  });

  // ── 4. node 状态 last-writer-wins ──
  it("node 状态 last-writer-wins（started → completed）", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(makeEvent("node_started", { seq: 1, node: "A" }));
    store.processEvent(
      makeEvent("node_completed", { seq: 2, node: "A", data: { output: "o" } })
    );
    expect(useWorkflowStore.getState().nodes.A.status).toBe("done");
    expect(useWorkflowStore.getState().nodes.A.output).toBe("o");
  });

  // ── 5. workflow status + gate + reasoning_tokens ──
  it("workflow_started/completed/failed 推 status；reasoning_tokens 累计", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "demo" } })
    );
    expect(useWorkflowStore.getState().status).toBe("running");
    expect(useWorkflowStore.getState().workflowName).toBe("demo");

    store.processEvent(
      makeEvent("agent_usage", {
        seq: 2,
        data: { cost_usd: 0.05, reasoning_tokens: 100 },
      })
    );
    expect(useWorkflowStore.getState().reasoningTokens).toBe(100);

    store.processEvent(
      makeEvent("workflow_completed", { seq: 3, data: { elapsed: 1.5 } })
    );
    expect(useWorkflowStore.getState().status).toBe("completed");
    expect(useWorkflowStore.getState().workflowElapsed).toBeCloseTo(1.5, 5);
  });

  it("human gate requested/resolved 推 gate 派生", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("human_decision_requested", {
        seq: 1,
        data: { gate_id: "g1", prompt: "继续？", options: ["yes", "no"] },
      })
    );
    expect(useWorkflowStore.getState().gate).toEqual({
      gate_id: "g1",
      prompt: "继续？",
      options: ["yes", "no"],
      source: undefined,
      context: undefined,
    });
    store.processEvent(
      makeEvent("human_decision_resolved", { seq: 2, data: { gate_id: "g1" } })
    );
    expect(useWorkflowStore.getState().gate).toBeNull();
  });

  // ── 6. D8 unknown_event / agent_step_started reducer no-op ──
  it("D8: unknown_event/agent_step_started reducer no-op（仅缓存 event）", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } })
    );
    const beforeStatus = useWorkflowStore.getState().status;
    const beforeCost = useWorkflowStore.getState().cost;
    const beforeReasoning = useWorkflowStore.getState().reasoningTokens;
    store.processEvent(
      makeEvent("unknown_event", {
        seq: 2,
        data: { raw: { foo: "bar" }, source: "opencode" },
      })
    );
    store.processEvent(
      makeEvent("agent_step_started", { seq: 3, data: { step_reason: "next" } })
    );
    const s = useWorkflowStore.getState();
    expect(s.status).toBe(beforeStatus); // 不投影
    expect(s.cost).toBe(beforeCost);
    expect(s.reasoningTokens).toBe(beforeReasoning);
    expect(s.events.length).toBe(3); // 仍缓存（LogStream 渲染）
  });

  // ── 7. D7 seq 升序 fold：序无关 ──
  it("D7: loadFromEvents 内部按 seq 升序 fold（reverse(T) 同结果）", () => {
    const events: WebEvent[] = [
      makeEvent("node_completed", { seq: 3, node: "A", data: { output: "end" } }),
      makeEvent("node_started", { seq: 1, node: "A" }),
      makeEvent("workflow_started", { seq: 0, data: { workflow_name: "x" } }),
      makeEvent("agent_usage", { seq: 2, data: { cost_usd: 0.1 } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const forward = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    const forwardCost = useWorkflowStore.getState().cost;

    resetStore();
    useWorkflowStore.getState().loadFromEvents([...events].reverse());
    const backward = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    const backwardCost = useWorkflowStore.getState().cost;

    expect(backward).toEqual(forward);
    expect(backwardCost).toBeCloseTo(forwardCost, 5);
  });

  it("loadFromEvents 幂等：同事件集 fold 两次结果一致", () => {
    const events: WebEvent[] = [
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } }),
      makeEvent("node_started", { seq: 2, node: "A" }),
      makeEvent("node_completed", { seq: 3, node: "A", data: { output: "o" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const snap1 = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    useWorkflowStore.getState().loadFromEvents(events);
    const snap2 = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    expect(snap2).toEqual(snap1);
  });

  // ── D7 processEvent out-of-order（绕开 loadFromEvents 的 sort）────────────
  it("D7: processEvent 乱序到达（WS resume 重放场景）终态 == 升序到达", () => {
    // 升序喂入
    resetStore();
    const forwardEvents: WebEvent[] = [
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } }),
      makeEvent("node_started", { seq: 2, node: "A" }),
      makeEvent("node_completed", {
        seq: 3,
        node: "A",
        data: { output: "fwd" },
      }),
      makeEvent("agent_usage", {
        seq: 4,
        node: "A",
        data: { cost_usd: 0.1, input_tokens: 10, output_tokens: 20 },
      }),
    ];
    for (const e of forwardEvents) useWorkflowStore.getState().processEvent(e);
    const fwdNodes = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    const fwdCost = useWorkflowStore.getState().cost;
    const fwdLastSeq = useWorkflowStore.getState().lastSeqSeen;

    // 乱序（倒序）喂入
    resetStore();
    for (const e of [...forwardEvents].reverse()) {
      useWorkflowStore.getState().processEvent(e);
    }
    const backNodes = JSON.parse(
      JSON.stringify(useWorkflowStore.getState().nodes)
    );
    const backCost = useWorkflowStore.getState().cost;
    const backLastSeq = useWorkflowStore.getState().lastSeqSeen;

    expect(backNodes).toEqual(fwdNodes);
    expect(backCost).toBeCloseTo(fwdCost, 5);
    expect(backLastSeq).toEqual(fwdLastSeq);
  });

  // ── 8. lastSeqSeen 派生（D6 WS resume 用）──
  it("lastSeqSeen = max(seq)（D6 resume 用）", () => {
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 5 }),
      makeEvent("node_started", { seq: 12, node: "A" }),
      makeEvent("node_completed", { seq: 8, node: "A" }),
    ]);
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(12);
  });

  // ── 9. loadRun / unloadRun ──
  it("loadRun：fetch /events → loadFromEvents → nodes 派生正确", async () => {
    const events: WebEvent[] = [
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "demo" } }),
      makeEvent("node_started", { seq: 2, node: "A" }),
      makeEvent("node_completed", { seq: 3, node: "A", data: { output: "o" } }),
    ];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => events,
    });
    vi.stubGlobal("fetch", fetchMock);

    await useWorkflowStore.getState().loadRun("run-xyz");

    expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-xyz/events");
    const state = useWorkflowStore.getState();
    expect(state.activeRunId).toBe("run-xyz");
    expect(state.workflowName).toBe("demo");
    expect(state.status).toBe("running");
    expect(state.nodes.A.status).toBe("done");
    expect(state.events.length).toBe(3);
    vi.unstubAllGlobals();
  });

  it("unloadRun：清空 events/nodes/gate/activeRunId", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [
        makeEvent("node_completed", { seq: 1, node: "A" }),
      ],
    });
    vi.stubGlobal("fetch", fetchMock);
    await useWorkflowStore.getState().loadRun("r1");
    expect(useWorkflowStore.getState().events.length).toBe(1);

    useWorkflowStore.getState().unloadRun();
    const state = useWorkflowStore.getState();
    expect(state.events).toEqual([]);
    expect(state.nodes).toEqual({});
    expect(state.gate).toBeNull();
    expect(state.activeRunId).toBeNull();
    expect(state.nodesIndex).toEqual({});
    expect(state.selectedNode).toBeNull();
    expect(state.selectedSession).toBeNull();
    vi.unstubAllGlobals();
  });
});

// ── P2：nodesIndex 四路径维护 + in-order 增量 fold vs refold 等价（D7）──────────────
// SPEC web-presentation-refinement §P2 / P0-5 / P0-6。
//
// 断言意图（Rule 9）：
//   1. **nodesIndex 一致性**：四路径（refold / loadFromEvents / loadEarlierChunk /
//      loadFull）+ in-order 增量 都维护 nodesIndex（P0-6 闭环）。
//   2. **D7 幂等不破**：in-order 增量 fold 是 seq 升序 fold 的特例 → 终态等价于
//      全量 refold（P0-5 闭环）。
//   3. **null session_id → "main"** 哨兵（SPEC §P2 接口契约）。
//   4. **setSelectedNode 联动**：设 selectedSession = 该 node 第一个 sub session（P1-3）。
describe("workflow-store P2 — nodesIndex 四路径 + 增量 fold D7", () => {
  beforeEach(() => resetStore());

  // 构造 family_detect 风格 fixture：1 main（null session）+ 2 sub sessions。
  // eventCount：main=2（node_started/completed lifecycle）；subA=3；subB=2。
  // 注：timestamp 显式设为递增值（makeEvent 默认 Date.now()/1000 秒级，连续事件可能同秒）。
  function buildFamilyFixture(): WebEvent[] {
    const events: WebEvent[] = [
      // main session（null → "main"）：lifecycle
      makeEvent("node_started", { seq: 1, node: "family_detect", session_id: null }),
      // sub A: 3 conversation events
      makeEvent("agent_message", { seq: 2, node: "family_detect", session_id: "ses_A", data: { text: "a1" } }),
      makeEvent("agent_thinking", { seq: 3, node: "family_detect", session_id: "ses_A", data: { text: "a2" } }),
      makeEvent("agent_tool_call", { seq: 4, node: "family_detect", session_id: "ses_A", data: { tool: "bash", tool_call_id: "ta" } }),
      // sub B: 2 conversation events
      makeEvent("agent_message", { seq: 5, node: "family_detect", session_id: "ses_B", data: { text: "b1" } }),
      makeEvent("agent_thinking", { seq: 6, node: "family_detect", session_id: "ses_B", data: { text: "b2" } }),
      // main 收尾
      makeEvent("node_completed", { seq: 7, node: "family_detect", session_id: null }),
      // 另一 node 的事件，验证索引不混淆
      makeEvent("node_started", { seq: 8, node: "other_node", session_id: null }),
      makeEvent("agent_message", { seq: 9, node: "other_node", session_id: "ses_C", data: { text: "c1" } }),
      // workflow 级（无 node）事件：不应进任何 node 的索引
      makeEvent("route_taken", { seq: 10, data: { from: "A", to: "B" } }),
    ];
    // 显式覆盖 timestamp 为 seq-based 单调递增（避免同秒冲突）
    events.forEach((e, i) => { e.timestamp = 1700000000 + i; });
    return events;
  }

  // ── 1. loadFromEvents（refold）维护 nodesIndex ──
  it("loadFromEvents → refold 维护 nodesIndex（main + 多 sub + 跨 node 隔离）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    const idx = useWorkflowStore.getState().nodesIndex;

    // family_detect: 3 sessions (main + ses_A + ses_B)，按首事件 seq 升序
    const fd = idx.family_detect;
    expect(fd).toBeDefined();
    expect(fd.sessions).toEqual(["main", "ses_A", "ses_B"]);
    expect(fd.sessionEventCounts).toEqual({
      main: 2, // node_started + node_completed
      ses_A: 3,
      ses_B: 2,
    });
    // firstTs：sessions 按首事件时序，main 最旧（seq=1 ts 最早）
    expect(fd.sessionFirstTs.main).toBeLessThan(fd.sessionFirstTs.ses_A);
    expect(fd.sessionFirstTs.ses_A).toBeLessThan(fd.sessionFirstTs.ses_B);

    // other_node: 2 sessions（main + ses_C）
    expect(idx.other_node.sessions).toEqual(["main", "ses_C"]);
    expect(idx.other_node.sessionEventCounts).toEqual({ main: 1, ses_C: 1 });
  });

  // ── 2. processEvent in-order 增量 patch nodesIndex ──
  it("processEvent in-order 到达 → 增量 patch nodesIndex（不全量 refold）", () => {
    const events = buildFamilyFixture();
    // 按 seq 升序逐条 processEvent（每条都 > lastSeqSeen → 走增量分支）
    for (const e of events) useWorkflowStore.getState().processEvent(e);

    const idx = useWorkflowStore.getState().nodesIndex;
    const fd = idx.family_detect;
    expect(fd.sessions).toEqual(["main", "ses_A", "ses_B"]);
    expect(fd.sessionEventCounts).toEqual({ main: 2, ses_A: 3, ses_B: 2 });
    expect(fd.sessionFirstTs.main).toBeLessThan(fd.sessionFirstTs.ses_A);
  });

  // ── 3. D7 等价：in-order 增量 vs 全量 refold ──（P0-5 闭环核心）
  it("D7: in-order 增量 fold 终态 == 全量 refold（nodes/nodesIndex/lastSeqSeen/cost）", () => {
    const events = buildFamilyFixture();

    // Path A：in-order 增量（processEvent 逐条，全部 > lastSeqSeen）
    resetStore();
    for (const e of events) useWorkflowStore.getState().processEvent(e);
    const incremental = {
      nodes: JSON.parse(JSON.stringify(useWorkflowStore.getState().nodes)),
      nodesIndex: JSON.parse(JSON.stringify(useWorkflowStore.getState().nodesIndex)),
      cost: useWorkflowStore.getState().cost,
      lastSeqSeen: useWorkflowStore.getState().lastSeqSeen,
      status: useWorkflowStore.getState().status,
    };

    // Path B：全量 refold（loadFromEvents 内部 sort + refold）
    resetStore();
    useWorkflowStore.getState().loadFromEvents(events);
    const refoldSnap = {
      nodes: JSON.parse(JSON.stringify(useWorkflowStore.getState().nodes)),
      nodesIndex: JSON.parse(JSON.stringify(useWorkflowStore.getState().nodesIndex)),
      cost: useWorkflowStore.getState().cost,
      lastSeqSeen: useWorkflowStore.getState().lastSeqSeen,
      status: useWorkflowStore.getState().status,
    };

    // 核心断言：两路径终态等价（D7 幂等 / 序无关）
    expect(incremental.nodes).toEqual(refoldSnap.nodes);
    expect(incremental.nodesIndex).toEqual(refoldSnap.nodesIndex);
    expect(incremental.cost).toBeCloseTo(refoldSnap.cost, 5);
    expect(incremental.lastSeqSeen).toEqual(refoldSnap.lastSeqSeen);
    expect(incremental.status).toEqual(refoldSnap.status);
  });

  // ── 4. out-of-order 到达 → refold（nodesIndex 重建正确） ──
  it("processEvent out-of-order（seq < lastSeqSeen）→ 全量 refold 重建 nodesIndex", () => {
    // 先 in-order 喂两条大 seq 事件（lastSeqSeen=8）
    useWorkflowStore.getState().processEvent(
      makeEvent("agent_message", { seq: 5, node: "n", session_id: "s1", data: { text: "x" } })
    );
    useWorkflowStore.getState().processEvent(
      makeEvent("agent_message", { seq: 8, node: "n", session_id: "s2", data: { text: "y" } })
    );
    // 喂 seq=3（out-of-order：3 < lastSeqSeen=8）→ 触发 refold，nodesIndex 全量重建
    useWorkflowStore.getState().processEvent(
      makeEvent("agent_thinking", { seq: 3, node: "n", session_id: "s1", data: { text: "z" } })
    );
    const idx = useWorkflowStore.getState().nodesIndex.n;
    expect(idx.sessionEventCounts.s1).toBe(2); // seq 5 + 3
    expect(idx.sessionEventCounts.s2).toBe(1);
    // sessions 顺序按首事件 seq 升序：s1(seq=3) 在 s2(seq=5) 之前
    expect(idx.sessions).toEqual(["s1", "s2"]);
  });

  // ── 5. setSelectedNode 联动：设第一个 sub session（P1-3） ──
  it("setSelectedNode：同步设 selectedSession = 该 node 第一个 sub session（无 main 偏好）", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    useWorkflowStore.getState().setSelectedNode("family_detect");
    // 第一个 sub = ses_A（sessions=[main, ses_A, ses_B]，跳 main 后第一个）
    expect(useWorkflowStore.getState().selectedNode).toBe("family_detect");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_A");

    // 切到 only-main node → "all"（无 sub 可选）
    useWorkflowStore.getState().setSelectedNode("other_node");
    // other_node sessions = [main, ses_C]，第一个 sub = ses_C
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_C");
  });

  it("setSelectedNode：无 sub session 的 node → selectedSession='all'", () => {
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("node_started", { seq: 1, node: "solo", session_id: null }),
      makeEvent("node_completed", { seq: 2, node: "solo", session_id: null }),
    ]);
    useWorkflowStore.getState().setSelectedNode("solo");
    expect(useWorkflowStore.getState().selectedSession).toBe("all");
  });

  it("setSelectedNode(null) → selectedSession=null；setSelectedSession 可独立切", () => {
    useWorkflowStore.getState().loadFromEvents(buildFamilyFixture());
    useWorkflowStore.getState().setSelectedNode("family_detect");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_A");

    useWorkflowStore.getState().setSelectedSession("ses_B");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_B");

    useWorkflowStore.getState().setSelectedSession("all");
    expect(useWorkflowStore.getState().selectedSession).toBe("all");

    useWorkflowStore.getState().setSelectedNode(null);
    expect(useWorkflowStore.getState().selectedSession).toBeNull();
  });
});
