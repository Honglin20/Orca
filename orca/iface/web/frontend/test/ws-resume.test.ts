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

// 字段枚举收敛到 _helpers.resetStore（计划步 0：7 处副本合一，防新增顶层字段漏同步）。
import { resetStore } from "./_helpers";

describe("useWebSocket — D6 resume by seq", () => {
  beforeEach(() => resetStore());

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  // SPEC audit-c defer-RESUME：初始连接行为改——loadStatus="loaded" → onopen 立即 sendResume
  // （非 subscribe）。subscribe 只在 reconnect 路径 + loaded/error 时作 server-restart fallback。
  it("初始连接 loadStatus=loaded → 立即 sendResume（defer-RESUME）；事件推进 lastSeqSeen", async () => {
    vi.useFakeTimers();
    const { factory, lastSocket } = makeFakeSocket();
    // resetStore 设 loadStatus="loaded"
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );

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
    // SPEC audit-c BLOCKER-1：loaded → onopen 立即 sendResume
    expect(resumes.length).toBe(1);
    expect(resumes[0]).toEqual({ type: "resume", run_id: "runA", since: 0 });
    // initial mount 路径不双发（与 reconnect 区分，§3 INV-7 契约）
    expect(subs.length).toBe(0);

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

  it("初始连接 loadStatus=loading → onopen 不发帧；listener 翻转 loaded 后 sendResume", async () => {
    vi.useFakeTimers();
    useWorkflowStore.setState({ loadStatus: "loading", activeRunId: "runA" });
    const { factory, lastSocket } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
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

    // loading 态：onopen 不发任何帧（等 listener 回调）
    expect(sock.sent.length).toBe(0);

    // listener 翻转 loaded → fire sendResume + one-shot 自清
    await act(async () => {
      useWorkflowStore.setState({ loadStatus: "loaded" });
    });
    const sent = sock.sent.map((s) => JSON.parse(s));
    const resumes = sent.filter((m) => m.type === "resume");
    expect(resumes.length).toBe(1);
    expect(resumes[0]).toEqual({ type: "resume", run_id: "runA", since: 0 });
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
