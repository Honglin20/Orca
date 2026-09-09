// test/ProfoptDocsPanel.test.tsx —— W-P2 面板验收（web SPEC §6.2）。
//
// 断言意图（Rule 9）：
//   1. §6.2-1 清单 payload（label `prof-opt/docs` table）→ 四组分组正确
//      （基线三文档 + 淘汰变体可见 = §6.2-2 前半，web §3.3 失败变体保留展示）
//   2. §6.2-1 点选 → mock artifacts 端点 → MarkdownText 渲染；只发 GET（web §5 只读）
//   3. §3.1 图片重写：md 内相对图片 → artifacts 端点前缀（doc 目录相对解析）
//   4. JSON 文档 → FileContentView 复用
//   5. §6.2-2 404 / 413 → 降级提示不崩（合法清单仍渲染）
//   6. 坏行（缺 path）→ schema warning 披露，合法行照常渲染（fail loud 不静默丢）

import { describe, expect, test, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { useWorkflowStore } from "@/stores/workflow-store";
import { ProfOptDocsPanel } from "@/components/profopt/ProfOptDocsPanel";
import { RunDetailPage } from "@/components/pages/RunDetailPage";
import type { WebEvent } from "@/types/events";

// W2-T2 挂载冒烟：绕开数据加载/WS 副作用（面板本身的数据流由直渲染用例覆盖）。
vi.mock("@/hooks/use-run-events", () => ({ useRunEvents: () => {} }));
vi.mock("@/hooks/use-websocket", () => ({ useWebSocket: () => {} }));

const RUN_ID = "run_po1";

let _seq = 900;
function manifestEvent(
  rows: Record<string, unknown>[],
  title = "prof-opt analysis docs"
): WebEvent {
  return {
    seq: _seq++,
    type: "custom",
    timestamp: Date.now() / 1000,
    node: "po_propose",
    session_id: "po_propose",
    data: {
      kind: "chart",
      chart: {
        chart_type: "table",
        label: "prof-opt/docs",
        title,
        columns: ["vid", "doc", "status", "path", "updated_at"],
        data: rows,
      },
    },
  };
}

/** 全量清单 fixture：基线三文档 + success 变体 + 淘汰（latency_fail）变体 + 轮次 + 规则。 */
function fullRows(): Record<string, unknown>[] {
  return [
    { vid: "baseline", doc: "business_logic.md", status: "ready", path: "baseline/business_logic.md", updated_at: "2026-08-31T10:00:00" },
    { vid: "baseline", doc: "information_analysis.md", status: "ready", path: "base/information_analysis.md" },
    { vid: "baseline", doc: "mfu_bottleneck_report.md", status: "ready", path: "base/profile/mfu_bottleneck_report.md" },
    { vid: "r1-01", doc: "business_logic.md", status: "success", path: "variants/r1-01/business_logic.md" },
    { vid: "r1-01", doc: "conformance.md", status: "success", path: "variants/r1-01/conformance.md" },
    { vid: "r2-01", doc: "business_logic.md", status: "latency_fail", path: "variants/r2-01/business_logic.md" },
    { vid: "round", doc: "analysis.md", status: "done", path: "rounds/002/analysis.md" },
    { vid: "rules", doc: "accuracy_rules_snapshot.json", status: "active", path: "base/accuracy_rules_snapshot.json" },
  ];
}

/** fetch mock：按 URL path 参数路由到固定响应（默认 200 空文）。 */
function mockFetch(
  routes: Record<string, { status?: number; body: string }>
): ReturnType<typeof vi.fn> {
  const impl = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    for (const [pathPrefix, r] of Object.entries(routes)) {
      if (url.includes(`path=${encodeURIComponent(pathPrefix)}`)) {
        return Promise.resolve({
          ok: (r.status ?? 200) < 400,
          status: r.status ?? 200,
          text: () => Promise.resolve(r.body),
        } as Response);
      }
    }
    // 未配置的 path → 404（fail loud，测试里意外路径显形）
    return Promise.resolve({ ok: false, status: 404, text: () => Promise.resolve("") } as Response);
  });
  vi.stubGlobal("fetch", impl);
  return impl;
}

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  useWorkflowStore.setState({ loadStatus: "loaded" });
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  _seq = 900;
});

function setup(rows?: Record<string, unknown>[]) {
  useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
  if (rows) {
    useWorkflowStore.getState().processEvent(manifestEvent(rows));
  }
  return render(<ProfOptDocsPanel runId={RUN_ID} />);
}

describe("W2-T3a 清单分组（web §6.2-1）", () => {
  test("四组分组正确；基线三文档 + 淘汰变体可见（§6.2-2 前半）", () => {
    setup(fullRows());
    // 四组齐
    for (const key of ["baseline", "variants", "rounds", "rules"]) {
      expect(screen.getByTestId(`docs-group-${key}`)).toBeInTheDocument();
    }
    // 基线三文档（web §0）
    for (const name of [
      "business_logic.md",
      "information_analysis.md",
      "mfu_bottleneck_report.md",
    ]) {
      expect(screen.getByTestId("docs-group-baseline").textContent).toContain(name);
    }
    // 淘汰变体保留展示（web §3.3）+ 状态徽标
    const card = screen.getByTestId("docs-variant-card-r2-01");
    expect(card.textContent).toContain("business_logic.md");
    expect(card.textContent).toContain("latency_fail");
    expect(screen.getByTestId("docs-variant-card-r1-01").textContent).toContain("success");
    // 轮次：Round 2 子区 + 纯文件名显示（完整 path 收进 card title 属性）
    expect(screen.getByTestId("docs-round-2").textContent).toContain("Round 2");
    expect(screen.getByTestId("docs-round-2").textContent).toContain("analysis.md");
    expect(
      screen
        .getAllByTestId("doc-item")
        .find((el) => el.getAttribute("title") === "rounds/002/analysis.md")
    ).toBeDefined();
    expect(screen.getByTestId("docs-group-rules").textContent).toContain("accuracy_rules_snapshot.json");
    // updated_at 显示（压成 YYYY-MM-DD HH:MM）；缺省行不显示（不造假值 → 组内仅 1 个时间串）
    expect(screen.getByTestId("docs-group-baseline").textContent).toContain("2026-08-31 10:00");
    // 不渲染正文（点开前无 markdown 容器）
    expect(screen.queryByTestId("file-content-view")).toBeNull();
  });

  test("无清单 → 空态提示不崩", () => {
    setup();
    expect(screen.getByTestId("docs-empty")).toBeInTheDocument();
  });

  test("坏行（缺 path / 变体行缺 vid）→ schema warning 披露，合法行照常渲染", () => {
    setup([
      { vid: "baseline", doc: "x.md", status: "ready" },
      { doc: "y.md", status: "impl", path: "variants/r9-01/y.md" },
      ...fullRows(),
    ]);
    expect(screen.getByTestId("docs-schema-warning").textContent).toContain("2");
    expect(screen.getByTestId("docs-group-rules")).toBeInTheDocument();
  });

  test("payload 缺 data 数组 → schema warning 披露（不静默空态）", () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
    useWorkflowStore.getState().processEvent({
      seq: _seq++,
      type: "custom",
      timestamp: Date.now() / 1000,
      node: "po_propose",
      session_id: "po_propose",
      data: { kind: "chart", chart: { chart_type: "table", label: "prof-opt/docs", title: "drifted" } },
    });
    render(<ProfOptDocsPanel runId={RUN_ID} />);
    expect(screen.getByTestId("docs-schema-warning").textContent).toContain("data");
  });

  test("同 label 二次推送（新 title）→ max-seq 胜，旧清单行消失（web §2.3 幂等替换）", () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
    useWorkflowStore.getState().processEvent(manifestEvent(fullRows(), "docs v1"));
    useWorkflowStore.getState().processEvent(
      manifestEvent(
        [{ vid: "rules", doc: "accuracy_rules_snapshot.json", status: "active", path: "base/accuracy_rules_snapshot.json" }],
        "docs v2"
      )
    );
    render(<ProfOptDocsPanel runId={RUN_ID} />);
    expect(screen.getByTestId("docs-group-rules")).toBeInTheDocument();
    expect(screen.queryByTestId("docs-group-baseline")).toBeNull();
    expect(screen.queryByTestId("docs-variant-card-r1-01")).toBeNull();
  });

  test("变体按轮序自然排序（r1-01 < r2-01 < r10-01，numeric compare）", () => {
    const v = (vid: string, status = "impl") => ({
      vid,
      doc: "business_logic.md",
      status,
      path: `variants/${vid}/business_logic.md`,
    });
    setup([v("r10-01"), v("r2-01"), v("r1-01")]);
    const cards = screen.getAllByTestId(/^docs-variant-card-/);
    expect(cards.map((c) => c.getAttribute("data-testid"))).toEqual([
      "docs-variant-card-r1-01",
      "docs-variant-card-r2-01",
      "docs-variant-card-r10-01",
    ]);
  });

  test("轮次按 Round N 子区分组（数字升序、组间不串）；非 rounds/<N>/ 形状行不隐藏", () => {
    setup([
      { vid: "round", doc: "architecture_decision.md", status: "final", path: "rounds/002/architecture_decision.md" },
      { vid: "round", doc: "analysis.md", status: "final", path: "rounds/001/analysis.md" },
      { vid: "round", doc: "semantic.md", status: "candidate", path: "rounds/002/candidates/semantic.md" },
      { vid: "round", doc: "analysis.md", status: "final", path: "rounds/002/analysis.md" },
      { vid: "round", doc: "summary.md", status: "final", path: "rounds/summary.md" },
    ]);
    // 子区按轮次号拆开：001 行不进 Round 2，002 行不进 Round 1（同名 analysis.md 各归各轮）
    expect(screen.getByTestId("docs-round-1").textContent).toContain("analysis.md");
    expect(screen.getByTestId("docs-round-1").textContent).not.toContain("architecture_decision.md");
    expect(screen.getByTestId("docs-round-2").textContent).toContain("architecture_decision.md");
    const round1Cards = Array.from(
      screen.getByTestId("docs-round-1").querySelectorAll("[data-testid=doc-item]")
    );
    expect(round1Cards.map((c) => c.getAttribute("title"))).toEqual(["rounds/001/analysis.md"]);
    // 不匹配 rounds/<N>/ 的行落入 misc 网格（不静默丢，且不混进任何 Round 子区）
    expect(screen.getByTestId("docs-round-misc").textContent).toContain("summary.md");
    expect(screen.getByTestId("docs-round-2").textContent).not.toContain("summary.md");
    // 显示名 = 纯文件名（同名 analysis.md 靠 Round 子区消歧，不再带路径前缀）
    expect(screen.getByTestId("docs-round-2").textContent).not.toContain("rounds/002");
  });

  test("updated_at 缺省行不显示时间（不造假值）", () => {
    setup(fullRows());
    const btn = screen
      .getAllByTestId("doc-item")
      .find((el) => el.getAttribute("title") === "base/information_analysis.md")!;
    expect(btn.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}/);
  });
});

describe("W2-T3b 点选拉取渲染（web §6.2-1 后半）", () => {
  test("点选 md → GET artifacts 端点 → MarkdownText 渲染；全程只 GET（web §5 只读）", async () => {
    const fetchMock = mockFetch({
      "baseline/business_logic.md": { body: "# 基线业务逻辑\n\n五段语义契约正文。" },
    });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "baseline/business_logic.md"
      )!
    );
    const h1 = await screen.findByText("基线业务逻辑");
    expect(h1.tagName).toBe("H1");
    // 唯一端点 + 只读（web §6.3-2 面板侧前置）
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit | undefined];
    expect(url).toBe(
      `/api/runs/${RUN_ID}/artifacts/file?path=${encodeURIComponent("baseline/business_logic.md")}`
    );
    expect(init?.method ?? "GET").toBe("GET");
  });

  test("md 内相对图片 → artifacts 端点前缀（doc 目录相对解析，§3.1）", async () => {
    mockFetch({
      "variants/r1-01/business_logic.md": {
        body: "# 变体\n\n![acc](plots/acc.png)\n\n![abs](https://cdn.x/y.png)",
      },
    });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "variants/r1-01/business_logic.md"
      )!
    );
    await screen.findByText("变体");
    // happy-dom 的 CSS 属性选择器不支持值内 `?` → querySelectorAll("img") 后 JS 过滤
    const srcs = Array.from(document.querySelectorAll("img")).map((i) =>
      i.getAttribute("src")
    );
    expect(srcs).toContain(
      `/api/runs/${RUN_ID}/artifacts/file?path=${encodeURIComponent("variants/r1-01/plots/acc.png")}`
    );
    // 绝对 https URL 直通（不二次改写）
    expect(srcs).toContain("https://cdn.x/y.png");
  });

  test("JSON 文档 → FileContentView 复用（§3.1）", async () => {
    mockFetch({
      "base/accuracy_rules_snapshot.json": { body: '{"rules": []}' },
    });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "base/accuracy_rules_snapshot.json"
      )!
    );
    await screen.findByTestId("file-content-view");
    expect(screen.getByTestId("doc-selected-name").textContent).toContain(
      "accuracy_rules_snapshot.json"
    );
  });

  test("围栏代码块（``` 与 ~~~）内的图片语法不改写（展示文本非真图）", async () => {
    mockFetch({
      "baseline/business_logic.md": {
        body: "# 围栏\n\n```\n![fake](fake.png)\n```\n\n~~~\n![fake2](fake2.png)\n~~~",
      },
    });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "baseline/business_logic.md"
      )!
    );
    await screen.findByText("围栏");
    // 代码块内图片语法保持原样文本
    const codeText = Array.from(document.querySelectorAll("code"))
      .map((c) => c.textContent)
      .join("\n");
    expect(codeText).toContain("![fake](fake.png)");
    expect(codeText).toContain("![fake2](fake2.png)");
    // 无被改写的 img（误改写会产出 artifacts 端点 img）
    expect(document.querySelectorAll("img").length).toBe(0);
  });

  test("快速连点两条目 → 旧请求过期返回不污染最新内容（竞态闸）", async () => {
    type Res = { ok: boolean; status: number; text: () => Promise<string> };
    const pending: Record<string, (r: Res) => void> = {};
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) =>
        new Promise<Res>((resolve) => {
          const url = String(input);
          if (url.includes(encodeURIComponent("variants/r1-01/business_logic.md"))) {
            pending.a = resolve;
          } else if (url.includes(encodeURIComponent("base/information_analysis.md"))) {
            pending.b = resolve;
          }
        })
      )
    );
    setup(fullRows());
    const item = (p: string) =>
      screen.getAllByTestId("doc-item").find((el) => el.getAttribute("title") === p)!;
    fireEvent.click(item("variants/r1-01/business_logic.md"));
    fireEvent.click(item("base/information_analysis.md"));
    // B（最新点选）先返回 → 立即可见
    pending.b({ ok: true, status: 200, text: () => Promise.resolve("# B 文档") });
    await screen.findByText("B 文档");
    // A（过期点选）后返回 → cancelled 闸拦下，不覆盖 B
    pending.a({ ok: true, status: 200, text: () => Promise.resolve("# A 文档") });
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText("A 文档")).toBeNull();
    expect(screen.getByText("B 文档")).toBeInTheDocument();
  });
});

describe("W2-T3c 404/413 降级（web §6.2-2 后半）", () => {
  test("404 → 降级提示不崩（清单仍渲染）", async () => {
    mockFetch({ "baseline/business_logic.md": { status: 404, body: "" } });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "baseline/business_logic.md"
      )!
    );
    const err = await screen.findByTestId("doc-fetch-error");
    expect(err.textContent).toContain("404");
    // 不崩：面板与清单仍在
    expect(screen.getByTestId("profopt-docs-panel")).toBeInTheDocument();
    expect(screen.getByTestId("docs-group-baseline")).toBeInTheDocument();
  });

  test("413 → 超限提示不崩（web §5 大文档降级）", async () => {
    mockFetch({ "base/profile/mfu_bottleneck_report.md": { status: 413, body: "" } });
    setup(fullRows());
    fireEvent.click(
      screen.getAllByTestId("doc-item").find(
        (el) => el.getAttribute("title") === "base/profile/mfu_bottleneck_report.md"
      )!
    );
    const err = await screen.findByTestId("doc-fetch-error");
    expect(err.textContent).toContain("1MB");
  });
});

describe("W2-T2 RunDetailPage 挂载冒烟（plan W-P2 + B4 页签重构）", () => {
  test("「文档」页签独占面板；「图表」页签内无文档面板、ChartsView 照常（B4）", async () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
    useWorkflowStore.getState().processEvent(manifestEvent(fullRows()));
    render(
      <MemoryRouter initialEntries={[`/runs/${RUN_ID}`]}>
        <Routes>
          <Route path="/runs/:runId" element={<RunDetailPage />} />
        </Routes>
      </MemoryRouter>
    );
    // B4：文档为独立页签（tab-docs），面板独占
    fireEvent.click(screen.getByTestId("tab-docs"));
    expect(await screen.findByTestId("profopt-docs-panel")).toBeInTheDocument();
    expect(screen.getByTestId("docs-group-baseline")).toBeInTheDocument();
    // 图表页签：ChartsView 在、文档面板不在（不再同栏挤占）
    // timeout 5s：ChartsView 懒加载 chunk 在全量并行跑下可超默认 1s（flake 实证），
    // 意图是「切页后 ChartsView 挂载」而非「1s 内挂载」。
    fireEvent.click(screen.getByTestId("tab-charts"));
    expect(await screen.findByTestId("charts-view", {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByTestId("profopt-docs-panel")).toBeNull();
  });
});

// ── B4/C3（2026-09-07）：正文三态 + 跨推送合并 + huge 防御 ──────────────────────

describe("B4/C3：正文三态（content 直渲 / omitted 提示 / legacy 回退）", () => {
  test("带 content 行 → 预览渲染 content 原文，网络层零 fetch（异机可见）", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setup([
      {
        vid: "baseline", doc: "business_logic.md", status: "ready",
        path: "baseline/business_logic.md", content: "# 事件通道正文\n\nC3 直送。",
      },
    ]);
    fireEvent.click(screen.getAllByTestId("doc-item")[0]);
    expect(await screen.findByText("事件通道正文")).toBeInTheDocument();
    // 意图：正文已随清单事件到达 → 不发任何请求（跨机同等体验的核心）
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("doc-loading")).toBeNull();
  });

  test("content_omitted=\"true\" → 显式提示「内容过大或本批未推送」，不 fetch（fail loud 不假试）", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setup([
      {
        vid: "baseline", doc: "business_logic.md", status: "ready",
        path: "baseline/business_logic.md", content: "", content_omitted: "true",
      },
    ]);
    fireEvent.click(screen.getAllByTestId("doc-item")[0]);
    const hint = await screen.findByTestId("doc-omitted");
    expect(hint.textContent).toContain("内容过大或本批未推送，仅清单可读");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("两连推合并：第一推带 content、第二推同 path 行 content 空 → 渲染第一推内容、mock fetch 零调用", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
    // 第一推（首推全量带 content）
    useWorkflowStore.getState().processEvent(
      manifestEvent(
        [
          {
            vid: "baseline", doc: "business_logic.md", status: "ready",
            path: "baseline/business_logic.md", content: "# 首推正文\n",
          },
        ],
        "docs v1"
      )
    );
    // 第二推（同 path 未变行 content 空 + 新 title → max-seq 胜）
    useWorkflowStore.getState().processEvent(
      manifestEvent(
        [
          {
            vid: "baseline", doc: "business_logic.md", status: "ready",
            path: "baseline/business_logic.md", content: "", content_omitted: "",
          },
        ],
        "docs v2"
      )
    );
    render(<ProfOptDocsPanel runId={RUN_ID} />);
    fireEvent.click(screen.getAllByTestId("doc-item")[0]);
    expect(await screen.findByText("首推正文")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("json 行带 content → FileContentView 直渲（零 fetch，通道不限 md）", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    setup([
      {
        vid: "rules", doc: "accuracy_rules_snapshot.json", status: "active",
        path: "base/accuracy_rules_snapshot.json",
        content: '{"rules": []}',
      },
    ]);
    fireEvent.click(screen.getAllByTestId("doc-item")[0]);
    expect(await screen.findByTestId("file-content-view")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("huge 未 loadFull 且 overview 目录含 docs chart → 既有 huge 提示分支（不显示「暂无清单」）（防御性用例）", () => {
    useWorkflowStore.setState({
      loadStatus: "loaded",
      activeRunId: RUN_ID,
      huge: true,
      hugeFullyLoaded: false,
      serverOverview: {
        agents: [],
        charts: [
          { label: "prof-opt/docs", title: "prof-opt analysis docs", chart_type: "table" },
        ],
        cost_usd: 0,
        run_status: "running",
      },
    });
    render(<ProfOptDocsPanel runId={RUN_ID} />);
    expect(screen.getByTestId("docs-huge-hint")).toBeInTheDocument();
    expect(screen.queryByTestId("docs-empty")).toBeNull();
  });
});
