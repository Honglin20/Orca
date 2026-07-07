// test/ws-resume.test.ts —— WS reconnect resume by seq（SPEC §0 D6 / §3.3）验收。
//
// 断言：
//   1. 初始连接 → 发 subscribe（无 resume）
//   2. last_seq_seen 推进（收到事件后）
//   3. 重连 → 发 resume(run_id, since=last_seq_seen)；并兜底 subscribe
//   4. onmessage 只处理匹配 run_id 的事件
//
// 反旧设计（旧 use-websocket 重连全量重拉）：D6 用 resume by seq，server 重放 seq>since。

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
    selectedNode: null,
    activeRunId: null,
  });
}

describe("useWebSocket — D6 resume by seq", () => {
  beforeEach(() => resetStore());

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("初始连接只发 subscribe；收到事件后 lastSeqSeen 推进", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );

    // 触发 onopen（react effect 异步：先 await
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock = lastSocket()!;
    expect(sock).toBeDefined();
    sock.onopen?.(new Event("open"));
    await Promise.resolve();

    const sent = sock.sent.map((s) => JSON.parse(s));
    const subs = sent.filter((m) => m.type === "subscribe");
    const resumes = sent.filter((m) => m.type === "resume");
    expect(subs.length).toBe(1);
    expect(subs[0]).toEqual({ type: "subscribe", run_id: "runA" });
    expect(resumes.length).toBe(0); // 初始连接不发 resume

    // 收到一个事件 seq=42 → lastSeqSeen=42
    const ev: WebEvent = {
      seq: 42,
      type: "node_completed",
      timestamp: 1,
      node: "A",
      session_id: null,
      data: {},
      run_id: "runA",
    };
    await act(async () => {
      sock.onmessage?.({ data: JSON.stringify(ev) } as MessageEvent);
    });
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(42);
  });

  it("重连发 resume(run_id, since=lastSeqSeen) + 兜底 subscribe（D6）", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runB", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock1 = lastSocket()!;
    sock1.onopen?.(new Event("open"));
    await Promise.resolve();

    // 注入事件让 lastSeqSeen=99
    await act(async () => {
      sock1.onmessage?.({
        data: JSON.stringify({
          seq: 99,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runB",
        } as WebEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().lastSeqSeen).toBe(99);

    // 触发重连：close（非主动）→ setTimeout → open 新 socket
    await act(async () => {
      sock1.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();

    const sock2 = lastSocket()!;
    expect(sock2).not.toBe(sock1);
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();

    const sent2 = sock2.sent.map((s) => JSON.parse(s));
    const resume = sent2.find((m) => m.type === "resume");
    expect(resume).toEqual({ type: "resume", run_id: "runB", since: 99 });
  });

  it("onmessage 过滤非匹配 run_id 事件", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runC", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );

    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock = lastSocket()!;
    sock.onopen?.(new Event("open"));
    await Promise.resolve();

    // 另一个 run 的事件 → 过滤
    await act(async () => {
      sock.onmessage?.({
        data: JSON.stringify({
          seq: 1,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "OTHER",
        } as WebEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(0);

    // 匹配的 → 接收
    await act(async () => {
      sock.onmessage?.({
        data: JSON.stringify({
          seq: 5,
          type: "node_completed",
          timestamp: 1,
          node: "A",
          session_id: null,
          data: {},
          run_id: "runC",
        } as WebEvent),
      } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(1);
  });
});
