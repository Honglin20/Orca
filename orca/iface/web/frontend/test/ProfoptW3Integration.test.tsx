// test/ProfoptW3Integration.test.tsx —— W-P3 联调验收（web SPEC §6.3-1/-2，vitest 侧）。
//
// fixture 来源（铁律：不手写臆造字段）——`scripts/gen-profopt-fixtures.py` 对真实
// `push_curves.py` 运行的逐字捕获（live 一次 + workspace 前进后 `(final)` 一次），
// 覆盖 §10.1 line / §10.2 pareto / §10.4 docs 三图 payload。重新生成（WSL repo 根）：
//   .venv/bin/python orca/iface/web/frontend/scripts/gen-profopt-fixtures.py
//
// 断言意图（Rule 9，不只行为）：
//   1. §6.3-1 真实清单 payload → 面板四组齐全 + **真实状态徽标**（latency_pass /
//      in-flight / success / latency_fail / final / snapshot / baseline——推送方
//      真会发出的字面量，非测试自造）→ 「推送方与消费方契约对上」
//   2. §6.3-1 幂等更新：live → (final) 二次推送（行集前进：+r5-01 / +rounds/003）
//      → 面板按最新清单替换，不复制不残留
//   3. §6.3-1 ParetoChartWidget（W-P3 修正回归）：y=null 占位点**不渲染**（不画在
//      0 位 = §10.2 占位披露不失真）+ per-row 状态着色被消费 + caption 披露在场；
//      无 color 字段的旧 payload 回退 dominated/front 双色（零回归）
//   4. §6.3-2 只读：面板点选全文只发 GET artifacts 端点（web §5 无写入口）
//
// happy-dom 已知限制（chart.test.tsx 同款注释）：recharts ComposedChart 的散点
// 逐点渲染在 happy-dom 下不出（真实浏览器 playwright 补验证），故点级断言走
// prepareParetoPoints / perRowColor 纯函数 + Legend 分支判别。

import { describe, expect, test, afterEach, vi } from "vitest";
import { cleanup, render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { ProfOptDocsPanel } from "@/components/profopt/ProfOptDocsPanel";
import { ParetoChartWidget } from "@/components/chart/widgets/ParetoChartWidget";
import {
  prepareParetoPoints,
  perRowColor,
} from "@/components/chart/widgets/ParetoChartWidget";
import { ChartWidget } from "@/components/chart/ChartWidget";
import { NEUTRAL } from "@/components/chart/chartTheme";
import type { ChartPayload } from "@/components/chart/types";
import type { WebEvent } from "@/types/events";
import raw from "./fixtures/profopt-push-curves.json";

const RUN_ID = "run_po_w3";
const liveDocs = raw.live["prof-opt/docs"] as unknown as ChartPayload;
const liveLine = raw.live["prof-opt/curves"] as unknown as ChartPayload;
const livePareto = raw.live["prof-opt/pareto"] as unknown as ChartPayload;
const finalDocs = raw.final["prof-opt/docs"] as unknown as ChartPayload;

let _seq = 900;
/** 真实 payload → chart 事件（与 chart socket → store 的现有事件形状一致）。 */
function chartEvent(chart: unknown): WebEvent {
  return {
    seq: _seq++,
    type: "custom",
    timestamp: Date.now() / 1000,
    node: "po_propose",
    session_id: "po_propose",
    data: { kind: "chart", chart },
  };
}

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  useWorkflowStore.setState({ loadStatus: "loaded" });
  vi.unstubAllGlobals();
  _seq = 900;
});

function setupPanel(...charts: unknown[]) {
  // INV-7：loadStatus 非 loaded 时 processEvent 会被丢；且事件需在 render 前进
  // store（同 W2 测试 / chart.test.tsx 的既定顺序：置态 → 喂事件 → 渲染）。
  useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: RUN_ID });
  for (const c of charts) {
    useWorkflowStore.getState().processEvent(chartEvent(c));
  }
  return render(<ProfOptDocsPanel runId={RUN_ID} />);
}

describe("W3-T1 联调：ProfOptDocsPanel 消费真实 push_curves 清单 payload", () => {
  test("真实 docs payload → 四组齐全 + 真实状态徽标（含达线未训与淘汰变体）", () => {
    setupPanel(liveDocs);
    for (const key of ["baseline", "variants", "rounds", "rules"]) {
      expect(screen.getByTestId(`docs-group-${key}`)).toBeInTheDocument();
    }
    // 真实状态字面量（推送方会发的，非测试自造）
    expect(screen.getByTestId("docs-group-baseline").textContent).toContain(
      "business_logic.md"
    );
    // 达线未训变体（latency_pass）与淘汰变体（latency_fail）保留展示（web §3.3）
    expect(screen.getByTestId("docs-variant-card-r3-01").textContent).toContain(
      "latency_pass"
    );
    expect(screen.getByTestId("docs-variant-card-r4-01").textContent).toContain(
      "latency_fail"
    );
    expect(screen.getByTestId("docs-variant-card-r1-01").textContent).toContain(
      "success"
    );
    expect(screen.getByTestId("docs-variant-card-r2-01").textContent).toContain(
      "in-flight"
    );
    expect(screen.getByTestId("docs-group-rounds").textContent).toContain(
      "rounds/002/analysis.md"
    );
    expect(screen.getByTestId("docs-group-rules").textContent).toContain(
      "accuracy_rules_snapshot.json"
    );
  });

  test("live → (final) 二次推送（行集前进）→ 幂等替换不复制（web §2.3/§6.3-1）", () => {
    setupPanel(liveDocs, finalDocs);
    // 最新清单生效：final 新增行在场（r5-01 / rounds/003）
    expect(screen.getByTestId("docs-variant-card-r5-01").textContent).toContain(
      "success"
    );
    expect(screen.getByTestId("docs-group-rounds").textContent).toContain(
      "rounds/003/analysis.md"
    );
    // 替换而非追加：条目总数 == final 清单行数（无复制残留）
    expect(screen.getAllByTestId("doc-item").length).toBe(
      finalDocs.data!.length
    );
  });

  test("真实 line payload → ChartWidget 现有 line 渲染路径不破（形状兼容）", async () => {
    render(<ChartWidget payload={liveLine} />);
    // 多系列 Line 的 path 在 happy-dom 下渲染不稳（chart.test.tsx area(hue) 同款
    // 已知限制，真实浏览器由 playwright 补验证）——断言 hue pivot 接线：真实
    // payload 的 4 个 vid（baseline + r1-01/r2-01，live 时刻）各成 legend 系列。
    await waitFor(() => {
      const legends = Array.from(
        document.querySelectorAll(".recharts-legend-item-text")
      ).map((el) => el.textContent);
      expect(legends).toEqual(
        expect.arrayContaining(["baseline", "r1-01", "r2-01"])
      );
    });
  });
});

describe("W3-T1 联调：ParetoChartWidget 修正回归（P4 上报缺陷）", () => {
  test("y=null 占位点整点剔除（不画在 0 位）——r3-01/r4-01 不进散点/轴/前沿", () => {
    const { plottableRows, points } = prepareParetoPoints(
      livePareto.data!,
      "x",
      "y"
    );
    // live fixture: r1-01 / r2-01 可绘；r3-01（达线未训）与 r4-01（latency_fail
    // 且无 gap/metric）y=null —— 原 Number(null)=0 会把它们画在 0 位
    expect(points.map((p) => p.x)).toEqual([20, -20]);
    expect(points.map((p) => p.y)).toEqual([0.02, 0.45]);
    expect(plottableRows.map((r) => r.vid)).toEqual(["r1-01", "r2-01"]);
  });

  test("per-row 状态着色被消费：行色 = §10.2 状态色，缺色回退 NEUTRAL", () => {
    const rows = livePareto.data!;
    const byVid = new Map(
      rows.map((r) => [String(r.vid), r as Record<string, unknown>])
    );
    expect(perRowColor(byVid.get("r1-01")!, "color")).toBe("#10b981"); // success
    expect(perRowColor(byVid.get("r2-01")!, "color")).toBe("#3b82f6"); // in-flight
    expect(perRowColor(byVid.get("r3-01")!, "color")).toBe("#94a3b8"); // latency_pass
    expect(perRowColor(byVid.get("r4-01")!, "color")).toBe("#f97316"); // latency_fail
    // 缺色回退：字段缺失与空串都不落「无色」点（与 isPlottable 的「缺」同口径）
    expect(perRowColor({ vid: "x" }, "color")).toBe(NEUTRAL);
    expect(perRowColor({ vid: "x", color: "" }, "color")).toBe(NEUTRAL);
  });

  test("isPlottable 意图全子句：null/undefined/空串/非有限数全部不可绘", () => {
    // 意图是「占位/脏值都不落 0 位」，不只 null 一条（Number(null)=0 是本缺陷根源）
    const { points } = prepareParetoPoints(
      [
        { x: 1, y: null },        // 达线未训占位（fixture 已覆盖，钉住）
        { x: 2, y: undefined },   // 字段缺席
        { x: 3, y: "" },          // 空串（Number("")=0 同款陷阱）
        { x: 4, y: "abc" },       // 非数值串（Number→NaN）
        { x: 5, y: Infinity },    // 非有限数（轴刻度会被 Infinity 毁掉）
        { x: null, y: 0.5 },      // x 侧同样剔除（对称性）
        { x: 6, y: 0.5 },         // 唯一可绘
      ],
      "x",
      "y"
    );
    expect(points).toEqual([{ x: 6, y: 0.5 }]);
  });

  test("color 字段在场 → 单散点系列（Legend=variants）+ caption 占位披露在场", async () => {
    render(<ParetoChartWidget payload={livePareto} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-legend-item")).toBeTruthy();
    });
    const legends = Array.from(
      document.querySelectorAll(".recharts-legend-item-text")
    ).map((el) => el.textContent);
    expect(legends).toContain("variants");
    expect(legends).not.toContain("Dominated"); // 状态着色路径，非 dominated/front 双色
    // §10.2 占位披露（y=null 语义）经 caption 显形，不静默
    expect(screen.getByTestId("chart-caption").textContent).toContain("null");
  });

  test("无 color 字段（旧 payload）→ 回退 dominated/front 双色（零回归）", async () => {
    const { color: _c, ...noColor } = livePareto;
    void _c;
    render(<ParetoChartWidget payload={noColor as ChartPayload} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-legend-item")).toBeTruthy();
    });
    const legends = Array.from(
      document.querySelectorAll(".recharts-legend-item-text")
    ).map((el) => el.textContent);
    expect(legends).toContain("Dominated");
    expect(legends).toContain("Pareto Front");
    expect(legends).not.toContain("variants");
  });
});

describe("W3-T2 只读验证（web §6.3-2 面板侧）", () => {
  test("点选真实清单条目 → 唯一请求是 GET artifacts 端点", async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        text: () => Promise.resolve("# 基线\n\n正文"),
      } as Response)
    );
    vi.stubGlobal("fetch", fetchMock);
    setupPanel(liveDocs);
    fireEvent.click(
      screen
        .getAllByTestId("doc-item")
        .find((el) => el.getAttribute("title") === "baseline/business_logic.md")!
    );
    await screen.findByText("基线");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const call = fetchMock.mock.calls[0] as unknown[];
    expect(String(call[0])).toBe(
      `/api/runs/${RUN_ID}/artifacts/file?path=${encodeURIComponent(
        "baseline/business_logic.md"
      )}`
    );
    expect((call[1] as RequestInit | undefined)?.method ?? "GET").toBe("GET");
  });
});
