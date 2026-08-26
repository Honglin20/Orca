// test/workflow-browse-store.test.ts —— workflow 浏览 store 单测。
//
// 断言意图（Rule 9）：
//   1. **R3 grep 守门**：本 store 不 import workflow-store（与 run-list-store 同约束）。
//   2. **m5 openWorkflow 切换清空**：openWorkflow 前先清 activeAgent/fileTree/activeFile，
//      防闪现上一 wf 文件树。
//   3. **m7 不轮询**：loadWorkflows 不 setInterval（与 run-list-store 的 startPolling 对比）。
//   4. **loadWorkflows / openAgent / openFile** happy path 与 error 路径。

import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkflowBrowseStore } from "@/stores/workflow-browse-store";

function reset() {
  useWorkflowBrowseStore.getState().reset();
}

function mockFetchRoutes(
  routes: {
    match: (url: string) => boolean;
    resp: () => Promise<unknown>;
    ok?: boolean;
    status?: number;
  }[] = [],
) {
  const fetchMock = vi.fn(async (url: string) => {
    for (const r of routes) {
      if (r.match(url)) {
        return {
          ok: r.ok ?? true,
          status: r.status ?? 200,
          json: r.resp,
        } as Response;
      }
    }
    return { ok: false, status: 404, json: async () => ({ detail: "nf" }) } as Response;
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
  return fetchMock;
}

describe("workflow-browse-store", () => {
  beforeEach(() => {
    reset();
    vi.unstubAllGlobals();
  });

  // ── R3 grep 守门 ──
  it("R3：workflow-browse-store 不 import workflow-store（与 run-list-store 同约束）", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        "..",
        "src",
        "stores",
        "workflow-browse-store.ts",
      ),
      "utf8",
    );
    const importLines = src.split("\n").filter((l) => /^\s*import\b/.test(l));
    for (const line of importLines) {
      expect(
        line,
        `R3 违规：workflow-browse-store 引用了 workflow-store：${line}`,
      ).not.toMatch(/workflow-store/);
    }
  });

  // ── R3 grep 守门（m7）：本 store 源码不含 setInterval（不轮询）──
  it("m7：workflow-browse-store 不 setInterval（不轮询）", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        "..",
        "src",
        "stores",
        "workflow-browse-store.ts",
      ),
      "utf8",
    );
    // 排除注释行（仅检查实际代码）
    const codeLines = src.split("\n").filter((l) => !/^\s*\/\//.test(l));
    for (const line of codeLines) {
      expect(
        line,
        `m7 违规：workflow-browse-store 不应轮询（含 setInterval）：${line}`,
      ).not.toMatch(/setInterval\s*\(/);
    }
  });

  // ── loadWorkflows happy path ──
  it("loadWorkflows 成功 → workflows 写入", async () => {
    mockFetchRoutes([
      {
        match: (u) => u.includes("/api/workflows"),
        resp: async () => [
          {
            name: "wf-a",
            description: "A",
            entry: "x",
            inputs_count: 0,
            inputs_schema: [],
          },
        ],
      },
    ]);
    await useWorkflowBrowseStore.getState().loadWorkflows();
    const s = useWorkflowBrowseStore.getState();
    expect(s.workflows.length).toBe(1);
    expect(s.workflows[0].name).toBe("wf-a");
    expect(s.workflowsError).toBeNull();
    expect(s.workflowsLoading).toBe(false);
  });

  it("loadWorkflows 失败 → workflowsError 写入；workflows 不变", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("network down");
      }),
    );
    await useWorkflowBrowseStore.getState().loadWorkflows();
    const s = useWorkflowBrowseStore.getState();
    expect(s.workflowsError).toMatch(/network down/);
    expect(s.workflows).toEqual([]);
    expect(s.workflowsLoading).toBe(false);
  });

  // ── m5 openWorkflow 切换清空 ──
  it("m5：openWorkflow 切换 → activeAgent/fileTree/activeFile 同步清空（防闪现）", async () => {
    // 先装一份「上一 wf」的 active agent / fileTree / file（模拟用户已浏览过）。
    useWorkflowBrowseStore.setState({
      activeAgent: "stale-agent",
      fileTree: [{ path: "stale.py", name: "stale.py", is_dir: false, size: 10, children: null }],
      activeFile: {
        path: "stale.py",
        text: "old",
        ext: "py",
        size: 3,
        truncated: false,
      },
    });
    expect(useWorkflowBrowseStore.getState().activeAgent).toBe("stale-agent");

    // mock openWorkflow 两个请求（detail + agents）让 fetch pending 一会。
    let resolveDetail!: (v: unknown) => void;
    const detailPromise = new Promise((r) => {
      resolveDetail = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/new-wf"),
        resp: async () => detailPromise.then(() => ({
          name: "new-wf",
          description: "",
          entry: "x",
          inputs_schema: {},
          agents_referenced: [],
        })),
      },
      {
        match: (u) => u.endsWith("/api/workflows/new-wf/agents"),
        resp: async () => [],
      },
    ]);

    // 触发 openWorkflow（不 await）—— m5 关键：调用瞬间应清空 activeAgent/fileTree/activeFile。
    const p = useWorkflowBrowseStore.getState().openWorkflow("new-wf");
    // 同步断言：fetch 还在 pending，但旧 agent/fileTree/file 已清空（防闪现）。
    expect(useWorkflowBrowseStore.getState().activeAgent).toBeNull();
    expect(useWorkflowBrowseStore.getState().fileTree).toBeNull();
    expect(useWorkflowBrowseStore.getState().activeFile).toBeNull();
    expect(useWorkflowBrowseStore.getState().workflowLoading).toBe(true);

    // 解开 detail promise 让其完成。
    resolveDetail({});
    await p;
    expect(useWorkflowBrowseStore.getState().activeWorkflow?.meta.name).toBe("new-wf");
  });

  // ── openAgent happy path ──
  it("openAgent 成功 → fileTree 写入", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
    });
    mockFetchRoutes([
      {
        match: (u) => u.includes("/agents/foo/tree"),
        resp: async () => ({
          agent: "foo",
          root: "/tmp/foo",
          nodes: [
            {
              path: "agent.md",
              name: "agent.md",
              is_dir: false,
              size: 10,
              children: null,
            },
          ],
        }),
      },
    ]);
    await useWorkflowBrowseStore.getState().openAgent("foo");
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeAgent).toBe("foo");
    expect(s.fileTree?.length).toBe(1);
    expect(s.fileTree?.[0].path).toBe("agent.md");
    // openAgent 顺带清空 activeFile（切 agent 时文件不再适用）
    expect(s.activeFile).toBeNull();
  });

  it("openAgent 失败 → treeError 写入；fileTree 不变", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({ detail: "boom" }) }) as Response),
    );
    await useWorkflowBrowseStore.getState().openAgent("foo");
    const s = useWorkflowBrowseStore.getState();
    expect(s.treeError).toMatch(/boom/);
    expect(s.fileTree).toBeNull();
  });

  // ── openFile happy path ──
  it("openFile 成功 → activeFile 写入", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
      activeAgent: "foo",
    });
    mockFetchRoutes([
      {
        match: (u) => u.includes("/agents/foo/file?path=helper.py"),
        resp: async () => ({
          path: "helper.py",
          text: "x = 1",
          ext: "py",
          size: 5,
          truncated: false,
        }),
      },
    ]);
    await useWorkflowBrowseStore.getState().openFile("helper.py");
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeFile?.text).toBe("x = 1");
    expect(s.activeFile?.ext).toBe("py");
  });

  it("openFile 失败 → fileError 写入", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
      activeAgent: "foo",
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ detail: "binary file" }) }) as Response),
    );
    await useWorkflowBrowseStore.getState().openFile("data.bin");
    const s = useWorkflowBrowseStore.getState();
    expect(s.fileError).toMatch(/binary file/);
  });

  // ── review 闭环：openWorkflow inflightSeq gate（防并发覆盖）──────────────────
  it("review-1：openWorkflow 并发——后发起的 wf-B 完成时不被先发起的 wf-A 覆盖", async () => {
    // wf-A 响应慢（延迟 resolve），wf-B 立即响应。期望最终 store 显示 wf-B。
    let resolveA!: () => void;
    const delayA = new Promise<void>((r) => {
      resolveA = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/wf-a"),
        resp: async () => {
          await delayA;
          return {
            name: "wf-a",
            description: "A",
            entry: "x",
            inputs_schema: {},
            agents_referenced: [],
          };
        },
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-a/agents"),
        resp: async () => [],
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-b"),
        resp: async () => ({
          name: "wf-b",
          description: "B",
          entry: "y",
          inputs_schema: {},
          agents_referenced: [],
        }),
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-b/agents"),
        resp: async () => [],
      },
    ]);

    // 同时发起两个 openWorkflow（A 先 B 后）。
    const pA = useWorkflowBrowseStore.getState().openWorkflow("wf-a");
    const pB = useWorkflowBrowseStore.getState().openWorkflow("wf-b");
    // B 立即完成。
    await pB;
    expect(useWorkflowBrowseStore.getState().activeWorkflow?.meta.name).toBe("wf-b");
    // 解开 A —— inflightSeq gate 应丢弃 A 的响应（不覆盖 wf-B）。
    resolveA();
    await pA;
    expect(
      useWorkflowBrowseStore.getState().activeWorkflow?.meta.name,
      "inflightSeq gate：wf-A 的 stale 响应不应覆盖 wf-B",
    ).toBe("wf-b");
  });

  // ── review 闭环：openWorkflow 同步清空 activeWorkflow（防 metadata flash）──
  it("review-2：openWorkflow 期间 activeWorkflow=null（loading 状态不让旧 wf metadata 闪现）", async () => {
    // 装入「旧 wf」。
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "stale", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
    });

    let resolveDetail!: () => void;
    const delay = new Promise<void>((r) => {
      resolveDetail = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/new"),
        resp: async () => {
          await delay;
          return {
            name: "new",
            description: "",
            entry: "x",
            inputs_schema: {},
            agents_referenced: [],
          };
        },
      },
      {
        match: (u) => u.endsWith("/api/workflows/new/agents"),
        resp: async () => [],
      },
    ]);

    const p = useWorkflowBrowseStore.getState().openWorkflow("new");
    // 同步断言：fetch pending 时 activeWorkflow 已清空（防 metadata flash）。
    expect(useWorkflowBrowseStore.getState().activeWorkflow).toBeNull();
    resolveDetail();
    await p;
    expect(useWorkflowBrowseStore.getState().activeWorkflow?.meta.name).toBe("new");
  });

  // ── reset 清空全部 ──
  it("reset 清空 workflows + activeWorkflow + activeAgent + fileTree + activeFile", async () => {
    useWorkflowBrowseStore.setState({
      workflows: [
        { name: "x", description: "", entry: "x", inputs_count: 0, inputs_schema: [] },
      ],
      activeWorkflow: {
        meta: { name: "x", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
      },
      activeAgent: "foo",
      fileTree: [],
      activeFile: { path: "x", text: "", ext: "", size: 0, truncated: false },
    });
    useWorkflowBrowseStore.getState().reset();
    const s = useWorkflowBrowseStore.getState();
    expect(s.workflows).toEqual([]);
    expect(s.activeWorkflow).toBeNull();
    expect(s.activeAgent).toBeNull();
    expect(s.fileTree).toBeNull();
    expect(s.activeFile).toBeNull();
  });
});
