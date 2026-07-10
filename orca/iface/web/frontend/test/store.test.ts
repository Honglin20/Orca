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
    vi.unstubAllGlobals();
  });
});
