// test/node-output.test.tsx —— SPEC-B B1：node_completed 渲染为 output block。
//
// 覆盖 spec-reviewer 必修项 + plan §验收：
//   1. buildEntries：node_completed → node-output（非 node-divider）
//   2. node_started / node_skipped 仍走 node-divider（边界感保留，spec-reviewer #3）
//   3. NodeOutputBlock 三分支 DOM 断言：string → MarkdownText / dict → pre JSON /
//      null → dim（spec-reviewer BLOCKER：dict 不显 [object Object]）
//   4. ConversationView 端到端：data-testid=node-output 出现，output 文字可见
//
// 注：真机 web 显示验证（in-session 跑 wf 看前端）交 test-agent E2E，本文件只覆盖
// 纯前端逻辑（buildEntries + 组件渲染）。

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { buildEntries } from "@/components/conversation/entries";
import { NodeOutputBlock } from "@/components/conversation/NodeOutputBlock";
import { ConversationView } from "@/components/views/ConversationView";
import type { EventType, WebEvent } from "@/types/events";

let _seq = 0;
function ev(type: EventType, overrides: Partial<WebEvent> = {}): WebEvent {
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

afterEach(() => {
  cleanup();
});

// ── buildEntries 分派（SPEC-B B1 核心：升格，非 divider）──────────────────────
describe("buildEntries — node_completed → node-output", () => {
  beforeEach(() => resetStore());

  it("node_completed 产出 node-output entry（不再进 node-divider 分支）", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n" }),
      ev("node_completed", { node: "n", data: { output: "done" } }),
    ];
    const entries = buildEntries(events);
    expect(entries.map((e) => e.kind)).toEqual(["node-divider", "node-output"]);
  });

  it("node_started / node_skipped 仍走 node-divider（边界感保留）", () => {
    // 回归保护：spec-reviewer #3 —— node_completed 升格后，边界由 node_started 提供。
    const events: WebEvent[] = [
      ev("node_started", { node: "n" }),
      ev("node_skipped", { node: "n2" }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(2);
    expect(entries.every((e) => e.kind === "node-divider")).toBe(true);
  });

  it("无 data.output 的 node_completed 也产 node-output（output 字段可选）", () => {
    const events: WebEvent[] = [ev("node_completed", { node: "n" })];
    const entries = buildEntries(events);
    expect(entries.length).toBe(1);
    expect(entries[0].kind).toBe("node-output");
  });

  it("node-output entry 携带原事件（output 可由渲染层读取）", () => {
    const events: WebEvent[] = [
      ev("node_completed", { node: "n", data: { output: "PAYLOAD" } }),
    ];
    const entries = buildEntries(events);
    const e = entries[0] as { kind: string; event: WebEvent };
    expect(e.event.data?.output).toBe("PAYLOAD");
  });
});

// ── NodeOutputBlock 三分支（spec-reviewer BLOCKER：dict 不显 [object Object]）──
describe("NodeOutputBlock — typeof data.output 分支", () => {
  it("string output → MarkdownText 渲染（markdown 生效，非 JSON pre）", () => {
    const event = ev("node_completed", {
      node: "n",
      data: { output: "## HEADER_TITLE\n\nbody text" },
    });
    render(React.createElement(NodeOutputBlock, { event }));
    expect(screen.getByTestId("node-output")).toBeTruthy();
    // markdown header 渲染（h2 文本可见）
    expect(screen.getByText("HEADER_TITLE")).toBeTruthy();
    // 不走 JSON 分支
    expect(screen.queryByTestId("node-output-json")).toBeNull();
    expect(screen.queryByTestId("node-output-empty")).toBeNull();
  });

  it("dict output → <pre> JSON（不显 [object Object]）", () => {
    // spec-reviewer BLOCKER：step._parse_output 在 output_schema 声明时返 dict。
    const event = ev("node_completed", {
      node: "n",
      data: { output: { selected: ["a", "b"], score: 0.87 } },
    });
    const { container } = render(
      React.createElement(NodeOutputBlock, { event })
    );
    expect(screen.getByTestId("node-output-json")).toBeTruthy();
    // JSON 内容出现（结构化呈现）
    expect(container.textContent).toContain('"selected"');
    expect(container.textContent).toContain('"a"');
    expect(container.textContent).toContain("0.87");
    // 关键反例：绝不显 [object Object]
    expect(container.textContent?.includes("[object Object]")).toBe(false);
  });

  it("list output（array）→ <pre> JSON", () => {
    const event = ev("node_completed", {
      node: "n",
      data: { output: [1, 2, 3] },
    });
    const { container } = render(
      React.createElement(NodeOutputBlock, { event })
    );
    expect(screen.getByTestId("node-output-json")).toBeTruthy();
    expect(container.textContent).toContain("1");
    expect(container.textContent).toContain("3");
  });

  it("number output → <pre> JSON（防御性，不静默丢）", () => {
    const event = ev("node_completed", {
      node: "n",
      data: { output: 42 },
    });
    const { container } = render(
      React.createElement(NodeOutputBlock, { event })
    );
    expect(screen.getByTestId("node-output-json")).toBeTruthy();
    expect(container.textContent).toContain("42");
  });

  it("boolean output → <pre> JSON（防御性，不静默丢）", () => {
    const event = ev("node_completed", {
      node: "n",
      data: { output: true },
    });
    const { container } = render(
      React.createElement(NodeOutputBlock, { event })
    );
    expect(screen.getByTestId("node-output-json")).toBeTruthy();
    expect(container.textContent).toContain("true");
  });

  it("循环引用 output → safeJson 降级 String，不 throw 不崩", () => {
    // 裸 JSON.stringify 在循环引用上 throw TypeError——会炸整个 ConversationView
    // 渲染循环。safeJson（_shared）兜底降级。后端正常路径不会产循环引用，但
    // 防御性组件不应留 throw 缺口（code-reviewer 🟡#1）。
    const cyclic: Record<string, unknown> = { a: 1 };
    cyclic.self = cyclic;
    const event = ev("node_completed", {
      node: "n",
      data: { output: cyclic },
    });
    const { container } = render(
      React.createElement(NodeOutputBlock, { event })
    );
    expect(screen.getByTestId("node-output-json")).toBeTruthy();
    // 降级路径产出非空字符串（String(cyclic) 含 [object Object] 字面也无妨——
    // 关键是不 throw、渲染稳定）。
    expect(container.textContent?.length).toBeGreaterThan(0);
  });

  it("null output → dim 「（无 output）」", () => {
    const event = ev("node_completed", {
      node: "n",
      data: { output: null },
    });
    render(React.createElement(NodeOutputBlock, { event }));
    expect(screen.getByTestId("node-output-empty")).toBeTruthy();
    expect(screen.getByText("（无 output）")).toBeTruthy();
    expect(screen.queryByTestId("node-output-json")).toBeNull();
  });

  it("缺 data.output（undefined）→ dim 「（无 output）」", () => {
    const event = ev("node_completed", { node: "n", data: {} });
    render(React.createElement(NodeOutputBlock, { event }));
    expect(screen.getByTestId("node-output-empty")).toBeTruthy();
  });

  it("node 标签出现在 header（边界感）", () => {
    const event = ev("node_completed", {
      node: "search-1",
      data: { output: "x" },
    });
    render(React.createElement(NodeOutputBlock, { event }));
    expect(screen.getByText(/search-1 output/)).toBeTruthy();
  });
});

// ── ConversationView 端到端（B1 用户验收：output 文字真可见）─────────────────
describe("ConversationView — node_completed output 端到端", () => {
  beforeEach(() => resetStore());

  it("string output 经 ConversationView 渲染为 node-output block（文字可见）", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "mid-conversation" },
      }),
      ev("node_completed", {
        node: "n",
        data: { output: "FINAL_OUTPUT_VISIBLE_TEXT" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));

    // node-output testid 存在
    expect(screen.getByTestId("node-output")).toBeTruthy();
    // output 文字真可见（B1 核心痛点修复）
    expect(screen.getByText("FINAL_OUTPUT_VISIBLE_TEXT")).toBeTruthy();
    // node_started 仍为 node-divider（边界感保留）
    expect(screen.getAllByTestId("node-divider").length).toBe(1);
    // 不应再为 node_completed 出现 node-divider（升格强约束）
    // （node_started 一条 + 无其它 divider 来源 → 总数=1 已隐含验证）
  });

  it("dict output 经 ConversationView 不崩、不显 [object Object]", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("node_completed", {
        node: "n",
        data: { output: { records: 3, best: "x" } },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const { container } = render(
      React.createElement(ConversationView, { nodeId: "n" })
    );
    expect(screen.getByTestId("node-output")).toBeTruthy();
    expect(container.textContent?.includes("[object Object]")).toBe(false);
    expect(container.textContent).toContain('"records"');
    expect(container.textContent).toContain("3");
  });

  it("null output 经 ConversationView 渲染 dim 占位", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("node_completed", { node: "n", data: { output: null } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByTestId("node-output-empty")).toBeTruthy();
    expect(screen.getByText("（无 output）")).toBeTruthy();
  });

  it("chart-ref 与 node-output 共存（互不干扰）", () => {
    // plan §验收 #1：chart-ref 独立渲染不受影响。
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("custom", {
        node: "n",
        session_id: "s",
        data: { kind: "chart", chart: { title: "loss curve" } },
      }),
      ev("node_completed", {
        node: "n",
        data: { output: "TEXT_OUTPUT" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByTestId("chart-ref-row")).toBeTruthy();
    expect(screen.getByTestId("node-output")).toBeTruthy();
    expect(screen.getByText("TEXT_OUTPUT")).toBeTruthy();
  });
});
