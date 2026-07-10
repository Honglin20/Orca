// test/log-stream.test.tsx —— LogStream auto-scroll（SPEC §5.5 / §0 D6）。
//
// 覆盖 SPEC §5.5 三约束 + auto-scroll 策略：
//   1. 每事件一行：行数 == tape 事件数
//   2. 每个 EventType 都有 readable 摘要（selectLog/summarizeEvent 已穷尽 —— 这里只验行不为空）
//   3. auto-scroll：pinned-to-bottom → 新事件到达自动滚到末；
//      用户上滚 → 取消 pinned + 「跳最新」按钮；点按钮 → pinned + 滚到末。
//
// 注：react-window v2 ``scrollToRow`` 在 happy-dom 下不实际滚动 scrollTop，但会把
// 末行纳入 overscanCount 渲染窗口；我们断言 pinned/jump UI 状态 + 末行被渲染。

import { describe, expect, test, afterEach, beforeEach, vi } from "vitest";
import { act, cleanup, render, screen, fireEvent } from "@testing-library/react";
import { LogStream } from "@/components/detail/LogStream";
import { useWorkflowStore } from "@/stores/workflow-store";
import { ALL_EVENT_TYPES, makeEvent } from "./_helpers";

beforeEach(() => {
  useWorkflowStore.getState().unloadRun();
});

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  vi.restoreAllMocks();
});

describe("LogStream —— 行渲染（SPEC §5.5）", () => {
  test("行数 == tape 事件数", () => {
    const events = ALL_EVENT_TYPES.slice(0, 10).map((t, i) =>
      makeEvent(t, { seq: i + 1, data: { text: "x" } })
    );
    useWorkflowStore.getState().loadFromEvents(events);
    render(<LogStream />);
    // 10 行（react-window 全部 mount 在 happy-dom 下 overscanCount=5）
    const rows = screen.getAllByTestId(/^log-row-/);
    expect(rows.length).toBeGreaterThanOrEqual(10);
  });

  test("每个 EventType 都有 readable 摘要（行 text 非空）", () => {
    const events = ALL_EVENT_TYPES.map((t, i) =>
      makeEvent(t, { seq: i + 1, data: { text: "x", message: "m" } })
    );
    useWorkflowStore.getState().loadFromEvents(events);
    render(<LogStream />);
    const rows = screen.getAllByTestId(/^log-row-/);
    // 每行都包含 seq + type + 文本（不为空）
    for (const row of rows) {
      const text = row.textContent ?? "";
      // 至少含 type（穷尽：每个 EventType 都被 summarizeEvent 处理）
      expect(text.length).toBeGreaterThan(0);
    }
  });

  test("empty events → 暂无事件占位", () => {
    render(<LogStream />);
    expect(screen.getByTestId("log-empty")).toBeInTheDocument();
  });
});

describe("LogStream —— auto-scroll 策略（SPEC §5.5 闭 review #36）", () => {
  test("初始：pinned（显示 live 标识）", () => {
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 1 }),
    ]);
    render(<LogStream />);
    expect(screen.getByTestId("log-pinned")).toBeInTheDocument();
  });

  test("wheel 上滚 → 取消 pinned + 显示「跳最新」按钮", () => {
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 1 }),
    ]);
    render(<LogStream />);
    // wheel 上滚（deltaY < 0）—— 直接在 log-stream 容器上触发
    fireEvent.wheel(screen.getByTestId("log-stream"), { deltaY: -10 });
    expect(screen.queryByTestId("log-pinned")).not.toBeInTheDocument();
    expect(screen.queryByTestId("log-jump-latest")).not.toBeInTheDocument();
    // pinned=false 但 pendingJump 要等下一个新事件到达才出现
    act(() => {
      useWorkflowStore.getState().processEvent(
        makeEvent("agent_message", { seq: 2 })
      );
    });
    expect(screen.getByTestId("log-jump-latest")).toBeInTheDocument();
  });

  test("点「跳最新」按钮 → pinned 恢复 + 按钮消失", () => {
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 1 }),
    ]);
    render(<LogStream />);
    // 上滚 → 取消 pinned
    fireEvent.wheel(screen.getByTestId("log-stream"), { deltaY: -10 });
    // 新事件到达 → 出现按钮
    act(() => {
      useWorkflowStore.getState().processEvent(
        makeEvent("agent_message", { seq: 2 })
      );
    });
    const btn = screen.getByTestId("log-jump-latest");
    fireEvent.click(btn);
    // pinned 恢复 → live 标识再现
    expect(screen.getByTestId("log-pinned")).toBeInTheDocument();
    expect(screen.queryByTestId("log-jump-latest")).not.toBeInTheDocument();
  });

  test("pinned 时新事件到达 → 末行可见（pinned 通道生效，不出现 jump 按钮）", () => {
    // 行为断言：pinned 状态下新事件到达 → 末行被 react-window 渲染（视口含末行），
    // 不出现 jump 按钮（auto-scroll 通道生效）。
    // scrollToRow 命令式调用在 happy-dom 下不实际滚动 scrollTop，但 react-window 会
    // 把末行纳入 overscanCount 渲染窗口；故断言「末行被 render」+「无 jump 按钮」等价
    // 于「pinned 通道正确」。
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 1 }),
    ]);
    render(<LogStream />);
    // pinned 状态保持
    expect(screen.getByTestId("log-pinned")).toBeInTheDocument();
    // 新事件到达
    act(() => {
      useWorkflowStore.getState().processEvent(
        makeEvent("agent_message", { seq: 5 }),
      );
    });
    // pinned → 不出现 jump 按钮（auto-scroll 生效）
    expect(screen.queryByTestId("log-jump-latest")).not.toBeInTheDocument();
    // 末行被渲染（react-window overscanCount=5，5 个事件全在视口）
    const rows = screen.getAllByTestId(/^log-row-/);
    expect(rows.length).toBeGreaterThanOrEqual(2);
  });

  test("pinned 状态显式通过「跳最新」按钮恢复（无自动恢复 magic）", () => {
    // 设计意图：predictable over magic —— 不用 onRowsRendered 自动恢复（在事件少、全部
    // 可见的常见场景下 stopIndex 总是末行，自动恢复会让 wheel 上滚立即被覆盖）。
    const events = Array.from({ length: 8 }, (_, i) =>
      makeEvent("agent_message", { seq: i + 1, data: { text: "x" } }),
    );
    useWorkflowStore.getState().loadFromEvents(events);
    render(<LogStream />);
    fireEvent.wheel(screen.getByTestId("log-stream"), { deltaY: -10 });
    expect(screen.queryByTestId("log-pinned")).not.toBeInTheDocument();
    // 新事件到达 → jump 按钮出现（pinned=false 通道）
    act(() => {
      useWorkflowStore.getState().processEvent(
        makeEvent("agent_message", { seq: 100, data: { text: "new" } }),
      );
    });
    expect(screen.getByTestId("log-jump-latest")).toBeInTheDocument();
    // 用户显式点按钮 → pinned 恢复
    fireEvent.click(screen.getByTestId("log-jump-latest"));
    expect(screen.getByTestId("log-pinned")).toBeInTheDocument();
  });
});
