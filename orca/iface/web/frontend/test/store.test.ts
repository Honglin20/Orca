// test/store.test.ts —— Zustand 单 store 验收（SPEC §7.3 / plan B2.2）。
//
// 断言意图（Rule 9）：
//   1. **单 store**（铁律 4）：全 src 只一个 create()
//   2. **eventHandlers 覆盖全部 EventType**（21 个，对齐 orca/schema/event.py EventType Literal）
//   3. **fold 幂等**（铁律 4 / §3.2.3）：同事件 N 次应用 = 状态一致（不翻倍/不拼接）
//   4. **loadRun**：fetch /events → replayState → nodes 派生正确
//   5. **unloadRun**：清 events/nodes/gate（懒加载红线）

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  HANDLED_EVENT_TYPES,
  useWorkflowStore,
} from "@/stores/workflow-store";
import type { EventType, WorkflowEvent } from "@/types/events";

// ── 后端 EventType 全集（逐字对齐 orca/schema/event.py EventType Literal，21 个）──
// 测试断言 handler 表覆盖这 21 个（unknown 事件类型静默忽略，但已知全部必须覆盖）。
const ALL_EVENT_TYPES: EventType[] = [
  "workflow_started",
  "workflow_completed",
  "workflow_failed",
  "node_started",
  "node_completed",
  "node_failed",
  "node_skipped",
  "agent_message",
  "agent_thinking",
  "agent_tool_call",
  "agent_tool_result",
  "agent_usage",
  "route_taken",
  "foreach_started",
  "foreach_item_started",
  "foreach_item_completed",
  "foreach_completed",
  "human_decision_requested",
  "human_decision_resolved",
  "custom",
  "error",
];

function makeEvent(
  type: EventType,
  overrides: Partial<WorkflowEvent> = {}
): WorkflowEvent {
  return {
    seq: Math.random(),
    type,
    timestamp: Date.now() / 1000,
    node: overrides.node ?? null,
    session_id: null,
    data: overrides.data ?? {},
    ...overrides,
  };
}

// 重置 store 到初始（每个测试独立）
function resetStore() {
  useWorkflowStore.setState({
    events: [],
    nodes: {},
    gate: null,
    workflowName: "",
    status: "idle",
    cost: 0,
    selectedNode: null,
    replayMode: false,
    replayPosition: 0,
    activeRunId: null,
  });
}

describe("workflow-store", () => {
  beforeEach(() => resetStore());

  // ── 1. 单 store（铁律 4）───────────────────────────────────────────────────
  it("源码中只存在一个 zustand create()（单 store 铁律）", async () => {
    // 读 workflow-store.ts 源码，断言 `create<` 只出现一次（一个 store）
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

  // ── 2. eventHandlers 覆盖全部 EventType ────────────────────────────────────
  it("eventHandlers 覆盖全部 phase-1 EventType（21 个）", () => {
    // HANDLED_EVENT_TYPES 是 eventHandlers 的 keys；断言每个已知 type 都有 handler
    for (const t of ALL_EVENT_TYPES) {
      expect(HANDLED_EVENT_TYPES, `handler 缺 ${t}`).toContain(t);
    }
    // 反向：handler 表里没有「未知」type（防 drift）
    for (const t of HANDLED_EVENT_TYPES) {
      expect(ALL_EVENT_TYPES, `handler 多了未知 type ${t}`).toContain(t);
    }
  });

  // ── 3. fold 幂等（铁律 4 / §3.2.3）─────────────────────────────────────────
  it("同事件应用两次：nodes 状态一致 + 不重复 + cost 不翻倍（fold 幂等）", () => {
    const store = useWorkflowStore.getState();
    const ev = makeEvent("node_completed", {
      seq: 1,
      node: "A",
      data: { output: { x: 1 }, elapsed: 0.1 },
    });
    store.processEvent(ev);
    store.processEvent(ev); // 同事件再应用一次

    const { nodes, events } = useWorkflowStore.getState();
    // 不重复：nodes 只有一个 A
    expect(Object.keys(nodes).length).toBe(1);
    expect(nodes.A.status).toBe("done");
    expect(nodes.A.output).toEqual({ x: 1 });
    // events 按 seq 去重：只有一条
    expect(events.length).toBe(1);
  });

  it("agent_usage 重复应用不翻倍 cost（seq 去重保证幂等）", () => {
    const store = useWorkflowStore.getState();
    const usage = makeEvent("agent_usage", {
      seq: 5,
      data: { cost_usd: 0.1 },
    });
    store.processEvent(usage);
    store.processEvent(usage); // 同事件再应用
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

  // ── 4. node 状态 last-writer-wins ──────────────────────────────────────────
  it("node 状态 last-writer-wins（started → completed 顺序覆盖）", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(makeEvent("node_started", { seq: 1, node: "A" }));
    store.processEvent(
      makeEvent("node_completed", { seq: 2, node: "A", data: { output: "o" } })
    );
    const { nodes } = useWorkflowStore.getState();
    expect(nodes.A.status).toBe("done");
    expect(nodes.A.output).toBe("o");
  });

  it("workflow_started/completed/failed 推动 workflow 级 status", () => {
    const store = useWorkflowStore.getState();
    store.processEvent(
      makeEvent("workflow_started", {
        seq: 1,
        data: { workflow_name: "demo" },
      })
    );
    expect(useWorkflowStore.getState().status).toBe("running");
    expect(useWorkflowStore.getState().workflowName).toBe("demo");

    store.processEvent(makeEvent("workflow_completed", { seq: 2 }));
    expect(useWorkflowStore.getState().status).toBe("completed");

    resetStore();
    useWorkflowStore.getState().processEvent(
      makeEvent("workflow_failed", { seq: 1 })
    );
    expect(useWorkflowStore.getState().status).toBe("failed");
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

  it("未知 type 不 crash 但 warn（fail loud，仅缓存 event）", () => {
    const store = useWorkflowStore.getState();
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const unknown = {
      seq: 1,
      type: "totally_unknown_type" as EventType,
      timestamp: 1,
      node: null,
      session_id: null,
      data: {},
    } as WorkflowEvent;
    expect(() => store.processEvent(unknown)).not.toThrow();
    // fail loud：未知 type 记 warn（让 handler 缺失可被发现）
    expect(warnSpy).toHaveBeenCalled();
    // 仍缓存（让 9c/9d 可读），但派生态不变
    expect(useWorkflowStore.getState().events.length).toBe(1);
    expect(useWorkflowStore.getState().status).toBe("idle");
    warnSpy.mockRestore();
  });

  // ── 5. loadRun（fetch /events → replayState → nodes 正确）────────────────
  it("loadRun：fetch /events → replayState → nodes 派生正确", async () => {
    const events: WorkflowEvent[] = [
      makeEvent("workflow_started", {
        seq: 1,
        data: { workflow_name: "demo" },
      }),
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

  it("loadRun 失败保持 idle（fail loud 记 console）", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal("fetch", fetchMock);
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    await useWorkflowStore.getState().loadRun("missing");
    expect(useWorkflowStore.getState().status).toBe("idle");
    expect(useWorkflowStore.getState().activeRunId).toBeNull();
    expect(errSpy).toHaveBeenCalled();
    vi.unstubAllGlobals();
    errSpy.mockRestore();
  });

  // ── 6. unloadRun（懒加载红线）──────────────────────────────────────────────
  it("unloadRun：清空 events/nodes/gate/activeRunId（懒加载红线）", async () => {
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

  // ── 7. replayState 重置再 fold（共用 processEvent，反双路径）───────────────
  it("replayState：重置派生态后逐条 fold（live/replay 共用单路径）", () => {
    const store = useWorkflowStore.getState();
    // 先污染
    store.processEvent(
      makeEvent("node_completed", { seq: 100, node: "OLD" })
    );
    expect(useWorkflowStore.getState().nodes.OLD).toBeDefined();

    // replayState 重置后只含新 events
    store.replayState([
      makeEvent("node_completed", { seq: 1, node: "NEW" }),
    ]);
    const { nodes, events } = useWorkflowStore.getState();
    expect(nodes.NEW.status).toBe("done");
    expect(nodes.OLD).toBeUndefined(); // 旧派生清掉
    expect(events.length).toBe(1);
  });
});
