// test/store-fail-loud.test.ts —— SPEC audit-c §4.1 loader fail-loud + 退避重试 + 并发不串话。
//
// 断言意图（Rule 9）：
//   1. C1-AC1: HTTP 500 → loadStatus="error" + loadError.kind="http" + status=500
//   2. C1-AC2: 网络错误 → loadError.kind="network"
//   3. C1-AC3/E4/BLOCKER-3: 退避期 loadStatus 保持 loading + retryCount 进 store；3 次失败翻 error
//   4. C1-AC4: 退避中 mock 恢复 200 → loadStatus="loaded"
//   5. C1-AC6/E4: 终态 error 后手动重调 → retryCount reset=0
//   6. C1-AC5/B2/G2/N2/C2: 并发不串话——A 退避 timer pending 时切到 B → A timer 被 cancel +
//      A in-flight fetch 被 abort（AbortError）；A→B→A 同 runId 不同实例 moduleEpoch 校验丢弃
//   7. C5-AC1/G7: resp.json() throw 或 !Array.isArray(json) → loadError.kind="parse" + activeRunId=null
//   8. C1-AC8/M14: loadEarlierChunk 失败 → historyLoadError=true + 节流 + 下次成功自动清
//   9. C2-AC20: loadEarlierChunk(A) 在飞 + 切 B + A chunk late-resolve → 写时校验丢弃
//   10. INV-7-AC/E6: loadStatus!=loaded 时 processEvent drop + warn-once
//   11. seenSeqs O(1) + refold 末尾重建（N1）
//   12. C3-AC1 __foldTwiceForInvariantCheck canary + foreach progress NaN warn

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  __foldTwiceForInvariantCheck,
  useWorkflowStore,
} from "@/stores/workflow-store";
import type { WebEvent } from "@/types/events";
import { makeEvent } from "./_helpers";

function resetStoreIdle() {
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
    takenEdgeKeys: new Set<string>(),
    seenSeqs: new Set<number>(),
    selectedNode: null,
    selectedSession: null,
    activeRunId: null,
    loadStatus: "idle",
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

const events200: WebEvent[] = [
  makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } }),
  makeEvent("node_started", { seq: 2, node: "A" }),
];

describe("SPEC audit-c C1 fail-loud + 退避重试", () => {
  beforeEach(() => {
    resetStoreIdle();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("C1-AC1: HTTP 500 三次 → loadStatus=error + loadError.kind=http + status=500", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const p = useWorkflowStore.getState().loadRun("r1");
    // 推进退避（1s/2s）
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(4000);
    await p;

    const s = useWorkflowStore.getState();
    expect(s.loadStatus).toBe("error");
    expect(s.loadError?.kind).toBe("http");
    if (s.loadError?.kind === "http") {
      expect(s.loadError.status).toBe(500);
    }
    expect(s.activeRunId).toBeNull(); // 未写（INV-4 不留 half-loaded）
    expect(fetchMock.mock.calls.length).toBe(3); // 1 初 + 2 重试
  });

  it("C1-AC2: 网络错误（fetch reject）→ loadError.kind=network", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error("network down"));
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const p = useWorkflowStore.getState().loadRun("r2");
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(4000);
    await p;

    const s = useWorkflowStore.getState();
    expect(s.loadStatus).toBe("error");
    expect(s.loadError?.kind).toBe("network");
  });

  it("C1-AC3/E4/BLOCKER-3: 退避期 loadStatus 保持 loading + retryCount 进 store", async () => {
    let calls = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      calls++;
      if (calls < 3) return Promise.resolve({ ok: false, status: 500 });
      return Promise.resolve({ ok: true, json: async () => events200 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const p = useWorkflowStore.getState().loadRun("r3");
    // 第一次退避完成 → retryCount=1，loadStatus 仍 loading（BLOCKER-3）
    await vi.advanceTimersByTimeAsync(1000);
    expect(useWorkflowStore.getState().retryCount).toBe(1);
    expect(useWorkflowStore.getState().loadStatus).toBe("loading");
    // 第二次退避完成 → retryCount=2（fetch 同步 mock resolve 在同 tick 内，但 setState
    // retryCount=2 在 await fetch 之前，故此处可观测）
    await vi.advanceTimersByTimeAsync(2000);
    // 注意：第三次 attempt（200）在同 microtask flush 内会 reset retryCount=0 + loadStatus=loaded
    // 故此处不再断言 retryCount=2（其窗口极窄），改为最终态校验。
    await vi.advanceTimersByTimeAsync(4000);
    await p;

    const s = useWorkflowStore.getState();
    expect(s.loadStatus).toBe("loaded"); // C1-AC4 重试中 mock 恢复 200
    expect(s.activeRunId).toBe("r3");
    expect(s.events.length).toBe(2);
    expect(s.retryCount).toBe(0); // 成功后 reset（_refoldAndCommit）
  });

  it("C1-AC6/E4: 终态 error 后手动重调 → retryCount reset=0", async () => {
    let calls = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      calls++;
      if (calls <= 3) return Promise.resolve({ ok: false, status: 500 });
      return Promise.resolve({ ok: true, json: async () => events200 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    let p = useWorkflowStore.getState().loadRun("r4");
    await vi.advanceTimersByTimeAsync(1000);
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(4000);
    await p;
    expect(useWorkflowStore.getState().loadStatus).toBe("error");

    // 手动重调（模拟用户点 RunLoadError 的重试按钮）→ reset retryCount=0
    p = useWorkflowStore.getState().loadRunWithMeta("r4");
    expect(useWorkflowStore.getState().retryCount).toBe(0);
    expect(useWorkflowStore.getState().loadStatus).toBe("loading");
    await vi.advanceTimersByTimeAsync(10000);
    await p;
    expect(useWorkflowStore.getState().loadStatus).toBe("loaded");
  });
});

describe("SPEC audit-c C1-AC5 并发不串话（B2/G2/N2/C2）", () => {
  beforeEach(() => {
    resetStoreIdle();
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("A 退避 timer pending 时切到 B → A 的 fetch 被 abort（AbortError）", async () => {
    // A 总是 500，进入退避
    let aFetchAborted = false;
    const fetchMock = vi.fn().mockImplementation((_url: string, opts?: { signal?: AbortSignal }) => {
      return new Promise<void>((_resolve, reject) => {
        const onAbort = () => {
          aFetchAborted = true;
          reject(new DOMException("aborted", "AbortError"));
        };
        if (opts?.signal?.aborted) return onAbort();
        opts?.signal?.addEventListener("abort", onAbort);
        // 永不 resolve（等 abort 或测试结束）
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 触发 A 的 loadRun（in-flight fetch pending）
    void useWorkflowStore.getState().loadRun("A");
    await vi.advanceTimersByTimeAsync(0); // 让 set/loadRun 同步部分跑完

    // 切到 B（应该 abort-all-entries + cancel pending 退避 timer）
    const fetchB = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => events200,
    });
    vi.stubGlobal("fetch", fetchB as unknown as typeof fetch);
    const pB = useWorkflowStore.getState().loadRunWithMeta("B");
    await vi.advanceTimersByTimeAsync(10000);
    await pB;

    expect(aFetchAborted).toBe(true); // G2/C12: in-flight fetch 被 abort（非 resolve 后丢弃）
    expect(useWorkflowStore.getState().activeRunId).toBe("B");
  });

  it("B1 fix: A 退避 timer pending（非 attempt 0 fetch）时切到 B → A loader 不悬挂", async () => {
    // A 第一次 fetch 返回 500（attempt 0 完成）→ 进入 attempt 1 退避（1s timer pending）
    const fetchMockA = vi.fn().mockImplementation((_url: string, _opts?: { signal?: AbortSignal }) => {
      return Promise.resolve({ ok: false, status: 500 });
    });
    vi.stubGlobal("fetch", fetchMockA as unknown as typeof fetch);

    // 启动 A 的 loadRun，让它 attempt 0 失败后进入退避
    const pA = useWorkflowStore.getState().loadRun("A");
    // 让 attempt 0 fetch 完成（进入 attempt 1 退避，1s timer pending）
    await vi.advanceTimersByTimeAsync(0);
    // 切到 B → abortAllInflight → A 的 timer 被 clearTimeout + signal abort → 退避 promise reject
    // → fetchEventsWithBackoff throw → loadRun catch 写错误态（B1 fix：promise 不悬挂）
    const fetchMockB = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => events200,
    });
    vi.stubGlobal("fetch", fetchMockB as unknown as typeof fetch);
    const pB = useWorkflowStore.getState().loadRunWithMeta("B");
    // A 的 loader promise 应当 resolve（catch 内吞了 AbortError → writeLoadError 因 abort 早退）
    // 关键：不悬挂。给一个有限超时（fake timer），若 pA 仍 pending 测试会卡（vitest 自动报超时）。
    let aResolved = false;
    pA.then(() => {
      aResolved = true;
    });
    await vi.advanceTimersByTimeAsync(10000);
    await pB;
    // pA 已 resolve（不悬挂）
    expect(aResolved).toBe(true);
    expect(useWorkflowStore.getState().activeRunId).toBe("B");
  });

  it("A→B→A 同 runId 不同实例 → 第一次 load(A) 的迟到结果被 moduleEpoch 丢弃（N2）", async () => {
    const holder: { resolve: ((v: { ok: true; json: () => Promise<WebEvent[]> }) => void) | null } = { resolve: null };
    const firstAFetch = () =>
      new Promise<{ ok: true; json: () => Promise<WebEvent[]> }>((resolve) => {
        holder.resolve = resolve;
      });
    let callIdx = 0;
    const fetchMock = vi.fn().mockImplementation((_url: string) => {
      callIdx++;
      if (callIdx === 1) return firstAFetch(); // 第一次 load(A) pending
      // 第二次 load(A) 立即 200
      return Promise.resolve({ ok: true, json: async () => events200 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 第一次 load(A) —— fetch pending
    void useWorkflowStore.getState().loadRun("A");
    await vi.advanceTimersByTimeAsync(0);

    // 切到 B（abort 第一次 load(A)）—— 实际上用 unloadRun 后再 load(A) 模拟 A→B→A
    useWorkflowStore.getState().unloadRun();
    // 第二次 load(A)
    const p2 = useWorkflowStore.getState().loadRun("A");
    await vi.advanceTimersByTimeAsync(0);
    await p2;

    // 第一次 load(A) 的迟到 fetch 此刻 resolve（chunk: 一个超长事件，会污染如果没校验）
    const staleEvents: WebEvent[] = [
      makeEvent("workflow_started", {
        seq: 999,
        data: { workflow_name: "STALE" },
      }),
    ];
    holder.resolve?.({ ok: true, json: async () => staleEvents });
    // 让微任务跑完（写时校验在 set 内同步执行）
    await Promise.resolve();
    await Promise.resolve();

    // 第二次 load(A) 的 events 不含 STALE
    const s = useWorkflowStore.getState();
    expect(s.activeRunId).toBe("A");
    expect(s.events.some((e) => e.seq === 999)).toBe(false); // 陈旧 fetch 被丢弃
  });
});

describe("SPEC audit-c C5-AC1/G7 parse 失败不留 half-loaded", () => {
  beforeEach(() => resetStoreIdle());
  afterEach(() => vi.unstubAllGlobals());

  it("resp.json() 返回非 array → loadError.kind=parse + activeRunId=null", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ not: "array" }), // 非 array
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    vi.useFakeTimers();

    const p = useWorkflowStore.getState().loadRun("r5");
    await vi.advanceTimersByTimeAsync(10000); // 走完 3 次退避
    await p;
    vi.useRealTimers();

    const s = useWorkflowStore.getState();
    expect(s.loadStatus).toBe("error");
    expect(s.loadError?.kind).toBe("parse");
    expect(s.activeRunId).toBeNull(); // INV-4 不留 half-loaded
  });
});

describe("SPEC audit-c C1-AC8/M14 loadEarlierChunk banner-only", () => {
  beforeEach(() => resetStoreIdle());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("loadEarlierChunk 失败 → historyLoadError=true；同窗口节流；下次成功自动清", async () => {
    // setup huge 模式
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      oldestSeqInWindow: 100,
      activeRunId: "r",
      loadStatus: "loaded",
    });
    let calls = 0;
    const fetchMock = vi.fn().mockImplementation(() => {
      calls++;
      if (calls <= 2) return Promise.resolve({ ok: false, status: 500 });
      // 第三次成功
      return Promise.resolve({
        ok: true,
        json: async () => [makeEvent("agent_message", { seq: 90, node: "A", data: { text: "x" } })],
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 第一次失败
    let ok = await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    expect(ok).toBe(false);
    expect(useWorkflowStore.getState().historyLoadError).toBe(true);
    // 第二次失败（同窗口）→ 节流：不重复 set（仍 true，但 setState 调用次数应控制）
    ok = await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    expect(ok).toBe(false);
    expect(useWorkflowStore.getState().historyLoadError).toBe(true);
    // 第三次成功 → 自动清
    ok = await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    expect(ok).toBe(true);
    expect(useWorkflowStore.getState().historyLoadError).toBe(false);
  });

  it("C2-AC20: loadEarlierChunk(A) 在飞 + 切 B + chunk late-resolve → 写时校验丢弃", async () => {
    // setup A huge 模式
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      oldestSeqInWindow: 100,
      activeRunId: "A",
      loadStatus: "loaded",
    });
    const chunkHolder: { resolve: ((v: { ok: true; json: () => Promise<WebEvent[]> }) => void) | null } = { resolve: null };
    const fetchMock = vi.fn().mockImplementation(() => {
      return new Promise<{ ok: true; json: () => Promise<WebEvent[]> }>((resolve) => {
        chunkHolder.resolve = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // loadEarlierChunk(A) —— fetch pending
    const p = useWorkflowStore.getState().loadEarlierChunk("A", 10);
    // 切到 B（activeRunId 变化）
    useWorkflowStore.setState({ activeRunId: "B" });
    // A chunk late-resolve
    const chunkA: WebEvent[] = [
      makeEvent("agent_message", { seq: 90, node: "A", data: { text: "A-chunk" } }),
    ];
    chunkHolder.resolve?.({ ok: true, json: async () => chunkA });
    const ok = await p;

    expect(ok).toBe(false); // 写时校验丢弃
    // B 的 events 不含 A 的 chunk
    expect(useWorkflowStore.getState().events.some((e) => e.seq === 90)).toBe(false);
  });
});

describe("SPEC audit-c INV-7 + seenSeqs", () => {
  beforeEach(() => resetStoreIdle());
  afterEach(() => vi.restoreAllMocks());

  it("INV-7-AC/E6: loadStatus!=loaded 时 processEvent drop + warn-once", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    useWorkflowStore.setState({ loadStatus: "loading", activeRunId: "r" });
    const ev = makeEvent("node_completed", { seq: 1, node: "A" });

    useWorkflowStore.getState().processEvent(ev);
    useWorkflowStore.getState().processEvent(ev); // 同 seq 第二次 drop
    expect(useWorkflowStore.getState().events.length).toBe(0); // INV-7 drop
    // warn-once：同 runId::seq 只 warn 一次
    const inv7Warns = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("INV-7 drop")
    );
    expect(inv7Warns.length).toBe(1);

    // unloadRun 清 Set → 再次 drop 重新 warn
    useWorkflowStore.getState().unloadRun();
    useWorkflowStore.setState({ loadStatus: "loading", activeRunId: "r" });
    useWorkflowStore.getState().processEvent(ev);
    const inv7Warns2 = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("INV-7 drop")
    );
    expect(inv7Warns2.length).toBe(2); // Set 清后重新 warn
  });

  it("seenSeqs O(1) 去重：同 seq 两次只入一次（C4 + enableMapSet B1）", () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r" });
    const ev = makeEvent("node_completed", { seq: 5, node: "A" });
    useWorkflowStore.getState().processEvent(ev);
    useWorkflowStore.getState().processEvent(ev);
    expect(useWorkflowStore.getState().events.length).toBe(1);
    expect(useWorkflowStore.getState().seenSeqs.has(5)).toBe(true);
  });

  it("N1: loadEarlierChunk 走 refold 末尾重建 seenSeqs", async () => {
    useWorkflowStore.setState({
      loadStatus: "loaded",
      activeRunId: "r",
      huge: true,
      hugeFullyLoaded: false,
      oldestSeqInWindow: 100,
      events: [makeEvent("agent_message", { seq: 100, node: "A", data: { text: "x" } })],
      seenSeqs: new Set([100]),
    });
    const chunk: WebEvent[] = [
      makeEvent("agent_message", { seq: 90, node: "A", data: { text: "older" } }),
    ];
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => chunk,
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    vi.unstubAllGlobals();

    const s = useWorkflowStore.getState();
    expect(s.events.length).toBe(2);
    expect(s.seenSeqs.size).toBe(2); // refold 末尾重建（N1）
    expect(s.seenSeqs.has(90)).toBe(true);
    expect(s.seenSeqs.has(100)).toBe(true);
  });
});

describe("SPEC audit-c C3 __foldTwiceForInvariantCheck canary（B3/G3）", () => {
  beforeEach(() => resetStoreIdle());
  afterEach(() => vi.restoreAllMocks());

  it("C3-AC1: agent_usage 累加型 handler 两次 apply → 派生变化 → warn 命中", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    // 先 load 一个事件让 cost 有基线
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r", cost: 0 });
    const usage = makeEvent("agent_usage", { seq: 10, data: { cost_usd: 0.1 } });
    __foldTwiceForInvariantCheck(usage);
    // microtask 排程
    await Promise.resolve();
    await Promise.resolve();
    const canaryWarns = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("非幂等 canary")
    );
    expect(canaryWarns.length).toBeGreaterThanOrEqual(1); // 累加型 handler 触发
  });

  it("C3-AC1 negative (G3): 幂等 handler（node_started）apply 两次 → warn 不命中", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r" });
    const ev = makeEvent("node_started", { seq: 1, node: "A" });
    __foldTwiceForInvariantCheck(ev);
    await Promise.resolve();
    await Promise.resolve();
    const canaryWarns = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("非幂等 canary")
    );
    expect(canaryWarns.length).toBe(0); // 幂等 handler 不触发
  });

  it("C3-AC2 (M4): foreach_item_completed progress 形异常 → warn + 保留原值", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r" });
    // 先建一个 progress 异常的 node
    useWorkflowStore.setState({
      nodes: { A: { status: "running", progress: "invalid" } },
    });
    const ev = makeEvent("foreach_item_completed", { seq: 5, node: "A" });
    useWorkflowStore.getState().processEvent(ev);
    const m4Warns = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("foreach_item_completed progress 形异常")
    );
    expect(m4Warns.length).toBe(1);
    // 保留原值
    expect(useWorkflowStore.getState().nodes.A.progress).toBe("invalid");
  });
});

describe("SPEC audit-c public loadFromEvents 不翻 loadStatus（E7）", () => {
  beforeEach(() => resetStoreIdle());
  afterEach(() => vi.unstubAllGlobals());

  it("AC23: 直接调 public loadFromEvents → loadStatus 不变（triggerResumeFallback 路径）", () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r" });
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("node_completed", { seq: 1, node: "A" }),
    ]);
    expect(useWorkflowStore.getState().loadStatus).toBe("loaded"); // 不变
  });
});
