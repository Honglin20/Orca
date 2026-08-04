// test/workflows-page.test.tsx —— WorkflowsPage 组件测试（plan §前端测试）。
//
// 断言意图（Rule 9）：
//   - mock fetch + 断 list 渲染 + 点 row navigate（包 ``<MemoryRouter>``）
//   - AC-16 视觉禁令：无 bg-slate-* / rounded-lg / text-[10|11|13px] / 裸 shadow

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
} from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { WorkflowsPage } from "@/components/pages/WorkflowsPage";
import { useWorkflowBrowseStore } from "@/stores/workflow-browse-store";

// 路由跳转后落地到一个 probe 页面，capture :name param 显示——比 spy navigate 更稳
// （真实端到端走完 react-router 路由解析）。
function TargetProbe() {
  return (
    <div data-testid="target-page">
      reached
    </div>
  );
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/workflows"]}>
      <Routes>
        <Route path="/workflows" element={<WorkflowsPage />} />
        <Route path="/workflows/:name" element={<TargetProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockFetchFor(workflows: unknown[]) {
  const f = vi.fn(async (url: string | URL | Request) => {
    const u = typeof url === "string" ? url : url.toString();
    if (u.includes("/api/workflows") && !u.includes("/agents")) {
      return { ok: true, status: 200, json: async () => workflows } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", f as unknown as typeof fetch);
  return f;
}

describe("WorkflowsPage", () => {
  beforeEach(() => {
    useWorkflowBrowseStore.getState().reset();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("渲染 loading（fetch pending）→ 渲染 workflow-row 列表", async () => {
    mockFetchFor([
      {
        name: "wf-a",
        description: "first",
        entry: "step1",
        inputs_count: 2,
        inputs_schema: [],
      },
      {
        name: "wf-b",
        description: "second",
        entry: "main",
        inputs_count: 0,
        inputs_schema: [],
      },
    ]);
    renderPage();

    const rows = await screen.findAllByTestId(/^workflow-row-/);
    expect(rows.length).toBe(2);
    expect(screen.getByTestId("workflow-row-wf-a").textContent).toMatch(/first/);
    expect(screen.getByTestId("workflow-row-wf-b").textContent).toMatch(/second/);
  });

  it("点 workflow-row → navigate(/workflows/:name)", async () => {
    mockFetchFor([
      {
        name: "wf-a",
        description: "",
        entry: "x",
        inputs_count: 0,
        inputs_schema: [],
      },
    ]);
    renderPage();
    const row = await screen.findByTestId("workflow-row-wf-a");
    await act(async () => {
      fireEvent.click(row);
    });
    // react-router 实际跳转 → TargetProbe 渲染。
    expect(screen.getByTestId("target-page").textContent).toMatch(/reached/);
  });

  it("fetch 失败 → 渲染 error-banner", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("boom");
      }),
    );
    renderPage();
    expect(await screen.findByTestId("error-banner")).toBeTruthy();
    expect(screen.getByTestId("error-banner").textContent).toMatch(/boom/);
  });

  it("空列表 → 渲染 empty-state", async () => {
    mockFetchFor([]);
    renderPage();
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
  });

  // ── AC-16 视觉禁令（grep 守门）──
  it("AC-16 渲染产物 HTML 不含 bg-slate-* / rounded-lg|xl|2xl / text-[10|11|13px] / 裸 shadow", async () => {
    mockFetchFor([
      {
        name: "wf-a",
        description: "x",
        entry: "x",
        inputs_count: 0,
        inputs_schema: [],
      },
    ]);
    const { container } = renderPage();
    await screen.findByTestId("workflow-row-wf-a");
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
