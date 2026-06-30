// test/log-detail.test.tsx —— Log Stream + Node Detail 验收（SPEC §5.3 / plan C3.3）。
//
// 断言意图（Rule 9）：
//   1. **虚拟滚动**：1000 事件渲染的 DOM row < 50（react-window 只渲染可见区 + overscan）
//   2. **session 分组**：连续相同 session 的事件归一组（isGroupStart 标记）
//   3. **replay 同步**：replay 模式只显示 events[0..replayPosition]
//   4. **NodeDetail**：选中节点 + replay 快照

import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { LogStream, formatLogLine } from "@/components/detail/LogStream";
import { NodeDetail } from "@/components/detail/NodeDetail";
import { useWorkflowStore } from "@/stores/workflow-store";
import { buildDemoStream, mkEvent, resetSeq } from "./fixtures/events";

function resetStore() {
  resetSeq();
  useWorkflowStore.setState({
    events: [],
    nodes: {},
    gate: null,
    workflowName: "",
    status: "idle",
    cost: 0,
    workflowDef: null,
    selectedNode: null,
    replayMode: false,
    replayPosition: 0,
    activeRunId: null,
  });
}

describe("LogStream: 虚拟滚动", () => {
  beforeEach(() => resetStore());

  it("1000 事件只渲染少量 DOM row（react-window，< 50）", () => {
    // 注入 1000 事件
    const events = Array.from({ length: 1000 }, (_, i) =>
      mkEvent({
        type: "agent_message",
        node: "n",
        session_id: "s1",
        data: { text: `msg ${i}` },
      })
    );
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));

    const { container } = render(<LogStream />);
    // react-window 渲染的 row 带 data-testid=log-row-N
    const rows = container.querySelectorAll('[data-testid^="log-row-"]');
    // 虚拟滚动：只渲染可见 + overscan，远少于 1000
    expect(rows.length).toBeLessThan(50);
    expect(rows.length).toBeGreaterThan(0);
  });
});

describe("LogStream: session 分组", () => {
  beforeEach(() => resetStore());

  it("连续相同 session 的事件，只有第一条标 isGroupStart", () => {
    // 2 个 session：s1（3 条）+ s2（2 条）
    const events = [
      mkEvent({ type: "agent_message", node: "a", session_id: "s1", data: { text: "1" } }),
      mkEvent({ type: "agent_message", node: "a", session_id: "s1", data: { text: "2" } }),
      mkEvent({ type: "agent_message", node: "a", session_id: "s1", data: { text: "3" } }),
      mkEvent({ type: "agent_message", node: "a", session_id: "s2", data: { text: "4" } }),
      mkEvent({ type: "agent_message", node: "a", session_id: "s2", data: { text: "5" } }),
    ];
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));

    const { container } = render(<LogStream />);
    // session 头（uppercase indigo）数量 = 2（s1 组 + s2 组）
    const sessionHeaders = container.querySelectorAll(".uppercase.text-indigo-500");
    expect(sessionHeaders.length).toBe(2);
  });
});

describe("LogStream: replay 同步", () => {
  beforeEach(() => resetStore());

  it("replay 模式只显示 events[0..replayPosition]", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    // 进 replay，拨到 pos=2（只显示前 3 条）
    useWorkflowStore.getState().enterReplay();
    useWorkflowStore.getState().setReplayTarget(2);

    const { container } = render(<LogStream />);
    const rows = container.querySelectorAll('[data-testid^="log-row-"]');
    // 应只渲染 events[0..2] 共 3 条（+ react-window 全部可见因少）
    expect(rows.length).toBeLessThanOrEqual(3);
    expect(rows.length).toBeGreaterThan(0);
  });

  it("live 模式显示全部事件", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    const { container } = render(<LogStream />);
    const rows = container.querySelectorAll('[data-testid^="log-row-"]');
    // demo 流 9 条（workflow_started + 2 nodes×3 + workflow_completed = 1+6+1=... 实际更多）
    // 只断言 ≥ 全部事件数的一部分（虚拟滚动可能不全渲染，但应 > 3）
    expect(rows.length).toBeGreaterThan(3);
  });
});

describe("formatLogLine", () => {
  it("workflow_started 格式化含 workflow 名", () => {
    const line = formatLogLine({
      seq: 1,
      type: "workflow_started",
      timestamp: 1_000_000,
      node: null,
      session_id: null,
      data: { workflow_name: "demo" },
    });
    expect(line).toContain("demo");
    expect(line).toContain("workflow");
  });

  it("route_taken 格式化含 from → to", () => {
    const line = formatLogLine({
      seq: 2,
      type: "route_taken",
      timestamp: 1,
      node: null,
      session_id: null,
      data: { from: "a", to: "b" },
    });
    expect(line).toContain("a");
    expect(line).toContain("b");
    expect(line).toContain("→");
  });
});

describe("NodeDetail", () => {
  beforeEach(() => resetStore());

  it("无选中节点 → 显示提示", () => {
    render(<NodeDetail />);
    expect(screen.getByTestId("detail-empty")).toBeDefined();
  });

  it("选中节点 → 显示 status + 该节点事件", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().setSelectedNode("start");

    render(<NodeDetail />);
    expect(screen.getByTestId("detail-name").textContent).toBe("start");
    expect(screen.getByTestId("detail-status").textContent).toBe("done");
    // start 节点的事件（started + completed + route_taken）
    const eventRows = screen.queryAllByTestId(/^detail-event-/);
    expect(eventRows.length).toBeGreaterThan(0);
  });

  it("replay 模式显示 replayPos 时的快照", () => {
    const events = buildDemoStream();
    events.forEach((e) => useWorkflowStore.getState().processEvent(e));
    useWorkflowStore.getState().enterReplay();
    // 拨到 pos=2（start 刚 completed，decide 未开始）
    useWorkflowStore.getState().setReplayTarget(2);
    useWorkflowStore.getState().setSelectedNode("start");

    render(<NodeDetail />);
    // start 在 pos=2 已完成
    expect(screen.getByTestId("detail-status").textContent).toBe("done");
    // detail header 显示 replay 标记
    expect(within(screen.getByTestId("node-detail")).getByText(/replay/)).toBeDefined();
  });
});
