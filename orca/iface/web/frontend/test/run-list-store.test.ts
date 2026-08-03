// test/run-list-store.test.ts —— run-list-store 单测（SPEC §3.1 / AC-10/11/12/17）。
//
// 断言意图（Rule 9）：
//   1. **AC-17 / AC11 R3 grep 守门**：run-list-store 不 import workflow-store。
//   2. **AC-10 inflightSeq**：并发 refresh 过期响应被丢弃（防 stale 覆盖 fresh）。
//   3. **AC-11 pendingDeletes**：删除期间 WS refresh 不复活 run（防幽灵 run）。
//   4. **AC-12 deleteRuns**：逐条乐观 + 独立回滚 + 部分失败 refresh 对账 + 返回 {deleted, failed}。
//   5. **deleteRun 单条**：成功移除 / 失败回滚 / 404 视为成功。
//   6. **onRunChanged**：deleted → 乐观移除；else → refresh。
//   7. **reset**：清 runs + pendingDeletes（下次 mount 无残留）。

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useRunListStore,
  startPolling,
  stopPolling,
  type RunSummary,
} from "@/stores/run-list-store";

function mkRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: overrides.run_id ?? "run-1",
    workflow_name: overrides.workflow_name ?? "demo",
    status: overrides.status ?? "completed",
    cost: overrides.cost ?? 0.5,
    elapsed: overrides.elapsed ?? 10,
    started_at: overrides.started_at ?? 1700000000,
    event_count: overrides.event_count ?? 5,
    project_name: overrides.project_name ?? "demo",
    project_id: overrides.project_id ?? "/tmp/demo",
    source: overrides.source ?? "in-process",
    ...overrides,
  };
}

/** 直接重置 store（含 pendingDeletes / inflightSeq 模块级状态，通过 reset() 触达）。 */
function reset() {
  useRunListStore.getState().reset();
}

/** mock fetch：返回 ``routes`` 匹配的第一个；可按需覆盖默认 200/json。 */
function mockFetch(
  runsResponse: RunSummary[],
  routes: { match: (url: string, init?: RequestInit) => boolean; resp: () => Promise<unknown>; ok?: boolean; status?: number }[] = [],
) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (typeof url !== "string") {
      // Request 对象形式（少数 path）
      url = (url as Request).url;
    }
    if (url.includes("/api/runs?scope=all")) {
      return { ok: true, status: 200, json: async () => runsResponse } as Response;
    }
    if (url.includes("/api/projects/stale")) {
      return { ok: true, status: 200, json: async () => [] } as Response;
    }
    for (const r of routes) {
      if (r.match(url, init)) {
        return {
          ok: r.ok ?? true,
          status: r.status ?? 200,
          json: r.resp,
        } as Response;
      }
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

describe("run-list-store", () => {
  beforeEach(() => {
    reset();
    stopPolling();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  // ── AC-17 / AC11 R3 grep 守门 ──
  it("AC-17：run-list-store 不 import workflow-store（R3 铁律）", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        "..",
        "src",
        "stores",
        "run-list-store.ts",
      ),
      "utf8",
    );
    // 仅检查 import 语句，不打击注释中的字符串。
    const importLines = src.split("\n").filter((l) => /^\s*import\b/.test(l));
    for (const line of importLines) {
      expect(
        line,
        `R3 违规：run-list-store 引用了 workflow-store：${line}`,
      ).not.toMatch(/workflow-store/);
    }
  });

  // ── AC-10 inflightSeq：并发 refresh 过期响应被丢弃 ──
  it("AC-10：inflightSeq gate——后发起的 refresh 完成时不被先发起的覆盖", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "Date"] });
    const seq1: RunSummary[] = [mkRun({ run_id: "stale", started_at: 1700000000 })];
    const seq2: RunSummary[] = [mkRun({ run_id: "fresh", started_at: 1800000000 })];

    let callCount = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        callCount++;
        if (callCount === 1) {
          // 模拟第一个请求很慢（5s 后才返回）
          await new Promise((r) => setTimeout(r, 5000));
          return { ok: true, status: 200, json: async () => seq1 } as Response;
        }
        // 第二个请求立即返回
        return { ok: true, status: 200, json: async () => seq2 } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const store = useRunListStore.getState();
    // 清掉节流，让两次 refresh 都进。
    useRunListStore.setState({ lastFetch: 0 });
    const p1 = store.refresh();
    // 第二次 refresh：因为 lastFetch 节流，可能被节流 gate 掉。强制清节流再调。
    useRunListStore.setState({ lastFetch: 0 });
    const p2 = store.refresh();

    // p2 立即返回（fetchMock 第二个 call 不等）。
    await p2;
    // 推进时间让 p1 完成。
    await vi.advanceTimersByTimeAsync(5000);
    await p1;

    // 期望：fresh 胜出（p1 的 stale 响应被 inflightSeq gate 丢弃）。
    const ids = useRunListStore.getState().runs.map((r) => r.run_id);
    expect(ids).toContain("fresh");
    expect(ids).not.toContain("stale");
    vi.useRealTimers();
  });

  // ── AC-11 pendingDeletes：删除期间 refresh 不复活 run ──
  it("AC-11：pendingDeletes 守卫——deleteRun 期间 refresh 拉回被删 run 被过滤", async () => {
    // DELETE 永远 pending（不 resolve），让 deleteRun 卡在 inflight，期间 refresh 应过滤该 run。
    let deleteResolve!: (v: Response) => void;
    const deletePromise = new Promise<Response>((r) => {
      deleteResolve = r;
    });
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        // refresh 始终返回 r1（模拟「服务端还没感知到删除」）。
        return {
          ok: true,
          status: 200,
          json: async () => [mkRun({ run_id: "r1" })],
        } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.startsWith("/api/runs/r1")) {
        // DELETE
        return deletePromise;
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 先 refresh 拉回 r1。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual(["r1"]);

    // 触发 deleteRun（卡 inflight）。
    const delP = useRunListStore.getState().deleteRun("r1");
    // 期间 runs 应不含 r1（乐观移除）。
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual([]);

    // 再 refresh——服务端仍返回 r1，但 pendingDeletes 应过滤它（防复活）。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(
      useRunListStore.getState().runs.map((r) => r.run_id),
      "pendingDeletes 应防 WS refresh 复活被删 run",
    ).toEqual([]);

    // DELETE 成功（NM4：成功 id **不**立即从 pendingDeletes 移除）。
    deleteResolve({ ok: true, status: 200, json: async () => ({}) } as Response);
    await delP;

    // 服务端仍返回 r1（删除尚未落盘）→ r1 仍在 pendingDeletes → 仍过滤（NM4 防幽灵复活）。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(
      useRunListStore.getState().runs.map((r) => r.run_id),
      "NM4：DELETE 200 后服务端仍返 r1 时，pendingDeletes 不移除，仍过滤",
    ).toEqual([]);

    // 服务端终于返空（删除已落盘）→ refresh 确认 → pendingDeletes 移除 r1。
    fetchMock.mockImplementationOnce(async () => ({
      ok: true,
      status: 200,
      json: async () => [],
    }) as Response);
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual([]);
  });

  // ── NM4 deferred cleanup：deleteRuns 成功 id 不立即移除，refresh 确认后才清 ──
  it("NM4：deleteRuns 成功后 pendingDeletes 仍守卫，直到 refresh 确认后端已无", async () => {
    // 后端始终返回原始 3 条（删除尚未传播）。
    let backendRuns: RunSummary[] = [
      mkRun({ run_id: "r1" }),
      mkRun({ run_id: "r2" }),
      mkRun({ run_id: "r3" }),
    ];
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        return { ok: true, status: 200, json: async () => backendRuns } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.startsWith("/api/runs/")) {
        return { ok: true, status: 200, json: async () => ({}) } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(3);

    const result = await useRunListStore.getState().deleteRuns(["r1", "r2", "r3"]);
    expect(result.deleted.sort()).toEqual(["r1", "r2", "r3"]);
    expect(result.failed).toEqual([]);

    // 乐观移除 → runs 清空。
    expect(useRunListStore.getState().runs.length).toBe(0);

    // 后端仍返 3 条（删除未落盘）→ refresh 应过滤（NM4 守卫）。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(
      useRunListStore.getState().runs.length,
      "NM4：后端未落盘时，pendingDeletes 守卫仍过滤，防幽灵复活",
    ).toBe(0);

    // 后端落盘 → 返空 → pendingDeletes 清空 → runs 仍空。
    backendRuns = [];
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(0);
  });

  // ── NM3 epoch guard：reset 后 inflight deleteRun 的 stale before 不写回 ──
  it("NM3：reset 后到达的 deleteRun 失败响应不把 stale before 写回 store", async () => {
    // DELETE 永远 pending（让 deleteRun 卡 inflight）。
    let deleteReject!: (v: Response) => void;
    const deletePromise = new Promise<Response>((r) => {
      deleteReject = r;
    });
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.startsWith("/api/runs/r1")) {
        return deletePromise;
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    // 先装一条 r1。
    useRunListStore.setState({
      runs: [mkRun({ run_id: "r1" })],
      lastFetch: 0,
    });
    expect(useRunListStore.getState().runs.length).toBe(1);

    // 触发 deleteRun（卡 inflight，乐观移除 r1）。
    const delP = useRunListStore.getState().deleteRun("r1");
    expect(useRunListStore.getState().runs.length).toBe(0);

    // 中途 reset（用户离开页面）：epoch++，store 清空。
    useRunListStore.getState().reset();
    expect(useRunListStore.getState().runs.length).toBe(0);

    // DELETE 失败响应到达——按旧逻辑会把 stale ``before=[r1]`` 写回，NM3 守卫应丢弃。
    deleteReject({ ok: false, status: 409, json: async () => ({ error: "live" }) } as Response);
    await expect(delP).rejects.toThrow(/删除失败/);

    // 关键断言：runs 仍为空（NM3 epoch guard 拦截 stale before 写回）。
    expect(
      useRunListStore.getState().runs.length,
      "NM3：reset 后到达的 stale before 不应复活 run",
    ).toBe(0);
  });

  // ── NM3 epoch guard for deleteRuns：批量路径也守 ──
  it("NM3：reset 后到达的 deleteRuns 响应不写回 stale before；返回值仍分桶", async () => {
    // DELETEs 永远 pending，让 deleteRuns 卡 inflight。
    const deleteResolvers: ((v: Response) => void)[] = [];
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.startsWith("/api/runs/r")) {
        return new Promise<Response>((r) => deleteResolvers.push(r));
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    useRunListStore.setState({
      runs: [
        mkRun({ run_id: "r1" }),
        mkRun({ run_id: "r2" }),
        mkRun({ run_id: "r3" }),
      ],
      lastFetch: 0,
    });
    expect(useRunListStore.getState().runs.length).toBe(3);

    const delP = useRunListStore.getState().deleteRuns(["r1", "r2", "r3"]);
    // 乐观移除 → runs 清空。
    expect(useRunListStore.getState().runs.length).toBe(0);

    // reset（用户离开页面）：epoch++。
    useRunListStore.getState().reset();
    expect(useRunListStore.getState().runs.length).toBe(0);

    // 三条 DELETE 响应到达——一条失败两成功，按旧逻辑会把 r2 恢复（部分回滚）。
    deleteResolvers[0]({ ok: true, status: 200, json: async () => ({}) } as Response);
    deleteResolvers[1]({
      ok: false,
      status: 409,
      json: async () => ({ error: "live" }),
    } as Response);
    deleteResolvers[2]({ ok: true, status: 200, json: async () => ({}) } as Response);

    const result = await delP;
    // 返回值仍正确分桶（UI 已等待）。
    expect(result.deleted.sort()).toEqual(["r1", "r3"]);
    expect(result.failed.length).toBe(1);
    expect(result.failed[0].id).toBe("r2");
    // 关键：runs 仍空（NM3 epoch guard 拦截 stale before 写回 + 部分回滚）。
    expect(
      useRunListStore.getState().runs.length,
      "NM3：reset 后到达的批量 stale 响应不写回 runs",
    ).toBe(0);
  });

  // ── nm2 WS action=deleted：onRunChanged 同步 pendingDeletes.delete ──
  it("nm2：onRunChanged action=deleted → 从 pendingDeletes 移除（服务端已确认）", async () => {
    // DELETE 返回 200 但后端 refresh 仍返该 run（落盘延迟）。
    let backendRuns: RunSummary[] = [mkRun({ run_id: "r1" })];
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/runs?scope=all")) {
        return { ok: true, status: 200, json: async () => backendRuns } as Response;
      }
      if (url.includes("/api/projects/stale")) {
        return { ok: true, status: 200, json: async () => [] } as Response;
      }
      if (url.startsWith("/api/runs/r1")) {
        return { ok: true, status: 200, json: async () => ({}) } as Response;
      }
      return { ok: false, status: 404 } as Response;
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual(["r1"]);

    // deleteRun 成功 → r1 乐观移除 + pendingDeletes 入队（NM4：不立即移除）。
    await useRunListStore.getState().deleteRun("r1");
    expect(useRunListStore.getState().runs.length).toBe(0);

    // 服务端广播 run_changed action=deleted → 同步从 pendingDeletes 移除（nm2）。
    useRunListStore.getState().onRunChanged({ run_id: "r1", action: "deleted" });
    expect(useRunListStore.getState().runs.length).toBe(0);

    // 后端仍返 r1（落盘延迟，但 nm2 已确认），但因 pendingDeletes 已移除，refresh 会复活？
    // 不——refresh 用过滤 ``data.filter(r => !pendingDeletes.has(r.run_id))``；
    // pendingDeletes 已空 → r1 复活。但这是「服务端确认删除 + 后端 stale 数据」的正常行为，
    // 下次 refresh 后端会真正返空。本测验证：nm2 后 pendingDeletes 不再阻挡 r1（移除）。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    // r1 重新出现——证明 pendingDeletes 已被 nm2 清空。
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual(["r1"]);
  });

  // ── AC-12 deleteRuns：批量乐观 + 独立回滚 + 部分失败 refresh 对账 ──
  it("AC-12：deleteRuns 全部成功 → 返回 {deleted:[3], failed:[]}", async () => {
    mockFetch(
      [mkRun({ run_id: "r1" }), mkRun({ run_id: "r2" }), mkRun({ run_id: "r3" })],
      [
        {
          match: (url) => url.startsWith("/api/runs/r"),
          resp: async () => ({}),
          ok: true,
          status: 200,
        },
      ],
    );

    // 初始 refresh 装载 r1/r2/r3。
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(3);

    const result = await useRunListStore.getState().deleteRuns(["r1", "r2", "r3"]);
    expect(result.deleted.sort()).toEqual(["r1", "r2", "r3"]);
    expect(result.failed).toEqual([]);
    // runs 应清空。
    expect(useRunListStore.getState().runs.length).toBe(0);
  });

  it("AC-12：deleteRuns 部分失败 → 失败 id 回滚到 runs + 返回 {failed} + refresh 对账", async () => {
    mockFetch(
      [
        mkRun({ run_id: "r1" }),
        mkRun({ run_id: "r2" }),
        mkRun({ run_id: "r3" }),
      ],
      [
        {
          match: (url) => url.endsWith("/api/runs/r2"),
          resp: async () => ({ error: "live" }),
          ok: false,
          status: 409,
        },
        {
          match: (url) => url.startsWith("/api/runs/"),
          resp: async () => ({}),
          ok: true,
          status: 200,
        },
      ],
    );

    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(3);

    const result = await useRunListStore.getState().deleteRuns(["r1", "r2", "r3"]);
    expect(result.deleted.sort()).toEqual(["r1", "r3"]);
    expect(result.failed.length).toBe(1);
    expect(result.failed[0].id).toBe("r2");
    // r2 应回滚到 runs（refresh 对账后仍存在）。
    const ids = useRunListStore.getState().runs.map((r) => r.run_id);
    expect(ids).toContain("r2");
  });

  it("AC-12：deleteRuns 空数组 → 直接返回 {deleted:[], failed:[]} 不发请求", async () => {
    const fetchMock = mockFetch([mkRun({ run_id: "r1" })]);
    const callsBefore = fetchMock.mock.calls.length;
    const result = await useRunListStore.getState().deleteRuns([]);
    expect(result).toEqual({ deleted: [], failed: [] });
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
  });

  // ── deleteRun 单条 ──
  it("deleteRun 成功 → 移除；失败 → 回滚 + rethrow", async () => {
    mockFetch(
      [mkRun({ run_id: "r1" })],
      [
        {
          match: (url) => url.startsWith("/api/runs/r1"),
          resp: async () => ({ error: "live" }),
          ok: false,
          status: 409,
        },
      ],
    );
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(1);

    await expect(
      useRunListStore.getState().deleteRun("r1"),
    ).rejects.toThrow(/删除失败/);
    // 回滚：r1 仍在 runs。
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual(["r1"]);
  });

  it("deleteRun 404 → 视为成功（已删）", async () => {
    mockFetch(
      [mkRun({ run_id: "r1" })],
      [
        {
          match: (url) => url.startsWith("/api/runs/r1"),
          resp: async () => ({}),
          ok: false,
          status: 404,
        },
      ],
    );
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    await useRunListStore.getState().deleteRun("r1");
    expect(useRunListStore.getState().runs.length).toBe(0);
  });

  // ── onRunChanged ──
  it("onRunChanged deleted → 乐观移除；else → 异步 refresh", async () => {
    mockFetch([mkRun({ run_id: "r1" }), mkRun({ run_id: "r2" })]);
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(2);

    useRunListStore.getState().onRunChanged({ run_id: "r1", action: "deleted" });
    expect(useRunListStore.getState().runs.map((r) => r.run_id)).toEqual(["r2"]);

    // action !== deleted → refresh（异步，fire-and-forget；轮询等其落定）。
    useRunListStore.setState({ lastFetch: 0 });
    useRunListStore.getState().onRunChanged({ run_id: "r2", action: "changed" });
    // 轮询 50ms × 20 次（共 1s），等 lastFetch 被刷新。
    let ok = false;
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 50));
      if (useRunListStore.getState().lastFetch > 0) {
        ok = true;
        break;
      }
    }
    expect(ok, "onRunChanged changed 应触发 refresh，但 lastFetch 未更新").toBe(true);
  });

  // ── reset ──
  it("reset 清 runs + staleProjects + error + lastFetch", async () => {
    mockFetch([mkRun({ run_id: "r1" })]);
    useRunListStore.setState({ lastFetch: 0 });
    await useRunListStore.getState().refresh();
    expect(useRunListStore.getState().runs.length).toBe(1);

    useRunListStore.getState().reset();
    const s = useRunListStore.getState();
    expect(s.runs).toEqual([]);
    expect(s.staleProjects).toEqual([]);
    expect(s.error).toBeNull();
    expect(s.lastFetch).toBe(0);
  });

  // ── startPolling / stopPolling 幂等 ──
  it("startPolling/stopPolling 幂等（多次调不报错，多次 stop 安全）", () => {
    expect(() => {
      startPolling();
      startPolling();
      stopPolling();
      stopPolling();
    }).not.toThrow();
  });
});
