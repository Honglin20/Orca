// test/conversation.test.tsx —— ConversationView 渲染 + 折叠 oracle + ▎ IFF + 工具配对/成组。
//
// SPEC §5.3 / §9 AC1 / §10 fixture：
//   1. 折叠 oracle：buildEntries(T) 输出的 entry kinds 与预期精确相等
//   2. ▎ oracle：finished tape → 0 cursor；running + last=message → cursor 出现
//   3. 工具配对/成组：连续 tool → 1 group；中间有 message → 拆开
//   4. orphan tool_result 不进 conversation（warn）
//   5. smart arg 格式：bash → $ cmd；read → basename；render_chart → chart_type|title
//   6. markdown 渲染：table / LaTeX / code 在 DOM 出现

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import * as React from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectStreamingCursor,
  selectConversation,
} from "@/selectors";
import {
  buildEntries,
  type ConvEntry,
} from "@/components/conversation/entries";
import { ConversationView } from "@/components/views/ConversationView";
import { previewArgs } from "@/components/conversation/tool-args";
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
    nodesIndex: {},
    selectedNode: null,
    selectedSession: null,
    activeRunId: null,
  });
}

afterEach(() => {
  cleanup();
});

describe("buildEntries — 折叠 oracle (SPEC §5.3 / §9 AC1)", () => {
  beforeEach(() => resetStore());

  it("prompt / thinking / message / chart-ref / unknown / status-line / step-marker / node-divider / node-output / node-error 全分类", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n" }),
      ev("prompt_rendered", { node: "n", session_id: "s1", data: { preview: "hi" } }),
      ev("agent_step_started", { node: "n", session_id: "s1", data: { step_reason: "step1" } }),
      ev("agent_thinking", { node: "n", session_id: "s1", data: { text: "hmm" } }),
      ev("agent_message", { node: "n", session_id: "s1", data: { text: "answer" } }),
      ev("retry_started", { node: "n", session_id: "s1", data: { attempt: 1, max_attempts: 3, kind: "x" } }),
      ev("custom", { node: "n", session_id: "s1", data: { kind: "chart", chart: { title: "loss" } } }),
      ev("custom", { node: "n", session_id: "s1", data: { kind: "other", x: 1 } }),
      ev("unknown_event", { node: "n", data: { source: "opencode" } }),
      ev("node_failed", { node: "n", data: { kind: "boom", message: "err" } }),
      ev("node_completed", { node: "n" }),
    ];
    const entries = buildEntries(events);
    const kinds = entries.map((e) => e.kind);
    // step_marker 应附到 thinking 而非独立 entry；
    // node_completed 升格为 node-output（B1：显示 output 文字），不再作 node-divider。
    expect(kinds).toEqual([
      "node-divider",
      "prompt",
      "thinking", // step_marker 附此
      "message",
      "status-line",
      "chart-ref",
      "custom-generic",
      "unknown",
      "node-error",
      "node-output",
    ]);
  });

  it("孤立 agent_step_started（无后续 thinking/message）→ step-marker entry", () => {
    const events: WebEvent[] = [
      ev("agent_step_started", { node: "n", data: { step_reason: "orphan" } }),
      ev("node_completed", { node: "n" }),
    ];
    const entries = buildEntries(events);
    expect(entries.map((e) => e.kind)).toContain("step-marker");
  });

  it("orphan tool_result（无 call）→ 不进 entries（warn）", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const events: WebEvent[] = [
      ev("agent_tool_result", {
        node: "n",
        session_id: "s1",
        data: { tool_call_id: "orphan1", result: "x" },
      }),
      ev("agent_message", { node: "n", session_id: "s1", data: { text: "hi" } }),
    ];
    const entries = buildEntries(events);
    // 不应有任何 tool-single / tool-group entry（orphan 被剔除）
    expect(entries.some((e) => e.kind === "tool-single" || e.kind === "tool-group")).toBe(false);
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe("buildEntries — 工具配对/成组 (SPEC §5.3)", () => {
  beforeEach(() => resetStore());

  it("连续 tool_call+result 无 message 间隔 → 1 个 tool-group", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "t1", args: { command: "ls" } } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "t1", result: "ok" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "t2", args: { command: "pwd" } } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "t2", result: "ok" } }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(1);
    expect(entries[0].kind).toBe("tool-group");
    expect((entries[0] as { pairs: unknown[] }).pairs.length).toBe(2);
  });

  it("中间有 agent_message → 拆为两个 tool-single", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "a", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "a", result: "x" } }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "between" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "b", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "b", result: "y" } }),
    ];
    const entries = buildEntries(events);
    const kinds = entries.map((e) => e.kind);
    expect(kinds).toEqual(["tool-single", "message", "tool-single"]);
  });

  it("pending tool（无 result）→ tool-single，pair.result 为 undefined", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "p", args: { command: "sleep 1" } } }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(1);
    const e = entries[0] as { kind: string; pair: { call?: WebEvent; result?: WebEvent } };
    expect(e.kind).toBe("tool-single");
    expect(e.pair.result).toBeUndefined();
  });

  it("单个 tool 对（仅一对）→ tool-single，不成 group", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "z", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "z", result: "ok" } }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(1);
    expect(entries[0].kind).toBe("tool-single");
  });

  it("乱序：result 在 call 之前（同 session 同 tool_call_id）→ 仍正确配对", () => {
    const events: WebEvent[] = [
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "rev", result: "ok" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "rev", args: {} } }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(1);
    const e = entries[0] as { kind: string; pair: { call?: WebEvent; result?: WebEvent } };
    expect(e.pair.call).toBeDefined();
    expect(e.pair.result).toBeDefined();
  });
});

describe("previewArgs — smart 一行摘要 (SPEC §5.3)", () => {
  it("bash → $ cmd", () => {
    expect(previewArgs("bash", { command: "ls -la" })).toBe("$ ls -la");
  });
  it("bash truncate", () => {
    const long = "x".repeat(80);
    const out = previewArgs("bash", { command: long });
    expect(out.startsWith("$ ")).toBe(true);
    expect(out.length).toBeLessThanOrEqual(60);
  });
  it("read → basename", () => {
    expect(previewArgs("read", { path: "/a/b/c.txt" })).toBe("c.txt");
  });
  it("write → basename", () => {
    expect(previewArgs("write", { path: "/x/y/app.tsx" })).toBe("app.tsx");
  });
  it("render_chart → chart_type | title", () => {
    expect(
      previewArgs("render_chart", { chart_type: "line", title: "loss curve" })
    ).toBe("line | loss curve");
  });
  it("render_chart 无 title → 仅 chart_type", () => {
    expect(previewArgs("render_chart", { chart_type: "bar" })).toBe("bar");
  });
  it("其它 → k=val, k=val", () => {
    const out = previewArgs("search", { q: "hello", limit: 10 });
    expect(out).toContain("q=hello");
    expect(out).toContain("limit=10");
  });
});

describe("selectStreamingCursor — ▎ IFF (SPEC §5.3 闭 review #4)", () => {
  beforeEach(() => resetStore());

  it("finished tape (status=completed) → cursor 不显", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "final" } }),
      ev("node_completed", { node: "n" }),
      ev("workflow_completed", { data: { elapsed: 5 } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    expect(selectStreamingCursor(useWorkflowStore.getState(), "n")).toBe(false);
  });

  it("running + last event=agent_message → cursor 显", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "typing" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    expect(selectStreamingCursor(useWorkflowStore.getState(), "n")).toBe(true);
  });

  it("running + 后续 tool_call → cursor 不显", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "typing" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "t", args: {} } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    expect(selectStreamingCursor(useWorkflowStore.getState(), "n")).toBe(false);
  });

  it("running + 后续 node_completed → cursor 不显", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_thinking", { node: "n", session_id: "s", data: { text: "hmm" } }),
      ev("node_completed", { node: "n" }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    expect(selectStreamingCursor(useWorkflowStore.getState(), "n")).toBe(false);
  });
});

describe("ConversationView — DOM 渲染 (happy-dom)", () => {
  beforeEach(() => resetStore());

  it("prompt/thinking/message/tool/error 全渲染", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s1" }),
      ev("prompt_rendered", { node: "n", session_id: "s1", data: { preview: "do thing" } }),
      ev("agent_thinking", { node: "n", session_id: "s1", data: { text: "ponder" } }),
      ev("agent_message", { node: "n", session_id: "s1", data: { text: "hello world" } }),
      ev("agent_tool_call", { node: "n", session_id: "s1", data: { tool: "bash", tool_call_id: "t1", args: { command: "ls" } } }),
      ev("agent_tool_result", { node: "n", session_id: "s1", data: { tool_call_id: "t1", result: "file.txt" } }),
      ev("node_failed", { node: "n", data: { kind: "exec_error", message: "boom" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);

    render(
      React.createElement(ConversationView, { nodeId: "n" })
    );

    // 关键 entry 存在
    expect(screen.getByTestId("prompt-row")).toBeTruthy();
    expect(screen.getByTestId("thinking-block")).toBeTruthy();
    expect(screen.getByTestId("message-block")).toBeTruthy();
    // 单 tool → tool-row
    expect(screen.getByTestId("tool-row")).toBeTruthy();
    expect(screen.getByTestId("error-block")).toBeTruthy();
  });

  it("message 永不折叠——markdown 渲染 + finished tape 无 ▎", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "## Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n" },
      }),
      ev("node_completed", { node: "n" }),
      ev("workflow_completed", { data: { elapsed: 1 } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);

    render(
      React.createElement(ConversationView, { nodeId: "n" })
    );
    // message 文字可见（未折叠）
    expect(screen.getByText("Title")).toBeTruthy();
    // table th 渲染
    const th = screen.queryByText("a");
    expect(th).toBeTruthy();
    // finished tape → 无 cursor
    expect(screen.queryByTestId("streaming-cursor")).toBeNull();
  });

  it("running + last=message → ▎ 出现", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "typing" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);

    render(
      React.createElement(ConversationView, { nodeId: "n" })
    );
    expect(screen.getByTestId("streaming-cursor")).toBeTruthy();
  });

  it("chart-ref row 渲染并触发 onChartClick", async () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("custom", {
        node: "n",
        session_id: "s",
        data: { kind: "chart", chart: { title: "loss curve" } },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    let clicked = false;
    render(
      React.createElement(ConversationView, {
        nodeId: "n",
        onChartClick: () => {
          clicked = true;
        },
      })
    );
    const row = screen.getByTestId("chart-ref-row");
    row.click();
    expect(clicked).toBe(true);
  });

  it("连续 tool 对 → tool-group（一个 ▸ N tools 按钮）", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "a", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "a", result: "x" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "b", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "b", result: "y" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByTestId("tool-group")).toBeTruthy();
    // 文案「2 tools」
    expect(screen.getByText("2 tools")).toBeTruthy();
  });
});

describe("markdown 渲染深度（gfm + math + prism）", () => {
  beforeEach(() => resetStore());

  it("code block + table + LaTeX 同时渲染", () => {
    const md = [
      "Here is code:",
      "",
      "```python",
      "def f(x):",
      "    return x + 1",
      "```",
      "",
      "| a | b |",
      "|---|---|",
      "| 1 | 2 |",
    ].join("\n");
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: md } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    // pre 标签存在（code block）
    expect(document.querySelector("pre")).toBeTruthy();
    // table 存在
    expect(document.querySelector("table")).toBeTruthy();
  });
});

describe("selectConversation — 与 buildEntries 端到端", () => {
  beforeEach(() => resetStore());

  it("selectConversation + buildEntries 串联 D7 序无关", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "a" } }),
      ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "x", args: {} } }),
      ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "x", result: "ok" } }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const fwd = JSON.stringify(
      buildEntries(
        selectConversation(useWorkflowStore.getState(), "n").events
      ).map((e: ConvEntry) => e.kind)
    );

    resetStore();
    _seq = 100;
    useWorkflowStore.getState().loadFromEvents([...events].reverse());
    const back = JSON.stringify(
      buildEntries(
        selectConversation(useWorkflowStore.getState(), "n").events
      ).map((e: ConvEntry) => e.kind)
    );
    // store 内 sort 后顺序一致 → entries 一致
    expect(back).toEqual(fwd);
  });
});

// ── P2：ConversationView session 选择器（子 agent 维度，SPEC §P2 方案 1）──────────────
// 验收：family_detect 多 session 顶部出现 All/main/sub 选项；默认选第一个 sub；
// 切 session → setSelectedSession；All = 旧行为零回归；单 session 不显选择器。
describe("ConversationView P2 — 会话选择器", () => {
  beforeEach(() => resetStore());

  /** family_detect 缩影：main(2) + subA(2) + subB(1)。sessionId 仿真 18 字符。 */
  function buildMultiSession(): WebEvent[] {
    return [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: null }),
      ev("agent_message", { node: "n", session_id: "ses_AAA111222333xxx", data: { text: "a1" } }),
      ev("agent_thinking", { node: "n", session_id: "ses_AAA111222333xxx", data: { text: "a2" } }),
      ev("agent_message", { node: "n", session_id: "ses_BBB222333444yyy", data: { text: "b1" } }),
      ev("node_completed", { node: "n", session_id: null }),
    ];
  }

  it("多 session → 顶部渲染 All / main / 各 sub 选择器（SPEC §P2 验收 1）", () => {
    useWorkflowStore.getState().loadFromEvents(buildMultiSession());
    // 模拟用户点 node：store setSelectedNode 联动设默认 session
    useWorkflowStore.getState().setSelectedNode("n");

    render(React.createElement(ConversationView, { nodeId: "n" }));

    // All tab + main tab + 两个 sub tab
    expect(screen.getByTestId("session-tab-all")).toBeTruthy();
    expect(screen.getByTestId("session-tab-main")).toBeTruthy();
    expect(screen.getByTestId("session-tab-ses_AAA111222333xxx")).toBeTruthy();
    expect(screen.getByTestId("session-tab-ses_BBB222333444yyy")).toBeTruthy();
    // All(N) 总数显示（main 2 + subA 2 + subB 1 = 5）
    expect(screen.getByText("All(5)")).toBeTruthy();
    expect(screen.getByText("main(2)")).toBeTruthy();
    expect(screen.getByText(/ses_AAA111.*\(2\)/)).toBeTruthy();
  });

  it("setSelectedNode 联动默认选第一个 sub session → buildEntries 只该 session 事件（非全量）", () => {
    useWorkflowStore.getState().loadFromEvents(buildMultiSession());
    useWorkflowStore.getState().setSelectedNode("n");
    // 默认 selectedSession = subA（第一个非 main）
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_AAA111222333xxx");

    render(React.createElement(ConversationView, { nodeId: "n" }));
    // 仅 subA 的 a1 message 渲染（subB 的 b1 不在）
    const msgs = screen.getAllByTestId("message-block");
    expect(msgs.length).toBe(1);
  });

  it("点击 sub tab → setSelectedSession → 切到该 session（testid 路径）", () => {
    useWorkflowStore.getState().loadFromEvents(buildMultiSession());
    useWorkflowStore.getState().setSelectedNode("n");

    render(React.createElement(ConversationView, { nodeId: "n" }));
    // 切 subB
    fireEvent.click(screen.getByTestId("session-tab-ses_BBB222333444yyy"));
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_BBB222333444yyy");
  });

  it("点击 All → selectedSession='all' → 全聚合（零回归，与 P1 前行为一致）", () => {
    useWorkflowStore.getState().loadFromEvents(buildMultiSession());
    useWorkflowStore.getState().setSelectedNode("n");

    render(React.createElement(ConversationView, { nodeId: "n" }));
    // 切到 All
    fireEvent.click(screen.getByTestId("session-tab-all"));
    expect(useWorkflowStore.getState().selectedSession).toBe("all");
    // 全量 message：subA 的 a1 + subB 的 b1 = 2 个
    const msgs = screen.getAllByTestId("message-block");
    expect(msgs.length).toBe(2);
  });

  it("单 session（无 sub） → 不渲染会话选择器（省 UI，YAGNI）", () => {
    useWorkflowStore.getState().loadFromEvents([
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: null }),
      ev("agent_message", { node: "n", session_id: null, data: { text: "x" } }),
    ]);
    useWorkflowStore.getState().setSelectedNode("n");

    render(React.createElement(ConversationView, { nodeId: "n" }));
    // 仅 main session → 无 session tabs 容器
    expect(screen.queryByTestId("session-tabs-n")).toBeNull();
    expect(screen.queryByTestId("session-tab-all")).toBeNull();
  });

  it("setSelectedNode 切到另一 node → selectedSession 联动重置为新 node 第一个 sub", () => {
    useWorkflowStore.getState().loadFromEvents([
      ...buildMultiSession(),
      // 把 ev 计数器推进，构造 second node
      ev("node_started", { node: "n2", session_id: null }),
      ev("agent_message", { node: "n2", session_id: "ses_N2_X_custom_id", data: { text: "x" } }),
    ]);
    useWorkflowStore.getState().setSelectedNode("n");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_AAA111222333xxx");
    // 切到 n2
    useWorkflowStore.getState().setSelectedNode("n2");
    expect(useWorkflowStore.getState().selectedSession).toBe("ses_N2_X_custom_id");
  });
});
