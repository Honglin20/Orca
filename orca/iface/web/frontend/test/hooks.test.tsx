// test/hooks.test.tsx —— hooks 验收（SPEC §7.5 / plan B3.2）。
//
// 断言意图（Rule 9）：
//   1. **useRunEvents**：mount → fetch /events 被调；unmount → unloadRun（activeRunId=null，懒加载红线）
//   2. **useRunsList**：mount → fetch /api/runs；polling interval 触发多次 fetch
//   3. **useWebSocket**：mount → 全量重拉 + subscribe(run_id)；onmessage 只处理匹配 run_id；
//      onclose（非主动）→ 指数退避重连（再全量重拉 + subscribe）

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useRunEvents } from "@/hooks/use-run-events";
import { useRunsList } from "@/hooks/use-runs-list";
import { useWebSocket } from "@/hooks/use-websocket";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { WorkflowEvent } from "@/types/events";

// ── 公用：每次测试重置 store ────────────────────────────────────────────────
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

// ── 假 WebSocket：可控 onopen/onmessage/onclose 触发 ────────────────────────
interface FakeSocket {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  sent: string[];
  close: () => void;
  send: (data: string) => void;
}

const OPEN = 1;

function makeFakeSocketFactory(): {
  factory: (url: string) => FakeSocket;
  lastSocket: () => FakeSocket | undefined;
  sockets: FakeSocket[];
} {
  const sockets: FakeSocket[] = [];
  return {
    sockets,
    factory: (url: string) => {
      const sock: FakeSocket = {
        url,
        readyState: OPEN,
        onopen: null,
        onmessage: null,
        onclose: null,
        onerror: null,
        sent: [],
        close: () => {
          sock.readyState = 3; // CLOSED
        },
        send: (data: string) => {
          sock.sent.push(data);
        },
      };
      sockets.push(sock);
      return sock;
    },
    lastSocket: () => sockets[sockets.length - 1],
  };
}

// 把 FakeSocket cast 成 WebSocket 给 hook（测试只用到 onopen/onmessage/onclose/send/close/readyState）
function asWebSocket(s: FakeSocket): WebSocket {
  return s as unknown as WebSocket;
}

describe("useRunEvents", () => {
  beforeEach(() => resetStore());

  it("mount（有 runId）→ 调 GET /api/runs/<id>/events", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [] as WorkflowEvent[],
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() => useRunEvents("run-A"));
    // 等异步 loadRun 完成
    await vi.waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith("/api/runs/run-A/events");
    });
    unmount();
    vi.unstubAllGlobals();
  });

  it("mount 无 runId → 不 fetch（列表页不拉 events，懒加载红线）", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { unmount } = renderHook(() => useRunEvents(undefined));
    await Promise.resolve();
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();
    vi.unstubAllGlobals();
  });

  it("unmount → unloadRun（清 activeRunId/events，懒加载红线）", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () =>
        [
          {
            seq: 1,
            type: "node_completed",
            timestamp: 1,
            node: "A",
            session_id: null,
            data: { output: "o" },
          },
        ] as WorkflowEvent[],
    });
    vi.stubGlobal("fetch", fetchMock);

    const { unmount } = renderHook(() => useRunEvents("run-A"));
    await vi.waitFor(() =>
      expect(useWorkflowStore.getState().activeRunId).toBe("run-A")
    );
    expect(useWorkflowStore.getState().events.length).toBe(1);

    act(() => unmount());
    // 懒加载红线：切走清派生态
    const state = useWorkflowStore.getState();
    expect(state.activeRunId).toBeNull();
    expect(state.events).toEqual([]);
    vi.unstubAllGlobals();
  });
});

describe("useRunsList", () => {
  beforeEach(() => resetStore());
  afterEach(() => vi.useRealTimers());

  it("mount → 调 GET /api/runs（元数据）+ 设置 interval 轮询", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [
        { run_id: "r1", workflow_name: "w", status: "running", progress: "1/2", cost: 0, elapsed: 0, error: null },
      ],
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();

    const { unmount } = renderHook(() => useRunsList(2000));
    // 首次立即拉
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock).toHaveBeenLastCalledWith("/api/runs");

    // 推进 2s → 第二次
    await act(async () => {
      vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    // 推进 2s → 第三次
    await act(async () => {
      vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(3);

    unmount();
    vi.unstubAllGlobals();
  });

  it("unmount → 清 interval（无 leak，推进时间不再 fetch）", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();

    const { unmount } = renderHook(() => useRunsList(2000));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    unmount();

    await act(async () => {
      vi.advanceTimersByTimeAsync(10_000);
    });
    // unmount 后 interval 已清，不应再 fetch
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.unstubAllGlobals();
  });
});

describe("useWebSocket", () => {
  beforeEach(() => resetStore());

  it("mount（有 runId）→ onopen 仅发 subscribe(run_id)（初始全量加载由 useRunEvents 负责，避免双拉）", async () => {
    const { factory, lastSocket } = makeFakeSocketFactory();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [] as WorkflowEvent[],
    });

    renderHook(() =>
      useWebSocket("run-A", {
        createSocket: (url) => asWebSocket(factory(url)),
        fetchImpl: fetchMock,
        wsUrl: "ws://test/ws",
      })
    );

    // 触发初始 open
    await act(async () => {
      lastSocket()!.onopen?.(new Event("open"));
    });

    // 初始连接：**不**全量重拉（useRunEvents 负责），仅 subscribe
    expect(fetchMock).not.toHaveBeenCalled();
    expect(lastSocket()!.sent).toEqual([
      JSON.stringify({ type: "subscribe", run_id: "run-A" }),
    ]);
  });

  it("onmessage：只处理 run_id 匹配的事件（按需订阅）", async () => {
    const { factory, lastSocket } = makeFakeSocketFactory();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [] as WorkflowEvent[],
    });

    renderHook(() =>
      useWebSocket("run-A", {
        createSocket: (url) => asWebSocket(factory(url)),
        fetchImpl: fetchMock,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      lastSocket()!.onopen?.(new Event("open"));
    });

    // 他 run 事件 → 应被丢弃
    const otherEvent: WorkflowEvent = {
      seq: 1,
      type: "node_completed",
      timestamp: 1,
      node: "X",
      session_id: null,
      data: {},
      run_id: "run-B",
    };
    act(() => {
      lastSocket()!.onmessage?.({
        data: JSON.stringify(otherEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(0);

    // 本 run 事件 → 处理
    const myEvent: WorkflowEvent = {
      seq: 2,
      type: "node_completed",
      timestamp: 2,
      node: "A",
      session_id: null,
      data: { output: "o" },
      run_id: "run-A",
    };
    act(() => {
      lastSocket()!.onmessage?.({
        data: JSON.stringify(myEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(1);
    expect(useWorkflowStore.getState().nodes.A.status).toBe("done");
  });

  it("onclose（非主动）→ 指数退避重连 + 再全量重拉 + 重新 subscribe", async () => {
    const { factory, sockets } = makeFakeSocketFactory();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [] as WorkflowEvent[],
    });

    renderHook(() =>
      useWebSocket("run-A", {
        createSocket: (url) => asWebSocket(factory(url)),
        fetchImpl: fetchMock,
        wsUrl: "ws://test/ws",
      })
    );
    // 首次连接 open
    await act(async () => {
      sockets[0].onopen?.(new Event("open"));
    });
    const initialFetchCount = fetchMock.mock.calls.length;

    // 模拟异常断开（非主动 close —— hook cleanup 时会置 onclose=null；这里 hook 还在跑，
    // onclose 仍指向内部回调，触发它即模拟异常断）
    vi.useFakeTimers();
    await act(async () => {
      sockets[0].onclose?.(new CloseEvent("close"));
    });

    // 推进 1s（初始 backoff）→ 重连开新 socket
    await act(async () => {
      vi.advanceTimersByTimeAsync(1000);
    });

    // 第二个 socket 已创建（重连）
    expect(sockets.length).toBe(2);
    await act(async () => {
      sockets[1].onopen?.(new Event("open"));
      await vi.waitFor(() =>
        expect(fetchMock.mock.calls.length).toBeGreaterThan(initialFetchCount)
      );
    });

    // 重连后重新 subscribe
    expect(sockets[1].sent).toEqual([
      JSON.stringify({ type: "subscribe", run_id: "run-A" }),
    ]);
    vi.useRealTimers();
  });

  it("unmount → 主动关 WS（不触发重连）+ 无 leak", async () => {
    const { factory, sockets } = makeFakeSocketFactory();
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => [],
    });

    const { unmount } = renderHook(() =>
      useWebSocket("run-A", {
        createSocket: (url) => asWebSocket(factory(url)),
        fetchImpl: fetchMock,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      sockets[0].onopen?.(new Event("open"));
    });

    unmount(); // 主动关

    // 推进很久都不该创建第二个 socket（没重连）
    vi.useFakeTimers();
    await act(async () => {
      vi.advanceTimersByTimeAsync(60_000);
    });
    expect(sockets.length).toBe(1);
    vi.useRealTimers();
  });
});
