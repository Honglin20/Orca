// test/websocket-defer-resume.test.tsx —— SPEC audit-c §3 INV-7 defer-RESUME + listener 生命周期。
//
// AC19 / AC22 / AC24：
//   1. defer-RESUME (E1 BLOCKER): fetch pending + ws.connect + ws.push(N+1) [INV-7 drop] +
//      fetch.resolve(≤N) [loadStatus→loaded] → listener fire sendResume(since=N) + ws.push(replay N+1) → fold
//   2. BLOCKER-2: listener 在 useEffect 顶层注册一次（非 onopen 内）；多次 reconnect → subscribe 调用次数 === 1
//   3. MAJOR-2 per-socket resumeSent dedup：onopen + listener fire 同 socket → resume call count === 1
//   4. F1: reconnect during loading → 不双发（subscribe call count === 0）
//   5. MAJOR-3 reconnect server-restart：loaded → onopen sendResume 首帧 + sendSubscribe 次帧
//   6. F6: loadStatus=loaded → onopen 立即 sendResume

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

function makeFakeSocket() {
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

function resetStoreLoading() {
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
    seenSeqs: new Set<number>(),
    selectedNode: null,
    selectedSession: null,
    activeRunId: "runX",
    loadStatus: "loading", // 模拟初始加载中
    loadError: null,
    retryCount: 0,
    historyLoadError: false,
    huge: false,
    hugeFullyLoaded: true,
    serverOverview: null,
    writable: true,
    oldestSeqInWindow: 0,
    newestSeqInWindow: 0,
  });
}

describe("SPEC audit-c defer-RESUME (E1 BLOCKER)", () => {
  beforeEach(() => {
    resetStoreLoading();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("AC19: fetch pending + ws.push(N+1) [drop] + fetch.resolve(≤N) [loaded] → sendResume(since=N) → replay N+1 → fold", async () => {
    // 准备：/meta 立即返回（non-huge），/events pending
    const fetchHolder: { resolve: ((v: { ok: true; json: () => Promise<WebEvent[]> }) => void) | null } = { resolve: null };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).endsWith("/meta")) {
        return Promise.resolve({
          ok: true,
          json: async () => ({ run_id: "runX", huge: false, writable: true }),
        });
      }
      // /events pending
      return new Promise<{ ok: true; json: () => Promise<WebEvent[]> }>((resolve) => {
        fetchHolder.resolve = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 启动 loadRunWithMeta（fetch pending）+ 同时 mount useWebSocket
    void useWorkflowStore.getState().loadRunWithMeta("runX");
    const { factory, lastSocket } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runX", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    const sock = lastSocket()!;
    // ws 连接打开（fetch 还在 pending，loadStatus 仍 loading）
    await act(async () => {
      sock.onopen?.(new Event("open"));
    });
    // 此时 onopen 读 loadStatus=loading → 不发任何帧
    expect(sock.sent.length).toBe(0);

    // server 推 N+1（lastSeqSeen+1）—— INV-7 因 loading drop
    const initialLastSeq = 0;
    const ev = {
      seq: initialLastSeq + 1,
      type: "node_completed" as const,
      timestamp: 1,
      node: "A",
      session_id: null,
      data: {},
      run_id: "runX",
    };
    await act(async () => {
      sock.onmessage?.({ data: JSON.stringify(ev) } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(0); // INV-7 drop

    // fetch.resolve 返回 events (seq ≤ N，例如空集合或 seq=1)
    fetchHolder.resolve?.({ ok: true, json: async () => [] });
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    // loadStatus → loaded，listener fire sendResume(since=lastSeqSeen)
    expect(useWorkflowStore.getState().loadStatus).toBe("loaded");
    const sentAfterLoaded = sock.sent.map((s) => JSON.parse(s));
    const resumes = sentAfterLoaded.filter((m) => m.type === "resume");
    expect(resumes.length).toBe(1);
    expect(resumes[0]).toEqual({ type: "resume", run_id: "runX", since: 0 });

    // server 经 _handle_resume 重放 N+1 → client fold（INV-7 现已 loaded，接收）
    await act(async () => {
      sock.onmessage?.({ data: JSON.stringify(ev) } as MessageEvent);
    });
    expect(useWorkflowStore.getState().events.length).toBe(1); // 补回不丢
    expect(useWorkflowStore.getState().events[0].seq).toBe(initialLastSeq + 1);
  });
});

describe("SPEC audit-c BLOCKER-2 listener 在 useEffect 顶层注册一次", () => {
  beforeEach(() => {
    resetStoreLoading();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("AC22: 多次 reconnect（onopen 多次）→ useWorkflowStore.subscribe 调用次数 === 1", async () => {
    const subscribeSpy = vi.spyOn(useWorkflowStore, "subscribe");
    const { factory, lastSocket, allSockets } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    // 第一次连接
    allSockets[0].onopen?.(new Event("open"));
    await Promise.resolve();
    // 触发重连
    await act(async () => {
      allSockets[0].onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    // 第二次连接
    lastSocket()!.onopen?.(new Event("open"));
    await Promise.resolve();
    // 触发重连
    await act(async () => {
      lastSocket()!.onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    lastSocket()!.onopen?.(new Event("open"));
    await Promise.resolve();

    // listener 在 useEffect 顶层注册一次（非 onopen 内累积）
    // subscribe 调用次数 === 1（顶层 useEffect 单次注册）
    expect(subscribeSpy.mock.calls.length).toBe(1);
    subscribeSpy.mockRestore();
  });
});

describe("SPEC audit-c MAJOR-2 per-socket resumeSent dedup", () => {
  beforeEach(() => {
    // 直接置 loaded，让 onopen 立即发 resume（与 listener fire 形成 race）
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
      lastSeqSeen: 5,
      nodesIndex: {},
      seenSeqs: new Set<number>(),
      selectedNode: null,
      selectedSession: null,
      activeRunId: "runA",
      loadStatus: "loaded", // loaded → onopen 立即 sendResume
      loadError: null,
      retryCount: 0,
      historyLoadError: false,
    });
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("AC22 MAJOR-2: onopen 立即 sendResume + listener 翻转 fire → resume call count === 1（dedup）", async () => {
    // 顺序 dedup 验证：先让 listener fire sendResume 一次（loading→loaded 翻转），
    // 再强制 onopen 二次触发——per-socket resumeSent 已 true，dedup 不重发。
    // （测试名虽含「race」，实际是顺序：trySendResume 在两路径都同步执行；
    // 真 race 测试见同 describe 下「reconnect 新 socket resumeSent 重置」。）
    useWorkflowStore.setState({ loadStatus: "loading", activeRunId: "runA", lastSeqSeen: 0 });
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
    await act(async () => {
      sock.onopen?.(new Event("open"));
    });
    // onopen 读 loadStatus=loading → 不发任何帧
    expect(sock.sent.length).toBe(0);

    // listener 翻转 fire（loading → loaded）→ sendResume 一次
    await act(async () => {
      useWorkflowStore.setState({ loadStatus: "loaded" });
    });
    let resumes = sock.sent.map((s) => JSON.parse(s)).filter((m) => m.type === "resume");
    expect(resumes.length).toBe(1);

    // 再次触发 onopen（模拟同 socket 第二次 onopen 事件，readyState 仍 OPEN）—— dedup 生效
    await act(async () => {
      sock.onopen?.(new Event("open"));
    });
    resumes = sock.sent.map((s) => JSON.parse(s)).filter((m) => m.type === "resume");
    expect(resumes.length).toBe(1); // MAJOR-2 dedup：仍只 1 次
  });

  it("reconnect 新 socket → resumeSent 重置（可再次 sendResume）", async () => {
    const { factory, lastSocket, allSockets } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    allSockets[0].onopen?.(new Event("open"));
    await Promise.resolve();
    expect(
      allSockets[0].sent.map((s) => JSON.parse(s)).filter((m) => m.type === "resume").length
    ).toBe(1);
    // reconnect
    await act(async () => {
      allSockets[0].onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    const sock2 = lastSocket()!;
    sock2.onopen?.(new Event("open"));
    await Promise.resolve();
    // 新 socket reset resumeSent → 可再次发 resume
    expect(
      sock2.sent.map((s) => JSON.parse(s)).filter((m) => m.type === "resume").length
    ).toBe(1);
  });
});

describe("SPEC audit-c F1 reconnect-during-loading 优先级 + MAJOR-3 server-restart 双帧", () => {
  beforeEach(() => {
    resetStoreLoading(); // loadStatus="loading"
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("F1: reconnect during loading → onopen 不双发（subscribe 调用次数 === 0）", async () => {
    const { factory, lastSocket, allSockets } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    // 第一次连接（loadStatus=loading → onopen 不发帧）
    allSockets[0].onopen?.(new Event("open"));
    await Promise.resolve();
    expect(allSockets[0].sent.length).toBe(0);
    // 触发重连
    await act(async () => {
      allSockets[0].onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    // 第二次 onopen：loadStatus 仍 loading → F1 不双发
    lastSocket()!.onopen?.(new Event("open"));
    await Promise.resolve();
    const sent = lastSocket()!.sent.map((s) => JSON.parse(s));
    const subs = sent.filter((m) => m.type === "subscribe");
    const resumes = sent.filter((m) => m.type === "resume");
    expect(subs.length).toBe(0); // F1：loading 态 reconnect 不双发
    expect(resumes.length).toBe(0); // loading 也不发 resume（等 listener）
  });

  it("MAJOR-3: reconnect + loadStatus=loaded → onopen sendResume 首帧 + sendSubscribe 次帧", async () => {
    useWorkflowStore.setState({ loadStatus: "loaded", lastSeqSeen: 7 });
    const { factory, lastSocket, allSockets } = makeFakeSocket();
    renderHook(() =>
      useWebSocket("runA", {
        createSocket: factory as unknown as (url: string) => WebSocket,
        wsUrl: "ws://test/ws",
      })
    );
    await act(async () => {
      vi.advanceTimersByTime(0);
    });
    // 第一次连接（everConnected=false → initial mount 路径，不双发）
    allSockets[0].onopen?.(new Event("open"));
    await Promise.resolve();
    // 触发重连
    await act(async () => {
      allSockets[0].onclose?.(new CloseEvent("close"));
      vi.advanceTimersByTime(2000);
    });
    await Promise.resolve();
    // 第二次 onopen：wasReconnect=true + loadStatus=loaded → MAJOR-3 双发
    lastSocket()!.onopen?.(new Event("open"));
    await Promise.resolve();
    const sent = lastSocket()!.sent.map((s) => JSON.parse(s));
    // 顺序：sendResume 首帧 + sendSubscribe 次帧
    expect(sent.length).toBeGreaterThanOrEqual(2);
    expect(sent[0].type).toBe("resume");
    expect(sent[0]).toEqual({ type: "resume", run_id: "runA", since: 7 });
    expect(sent[1].type).toBe("subscribe");
    expect(sent[1]).toEqual({ type: "subscribe", run_id: "runA" });
  });
});
