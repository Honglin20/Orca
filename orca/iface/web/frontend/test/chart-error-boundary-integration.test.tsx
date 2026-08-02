// test/chart-error-boundary-integration.test.tsx —— SPEC audit-c AC10/M17 真集成测试。
//
// 独立文件：vi.mock 顶层 hoist 影响整个文件，故隔离避免污染其他 chart 测试。
//
// 断言意图（Rule 9）：vi.mock LineChartWidget 让其在渲染时 throw → 验证
// ChartRenderer → ChartGroup → LazyChartWidget → ChartErrorBoundary → ChartWidget 抛错
// 被 boundary 兜底，同组其他 chart 保留，整个 chart-renderer 容器仍在 DOM（不冒泡卸载整 tab）。

import { describe, expect, test, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";

// 顶层 vi.mock（hoist）：LineChartWidget render 时 throw
vi.mock("@/components/chart/widgets/LineChartWidget", () => ({
  LineChartWidget: () => {
    throw new Error("widget crashed");
  },
}));

// 其他 widget 保持原样
vi.mock("@/components/chart/widgets/ScatterChartWidget", async (importOriginal) => {
  const orig = await importOriginal<
    typeof import("@/components/chart/widgets/ScatterChartWidget")
  >();
  return { ScatterChartWidget: orig.ScatterChartWidget };
});

// import 在 mock 之后（确保 mock 生效）
const { ChartRenderer } = await import("@/components/chart/ChartRenderer");
import type { WebEvent } from "@/types/events";

let _seq = 100;
function chartEvent(node: string, chartObj: Record<string, unknown>): WebEvent {
  return {
    seq: _seq++,
    type: "custom",
    timestamp: Date.now() / 1000,
    node,
    session_id: node,
    data: { kind: "chart", chart: chartObj },
  };
}

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  useWorkflowStore.setState({ loadStatus: "loaded" });
  _seq = 100;
});

describe("SPEC audit-c AC10/M17 ErrorBoundary 真集成", () => {
  test("LineChartWidget throw → 抛错 cell fallback + 同组 scatter 保留 + tab 不卸载", () => {
    useWorkflowStore.setState({ loadStatus: "loaded", activeRunId: "r" });
    // 一个 line chart（会 throw）+ 一个 scatter chart（保留）同组
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        chart_type: "line",
        data: [{ x: 1, y: 2 }],
        x: "x",
        y: "y",
        label: "g1",
        title: "throw-line",
      })
    );
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        chart_type: "scatter",
        data: [{ x: 1, y: 2 }, { x: 3, y: 4 }],
        x: "x",
        y: "y",
        label: "g1",
        title: "healthy-scatter",
      })
    );

    render(<ChartRenderer nodeId="n1" />);

    // 抛错的 line cell 显示 fallback
    expect(screen.getAllByTestId("chart-error-fallback").length).toBe(1);
    // chart-renderer 容器仍在 DOM（tab 不卸载，INV-6）
    expect(screen.getByTestId("chart-renderer")).toBeInTheDocument();
    // 同组 scatter chart 仍渲染（chart-widget 存在）
    expect(screen.getAllByTestId("chart-widget").length).toBe(1);
  });
});
