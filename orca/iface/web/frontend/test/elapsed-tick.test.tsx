// test/elapsed-tick.test.ts —— useElapsedTick singleton（SPEC §0 D5 / §5.2）。
//
// 验证：
//   1. 单一 timer：N 次订阅 + M 个 active consumer 只开 1 个 setInterval。
//   2. active=false 不启 tick；active=true 启 tick。
//   3. useElapsedNow 在 tick 后同步更新（订阅方刷新）。
//   4. snap 语义：D5 完成时 selector 返回 workflowElapsed（停 tick 后值不变）。

import { describe, expect, test, afterEach, beforeEach, vi } from "vitest";
import { render, cleanup, renderHook, act } from "@testing-library/react";
import {
  useElapsedTickActive,
  useElapsedNow,
  __testReset,
  __testTick,
} from "@/hooks/use-elapsed-tick";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  selectWorkflowElapsed,
  selectNodeElapsed,
  selectStall,
  DEFAULT_STALL_THRESHOLD_MS,
} from "@/selectors";

afterEach(() => {
  cleanup();
  __testReset();
  useWorkflowStore.getState().unloadRun();
  vi.restoreAllMocks();
});

beforeEach(() => {
  __testReset();
  useWorkflowStore.getState().unloadRun();
});

describe("useElapsedTickActive —— 单一 timer 引用计数", () => {
  test("active=true 启 tick；active=false 不启；unmount 清理", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
    const { rerender, unmount } = renderHook(
      ({ active }) => useElapsedTickActive(active),
      { initialProps: { active: false } }
    );
    // active=false → 不启 tick
    expect(setIntervalSpy).not.toHaveBeenCalled();
    // 切到 active=true → 启 1 个 setInterval
    rerender({ active: true });
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    // unmount → 引用计数减到 0 → clearInterval
    unmount();
    expect(clearIntervalSpy).toHaveBeenCalled();
  });

  test("多 consumer active → 仅一个 setInterval", () => {
    const setIntervalSpy = vi.spyOn(globalThis, "setInterval");
    const h1 = renderHook(() => useElapsedTickActive(true));
    const h2 = renderHook(() => useElapsedTickActive(true));
    const h3 = renderHook(() => useElapsedTickActive(true));
    // 3 consumer active → setInterval 只被调用 1 次（singleton）
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    h1.unmount();
    h2.unmount();
    // 仍有一个 active → timer 不停
    const clearIntervalSpy = vi.spyOn(globalThis, "clearInterval");
    expect(clearIntervalSpy).not.toHaveBeenCalled();
    h3.unmount();
    // 最后一个 consumer unmount → 停 timer
    expect(clearIntervalSpy).toHaveBeenCalled();
  });
});

describe("useElapsedNow —— tick 触发订阅者 re-render", () => {
  test("__testTick 触发后，now 值更新", () => {
    const { result } = renderHook(() => useElapsedNow());
    const beforeNow = result.current;
    // 等微任务 + setTimeout flush（useSyncExternalStore 在 act 内同步刷新）
    act(() => {
      __testTick();
    });
    // now 应该 >= beforeNow（Date.now 单调向前）
    expect(result.current).toBeGreaterThanOrEqual(beforeNow);
  });
});

describe("selectWorkflowElapsed —— D5 snap 语义", () => {
  test("idle → null", () => {
    useWorkflowStore.getState().unloadRun();
    const s = useWorkflowStore.getState();
    expect(selectWorkflowElapsed(s, 1000)).toBeNull();
  });

  test("running → now - workflowStartedAt（live）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf", topology: { nodes: [], routes: [], parallel: [], entry: "" } },
    });
    const s = useWorkflowStore.getState();
    // workflowStartedAt = 100, status = running
    expect(selectWorkflowElapsed(s, 160)).toBe(60);
  });

  test("completed → snap workflowElapsed（不再用 wall-clock）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "workflow_completed",
      timestamp: 150,
      node: null,
      session_id: null,
      data: { elapsed: 50 },
    });
    const s = useWorkflowStore.getState();
    // snap = 50（D5）；即便 now 推进也保持
    expect(selectWorkflowElapsed(s, 1000)).toBe(50);
    expect(selectWorkflowElapsed(s, 99999)).toBe(50);
  });

  test("failed → snap = workflow_failed.ts - workflowStartedAt（终态不丢 elapsed）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "workflow_failed",
      timestamp: 250,
      node: null,
      session_id: null,
      data: { message: "boom" },
    });
    const s = useWorkflowStore.getState();
    // snap = 250 - 100 = 150（终态 elapsed 不丢，wall-clock 不漂移）
    expect(selectWorkflowElapsed(s, 0)).toBe(150);
    expect(selectWorkflowElapsed(s, 99999)).toBe(150);
  });

  test("cancelled → snap = workflow_cancelled.ts - workflowStartedAt", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "workflow_cancelled",
      timestamp: 180,
      node: null,
      session_id: null,
      data: { reason: "user" },
    });
    const s = useWorkflowStore.getState();
    expect(selectWorkflowElapsed(s, 0)).toBe(80);
  });
});

describe("selectNodeElapsed —— D5 per-node snap", () => {
  test("running node → now - startedAt", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 200,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    const s = useWorkflowStore.getState();
    expect(selectNodeElapsed(s, "n1", 250)).toBe(50);
  });

  test("completed node → snap node_completed.data.elapsed", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 200,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "node_completed",
      timestamp: 280,
      node: "n1",
      session_id: "n1",
      data: { elapsed: 80 },
    });
    const s = useWorkflowStore.getState();
    expect(selectNodeElapsed(s, "n1", 9999)).toBe(80);
  });

  test("unknown node → null", () => {
    const s = useWorkflowStore.getState();
    expect(selectNodeElapsed(s, "nope", 1000)).toBeNull();
  });
});

describe("selectStall —— D9 阈值", () => {
  test("running node 内最后事件 < 阈值 → 无 stall", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 100,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "agent_message",
      timestamp: 105,
      node: "n1",
      session_id: "n1",
      data: { text: "hi" },
    });
    const s = useWorkflowStore.getState();
    // now=108 < 阈值 5s → 无 stall
    expect(selectStall(s, "n1", 108)).toBeNull();
  });

  test("running node 内最后事件 > 阈值 → stall（thinking=false）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 100,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "agent_message",
      timestamp: 105,
      node: "n1",
      session_id: "n1",
      data: { text: "hi" },
    });
    const s = useWorkflowStore.getState();
    const result = selectStall(s, "n1", 200); // 95s 静默
    expect(result).not.toBeNull();
    expect(result!.thinking).toBe(false);
    // sinceMs = (200-105)*1000 = 95000
    expect(result!.sinceMs).toBe(95000);
  });

  test("最后事件是 agent_thinking → thinking=true（更准确）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 100,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "agent_thinking",
      timestamp: 105,
      node: "n1",
      session_id: "n1",
      data: { text: "thinking..." },
    });
    const s = useWorkflowStore.getState();
    const result = selectStall(s, "n1", 200);
    expect(result).not.toBeNull();
    expect(result!.thinking).toBe(true);
  });

  test("已完成 node → 无 stall（不再运行）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 100,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "node_completed",
      timestamp: 105,
      node: "n1",
      session_id: "n1",
      data: { elapsed: 5 },
    });
    const s = useWorkflowStore.getState();
    expect(selectStall(s, "n1", 9999)).toBeNull();
  });

  test("自定义阈值生效", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "node_started",
      timestamp: 100,
      node: "n1",
      session_id: "n1",
      data: {},
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "agent_message",
      timestamp: 105,
      node: "n1",
      session_id: "n1",
      data: { text: "hi" },
    });
    const s = useWorkflowStore.getState();
    // 1s 静默，阈值 500ms → stall
    expect(selectStall(s, "n1", 106, 500)).not.toBeNull();
    // 阈值 5000ms → 无
    expect(selectStall(s, "n1", 106, DEFAULT_STALL_THRESHOLD_MS)).toBeNull();
  });
});

describe("React 集成：consumer 在 tick 后 re-render", () => {
  test("useElapsedNow 在两个组件间共享同一 tick", () => {
    let aNow = 0;
    let bNow = 0;
    const A = () => {
      aNow = useElapsedNow();
      return null;
    };
    const B = () => {
      bNow = useElapsedNow();
      return null;
    };
    // 启用 tick（页根 active）
    const Root = ({ active }: { active: boolean }) => {
      useElapsedTickActive(active);
      return (
        <>
          <A />
          <B />
        </>
      );
    };
    render(<Root active={true} />);
    const beforeA = aNow;
    const beforeB = bNow;
    act(() => {
      __testTick();
    });
    expect(aNow).toBeGreaterThanOrEqual(beforeA);
    expect(bNow).toBeGreaterThanOrEqual(beforeB);
  });
});
