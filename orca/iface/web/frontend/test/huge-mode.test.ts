// test/huge-mode.test.ts —— web-attach Step1 huge-mode + writable + selector AST guard
// （SPEC web-attach §3 / §8 AC10/11/12）。
//
// 断言意图（Rule 9）：
//   1. **loadRunWithMeta**：huge=true → tail=500 + serverOverview 设；huge=false → 全量 fold
//   2. **loadEarlierChunk**：增量 prepend 合并 seq 去重 + 更新 oldestSeqInWindow
//   3. **loadFull**：清 serverOverview + hugeFullyLoaded=true（selectors 回退 client-fold）
//   4. **unloadRun** 清 huge-mode 状态
//   5. **selector AST 守门**（AC §8.12）：所有 ``selectX`` 签名 ``(state)=>...`` 单 state 入参
//   6. **selectAgents/selectCharts huge 模式读 serverOverview**

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkflowStore } from "@/stores/workflow-store";
import { selectAgents, selectCharts } from "@/selectors";
import { makeEvent, resetStore } from "./_helpers";
import type { WebEvent } from "@/types/events";

describe("huge-mode + writable (web-attach Step1)", () => {
  beforeEach(() => resetStore());

  it("loadRunWithMeta：huge=true → tail=500 + serverOverview 设", async () => {
    const tail: WebEvent[] = [
      makeEvent("workflow_started", { seq: 999, data: { workflow_name: "big" } }),
      makeEvent("node_started", { seq: 1000, node: "A" }),
    ];
    const meta = {
      run_id: "r-big",
      status: "running" as const,
      source: "attached" as const,
      event_count: 100000,
      byte_size: 50_000_000,
      oldest_seq: 1,
      newest_seq: 1000,
      writable: false,
      huge: true,
      overview: {
        agents: [{ name: "A", status: "running", tokens: 1234 }],
        charts: [],
        cost_usd: 0.42,
        run_status: "running",
      },
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/meta")) {
        return Promise.resolve({ ok: true, json: async () => meta });
      }
      if (url.includes("?tail=500")) {
        return Promise.resolve({ ok: true, json: async () => tail });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-big");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(true);
    expect(s.hugeFullyLoaded).toBe(false);
    expect(s.writable).toBe(false); // attached run
    expect(s.serverOverview).toEqual(meta.overview);
    expect(s.activeRunId).toBe("r-big");
    expect(s.events.length).toBe(2);
    expect(s.oldestSeqInWindow).toBe(999);
    expect(s.newestSeqInWindow).toBe(1000);
    vi.unstubAllGlobals();
  });

  it("loadRunWithMeta：huge=false → 全量 fold + serverOverview null", async () => {
    const events: WebEvent[] = [
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "smol" } }),
      makeEvent("node_completed", { seq: 2, node: "A", data: { output: "ok" } }),
    ];
    const meta = {
      run_id: "r-smol",
      status: "completed" as const,
      source: "in-process" as const,
      event_count: 2,
      byte_size: 200,
      oldest_seq: 1,
      newest_seq: 2,
      writable: true,
      huge: false,
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/meta")) {
        return Promise.resolve({ ok: true, json: async () => meta });
      }
      // 全量 events 路径（无 query）
      if (url.endsWith("/events")) {
        return Promise.resolve({ ok: true, json: async () => events });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-smol");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(false);
    expect(s.hugeFullyLoaded).toBe(true);
    expect(s.writable).toBe(true);
    expect(s.serverOverview).toBeNull();
    expect(s.events.length).toBe(2);
    vi.unstubAllGlobals();
  });

  it("loadEarlierChunk：增量 prepend 合并 + oldestSeqInWindow 更新", async () => {
    // 先 setup huge 模式：tail = [seq 100..101]
    const tail: WebEvent[] = [
      makeEvent("workflow_started", { seq: 100, data: { workflow_name: "x" } }),
      makeEvent("node_started", { seq: 101, node: "A" }),
    ];
    const meta = {
      run_id: "r",
      status: "running" as const,
      source: "attached" as const,
      event_count: 101,
      byte_size: 5000,
      oldest_seq: 1,
      newest_seq: 101,
      writable: false,
      huge: true,
      overview: { agents: [], charts: [], cost_usd: 0, run_status: "running" },
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/meta")) return Promise.resolve({ ok: true, json: async () => meta });
      if (url.includes("?tail=500")) return Promise.resolve({ ok: true, json: async () => tail });
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    await useWorkflowStore.getState().loadRunWithMeta("r");
    vi.unstubAllGlobals();

    // 再 prepend chunk：seq 90..99（10 条）
    const chunk: WebEvent[] = [];
    for (let i = 90; i < 100; i++) {
      chunk.push(makeEvent("agent_message", { seq: i, node: "A", data: { text: "x" } }));
    }
    const fetchMock2 = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => chunk,
    });
    vi.stubGlobal("fetch", fetchMock2 as unknown as typeof fetch);
    const ok = await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    expect(ok).toBe(true);

    const s = useWorkflowStore.getState();
    expect(s.oldestSeqInWindow).toBe(90);
    expect(s.events.length).toBe(12); // 10 chunk + 2 tail
    expect(s.events.map((e) => e.seq)).toEqual([
      90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101,
    ]);
    vi.unstubAllGlobals();
  });

  it("loadEarlierChunk：已到顶（oldest<=1）→ false（不发 fetch）", async () => {
    // setup: oldestSeqInWindow = 1
    useWorkflowStore.setState({
      huge: true,
      oldestSeqInWindow: 1,
      hugeFullyLoaded: false,
    });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    const ok = await useWorkflowStore.getState().loadEarlierChunk("r", 10);
    expect(ok).toBe(false);
    expect(fetchMock).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("loadFull：清 serverOverview + hugeFullyLoaded=true", async () => {
    const full: WebEvent[] = [
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } }),
      makeEvent("node_completed", { seq: 2, node: "A", data: { output: "o" } }),
      makeEvent("workflow_completed", { seq: 3, data: { elapsed: 1.0 } }),
    ];
    // setup huge mode
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [],
        charts: [],
        cost_usd: 0,
        run_status: "running",
      },
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => full,
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    await useWorkflowStore.getState().loadFull("r");
    const s = useWorkflowStore.getState();
    expect(s.hugeFullyLoaded).toBe(true);
    expect(s.serverOverview).toBeNull();
    expect(s.events.length).toBe(3);
    vi.unstubAllGlobals();
  });

  it("selectAgents huge 模式读 serverOverview（信任服务端 fold）", () => {
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [
          { name: "A", status: "running", tokens: 100 },
          { name: "B", status: "pending" },
        ],
        charts: [],
        cost_usd: 0,
        run_status: "running",
      },
    });
    const agents = selectAgents(useWorkflowStore.getState());
    expect(agents.length).toBe(2);
    expect(agents[0]).toEqual({ node: "A", status: "running", elapsed: undefined });
    expect(agents[1]).toEqual({ node: "B", status: "pending", elapsed: undefined });
  });

  it("selectAgents：loadFull 后回退 client-fold（M4 可验）", () => {
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: true, // 已 loadFull → serverOverview 不生效
      serverOverview: null,
    });
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", {
        seq: 1,
        data: {
          workflow_name: "x",
          topology: {
            entry: "A",
            nodes: [{ name: "A", kind: "agent" }],
            routes: [{ from: "A", to: "$end" }],
            parallel: [],
          },
        },
      }),
      makeEvent("node_started", { seq: 2, node: "A" }),
    ]);
    const agents = selectAgents(useWorkflowStore.getState());
    expect(agents.length).toBe(1);
    expect(agents[0].node).toBe("A");
    expect(agents[0].status).toBe("running");
  });

  it("selectCharts huge 模式读 serverOverview charts 清单", () => {
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [],
        charts: [
          { label: "g1", title: "Chart A", chart_type: "line" },
          { label: "g1", title: "Chart B", chart_type: "bar" },
        ],
        cost_usd: 0,
        run_status: "running",
      },
    });
    const { groups } = selectCharts(useWorkflowStore.getState());
    expect(groups.length).toBe(1); // 都在 g1
    expect(groups[0].entries.length).toBe(2);
  });

  it("unloadRun 清 huge-mode 状态", async () => {
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [],
        charts: [],
        cost_usd: 0,
        run_status: "running",
      },
      writable: false,
      oldestSeqInWindow: 100,
      newestSeqInWindow: 200,
      activeRunId: "r",
    });
    useWorkflowStore.getState().unloadRun();
    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(false);
    expect(s.hugeFullyLoaded).toBe(true);
    expect(s.serverOverview).toBeNull();
    expect(s.writable).toBe(true);
    expect(s.oldestSeqInWindow).toBe(0);
    expect(s.activeRunId).toBeNull();
  });

  // ── AC §8.12 selector AST 守门：所有 selectX 第一参数 = state（单 store 入参） ──
  // SPEC §8.12 真义：「single state 入参」= 第一参数名 = state（杜绝第二 store 旁路）。
  // 允许额外 scalar 参数（``now: number``、``node: string``）——它们不是 store。
  it("selector AST 守门：所有 selectX 第一参数 = state（杜绝第二 store 旁路）", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(import.meta.dirname, "..", "src", "selectors.ts"),
      "utf8"
    );
    const fnRegex = /export\s+function\s+(select\w+)\s*\(([^)]*)\)/g;
    const fns: { name: string; params: string }[] = [];
    let m: RegExpExecArray | null;
    while ((m = fnRegex.exec(src)) !== null) {
      fns.push({ name: m[1], params: m[2].trim() });
    }
    expect(fns.length).toBeGreaterThanOrEqual(4);
    for (const fn of fns) {
      // 第一参数名必须是 ``state``
      const firstParamName = fn.params.split(":")[0].trim();
      expect(
        firstParamName,
        `${fn.name} 第一参数应叫 state（杜绝第二 store 旁路）`
      ).toBe("state");
      // 任何额外参数必须是 scalar（now: number / node: string / thresholdMs: number = ...）
      // —— 不允许 ``otherStore: WorkflowState`` 形态。
      const params = splitTopLevelCommas(fn.params);
      for (let i = 1; i < params.length; i++) {
        const p = params[i];
        // 提取类型 annotation（冒号后）
        const colonIdx = p.indexOf(":");
        const typeAnn = colonIdx >= 0 ? p.slice(colonIdx + 1).trim() : "";
        // 禁止 ``WorkflowState`` 作为额外参数类型（第二 store 旁路）
        expect(
          typeAnn,
          `${fn.name} 第 ${i + 1} 参数类型不允许 WorkflowState（第二 store 旁路）`
        ).not.toContain("WorkflowState");
      }
    }
  });
});

/** 按顶层逗号切分参数列表（保留每个参数的 ``name: type`` 完整片段）。 */
function splitTopLevelCommas(s: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of s) {
    if (ch === "(" || ch === "{" || ch === "[") depth++;
    else if (ch === ")" || ch === "}" || ch === "]") depth--;
    if (ch === "," && depth === 0) {
      out.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}
