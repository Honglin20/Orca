// test/huge-mode.test.ts —— web-attach Step1 huge-mode + writable + selector AST guard
// （SPEC web-attach §3 / §8 AC10/11/12）。
//
// 断言意图（Rule 9）：
//   1. **loadRunWithMeta**（web-perf P3 2026-09-08）：首屏一律 ?tail=500 窗口——满窗=截断
//      （hugeFullyLoaded=false；huge 时启用 serverOverview，非 huge 走 client-fold 窗口）；
//      未满窗=全量等价（hugeFullyLoaded=true）
//   2. **loadEarlierChunk**：增量 prepend 合并 seq 去重 + 更新 oldestSeqInWindow；gate =
//      窗口态（!hugeFullyLoaded）且未到顶（oldest>1）——非 huge 窗口态同样可用
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

  // web-perf P3（2026-09-08）：首屏一律 ?tail=500 窗口；满窗=截断（hugeFullyLoaded=false +
  // oldestSeqInWindow 置窗内最旧 seq），未满窗=全量等价。下列测试按新契约同步（Rule 9：
  // 同步断言意图，非静默放宽）。

  it("loadRunWithMeta：huge=true + 满窗 → tail 窗口态（hugeFullyLoaded=false + serverOverview 启用）", async () => {
    // 500 条满窗（seq 501..1000，模拟大 run 的尾窗）
    const tailWindow: WebEvent[] = [];
    for (let i = 501; i <= 1000; i++) {
      tailWindow.push(
        makeEvent("agent_message", { seq: i, node: "A", data: { text: "x" } }),
      );
    }
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
      // P3：首屏 tail 窗口（带 ?tail=500 query）
      if (url.endsWith("/events?tail=500")) {
        return Promise.resolve({ ok: true, json: async () => tailWindow });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-big");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(true);
    expect(s.hugeFullyLoaded).toBe(false); // 满窗=截断 → 窗口态（加载更早/loadFull 可用）
    expect(s.writable).toBe(false); // attached run
    expect(s.serverOverview).toEqual(meta.overview); // huge 窗口态启用服务端 fold
    expect(s.activeRunId).toBe("r-big");
    expect(s.events.length).toBe(500);
    expect(s.oldestSeqInWindow).toBe(501); // 窗内最旧 seq（「加载更早」的 since 基准）
    vi.unstubAllGlobals();
  });

  it("loadRunWithMeta：huge=false + 未满窗 → 全量等价（hugeFullyLoaded=true + serverOverview null）", async () => {
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
      if (url.endsWith("/events?tail=500")) {
        return Promise.resolve({ ok: true, json: async () => events });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-smol");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(false);
    expect(s.hugeFullyLoaded).toBe(true); // 未满窗=全量，行为与旧全量加载等价
    expect(s.writable).toBe(true);
    expect(s.serverOverview).toBeNull();
    expect(s.events.length).toBe(2);
    expect(s.oldestSeqInWindow).toBe(1); // 全量 → 已到顶
    vi.unstubAllGlobals();
  });

  it("loadRunWithMeta：huge=false + 满窗 → 窗口化但无 serverOverview（client-fold 窗口）", async () => {
    // P3 核心：非 huge 的多事件 run 也窗口化（500+），但后端 /meta 无 overview（cache
    // 缺失等异常）→ serverOverview null 退化 client-fold 窗口（不崩，缺补偿而已）。
    const windowed: WebEvent[] = [];
    for (let i = 1; i <= 500; i++) {
      windowed.push(
        makeEvent("agent_message", { seq: i, node: "A", data: { text: "x" } }),
      );
    }
    const meta = {
      run_id: "r-mid",
      status: "completed" as const,
      source: "attached" as const,
      event_count: 20000,
      byte_size: 1_000_000,
      oldest_seq: 1,
      newest_seq: 20000,
      writable: true,
      huge: false, // 未达 huge 阈值（>50000 events / >5MB）
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/meta")) {
        return Promise.resolve({ ok: true, json: async () => meta });
      }
      if (url.endsWith("/events?tail=500")) {
        return Promise.resolve({ ok: true, json: async () => windowed });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-mid");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(false);
    expect(s.hugeFullyLoaded).toBe(false); // 满窗=截断
    expect(s.serverOverview).toBeNull(); // meta 无 overview → 退化 client-fold 窗口
    expect(s.events.length).toBe(500);
    expect(s.oldestSeqInWindow).toBe(1); // 恰好从 1 起 → loadEarlierChunk 已到顶
    vi.unstubAllGlobals();
  });

  it("loadRunWithMeta：非 huge + 满窗 + overview → serverOverview/workflowName 补偿（review MAJOR-1）", async () => {
    // MAJOR-1 补偿通道：非 huge 的截断 run 也启用 serverOverview（后端 /meta 对所有
    // run 返回 overview）；workflow_started（seq 1）在窗外 → workflowName 由 overview
    // 补偿，否则 TopBar 名称消失。
    const windowed: WebEvent[] = [];
    for (let i = 2; i <= 501; i++) {
      windowed.push(
        makeEvent("agent_message", { seq: i, node: "A", data: { text: "x" } }),
      );
    }
    const meta = {
      run_id: "r-mid2",
      status: "completed" as const,
      source: "attached" as const,
      event_count: 800,
      byte_size: 500_000,
      oldest_seq: 1,
      newest_seq: 801,
      writable: true,
      huge: false,
      overview: {
        agents: [{ name: "A", status: "done" }],
        charts: [{ label: "g1", title: "Chart X", chart_type: "line" }],
        cost_usd: 0.1,
        run_status: "completed",
        workflow_name: "mid_wf",
      },
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/meta")) {
        return Promise.resolve({ ok: true, json: async () => meta });
      }
      if (url.endsWith("/events?tail=500")) {
        return Promise.resolve({ ok: true, json: async () => windowed });
      }
      return Promise.resolve({ ok: false, status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await useWorkflowStore.getState().loadRunWithMeta("r-mid2");

    const s = useWorkflowStore.getState();
    expect(s.huge).toBe(false);
    expect(s.hugeFullyLoaded).toBe(false);
    expect(s.serverOverview).toEqual(meta.overview); // 窗口态补偿启用（不要求 huge）
    expect(s.workflowName).toBe("mid_wf"); // seq1 在窗外 → overview 补偿
    expect(s.oldestSeqInWindow).toBe(2);
    vi.unstubAllGlobals();
  });

  it("loadFromEvents（resume fallback 全量重拉）→ 回整窗口簿记（review MAJOR-2）", () => {
    // 窗口态遇 resume-fallback 全量重拉后，若窗口字段不回整：HistoryBanner 恒挂 +
    // loadEarlierChunk 会把窗外已存在事件重复并入（图表双计/会话双渲染）。
    useWorkflowStore.setState({
      huge: false,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [],
        charts: [],
        cost_usd: 0,
        run_status: "completed",
      },
      oldestSeqInWindow: 501,
      activeRunId: "r",
    });
    // 全量事件（seq 1..2）
    useWorkflowStore.getState().loadFromEvents([
      makeEvent("workflow_started", { seq: 1, data: { workflow_name: "x" } }),
      makeEvent("workflow_completed", { seq: 2, data: { elapsed: 1.0 } }),
    ]);
    const s = useWorkflowStore.getState();
    expect(s.hugeFullyLoaded).toBe(true);
    expect(s.oldestSeqInWindow).toBe(1);
    expect(s.serverOverview).toBeNull();
  });

  it("selectAgents：非 huge 窗口态同样读 serverOverview（review MAJOR-1 gate 前提去除）", () => {
    useWorkflowStore.setState({
      huge: false,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [
          { name: "A", status: "done" },
          { name: "B", status: "pending" },
        ],
        charts: [],
        cost_usd: 0,
        run_status: "completed",
      },
    });
    const agents = selectAgents(useWorkflowStore.getState());
    expect(agents.map((a) => a.node)).toEqual(["A", "B"]);
  });

  it("loadEarlierChunk：增量 prepend 合并 + oldestSeqInWindow 更新", async () => {
    // huge 现直接全量加载（loadRunWithMeta 不再产 tail 态），故 loadEarlierChunk 的
    // tail 场景在此显式构造：loadFromEvents + setState huge/window 边界。
    const tail: WebEvent[] = [
      makeEvent("workflow_started", { seq: 100, data: { workflow_name: "x" } }),
      makeEvent("node_started", { seq: 101, node: "A" }),
    ];
    useWorkflowStore.getState().loadFromEvents(tail);
    useWorkflowStore.setState({
      huge: true,
      hugeFullyLoaded: false,
      oldestSeqInWindow: 100,
      newestSeqInWindow: 101,
      activeRunId: "r",
    });

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

  it("loadEarlierChunk：全量态（hugeFullyLoaded）→ false（不发 fetch；P3 gate 非 huge 化）", async () => {
    // P3：gate 从 !huge 改为 !hugeFullyLoaded——全量态无更早历史，窗口态（含非 huge
    // 的多事件 run）可用。
    useWorkflowStore.setState({
      huge: false,
      oldestSeqInWindow: 100,
      hugeFullyLoaded: true,
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

  it("selectCharts huge 模式占位 entry 带 placeholder:true（partition 不误 reject）", () => {
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
    const entries = groups[0].entries;
    expect(entries.length).toBe(2);
    expect(entries.every((e) => e.placeholder === true)).toBe(true);
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
    expect(s.loadStatus).toBe("idle"); // SPEC audit-c M18
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
