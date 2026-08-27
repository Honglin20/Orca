// test/workflow-browse-page.test.tsx —— WorkflowBrowsePage 组件测试（批 G §5.3）。
//
// 断言意图（Rule 9）：
//   - detail meta + Subagents 区渲染（subagent-row-* + 描述截断）
//   - 中栏自动渲染 wf 级全资产树（tree-file-workflow.yaml——落地即见全部资产）
//   - 点 subagent 行 → 右栏 file-markdown（MarkdownText 渲染 subagents/<name>.md）
//   - subagents 空 → subagent-list-empty
//   - 点 agent 行 → 中栏切 agent 树（header Files · <agent>）
//   - AC-16 视觉禁令：无 bg-slate-* / rounded-lg / text-[10|11|13px] / 裸 shadow
//
// 范式照 workflows-page.test.tsx：MemoryRouter + mock fetch + findByTestId。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { WorkflowBrowsePage } from "@/components/pages/WorkflowBrowsePage";
import { useWorkflowBrowseStore } from "@/stores/workflow-browse-store";

const WF_NAME = "mywf";

const DETAIL_BODY = {
  name: WF_NAME,
  description: "demo workflow",
  entry: "a",
  inputs_schema: {},
  agents_referenced: ["foo"],
  subagents: [
    { name: "sa-one", description: "第一个子代理" },
    { name: "sa-two", description: "" },
  ],
};

const AGENTS_BODY = [
  { name: "foo", is_folder: true, description: "foo agent", missing: false },
];

function wfTreeNode(
  path: string,
  isDir: boolean,
  children: Array<Record<string, unknown>> | null = null,
) {
  return {
    path,
    name: path.split("/").pop() ?? path,
    is_dir: isDir,
    size: isDir ? 0 : path.length,
    children,
  };
}

const WF_TREE_BODY = {
  workflow: WF_NAME,
  root: "/workflows/mywf",
  nodes: [
    wfTreeNode("agents", true, [wfTreeNode("agents/foo", true, [
      wfTreeNode("agents/foo/agent.md", false),
    ])]),
    wfTreeNode("scripts", true, [wfTreeNode("scripts/s.py", false)]),
    wfTreeNode("subagents", true, [wfTreeNode("subagents/sa-one.md", false)]),
    wfTreeNode("workflow.yaml", false),
  ],
};

const AGENT_TREE_BODY = {
  agent: "foo",
  root: "/workflows/mywf/agents/foo",
  nodes: [wfTreeNode("agent.md", false)],
};

const SUBAGENT_FILE_BODY = {
  path: "subagents/sa-one.md",
  text: "# sa-one\n\nbody",
  ext: "md",
  size: 14,
  truncated: false,
};

interface MockOpts {
  detail?: Record<string, unknown>;
}

function mockFetchFor(opts: MockOpts = {}) {
  const f = vi.fn(async (url: string | URL | Request) => {
    const u = typeof url === "string" ? url : url.toString();
    const detail = { ...DETAIL_BODY, ...(opts.detail ?? {}) };
    if (u.endsWith(`/api/workflows/${WF_NAME}/agents/foo/tree`)) {
      return { ok: true, status: 200, json: async () => AGENT_TREE_BODY } as Response;
    }
    if (u.endsWith(`/api/workflows/${WF_NAME}/file?path=subagents%2Fsa-one.md`)) {
      return { ok: true, status: 200, json: async () => SUBAGENT_FILE_BODY } as Response;
    }
    if (u.endsWith(`/api/workflows/${WF_NAME}/tree`)) {
      return { ok: true, status: 200, json: async () => WF_TREE_BODY } as Response;
    }
    if (u.endsWith(`/api/workflows/${WF_NAME}/agents`)) {
      return { ok: true, status: 200, json: async () => AGENTS_BODY } as Response;
    }
    if (u.endsWith(`/api/workflows/${WF_NAME}`)) {
      return { ok: true, status: 200, json: async () => detail } as Response;
    }
    return { ok: false, status: 404, json: async () => ({ detail: "nf" }) } as Response;
  });
  vi.stubGlobal("fetch", f as unknown as typeof fetch);
  return f;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={[`/workflows/${WF_NAME}`]}>
      <Routes>
        <Route path="/workflows/:name" element={<WorkflowBrowsePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkflowBrowsePage（批 G 全资产浏览）", () => {
  beforeEach(() => {
    useWorkflowBrowseStore.getState().reset();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("渲染 detail meta + Subagents 区（subagent-row-* 行 + 描述）", async () => {
    mockFetchFor();
    renderPage();
    // detail meta
    expect(await screen.findByTestId("workflow-meta")).toBeTruthy();
    expect(screen.getByTestId("workflow-meta").textContent).toMatch(/mywf/);
    // Subagents 区：两行 + 描述渲染
    expect(await screen.findByTestId("subagent-row-sa-one")).toBeTruthy();
    expect(screen.getByTestId("subagent-row-sa-one").textContent).toMatch(
      /第一个子代理/,
    );
    expect(screen.getByTestId("subagent-row-sa-two").textContent).toMatch(
      /sa-two/,
    );
  });

  it("中栏自动渲染 wf 级 file-tree（tree-file-workflow.yaml 存在）", async () => {
    mockFetchFor();
    renderPage();
    // openWorkflow 成功后自动加载 wf 树（不点任何 agent）
    expect(await screen.findByTestId("tree-file-workflow.yaml")).toBeTruthy();
    // 树里全资产子目录可见
    expect(screen.getByTestId("tree-dir-agents")).toBeTruthy();
    expect(screen.getByTestId("tree-dir-subagents")).toBeTruthy();
    expect(screen.getByTestId("tree-dir-scripts")).toBeTruthy();
  });

  it("点 subagent 行 → 右栏 file-markdown 渲染（MarkdownText）", async () => {
    mockFetchFor();
    renderPage();
    const row = await screen.findByTestId("subagent-row-sa-one");
    await act(async () => {
      fireEvent.click(row);
    });
    // lazy MarkdownText chunk 加载后渲染（ext=md → file-markdown testid）。
    // 外层 div 立即出现（内含 Suspense fallback），waitFor 等 markdown 实际内容到位
    // （左栏行名同名，须限定在 file-markdown 容器内断言）。
    expect(await screen.findByTestId("file-markdown")).toBeTruthy();
    await waitFor(() => {
      expect(screen.getByTestId("file-markdown").textContent).toMatch(/sa-one/);
    });
  });

  it("subagents 空 → subagent-list-empty 文案", async () => {
    mockFetchFor({ detail: { subagents: [] } });
    renderPage();
    expect(await screen.findByTestId("subagent-list-empty")).toBeTruthy();
    expect(screen.getByTestId("subagent-list-empty").textContent).toMatch(
      /该 workflow 无 subagents/,
    );
  });

  it("点 agent 行 → 中栏切 agent 树（header Files · foo + fetch agent tree）", async () => {
    const f = mockFetchFor();
    renderPage();
    // 等 wf 树落地（默认态）再切 agent
    await screen.findByTestId("tree-file-workflow.yaml");
    const row = await screen.findByTestId("agent-row-foo");
    await act(async () => {
      fireEvent.click(row);
    });
    // 中栏 header 切 agent 视图 + agent 树 fetch 发生
    expect(await screen.findByTestId("tree-file-agent.md")).toBeTruthy();
    expect(screen.queryByTestId("tree-file-workflow.yaml")).toBeNull();
    expect(screen.getByText("Files · foo")).toBeTruthy();
    const urls = f.mock.calls.map((c) => String(c[0]));
    expect(
      urls.some((u) => u.endsWith(`/api/workflows/${WF_NAME}/agents/foo/tree`)),
    ).toBe(true);
  });

  // ── AC-16 视觉禁令（grep 守门，照 workflows-page.test.tsx 逐字）──
  it("AC-16 渲染产物 HTML 不含 bg-slate-* / rounded-lg|xl|2xl / text-[10|11|13px] / 裸 shadow", async () => {
    mockFetchFor();
    const { container } = renderPage();
    await screen.findByTestId("tree-file-workflow.yaml");
    await screen.findByTestId("subagent-row-sa-one");
    const html = container.innerHTML;
    expect(html).not.toMatch(/bg-slate-/);
    expect(html).not.toMatch(/rounded-lg\b/);
    expect(html).not.toMatch(/rounded-xl\b/);
    expect(html).not.toMatch(/rounded-2xl\b/);
    expect(html).not.toMatch(/text-\[10px\]/);
    expect(html).not.toMatch(/text-\[11px\]/);
    expect(html).not.toMatch(/text-\[13px\]/);
    expect(html).not.toMatch(/shadow(?![-a-z])/);
  });
});
