// test/ws-resume-fallback.test.ts —— D4 resume-fallback watchdog（SPEC §0 D6 失败路径）。
//
// 断言意图（Rule 9）：重连发 resume 后，若 watchdog 时窗内未收到任何事件 → 判定 resume
// 失败 → 全量 re-fetch + loadFromEvents re-fold + onResumeFallback（dropBuffer）调用。
// 任一事件到达即清 watchdog（resume 成功，不 fallback）。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useWebSocket } from "@/hooks/use-websocket";
import type { WebEvent } from "@/types/events";

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

function makeFakeSocket(): {
  factory: (url: string) => FakeSocket;
  lastSocket: () => FakeSocket | undefined;
  allSockets: FakeSocket[];
} {
  const allSockets: FakeSocket[] = [];
  const factory = (url: string): FakeSocket => {
    const sock: FakeSocket = {
      url,
      readyState: OPEN,
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      sent: [],
      close: () => {
        sock.readyState = 3;
      },
      send: (data: string) => {
        sock.sent.push(data);
      },
    };
    allSockets.push(sock);
    return sock;
  };
  return {
    factory,
    lastSocket: () => allSockets[allSockets.length - 1],
    allSockets,
  };
}

function resetStore() {
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

describe("useWebSocket — D4 resume-fallback watchdog", () => {
  beforeEach(() => resetStore());

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("resume 后 watchdog 时窗内无事件 → 全量 re-fetch + re-fold + onResumeFallback", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify([
          {
            seq: 1,
            type: "workflow_started",
            timestamp: 1,
            node: null,
            session_id: null,
            data: { workflow_name: "fallback_wf" },
            run_id: "runX",
          },
          {
            seq: 2,
            type: "workflow_completed",
            timestamp: 2,
            node: null,
            session_id: null,
            data: { elapsed: 99 },
            run_id: "runX",
          },
        ] satisfies WebEvent[]),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    );
    const onResumeFallback = vi.fn();

    renderHook(() =>
      useWebSocket("runX", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
        fetchImpl: fetchSpy as unknown as typeof fetch,
        onResumeFallback,
      })
    );

    // 首次连接 + 推一个事件让 lastSeqSeen=5
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 5,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runX",
        } satisfies WebEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(5);

    // 触发重连：close（非主动）
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000); // INITIAL_BACKOFF_MS=1000，advance 2s 触发 open
    });
    await Promise.resolve();

    const sock2 = lastSocket()!;
    expect(sock2).not.toBe(sock1);
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();

    // sock2 应发了 resume（since=5）
    const sentResume = sock2.sent
      .map((s) => JSON.parse(s))
      .find((m) => m.type === "resume");
    expect(sentResume).toEqual({ type: "resume", run_id: "runX", since: 5 });

    // 推进 watchdog（RESUME_WATCHDOG_MS=3000），**不** 发任何事件 → fallback 应触发
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    // microtask flush
    await Promise.resolve();
    await act(async () => {
      await Promise.resolve();
    });

    // 全量 re-fetch 被 called（URL 含 /events）
    expect(fetchSpy).toHaveBeenCalled();
    const fetchUrl = String(fetchSpy.mock.calls[0][0]);
    expect(fetchUrl).toContain("/api/runs/runX/events");

    // store 被全量 re-fold（含 fallback fixture 的 workflow_completed）
    expect(useWorkflowStore.getState().status).toBe("completed");
    expect(useWorkflowStore.getState().workflowName).toBe("fallback_wf");
    expect(useWorkflowStore.getState().workflowElapsed).toBe(99);

    // onResumeFallback（dropBuffer）被调
    expect(onResumeFallback).toHaveBeenCalledTimes(1);
  });

  it("resume 后 watchdog 时窗内收到事件 → watchdog 清除，不 fallback", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    const fetchSpy = vi.fn();
    const onResumeFallback = vi.fn();

    renderHook(() =>
      useWebSocket("runY", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
        fetchImpl: fetchSpy as unknown as typeof fetch,
        onResumeFallback,
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();

    // 推一个事件让 lastSeqSeen=10
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 10,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runY",
        } satisfies WebEvent),
      } as MessageEvent);
    });

    // 重连
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    const sock2 = lastSocket()!;
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();

    // resume 后立刻收到事件（成功）
    await act(async () => {
      sock2.onmessage?.({
        data: JSON.stringify({
          seq: 11,
          type: "node_completed",
          timestamp: 2,
          node: "B",
          session_id: null,
          data: {},
          run_id: "runY",
        } satisfies WebEvent),
      } as MessageEvent);
    });

    // 推进远超 watchdog 时窗 → **不应** 触发 fallback
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onResumeFallback).not.toHaveBeenCalled();
    // lastSeqSeen 推进到 11（事件确实被处理）
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(11);
  });

  it("全量 re-fetch 网络失败 → fail loud（不静默吞，store 保持原状）", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    const fetchSpy = vi
      .fn()
      .mockRejectedValue(new Error("network down"));
    const onResumeFallback = vi.fn();

    renderHook(() =>
      useWebSocket("runZ", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
        fetchImpl: fetchSpy as unknown as typeof fetch,
        onResumeFallback,
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 7,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runZ",
        } satisfies WebEvent),
      } as MessageEvent);
    });

    // 重连 + 等 watchdog 超时
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    const sock2 = lastSocket()!;
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await Promise.resolve();
    await act(async () => {
      await Promise.resolve();
    });

    // fetch 失败：onResumeFallback 不应被调用（顺序：先 re-fold 成功再 dropBuffer）
    expect(fetchSpy).toHaveBeenCalled();
    expect(onResumeFallback).not.toHaveBeenCalled();
    // store 未被覆盖（lastSeqSeen 保留原值 7）
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(7);
  });

  it("dropBuffer 时序：loadFromEvents 先于 onResumeFallback（避免旧 buffer frame 闪现）", async () => {
    // 拦截 loadFromEvents + onResumeFallback 验证调用顺序——意图：源代码注释强调 dropBuffer
    // 必须在 loadFromEvents 之后（避免 re-fold 渲染的瞬间残留旧 buffer frame）。
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    const fetchSpy = vi.fn().mockResolvedValue(
      new Response(JSON.stringify([] satisfies WebEvent[]), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    );
    const callOrder: string[] = [];
    const onResumeFallback = vi.fn(() => callOrder.push("dropBuffer"));

    // 用 spy 拦截 store.loadFromEvents
    const origLoad = useWorkflowStore.getState().loadFromEvents;
    const loadSpy = vi
      .spyOn(useWorkflowStore.getState(), "loadFromEvents")
      .mockImplementation((events) => {
        callOrder.push("loadFromEvents");
        return origLoad.call(useWorkflowStore.getState(), events);
      });

    renderHook(() =>
      useWebSocket("runTime", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
        fetchImpl: fetchSpy as unknown as typeof fetch,
        onResumeFallback,
      })
    );

    // 首次连接 + 一个事件让 lastSeqSeen=3
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 3,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runTime",
        } satisfies WebEvent),
      } as MessageEvent);
    });

    // 重连 + 等 watchdog 超时触发 fallback
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    const sock2 = lastSocket()!;
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    await Promise.resolve();
    await act(async () => {
      await Promise.resolve();
    });

    // 关键断言：loadFromEvents 先于 dropBuffer（避免 re-fold 时旧 buffer frame 残留）
    expect(callOrder).toEqual(["loadFromEvents", "dropBuffer"]);
    loadSpy.mockRestore();
  });

  it("resume_ok ack 帧 → 清 watchdog，不进 store.events（控制平面帧）", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    const fetchSpy = vi.fn();
    const onResumeFallback = vi.fn();

    renderHook(() =>
      useWebSocket("runAck", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
        fetchImpl: fetchSpy as unknown as typeof fetch,
        onResumeFallback,
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();
    // 推一个事件让 lastSeqSeen=2
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 2,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runAck",
        } satisfies WebEvent),
      } as MessageEvent);
    });
    const eventsLenBefore = useWorkflowStore.getState().events.length;

    // 重连 + 收到 resume_ok（无业务事件重放，server 已 caught-up）
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    const sock2 = lastSocket()!;
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();

    // 模拟 server resume_ok ack（D4 配套）
    await act(async () => {
      sock2.onmessage?.({
        data: JSON.stringify({
          type: "resume_ok",
          run_id: "runAck",
          last_seq: 2,
        }),
      } as MessageEvent);
    });

    // 推进远超 watchdog 时窗 → 不应触发 fallback（ack 已清 watchdog）
    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(onResumeFallback).not.toHaveBeenCalled();
    // resume_ok 不进 store.events（控制平面帧，非业务事件）
    expect(useWorkflowStore.getState().events.length).toBe(eventsLenBefore);
  });
});
