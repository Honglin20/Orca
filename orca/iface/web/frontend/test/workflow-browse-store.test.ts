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
        subagents: [],
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
        subagents: [],
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
        subagents: [],
      },
      activeAgent: "foo",
      // 批 G：openFile 按 treeScope 分流——本用例锁 agent scope 语义（配 agent file mock）。
      treeScope: "agent",
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
        subagents: [],
      },
      activeAgent: "foo",
      treeScope: "agent",
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
        subagents: [],
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
        subagents: [],
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

  // ── 批 G（2026-08-27）：treeScope 状态机 + wf 级树 + openFile 分流 ──────────────

  const _WF_TREE_NODES = [
    {
      path: "workflow.yaml",
      name: "workflow.yaml",
      is_dir: false,
      size: 10,
      children: null,
    },
  ];

  function mockOpenWorkflowRoutes(opts?: { treeFail?: boolean }) {
    return mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/wf-g"),
        resp: async () => ({
          name: "wf-g",
          description: "",
          entry: "x",
          inputs_schema: {},
          agents_referenced: [],
          subagents: [{ name: "sa-a", description: "d" }],
        }),
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-g/agents"),
        resp: async () => [],
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-g/tree"),
        ok: !opts?.treeFail,
        status: opts?.treeFail ? 500 : 200,
        resp: async () => ({ workflow: "wf-g", root: "/r", nodes: _WF_TREE_NODES }),
      },
    ]);
  }

  it("批G-1：openWorkflow 成功 → subagents 填充 + wf 树自动加载 + treeScope=workflow", async () => {
    mockOpenWorkflowRoutes();
    await useWorkflowBrowseStore.getState().openWorkflow("wf-g");
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeWorkflow?.subagents).toEqual([{ name: "sa-a", description: "d" }]);
    expect(s.treeScope).toBe("workflow");
    // wf 级树自动加载：落地即见 workflow.yaml（全部资产默认态）
    expect(s.fileTree?.[0]?.path).toBe("workflow.yaml");
    expect(s.treeLoading).toBe(false);
    expect(s.treeError).toBeNull();
  });

  it("批G-2：openWorkflow 树请求失败 → treeError 写入，activeWorkflow 照常（fail-soft 分层）", async () => {
    const f = mockOpenWorkflowRoutes({ treeFail: true });
    // 500 时 mockFetchRoutes 的 resp 返回 wf 树 body，但 ok:false → fetchJsonOrThrow
    // 走 error 分支读 detail（undefined → 空串）。
    await useWorkflowBrowseStore.getState().openWorkflow("wf-g");
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeWorkflow?.meta.name).toBe("wf-g");
    expect(s.activeWorkflow?.subagents).toEqual([{ name: "sa-a", description: "d" }]);
    expect(s.fileTree).toBeNull();
    expect(s.treeError).toMatch(/HTTP 500/);
    expect(s.treeLoading).toBe(false);
    // wf 树请求确实发出过（fail-soft 路径真实走到，而非路由缺失兜底）
    const urls = f.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.endsWith("/api/workflows/wf-g/tree"))).toBe(true);
  });

  it("批G-3：openAgent → treeScope=agent；随后 openFile 走 /agents/<a>/file URL", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [],
      },
    });
    const f = mockFetchRoutes([
      {
        match: (u) => u.includes("/agents/foo/tree"),
        resp: async () => ({ agent: "foo", root: "/foo", nodes: [] }),
      },
      {
        match: (u) => u.includes("/agents/foo/file?path=agent.md"),
        resp: async () => ({ path: "agent.md", text: "t", ext: "md", size: 1, truncated: false }),
      },
    ]);
    await useWorkflowBrowseStore.getState().openAgent("foo");
    expect(useWorkflowBrowseStore.getState().treeScope).toBe("agent");
    await useWorkflowBrowseStore.getState().openFile("agent.md");
    const lastUrl = String(f.mock.calls[f.mock.calls.length - 1]?.[0]);
    expect(lastUrl).toMatch(/\/api\/workflows\/wf\/agents\/foo\/file\?path=agent\.md$/);
    expect(useWorkflowBrowseStore.getState().activeFile?.path).toBe("agent.md");
  });

  it("批G-4：scope=workflow 时 openFile 走 /api/workflows/<wf>/file?path= URL", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [],
      },
      treeScope: "workflow",
    });
    const f = mockFetchRoutes([
      {
        match: (u) => u.includes("/api/workflows/wf/file?path=scripts%2Fs.py"),
        resp: async () => ({ path: "scripts/s.py", text: "x", ext: "py", size: 1, truncated: false }),
      },
    ]);
    await useWorkflowBrowseStore.getState().openFile("scripts/s.py");
    expect(useWorkflowBrowseStore.getState().activeFile?.path).toBe("scripts/s.py");
    const lastUrl = String(f.mock.calls[f.mock.calls.length - 1]?.[0]);
    expect(lastUrl).toMatch(/\/api\/workflows\/wf\/file\?path=scripts%2Fs\.py$/);
  });

  it("批G-5：openSubagent → activeFile.path=subagents/<name>.md；离开 wf scope 时补载 wf 树", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [{ name: "x", description: "" }],
      },
      treeScope: "agent", // 用户看过 agent → openSubagent 须先补载 wf 树
      fileTree: null,
    });
    const f = mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/wf/tree"),
        resp: async () => ({ workflow: "wf", root: "/r", nodes: _WF_TREE_NODES }),
      },
      {
        match: (u) => u.includes("/api/workflows/wf/file?path=subagents%2Fx.md"),
        resp: async () => ({
          path: "subagents/x.md",
          text: "# x",
          ext: "md",
          size: 3,
          truncated: false,
        }),
      },
    ]);
    useWorkflowBrowseStore.getState().openSubagent("x");
    // openSubagent 是 fire-and-forget（补载树 + 打开文件两个并发 async）。
    await new Promise((r) => setTimeout(r, 0));
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeFile?.path).toBe("subagents/x.md");
    expect(s.treeScope).toBe("workflow");
    const urls = f.mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.endsWith("/api/workflows/wf/tree"))).toBe(true);
  });

  it("批G-6：openWorkflow 入口同步清空 + treeScope 复位 workflow（m5 扩展）", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "stale", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [],
      },
      activeAgent: "stale-agent",
      treeScope: "agent",
      fileTree: [
        { path: "stale.py", name: "stale.py", is_dir: false, size: 10, children: null },
      ],
    });
    let resolveDetail!: () => void;
    const delay = new Promise<void>((r) => {
      resolveDetail = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/new-wf"),
        resp: async () => {
          await delay;
          return {
            name: "new-wf",
            description: "",
            entry: "x",
            inputs_schema: {},
            agents_referenced: [],
            subagents: [],
          };
        },
      },
      {
        match: (u) => u.endsWith("/api/workflows/new-wf/agents"),
        resp: async () => [],
      },
    ]);
    const p = useWorkflowBrowseStore.getState().openWorkflow("new-wf");
    // 同步断言：fetch pending 时已清空 + treeScope 复位（agent 视图残留清除）。
    expect(useWorkflowBrowseStore.getState().treeScope).toBe("workflow");
    expect(useWorkflowBrowseStore.getState().activeAgent).toBeNull();
    expect(useWorkflowBrowseStore.getState().fileTree).toBeNull();
    resolveDetail();
    await p;
    expect(useWorkflowBrowseStore.getState().activeWorkflow?.meta.name).toBe("new-wf");
  });

  it("批G-7：reset 清 treeScope（agent → workflow）", async () => {
    useWorkflowBrowseStore.setState({ treeScope: "agent" });
    useWorkflowBrowseStore.getState().reset();
    expect(useWorkflowBrowseStore.getState().treeScope).toBe("workflow");
  });

  it("批G-8：openWorkflow 树响应晚于用户 openAgent → 不覆盖 agent 树（慢到守卫）", async () => {
    let resolveTree!: () => void;
    const delayTree = new Promise<void>((r) => {
      resolveTree = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.endsWith("/api/workflows/wf-s"),
        resp: async () => ({
          name: "wf-s",
          description: "",
          entry: "x",
          inputs_schema: {},
          agents_referenced: [],
          subagents: [],
        }),
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-s/agents"),
        resp: async () => [],
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf-s/tree"),
        resp: async () => {
          await delayTree; // wf 树响应慢到
          return { workflow: "wf-s", root: "/r", nodes: _WF_TREE_NODES };
        },
      },
      {
        match: (u) => u.includes("/agents/foo/tree"),
        resp: async () => ({
          agent: "foo",
          root: "/foo",
          nodes: [
            { path: "agent.md", name: "agent.md", is_dir: false, size: 5, children: null },
          ],
        }),
      },
    ]);
    const p = useWorkflowBrowseStore.getState().openWorkflow("wf-s");
    // 等 detail/agents（立即 resolve）完成、树仍 pending。
    await new Promise((r) => setTimeout(r, 0));
    expect(useWorkflowBrowseStore.getState().activeWorkflow?.meta.name).toBe("wf-s");
    expect(useWorkflowBrowseStore.getState().treeLoading).toBe(true);
    // 用户在 wf 树慢到期间点了 agent → treeScope 切 "agent"、agent 树写入。
    await useWorkflowBrowseStore.getState().openAgent("foo");
    expect(useWorkflowBrowseStore.getState().fileTree?.[0]?.path).toBe("agent.md");
    // 慢到的 wf 树响应到达——treeScope 守卫应丢弃，不覆盖 agent 树。
    resolveTree();
    await p;
    const s = useWorkflowBrowseStore.getState();
    expect(
      s.fileTree?.[0]?.path,
      "慢到的 wf 树不应覆盖 agent 树（treeScope 双守卫）",
    ).toBe("agent.md");
    expect(s.treeScope).toBe("agent");
  });

  it("批G-9：openAgent 树响应晚于 openWorkflowTree → 不覆盖 wf 树（对称守卫）+ activeAgent 清空", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [],
      },
    });
    let resolveAgentTree!: () => void;
    const delayAgentTree = new Promise<void>((r) => {
      resolveAgentTree = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.includes("/agents/foo/tree"),
        resp: async () => {
          await delayAgentTree; // agent 树响应慢到
          return {
            agent: "foo",
            root: "/foo",
            nodes: [
              { path: "agent.md", name: "agent.md", is_dir: false, size: 5, children: null },
            ],
          };
        },
      },
      {
        match: (u) => u.endsWith("/api/workflows/wf/tree"),
        resp: async () => ({ workflow: "wf", root: "/r", nodes: _WF_TREE_NODES }),
      },
    ]);
    // 用户点 agent（树慢到）→ 立刻点「全部资产」（wf 树立即写入）。
    const pAgent = useWorkflowBrowseStore.getState().openAgent("foo");
    expect(useWorkflowBrowseStore.getState().activeAgent).toBe("foo");
    const pAll = useWorkflowBrowseStore.getState().openWorkflowTree();
    await pAll;
    // openWorkflowTree 清 activeAgent（scope 切回 workflow，聚焦 agent 语义失效）。
    expect(useWorkflowBrowseStore.getState().activeAgent).toBeNull();
    expect(useWorkflowBrowseStore.getState().fileTree?.[0]?.path).toBe("workflow.yaml");
    // 慢到的 agent 树响应到达——对称守卫应丢弃，不覆盖 wf 全资产树。
    resolveAgentTree();
    await pAgent;
    expect(
      useWorkflowBrowseStore.getState().fileTree?.[0]?.path,
      "慢到的 agent 树不应覆盖 wf 全资产树（对称守卫）",
    ).toBe("workflow.yaml");
    expect(useWorkflowBrowseStore.getState().treeScope).toBe("workflow");
  });

  it("批G-10：openFile 响应晚于 scope 切换 → 旧 scope 文件不复活（scope 快照守卫）", async () => {
    useWorkflowBrowseStore.setState({
      activeWorkflow: {
        meta: { name: "wf", description: "", entry: "x", inputs_schema: {} },
        agents_referenced: [],
        all_agents: [],
        subagents: [{ name: "x", description: "" }],
      },
      treeScope: "workflow",
      fileTree: _WF_TREE_NODES,
    });
    let resolveFile!: () => void;
    const delayFile = new Promise<void>((r) => {
      resolveFile = r;
    });
    mockFetchRoutes([
      {
        match: (u) => u.includes("/api/workflows/wf/file?path=subagents%2Fx.md"),
        resp: async () => {
          await delayFile; // subagent 文件响应慢到
          return { path: "subagents/x.md", text: "# x", ext: "md", size: 3, truncated: false };
        },
      },
      {
        match: (u) => u.includes("/agents/foo/tree"),
        resp: async () => ({ agent: "foo", root: "/foo", nodes: [] }),
      },
    ]);
    // 用户点 subagent（文件慢到）→ 快速点 agent 行（scope 切 "agent"）。
    useWorkflowBrowseStore.getState().openSubagent("x"); // fire-and-forget
    await new Promise((r) => setTimeout(r, 0)); // 让 openFile 的 fetch 发出（挂起 delayFile）
    await useWorkflowBrowseStore.getState().openAgent("foo");
    expect(useWorkflowBrowseStore.getState().treeScope).toBe("agent");
    expect(useWorkflowBrowseStore.getState().activeFile).toBeNull();
    // 慢到的 subagent 文件响应到达——scope 快照守卫应丢弃（不在 agent scope 下复活）。
    resolveFile();
    await new Promise((r) => setTimeout(r, 0)); // 等响应链走完守卫判定
    const s = useWorkflowBrowseStore.getState();
    expect(s.activeFile, "旧 scope 的文件不应在新 scope 下复活").toBeNull();
    expect(s.fileLoading).toBe(false);
  });
});
