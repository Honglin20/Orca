// test/conversation-coverage.test.tsx —— 闭 review 缺口补强（G1/G2/G4/G5/G6/G7/G9/G10）。
//
// 此文件专门覆盖 review 报告 2 指出的关键测试缺口：
//   - G1：B1 早 return bug 回归保护（无 id 的 result 不再让整组 tool 消失）
//   - G2：折叠 oracle DOM 断言（默认折叠态 vs message 永不折叠）
//   - G4：EventType 穷尽覆盖（每个 conversation-eligible type 至少产生 1 entry；
//         每个 non-conversation type 不进 conversation）
//   - G5：workflow_failed 渲染（data.node 关联）
//   - G6：LaTeX / Prism / 行内 vs 块 code 深度断言
//   - G7：smart arg 通过 ToolRow DOM 验证
//   - G9：DiffView / FileContentView 最小覆盖
//   - G10：StatusLine 14 type 摘要表驱动断言

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import * as React from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectConversation } from "@/selectors";
import { buildEntries } from "@/components/conversation/entries";
import { ConversationView } from "@/components/views/ConversationView";
import { StatusLine } from "@/components/conversation/StatusLine";
import { DiffView } from "@/components/conversation/DiffView";
import { FileContentView } from "@/components/conversation/FileContentView";
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

// ── G1: B1 早 return bug 回归 ──────────────────────────────────────────────────
describe("G1: buildEntries — 无 id 的 result 不让整组 tool 消失", () => {
  beforeEach(() => resetStore());

  it("tool_run 中夹杂无 id result：其它正常 pair 仍渲染", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: { tool: "bash", tool_call_id: "t1", args: {} },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { /* 无 tool_call_id */ result: "x" },
      }),
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: { tool: "bash", tool_call_id: "t2", args: {} },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "t2", result: "y" },
      }),
    ];
    const entries = buildEntries(events);
    const toolEntries = entries.filter(
      (e) => e.kind === "tool-group" || e.kind === "tool-single"
    );
    // 修复前：toolEntries 为空（bug）；修复后：1 个 tool-group 含 t1+t2
    expect(toolEntries.length).toBe(1);
    const e = toolEntries[0] as {
      kind: string;
      pairs: { tool_call_id: string }[];
    };
    if (e.kind === "tool-group") {
      expect(e.pairs.length).toBe(2);
      expect(e.pairs.map((p) => p.tool_call_id).sort()).toEqual(["t1", "t2"]);
    }
  });
});

// ── G2: 折叠 oracle DOM 断言 ──────────────────────────────────────────────────
describe("G2: 折叠默认态 DOM 断言 (SPEC §9 AC1)", () => {
  beforeEach(() => resetStore());

  it("prompt / thinking / tool-group 默认折叠；message 永不折叠（无 aria-expanded）", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("prompt_rendered", {
        node: "n",
        session_id: "s",
        data: { preview: "PROMPT_PREVIEW_TEXT" },
      }),
      ev("agent_thinking", {
        node: "n",
        session_id: "s",
        data: { text: "THINKING_HIDDEN_TEXT" },
      }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "MESSAGE_VISIBLE_TEXT" },
      }),
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: { tool: "bash", tool_call_id: "a", args: {} },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "a", result: "r" },
      }),
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: { tool: "bash", tool_call_id: "b", args: {} },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "b", result: "r2" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));

    // message 文字立即可见（永不折叠）
    expect(screen.getByText("MESSAGE_VISIBLE_TEXT")).toBeTruthy();

    // prompt preview 默认折叠 → 文字不在 DOM
    expect(screen.queryByText("PROMPT_PREVIEW_TEXT")).toBeNull();

    // thinking text 默认折叠 → 文字不在 DOM
    expect(screen.queryByText("THINKING_HIDDEN_TEXT")).toBeNull();

    // message 无 aria-expanded（不 collapsible）
    const messageBlock = screen.getByTestId("message-block");
    expect(messageBlock.querySelector('[aria-expanded]')).toBeNull();

    // tool-group 默认折叠（aria-expanded=false 在 trigger button 上）
    const group = screen.getByTestId("tool-group");
    const trigger = group.querySelector('[aria-expanded]');
    expect(trigger).toBeTruthy();
    expect(trigger!.getAttribute("aria-expanded")).toBe("false");
  });

  it("message 永不折叠：无 collapsed UI、点击不收起", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "PLAIN_MESSAGE" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    const block = screen.getByTestId("message-block");
    // 无 collapse 触发按钮
    expect(block.querySelector("button")).toBeNull();
  });
});

// ── G4: EventType 穷尽覆盖（OCP——codegen 加新 type 时第一时间失败）──────────────
describe("G4: EventType 穷尽覆盖（每个 conversation-eligible 都产生 entry）", () => {
  const CONVERSATION_ELIGIBLE: EventType[] = [
    "prompt_rendered",
    "agent_thinking",
    "agent_message",
    "agent_tool_call",
    "agent_tool_result",
    "agent_step_started",
    "dialog_started",
    "dialog_message",
    "dialog_ended",
    "node_started",
    "node_completed",
    "node_failed",
    "node_skipped",
    "retry_started",
    "retry_succeeded",
    "retry_exhausted",
    "interrupt_requested",
    "interrupt_resolved",
    "validator_started",
    "validator_passed",
    "validator_failed",
    "wait_started",
    "wait_completed",
    "foreach_started",
    "foreach_item_started",
    "foreach_item_completed",
    "foreach_completed",
    "custom",
    "workflow_failed",
    "unknown_event",
  ];

  const NON_CONVERSATION: EventType[] = [
    "workflow_started",
    "workflow_completed",
    "workflow_cancelled",
    "workflow_resumed",
    "route_taken",
    "human_decision_requested",
    "human_decision_resolved",
    "agent_usage",
    "error",
  ];

  // 每个 conversation-eligible type：单事件输入 → 至少 1 entry（orphan result 除外）
  it.each(CONVERSATION_ELIGIBLE.filter((t) => t !== "agent_tool_result"))(
    "type=%s 至少产生 1 个 entry",
    (type) => {
      const data = minimalDataFor(type);
      const events: WebEvent[] = [ev(type, { node: "n", data })];
      const entries = buildEntries(events);
      expect(entries.length).toBeGreaterThanOrEqual(1);
    }
  );

  it("agent_tool_result（无 call 配对）→ orphan 不进 entries", () => {
    const events: WebEvent[] = [
      ev("agent_tool_result", {
        node: "n",
        data: { tool_call_id: "x", result: "y" },
      }),
    ];
    const entries = buildEntries(events);
    expect(entries.length).toBe(0);
  });

  it.each(NON_CONVERSATION)("type=%s 不进 conversation", (type) => {
    const events: WebEvent[] = [ev(type, { node: "n", data: minimalDataFor(type) })];
    expect(buildEntries(events).length).toBe(0);
  });

  /** 各 type 最小 data（让 buildEntries 走对应分支而不 crash）。 */
  function minimalDataFor(type: EventType): Record<string, unknown> {
    switch (type) {
      case "agent_tool_call":
        return { tool: "bash", tool_call_id: "x", args: {} };
      case "agent_thinking":
      case "agent_message":
        return { text: "x" };
      case "prompt_rendered":
        return { preview: "x" };
      case "agent_step_started":
        return { step_reason: "x" };
      case "dialog_message":
        return { role: "user", text: "x" };
      case "node_failed":
      case "workflow_failed":
        return { kind: "k", message: "m" };
      case "custom":
        return { kind: "other", x: 1 };
      case "retry_started":
        return { attempt: 1, max_attempts: 3, kind: "x" };
      case "foreach_started":
        return { item_count: 1 };
      case "foreach_item_started":
      case "foreach_item_completed":
        return { index: 0 };
      case "foreach_completed":
        return { count: 1 };
      case "wait_started":
        return { duration_seconds: 1, reason: "x" };
      case "wait_completed":
        return { elapsed_seconds: 1 };
      case "human_decision_requested":
        return { gate_id: "g", prompt: "p" };
      case "human_decision_resolved":
        return { gate_id: "g", answer: "y" };
      case "unknown_event":
        return { source: "x" };
      default:
        return {};
    }
  }
});

// ── G5: workflow_failed → 红 error block（data.node 关联）─────────────────────
describe("G5: workflow_failed 红 block（data.node 关联）", () => {
  beforeEach(() => resetStore());

  it("selectConversation 按 data.node 命中 workflow_failed", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("workflow_failed", {
        node: null, // workflow 级 top-level node=null
        data: { kind: "exec", message: "BOOM_MSG", node: "n" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const conv = selectConversation(useWorkflowStore.getState(), "n");
    const types = conv.events.map((e) => e.type);
    expect(types).toContain("workflow_failed");
  });

  it("渲染：workflow_failed 出现在 ErrorBlock（testid=error-block）", () => {
    const events: WebEvent[] = [
      ev("workflow_started", { data: { workflow_name: "w" } }),
      ev("node_started", { node: "n", session_id: "s" }),
      ev("workflow_failed", {
        node: null,
        data: { kind: "exec", message: "WF_MSG", node: "n" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByTestId("error-block")).toBeTruthy();
    expect(screen.getByText("WF_MSG")).toBeTruthy();
  });
});

// ── G6: markdown LaTeX / Prism / 行内 vs 块 code ──────────────────────────────
describe("G6: markdown 渲染深度", () => {
  beforeEach(() => resetStore());

  it("行内 LaTeX $E=mc^2$ → 渲染 .katex 元素", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "Inline math $E=mc^2$ here" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    const katex = document.querySelector(".katex");
    expect(katex).toBeTruthy();
  });

  it("块级代码 ```python 带 language-python class", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "```python\nprint('hi')\n```" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    const code = document.querySelector("code.language-python");
    expect(code).toBeTruthy();
  });

  it("无语言围栏代码块仍渲染为 pre（块而非行内）", () => {
    const events: WebEvent[] = [
      ev("node_started", { node: "n", session_id: "s" }),
      ev("agent_message", {
        node: "n",
        session_id: "s",
        data: { text: "```\nbare block\nsecond line\n```" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(document.querySelector("pre")).toBeTruthy();
  });
});

// ── G7: ToolRow DOM 显示 smart arg ─────────────────────────────────────────────
describe("G7: ToolRow DOM 显示 smart arg", () => {
  it("bash 工具行显示 $ ls", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: { tool: "bash", tool_call_id: "t", args: { command: "ls" } },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "t", result: "file.txt" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByText("$ ls")).toBeTruthy();
  });

  it("read 工具行显示 basename(path)", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: {
          tool: "read",
          tool_call_id: "r",
          args: { path: "/a/b/cfg.toml" },
        },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "r", result: "x = 1" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByText("cfg.toml")).toBeTruthy();
  });
});

// ── G9: DiffView / FileContentView ─────────────────────────────────────────────
describe("G9: DiffView / FileContentView 内容", () => {
  it("DiffView create mode 渲染 + header + 内容行", () => {
    const { container } = render(
      React.createElement(DiffView, {
        oldText: "",
        newText: "line1\nline2",
        fileName: "new.txt",
        mode: "create",
      })
    );
    expect(screen.getByText("+ new.txt")).toBeTruthy();
    expect(container.textContent).toContain("line1");
    expect(container.textContent).toContain("line2");
  });

  it("DiffView edit mode 渲染 - / + 行", () => {
    render(
      React.createElement(DiffView, {
        oldText: "old",
        newText: "new",
        fileName: "f.txt",
        mode: "edit",
      })
    );
    expect(screen.getByText("~ f.txt")).toBeTruthy();
  });

  it("FileContentView 渲染行号 + 内容", () => {
    const { container } = render(
      React.createElement(FileContentView, {
        content: "alpha\nbeta",
        filePath: "/x/y.txt",
      })
    );
    expect(container.textContent).toContain("alpha");
    expect(container.textContent).toContain("beta");
    expect(screen.getByText("/x/y.txt")).toBeTruthy();
  });
});

// ── G10: StatusLine 14 type 表驱动 ────────────────────────────────────────────
describe("G10: StatusLine 每个 status type 渲染摘要", () => {
  const cases: Array<{ type: EventType; data: Record<string, unknown>; expectSubstr: string }> = [
    { type: "retry_started", data: { attempt: 1, max_attempts: 3, kind: "transient" }, expectSubstr: "retry 1/3" },
    { type: "retry_succeeded", data: { attempt_total: 2 }, expectSubstr: "retry succeeded" },
    { type: "retry_exhausted", data: { attempts: 5 }, expectSubstr: "retry exhausted" },
    { type: "interrupt_requested", data: { source: "user" }, expectSubstr: "interrupt requested" },
    { type: "interrupt_resolved", data: { action: "continue" }, expectSubstr: "interrupt resolved" },
    { type: "validator_started", data: {}, expectSubstr: "validator running" },
    { type: "validator_passed", data: {}, expectSubstr: "validator passed" },
    { type: "validator_failed", data: { message: "bad" }, expectSubstr: "validator FAILED" },
    { type: "wait_started", data: { duration_seconds: 5, reason: "cool" }, expectSubstr: "wait 5" },
    { type: "wait_completed", data: { elapsed_seconds: 4 }, expectSubstr: "wait done" },
    { type: "foreach_started", data: { item_count: 3 }, expectSubstr: "foreach: 3" },
    { type: "foreach_item_started", data: { index: 0 }, expectSubstr: "foreach item[0]" },
    { type: "foreach_item_completed", data: { index: 0 }, expectSubstr: "foreach item[0]" },
    { type: "foreach_completed", data: { count: 1 }, expectSubstr: "foreach done" },
  ];

  it.each(cases)(
    "StatusLine type=$type 渲染含「$expectSubstr」",
    ({ type, data, expectSubstr }) => {
      const event: WebEvent = {
        seq: 1,
        type,
        timestamp: 0,
        node: null,
        session_id: null,
        data,
      };
      const { container } = render(
        React.createElement(StatusLine, { event })
      );
      expect(container.textContent).toContain(expectSubstr);
    }
  );
});

// ── 工具展开交互（light）─────────────────────────────────────────────────────
describe("ToolRow 展开交互", () => {
  it("点击 ToolRow 触发器切换展开（result 区可见/隐藏）", () => {
    const events: WebEvent[] = [
      ev("agent_tool_call", {
        node: "n",
        session_id: "s",
        data: {
          tool: "bash",
          tool_call_id: "t",
          args: { command: "echo hi" },
        },
      }),
      ev("agent_tool_result", {
        node: "n",
        session_id: "s",
        data: { tool_call_id: "t", result: "OUTPUT_TEXT" },
      }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    render(React.createElement(ConversationView, { nodeId: "n" }));
    // done 状态默认折叠 → 输出不在 DOM
    expect(screen.queryByText("OUTPUT_TEXT")).toBeNull();
    // 点击展开
    const trigger = screen.getByTestId("tool-row").querySelector("button");
    expect(trigger).toBeTruthy();
    fireEvent.click(trigger!);
    // result 现在可见
    expect(screen.getByText("OUTPUT_TEXT")).toBeTruthy();
  });
});
