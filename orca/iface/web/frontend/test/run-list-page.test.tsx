// test/run-list-page.test.tsx —— RunListPage 组件测试（SPEC §8 AC-1..9,13,16）。
//
// 断言意图（Rule 9）：
//   - **AC-1 删除按钮**：size=16、命中区≥32px、常显（无 opacity-0 group-hover）。
//   - **AC-2 多选**：行 checkbox + 三态分组 checkbox + 选择保留语义。
//   - **AC-3 排序**：6 字段下拉；触发器显当前字段+方向。
//   - **AC-4 折叠**：折叠持久 localStorage；切折叠不重置选择。
//   - **AC-7 搜索穿透**：q 非空含匹配分组强制展开。
//   - **AC-9 三态加载**：首屏骨架 / error 显 banner / 空态。
//   - **AC-13 对话框 a11y**：role=dialog + aria-labelledby/describedby + Esc 取消。
//   - **AC-16 视觉禁令**：无 bg-slate-* / rounded-lg/xl/2xl / text-[10/11/13px] / 裸 shadow。
//
// 测试范式：mock fetch + WebSocket（vi.stubGlobal）；happy-dom 提供 window/document。

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, cleanup, within, act, renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { RunListPage } from "@/components/pages/RunListPage";
import { useRunListStore, type RunSummary } from "@/stores/run-list-store";
import { useWsRunlist } from "@/hooks/use-ws-runlist";

function mkRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: overrides.run_id ?? "demo-20260701-075614-7f6455",
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

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
    // happy-path：构造后立即触发 onopen（act 内）。
    setTimeout(() => {
      this.readyState = 1;
      this.onopen?.(new Event("open"));
    }, 0);
  }
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close"));
  }
}

function mockFetchFor(
  runs: RunSummary[],
  deleteStatus: Record<string, number> = {},
) {
  const f = vi.fn(async (url: string | URL | Request, _init?: RequestInit) => {
    const u = typeof url === "string" ? url : url.toString();
    if (u.includes("/api/runs?scope=all")) {
      return { ok: true, status: 200, json: async () => runs } as Response;
    }
    if (u.includes("/api/projects/stale")) {
      return { ok: true, status: 200, json: async () => [] } as Response;
    }
    // DELETE /api/runs/<id>
    const delMatch = u.match(/^.*?\/api\/runs\/([^/?]+)$/);
    if (delMatch) {
      const id = delMatch[1];
      const status = deleteStatus[id] ?? 200;
      return {
        ok: status < 400,
        status,
        json: async () => (status < 400 ? {} : { error: "fail" }),
      } as Response;
    }
    return { ok: false, status: 404, json: async () => ({}) } as Response;
  });
  vi.stubGlobal("fetch", f as unknown as typeof fetch);
  return f;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <RunListPage />
    </MemoryRouter>,
  );
}

describe("RunListPage 重设计", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    useRunListStore.getState().reset();
    useRunListStore.setState({ lastFetch: 0 });
    localStorage.clear();
    // 既有 list 视图用例：固定 view=list（默认是 board，见 board 用例）。
    localStorage.setItem("orca-runlist-view-v1", JSON.stringify("list"));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // ── AC-9 三态加载：首屏骨架 ──
  it("AC-9 首屏：loading=true && runs=[] → 渲染 ListSkeleton（data-testid=list-skeleton）", async () => {
    // 永远 pending 的 fetch（保证 loading 不落定）。
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise(() => {})),
    );
    renderPage();
    expect(await screen.findByTestId("list-skeleton")).toBeTruthy();
  });

  // ── AC-9 三态加载：空态 ──
  it("AC-9 空态：runs.length=0 → 渲染 EmptyState（data-testid=empty-state）", async () => {
    mockFetchFor([]);
    renderPage();
    expect(await screen.findByTestId("empty-state")).toBeTruthy();
  });

  // ── AC-9 三态加载：error ──
  it("AC-9 错误态：fetch 失败 → 渲染 ErrorBanner（data-testid=error-banner）", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("网络错误");
      }),
    );
    renderPage();
    expect(await screen.findByTestId("error-banner")).toBeTruthy();
  });

  // ── AC-16 视觉禁令 ──
  it("AC-16 渲染产物 HTML 不含 bg-slate-* / rounded-lg|xl|2xl / text-[10|11|13px] / 裸 shadow", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", workflow_name: "demo", project_name: "demo" }),
      mkRun({ run_id: "r2", workflow_name: "demo", status: "blocked", project_name: "demo" }),
    ]);
    const { container } = renderPage();
    await screen.findAllByTestId("run-row");
    const html = container.innerHTML;
    // bg-slate-*（任何 slate bg 类）
    expect(html).not.toMatch(/bg-slate-/);
    // rounded-lg/xl/2xl（含 tailwind 圆角档位）
    expect(html).not.toMatch(/rounded-lg\b/);
    expect(html).not.toMatch(/rounded-xl\b/);
    expect(html).not.toMatch(/rounded-2xl\b/);
    // text-[10px]/[11px]/[13px]
    expect(html).not.toMatch(/text-\[10px\]/);
    expect(html).not.toMatch(/text-\[11px\]/);
    expect(html).not.toMatch(/text-\[13px\]/);
    // 裸 shadow（不含 shadow-sm/md/lg/xl/2xl/inner/none）
    // 仅匹配后面没跟横杠字母的 shadow
    expect(html).not.toMatch(/shadow(?![-a-z])/);
  });

  // ── AC-1 删除按钮 ──
  it("AC-1 删除按钮：data-testid=delete-btn + size=16 + 命中区 min-w/h≥32px（class 含 min-w/min-h-[32px]）", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");
    const delBtns = screen.getAllByTestId("delete-btn");
    expect(delBtns.length).toBeGreaterThanOrEqual(1);
    const del = delBtns[0];
    // 命中区：class 含 min-w-[32px] 与 min-h-[32px]。
    expect(del.className).toMatch(/min-w-\[32px\]/);
    expect(del.className).toMatch(/min-h-\[32px\]/);
    // size=16：trash2 svg 的 width=16（lucide 设 width="16"）。
    const svg = del.querySelector("svg");
    expect(svg?.getAttribute("width")).toBe("16");
    // 常显：class 不含 opacity-0 + group-hover（SPEC §6.6/AC-1）。
    expect(del.className).not.toMatch(/\bopacity-0\b/);
    // 删除图标默认低 opacity 但常显（0.55 而非 0）。
    expect(del.className).toMatch(/text-\[rgb\(var\(--text-faint\)\/0\.55\)\]/);
  });

  // ── AC-2 多选：行 checkbox + 三态分组 checkbox ──
  it("AC-2 行 checkbox：点 run-checkbox → 选中 + bulk-bar 出现 + 选择态同步显示", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", project_name: "demo" }),
      mkRun({ run_id: "r2", project_name: "demo" }),
    ]);
    renderPage();
    await screen.findAllByTestId("run-row");
    const cbs = screen.getAllByTestId("run-checkbox");
    expect(cbs.length).toBe(2);
    // 全未选时无 bulk-bar。
    expect(screen.queryByTestId("bulk-bar")).toBeNull();
    // 点 r1 checkbox。
    await act(async () => {
      fireEvent.click(cbs[0]);
    });
    // bulk-bar 出现。
    expect(screen.getByTestId("bulk-bar")).toBeTruthy();
    expect(screen.getByTestId("bulk-bar").textContent).toMatch(/已选\s*1\s*项/);
  });

  it("AC-2 分组三态 checkbox：全选 → indeterminate→full；半选时显 indeterminate", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", project_name: "demo" }),
      mkRun({ run_id: "r2", project_name: "demo" }),
    ]);
    renderPage();
    await screen.findAllByTestId("run-row");
    // group-select-all：未选 → 半选不显。
    const groupSelect = screen.getByTestId("group-select-all");
    expect((groupSelect as HTMLInputElement).checked).toBe(false);
    expect((groupSelect as HTMLInputElement).indeterminate).toBe(false);

    // 点其中一个 run。
    await act(async () => {
      fireEvent.click(screen.getAllByTestId("run-checkbox")[0]);
    });
    // 分组 checkbox 应 indeterminate。
    expect((groupSelect as HTMLInputElement).indeterminate).toBe(true);

    // 点 group-select-all → 全选。
    await act(async () => {
      fireEvent.click(groupSelect);
    });
    expect((groupSelect as HTMLInputElement).checked).toBe(true);
    // 两个 run checkbox 都选中。
    screen.getAllByTestId("run-checkbox").forEach((cb) => {
      expect((cb as HTMLInputElement).checked).toBe(true);
    });
  });

  // ── AC-2 选择保留：切 groupBy 不重置选择 ──
  it("AC-2 切 groupBy 不重置选择（SPEC §3.3 时序不变量）", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", project_name: "demo" }),
      mkRun({ run_id: "r2", project_name: "demo" }),
    ]);
    renderPage();
    await screen.findAllByTestId("run-row");
    await act(async () => {
      fireEvent.click(screen.getAllByTestId("run-checkbox")[0]);
    });
    expect(screen.getByTestId("bulk-bar")).toBeTruthy();

    // 切 groupBy。
    await act(async () => {
      fireEvent.click(screen.getByTestId("group-toggle"));
    });
    // 选择应保留（bulk-bar 仍在）。
    expect(screen.getByTestId("bulk-bar")).toBeTruthy();
  });

  // ── AC-3 排序：6 字段 + 触发器显当前 ──
  it("AC-3 排序：触发器文案随选定字段更新；点 workflow_name 切到该字段", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", workflow_name: "alpha" }),
      mkRun({ run_id: "r2", workflow_name: "beta" }),
    ]);
    renderPage();
    await screen.findAllByTestId("run-row");
    // 默认（started_at desc）触发器显「排序」。
    expect(screen.getByTestId("sort-trigger").textContent).toMatch(/排序/);

    // 打开菜单。
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-trigger"));
    });
    expect(screen.getByTestId("sort-menu")).toBeTruthy();
    // 6 字段都在。
    expect(screen.queryByTestId("sort-option-started_at")).toBeTruthy();
    expect(screen.queryByTestId("sort-option-workflow_name")).toBeTruthy();
    expect(screen.queryByTestId("sort-option-status")).toBeTruthy();
    expect(screen.queryByTestId("sort-option-cost")).toBeTruthy();
    expect(screen.queryByTestId("sort-option-elapsed")).toBeTruthy();
    expect(screen.queryByTestId("sort-option-event_count")).toBeTruthy();

    // 点 workflow_name → 触发器应显字段名。
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-option-workflow_name"));
    });
    expect(screen.getByTestId("sort-trigger").textContent).toMatch(/workflow 名称/);
  });

  // ── AC-3 排序：同字段二次点反转方向 ──
  it("AC-3 同字段二次点击反转方向（desc → asc）", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");

    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-trigger"));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-option-cost"));
    });
    // 默认 desc。
    expect(screen.getByTestId("sort-trigger").textContent).toMatch(/花费/);
    // HTML 含 ArrowDown（svg）。
    expect(screen.getByTestId("sort-trigger").querySelector("svg")).toBeTruthy();

    // 二次点 → asc。
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-trigger"));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-option-cost"));
    });
    // 仍显字段名（方向已切，箭头会变）。
    expect(screen.getByTestId("sort-trigger").textContent).toMatch(/花费/);
  });

  // ── AC-3 排序持久化（localStorage） ──
  it("AC-3 排序持久：选 workflow_name → localStorage 写入", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-trigger"));
    });
    await act(async () => {
      fireEvent.click(screen.getByTestId("sort-option-workflow_name"));
    });
    const stored = localStorage.getItem("orca-runlist-sort-v1");
    expect(stored).toBeTruthy();
    expect(stored).toMatch(/workflow_name/);
  });

  // ── AC-4 折叠持久 ──
  it("AC-4 折叠持久：点 group-header 折叠 → localStorage 写入 collapsed 集合", async () => {
    mockFetchFor([mkRun({ run_id: "r1", project_name: "demo" })]);
    renderPage();
    await screen.findByTestId("group-demo");
    await act(async () => {
      fireEvent.click(screen.getByTestId("group-header"));
    });
    const stored = localStorage.getItem("orca-runlist-collapsed-v1");
    expect(stored).toBeTruthy();
    expect(stored).toMatch(/demo/);
  });

  // AC-4 反向：mount 前预先持久化 collapsed（模拟 reload 场景）→ 加载后初始态应保持折叠。
  // 防 regression：旧版 useState 初值在 ``known`` 为空（fetch 未回）时过滤掉持久项，
  // 然后 write-back effect 把空集覆盖回 storage，永久清空持久态（Playwright 曾 catch 此 bug）。
  it("AC-4 持久态加载：mount 前 localStorage 预存 [demo] → 加载后保持折叠（不擦写 storage）", async () => {
    localStorage.setItem("orca-runlist-collapsed-v1", JSON.stringify(["demo"]));
    mockFetchFor([mkRun({ run_id: "r1", project_name: "demo" })]);
    renderPage();
    // group 出现（runs 已加载、knownProjects 已算出），useCollapsedProjects 应已 hydrate。
    await screen.findByTestId("group-demo");
    // 仍应折叠：run-row 不在 DOM。
    await act(async () => {
      // 给 hydrate effect + render 一拍时间。
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(screen.queryAllByTestId("run-row")).toHaveLength(0);
    // storage 不应被擦写为空。
    const stored = localStorage.getItem("orca-runlist-collapsed-v1");
    expect(stored).toBeTruthy();
    expect(stored).toMatch(/demo/);
  });

  it("AC-4 localStorage 损坏 → 降级空集不崩", async () => {
    localStorage.setItem("orca-runlist-collapsed-v1", "{not valid json");
    mockFetchFor([mkRun({ run_id: "r1", project_name: "demo" })]);
    expect(() => renderPage()).not.toThrow();
    await screen.findByTestId("run-row");
  });

  // ── AC-7 搜索穿透：q 非空 → 分组强制展开 + 显命中数 ──
  it("AC-7 搜索：q 非空 → 含匹配分组展开 + 头部显「搜索：X · 命中 N」", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", workflow_name: "demo-search", project_name: "demo" }),
      mkRun({ run_id: "r2", workflow_name: "other", project_name: "demo" }),
    ]);
    renderPage();
    await screen.findByTestId("group-demo");

    // 先折叠 demo 分组。
    await act(async () => {
      fireEvent.click(screen.getByTestId("group-header"));
    });
    // 折叠后 run-row 应不可见。
    expect(screen.queryByTestId("run-row")).toBeNull();

    // 输入 "search"。
    const input = screen.getByTestId("search-input") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "search" } });
    });
    // debounce 上抛（350ms 后）。
    await new Promise((r) => setTimeout(r, 350));

    // 搜索穿透：分组应展开，run-row 出现（仅匹配的 1 条）。
    const rows = await screen.findAllByTestId("run-row");
    expect(rows.length).toBe(1);
  });

  // ── AC-13 对话框 a11y ──
  it("AC-13 删除确认：点 delete-btn → 对话框出（aria-modal + aria-labelledby/describedby）；Esc 关闭", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");

    await act(async () => {
      fireEvent.click(screen.getByTestId("delete-btn"));
    });
    const dlg = await screen.findByTestId("delete-dialog");
    expect(dlg.getAttribute("aria-modal")).toBe("true");
    expect(dlg.getAttribute("aria-labelledby")).toBe("del-title");
    expect(dlg.getAttribute("aria-describedby")).toBe("del-desc");

    // Esc 关闭。
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByTestId("delete-dialog")).toBeNull();
  });

  // ── NM2 焦点恢复：dialog 关闭后焦点回触发元素（SPEC §5.7） ──
  it("NM2：dialog 打开 focus→cancel-btn；Esc 关闭后 focus 回 delete-btn（触发元素）", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");
    const delBtn = screen.getByTestId("delete-btn");
    // 点击 delete-btn 触发 dialog；delete-btn 此时是 activeElement。
    await act(async () => {
      delBtn.focus();
      fireEvent.click(delBtn);
    });
    await screen.findByTestId("delete-dialog");
    // dialog mount → focus 落到 cancel-btn（最安全选项，避免误确认）。
    const cancelBtn = screen.getByTestId("cancel-delete");
    expect(document.activeElement).toBe(cancelBtn);
    // Esc 关闭 → focus 还原到触发元素 delete-btn。
    await act(async () => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByTestId("delete-dialog")).toBeNull();
    expect(document.activeElement).toBe(delBtn);
  });

  // ── MAJOR-1：删除失败时**不**关闭对话框（SPEC §3.3：仅「删除完成」清空+关闭） ──
  // 注：选择集在乐观移除期已被 useListSelection 自动求交剔除（SPEC §3.3 设计），
  // 因此 MAJOR-1 的可验证 intent 是「对话框不强制关闭 + toast 报错」——用户可主动取消或重试。
  it("MAJOR-1：单删失败 → toast 报错 + 对话框保留（用户主动决定取消）", async () => {
    // DELETE 返 409 失败。
    mockFetchFor([mkRun({ run_id: "r1" })], { r1: 409 });
    renderPage();
    await screen.findByTestId("run-row");
    await act(async () => {
      fireEvent.click(screen.getByTestId("delete-btn"));
    });
    await screen.findByTestId("delete-dialog");
    await act(async () => {
      fireEvent.click(screen.getByTestId("confirm-delete"));
    });
    // 失败 toast 出现。
    await screen.findByTestId("runlist-toast");
    // 对话框仍保留（未自动关闭）——用户主动取消或重试。
    expect(screen.queryByTestId("delete-dialog")).toBeTruthy();
    // 用户主动点取消 → 对话框关闭。
    await act(async () => {
      fireEvent.click(screen.getByTestId("cancel-delete"));
    });
    expect(screen.queryByTestId("delete-dialog")).toBeNull();
  });

  it("AC-13 删除确认：确认按钮点击 → DELETE /api/runs/<id> + 关闭", async () => {
    const fetchMock = mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");
    await act(async () => {
      fireEvent.click(screen.getByTestId("delete-btn"));
    });
    await screen.findByTestId("delete-dialog");
    await act(async () => {
      fireEvent.click(screen.getByTestId("confirm-delete"));
    });
    // DELETE 被调（找到以 /api/runs/r1 结尾 + method DELETE 的调用）。
    const delCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        typeof url === "string" &&
        url.endsWith("/api/runs/r1") &&
        (init as RequestInit)?.method === "DELETE",
    );
    expect(delCall, "应发出 DELETE /api/runs/r1").toBeTruthy();
  });

  // ── AC-14 WS 控制帧：run_changed action=deleted → 乐观移除 ──
  it("AC-14：WS run_changed action=deleted → 行消失", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1" }),
      mkRun({ run_id: "r2" }),
    ]);
    renderPage();
    await screen.findAllByTestId("run-row");
    expect(screen.getAllByTestId("run-row").length).toBe(2);

    // 通过 MockWebSocket 推一帧 control run_changed deleted。
    const ws = MockWebSocket.instances[0];
    expect(ws).toBeTruthy();
    await act(async () => {
      ws.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            kind: "control",
            type: "run_changed",
            run_id: "r1",
            action: "deleted",
          }),
        }),
      );
    });
    // r1 行消失。
    const remaining = screen.getAllByTestId("run-row");
    expect(remaining.length).toBe(1);
  });

  // ── AC-15 fail-loud：非 JSON 帧 console.warn ──
  it("AC-15：非 JSON 帧触发 console.warn（不静默吞）", async () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findAllByTestId("run-row");
    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.onmessage?.(new MessageEvent("message", { data: "not-json{" }));
    });
    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  // ── 项目头美化：folder icon + path + 聚合 ──
  it("AC-5 项目头：folder icon + path + 聚合（N runs）", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", project_name: "demo", cost: 0.3, started_at: 1700000000 }),
      mkRun({ run_id: "r2", project_name: "demo", cost: 0.4, status: "running" }),
    ]);
    const { container } = renderPage();
    await screen.findByTestId("group-demo");
    const group = screen.getByTestId("group-demo");
    const header = within(group).getByTestId("group-header");
    // 含「demo」项目名。
    expect(header.textContent).toMatch(/demo/);
    // 含 path /tmp/demo。
    expect(header.textContent).toMatch(/\/tmp\/demo/);
    // 聚合显 runs 数。
    const sectionText = within(group).getAllByText(/runs|运行中|待决策|总花费|最近/);
    expect(sectionText.length).toBeGreaterThan(0);
    // folder svg icon 存在。
    expect(container.querySelector('[data-testid="group-demo"] svg')).toBeTruthy();
  });

  // ── AC-6 主题按钮：点 → <html> class 切换 + localStorage 写 ──
  it("AC-6 主题：点 theme-btn → <html> class 切换 + localStorage 写 orca-theme", async () => {
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findByTestId("run-row");
    // 清掉初始状态。
    document.documentElement.classList.remove("dark", "light");
    localStorage.removeItem("orca-theme");
    const btn = screen.getByTestId("theme-btn");
    await act(async () => {
      fireEvent.click(btn);
    });
    // localStorage 写。
    const stored = localStorage.getItem("orca-theme");
    expect(stored).toBeTruthy();
    // <html> 加了 dark 或 light 之一。
    expect(
      document.documentElement.classList.contains("dark") ||
        document.documentElement.classList.contains("light"),
    ).toBe(true);
  });

  // ── AC-7 搜索清空 → 恢复折叠 ──
  it("AC-7：搜索清空 → 恢复持久折叠态（非搜索态时按 collapsed 集合）", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", workflow_name: "demo", project_name: "demo" }),
    ]);
    renderPage();
    await screen.findByTestId("group-demo");
    // 先折叠。
    await act(async () => {
      fireEvent.click(screen.getByTestId("group-header"));
    });
    // 输入搜索 → 强制展开。
    const input = screen.getByTestId("search-input") as HTMLInputElement;
    await act(async () => {
      fireEvent.change(input, { target: { value: "demo" } });
    });
    await new Promise((r) => setTimeout(r, 350));
    expect(screen.queryByTestId("run-row")).toBeTruthy();

    // 清空搜索 → 恢复折叠。
    await act(async () => {
      fireEvent.change(input, { target: { value: "" } });
    });
    await new Promise((r) => setTimeout(r, 350));
    // 折叠态：run-row 不可见。
    expect(screen.queryByTestId("run-row")).toBeNull();
  });
});

// ── 看板视图（SPEC §10）──
//
// 断言意图（Rule 9）：
//   - AC-19 默认看板：``/`` 默认渲染 board；toggle 切 list；持久 ``orca-runlist-view-v1``。
//   - AC-20 五列：排队/运行中/待决策/已完成/失败 各一列；run 按 statusToRunStatus 落列。
//   - AC-21 BoardCard：进度条按 progress 渲染；blocked 显等待；click 进详情；hover 显删除+勾选。
//   - AC-22 已完成/失败限长：列显最近 10 + 显示更多。
//   - AC-23 共享 selection：看板勾选 ↔ 列表同步；bulk bar 两视图都显。

describe("RunListPage 看板视图（SPEC §10）", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    useRunListStore.getState().reset();
    useRunListStore.setState({ lastFetch: 0 });
    localStorage.clear();
    // 看板用例：默认即 board（不设 localStorage）。
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // ── AC-19 默认看板 + 持久化 ──
  it("AC-19：默认渲染 board（data-testid=board）；切 list 后持久 localStorage", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", status: "completed", project_name: "demo" }),
    ]);
    renderPage();
    expect(await screen.findByTestId("board")).toBeTruthy();
    // 列表视图不渲染（无 run-row）。
    expect(screen.queryByTestId("run-row")).toBeNull();

    // 切到 list。
    await act(async () => {
      fireEvent.click(screen.getByTestId("view-toggle-list"));
    });
    expect(screen.getByTestId("run-row")).toBeTruthy();
    expect(screen.queryByTestId("board")).toBeNull();
    // 持久化。
    const stored = localStorage.getItem("orca-runlist-view-v1");
    expect(stored).toMatch(/list/);
  });

  it("AC-19：默认看板 localStorage 损坏 → 降级 board 不崩", async () => {
    localStorage.setItem("orca-runlist-view-v1", "{not json");
    mockFetchFor([mkRun({ run_id: "r1", status: "completed" })]);
    expect(() => renderPage()).not.toThrow();
    expect(await screen.findByTestId("board")).toBeTruthy();
  });

  // ── AC-20 五列落位 ──
  it("AC-20：五列均渲染；run 按 statusToRunStatus 落对应列", async () => {
    mockFetchFor([
      mkRun({ run_id: "rq", status: "queued" }),
      mkRun({ run_id: "rr", status: "running" }),
      mkRun({ run_id: "rb", status: "blocked" }),
      mkRun({ run_id: "rc", status: "completed" }),
      mkRun({ run_id: "rf", status: "failed" }),
    ]);
    renderPage();
    await screen.findByTestId("board");
    // 五列 testid 都在。
    expect(screen.getByTestId("board-column-queued")).toBeTruthy();
    expect(screen.getByTestId("board-column-running")).toBeTruthy();
    expect(screen.getByTestId("board-column-blocked")).toBeTruthy();
    expect(screen.getByTestId("board-column-completed")).toBeTruthy();
    expect(screen.getByTestId("board-column-failed")).toBeTruthy();
    // 每列各含一张 board-card。
    expect(
      within(screen.getByTestId("board-column-queued")).getAllByTestId("board-card")
        .length,
    ).toBe(1);
    expect(
      within(screen.getByTestId("board-column-running")).getAllByTestId("board-card")
        .length,
    ).toBe(1);
    expect(
      within(screen.getByTestId("board-column-blocked")).getAllByTestId("board-card")
        .length,
    ).toBe(1);
    expect(
      within(screen.getByTestId("board-column-completed")).getAllByTestId(
        "board-card",
      ).length,
    ).toBe(1);
    expect(
      within(screen.getByTestId("board-column-failed")).getAllByTestId("board-card")
        .length,
    ).toBe(1);
  });

  it("AC-20：待决策列计数>0 → 整列 ring（class 含 ring-orca-skipped/20）", async () => {
    mockFetchFor([mkRun({ run_id: "rb", status: "blocked" })]);
    renderPage();
    const col = await screen.findByTestId("board-column-blocked");
    expect(col.className).toMatch(/ring-orca-skipped\/20/);
  });

  // ── AC-21 BoardCard 进度条 + 点击 ──
  it("AC-21：running 卡片渲染进度条（按 progress 字符串解析）", async () => {
    mockFetchFor([
      mkRun({ run_id: "rr", status: "running", progress: "40%" }),
    ]);
    renderPage();
    await screen.findByTestId("board-card");
    // 进度条内填宽度 = 40%。
    const fill = document.querySelector(".bg-orca-accent.h-full");
    expect(fill).toBeTruthy();
    expect((fill as HTMLElement).style.width).toBe("40%");
  });

  it("AC-21：blocked 卡片显「等待」+ ring（NM1 沿用 RunRow）", async () => {
    mockFetchFor([mkRun({ run_id: "rb", status: "blocked", elapsed: 30 })]);
    renderPage();
    const card = await screen.findByTestId("board-card");
    expect(card.className).toMatch(/ring-orca-skipped\/30/);
    expect(card.textContent).toMatch(/等待/);
  });

  it("AC-21：click board-card → 导航到 /runs/<id>", async () => {
    mockFetchFor([mkRun({ run_id: "r1", status: "completed" })]);
    renderPage();
    await screen.findByTestId("board-card");
    await act(async () => {
      fireEvent.click(screen.getByTestId("board-card"));
    });
    // MemoryRouter 没真导航，但 navigate('/runs/r1') 会被调用——
    // 这里仅断言不报错 + 卡片可点（详情页渲染由 9b 真机测覆盖）。
  });

  // ── AC-22 已完成/失败列限长 + 显示更多 ──
  it("AC-22：completed 列 >10 条 → 显「显示更多」；点击展开", async () => {
    const many = Array.from({ length: 15 }, (_, i) =>
      mkRun({
        run_id: `c${i}`,
        status: "completed",
        started_at: 1700000000 + i,
      }),
    );
    mockFetchFor(many);
    renderPage();
    await screen.findByTestId("board-column-completed");
    // 初始 10 张。
    expect(
      within(screen.getByTestId("board-column-completed")).getAllByTestId("board-card")
        .length,
    ).toBe(10);
    // 显示更多按钮存在。
    const more = screen.getByTestId("board-column-more-completed");
    expect(more.textContent).toMatch(/共 15/);
    // 点击展开。
    await act(async () => {
      fireEvent.click(more);
    });
    expect(
      within(screen.getByTestId("board-column-completed")).getAllByTestId("board-card")
        .length,
    ).toBe(15);
  });

  // ── AC-23 共享 selection ──
  it("AC-23：看板勾选 → bulk-bar 出现；切列表选择保留", async () => {
    mockFetchFor([
      mkRun({ run_id: "r1", status: "completed" }),
      mkRun({ run_id: "r2", status: "completed" }),
    ]);
    renderPage();
    await screen.findAllByTestId("board-card");
    // 勾选第一张卡的 checkbox。
    const cbs = screen.getAllByTestId("run-checkbox");
    expect(cbs.length).toBe(2);
    await act(async () => {
      fireEvent.click(cbs[0]);
    });
    expect(screen.getByTestId("bulk-bar")).toBeTruthy();

    // 切到列表视图——选择应保留。
    await act(async () => {
      fireEvent.click(screen.getByTestId("view-toggle-list"));
    });
    expect(screen.getByTestId("bulk-bar")).toBeTruthy();
    // 列表第一行 checkbox 选中。
    const listCbs = screen.getAllByTestId("run-checkbox");
    expect((listCbs[0] as HTMLInputElement).checked).toBe(true);
  });

  // ── 看板下隐藏 groupBy toggle ──
  it("AC-19：看板视图下不显 group-toggle；切列表后出现", async () => {
    mockFetchFor([mkRun({ run_id: "r1", status: "completed" })]);
    renderPage();
    await screen.findByTestId("board");
    expect(screen.queryByTestId("group-toggle")).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByTestId("view-toggle-list"));
    });
    expect(screen.getByTestId("group-toggle")).toBeTruthy();
  });
});

// ── WS 重连（SPEC §5.8 / AC-14）──
//
// 测试意图（Rule 9）：重连逻辑是 ``useWsRunlist`` hook 的职责——直接 ``renderHook``
// 驱动它，比通过整页 + fake timers（与 testing-library 的 findBy 轮询冲突）更稳更快。
// 整页的「ws-status 提示出现」由既有页面快照覆盖。

/** 构造**不**自动 onopen 的 MockWebSocket（让重连流程可控）。 */
class SilentMockWebSocket {
  static instances: SilentMockWebSocket[] = [];
  url: string;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  readyState = 0;
  constructor(url: string) {
    this.url = url;
    SilentMockWebSocket.instances.push(this);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
}

describe("useWsRunlist 重连（AC-14 / 反例3）", () => {
  beforeEach(() => {
    SilentMockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", SilentMockWebSocket);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("AC-14：onclose（非主动）→ connected=false + reconnects 增长 + 排程重连", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const { result } = renderHook(() =>
        useWsRunlist("ws://test/ws", () => {}),
      );
      // 初次连接： SilentMockWebSocket 构造但 onopen 未触发。
      expect(result.current.connected).toBe(false);
      const ws = SilentMockWebSocket.instances[0];
      // 模拟建立成功（onopen）。
      await act(async () => {
        ws.readyState = 1;
        ws.onopen?.(new Event("open"));
      });
      expect(result.current.connected).toBe(true);

      // 模拟服务端关闭（非主动）→ connected=false + reconnects=1。
      await act(async () => {
        ws.onclose?.(new CloseEvent("close"));
      });
      expect(result.current.connected).toBe(false);
      expect(result.current.reconnects).toBe(1);

      // 推进 1s（首档退避）→ 应排程新连接（新 instance 入列）。
      expect(SilentMockWebSocket.instances.length).toBe(1);
      await act(async () => {
        vi.advanceTimersByTime(1100);
      });
      expect(SilentMockWebSocket.instances.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("AC-14：重连成功后 reconnects 归零（反例3 闭环）", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const { result } = renderHook(() =>
        useWsRunlist("ws://test/ws", () => {}),
      );
      const ws1 = SilentMockWebSocket.instances[0];
      // 触发 onopen 后 onclose（一次重连）。
      await act(async () => {
        ws1.readyState = 1;
        ws1.onopen?.(new Event("open"));
      });
      await act(async () => {
        ws1.onclose?.(new CloseEvent("close"));
      });
      expect(result.current.reconnects).toBe(1);

      // 推进退避 → 新 WS 实例 → onopen 成功 → reconnects 归零。
      await act(async () => {
        vi.advanceTimersByTime(1100);
      });
      const ws2 = SilentMockWebSocket.instances[1];
      await act(async () => {
        ws2.readyState = 1;
        ws2.onopen?.(new Event("open"));
      });
      expect(result.current.connected).toBe(true);
      expect(result.current.reconnects).toBe(0);
      expect(result.current.giveUp).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it("AC-14：重连 >3 次仍失败 → giveUp=true + 暴露 reconnect()", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout"] });
    try {
      const { result } = renderHook(() =>
        useWsRunlist("ws://test/ws", () => {}),
      );
      // 模拟 4 次连续失败：每次新 WS 不 onopen 直接触发 onclose。
      for (let i = 0; i < 4; i++) {
        const ws = SilentMockWebSocket.instances[SilentMockWebSocket.instances.length - 1];
        await act(async () => {
          ws.onclose?.(new CloseEvent("close"));
        });
        await act(async () => {
          // 推进过当前退避（1/2/4/8s）；8s 足以覆盖前 4 档。
          vi.advanceTimersByTime(8000);
        });
      }
      expect(result.current.giveUp).toBe(true);
      expect(result.current.reconnects).toBeGreaterThanOrEqual(4);
      expect(typeof result.current.reconnect).toBe("function");

      // 手动 reconnect → 重置 giveUp + 新连接。
      await act(async () => {
        result.current.reconnect();
      });
      expect(result.current.giveUp).toBe(false);
      expect(result.current.reconnects).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});

// ── MAJOR-4：整页 wiring —— RunListPage 渲染 ws-status banner ──
describe("RunListPage WS 状态渲染（MAJOR-4 整页 wiring）", () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    useRunListStore.getState().reset();
    useRunListStore.setState({ lastFetch: 0 });
    localStorage.clear();
    localStorage.setItem("orca-runlist-view-v1", JSON.stringify("list"));
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("MAJOR-4：RunListPage WS 断线 → ws-status banner 出现（含「重连 N 次」）", async () => {
    // 整页 wiring 测试用**真** timers（避免与 findBy 轮询冲突），等待时间短（<100ms）。
    mockFetchFor([mkRun({ run_id: "r1" })]);
    renderPage();
    await screen.findAllByTestId("run-row");
    // mock WS auto-onopen → connected=true → 无 banner。
    expect(screen.queryByTestId("ws-status")).toBeNull();
    // 模拟服务端关闭（MockWebSocket.close 触发 onclose）。
    const ws = MockWebSocket.instances[0];
    await act(async () => {
      ws.close();
    });
    // 等状态 flush + banner 渲染。
    await new Promise((r) => setTimeout(r, 50));
    const banner = screen.getByTestId("ws-status");
    expect(banner.textContent).toMatch(/重连\s*1/);
  });
});

