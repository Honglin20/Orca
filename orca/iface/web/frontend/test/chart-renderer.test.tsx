// test/chart-renderer.test.tsx —— SPEC audit-c §4.2 partition + ErrorBoundary 验收。
//
// 断言意图（Rule 9）：
//   1. C2-AC1/INV-5: 缺 chart_type / data 非 array → 显示 chart-schema-warning + 正常 chart 仍渲染
//   2. C2-AC2/M17 (vi.mock): widget throw → 显示 chart-error-fallback + 同组其他 chart 保留
//   3. C2 #27/E3/BLOCKER-1: ChartGroup key={chart.identity}（grep + huge→full 稳定性 canary）
//   4. MINOR-5: 无 title chart dev warn-once-per-identity（spy 调用次数 === 1 跨多次 render）

import { describe, expect, test, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { useWorkflowStore, untitledChartWarned } from "@/stores/workflow-store";
import { ChartRenderer } from "@/components/chart/ChartRenderer";
import { ChartErrorBoundary } from "@/components/chart/ChartErrorBoundary";
import type { ChartPayload } from "@/components/chart/types";
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

const LINE_OK: ChartPayload = {
  chart_type: "line",
  data: [{ x: 1, y: 2 }],
  x: "x",
  y: "y",
  label: "g1",
  title: "ok-line",
};

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
  untitledChartWarned.clear();
  // 重置 _seq 防跨测试 identity 漂移
  _seq = 100;
  // 测试 setup 默认 loaded 让 processEvent 可驱动
  useWorkflowStore.setState({ loadStatus: "loaded" });
  vi.restoreAllMocks();
});

describe("SPEC audit-c C2 partition（INV-5 schema 漂移显形）", () => {
  test("C2-AC1: 缺 chart_type 的 payload → chart-schema-warning + 正常 chart 仍渲染", () => {
    useWorkflowStore.setState({ loadStatus: "loaded" });
    // 一个 valid chart + 一个缺 chart_type 的 chart（同 group）
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_OK, label: "g1", title: "ok1" })
    );
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { label: "g1", title: "bad", data: [{ x: 1 }] }) // 缺 chart_type
    );
    render(<ChartRenderer nodeId="n1" />);
    // schema warning 显示
    expect(screen.getByTestId("chart-schema-warning")).toBeInTheDocument();
    // 正常 chart 仍渲染
    expect(screen.getAllByTestId("chart-widget").length).toBe(1);
  });

  test("C2-AC1: data 非 array → rejected", () => {
    useWorkflowStore.setState({ loadStatus: "loaded" });
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        chart_type: "line",
        data: { not: "array" }, // 非 array
        label: "g1",
        title: "bad-data",
      })
    );
    render(<ChartRenderer nodeId="n1" />);
    expect(screen.getByTestId("chart-schema-warning")).toBeInTheDocument();
    expect(screen.queryByTestId("chart-widget")).toBeNull();
  });

  test("无 rejected 时无 warning 条", () => {
    useWorkflowStore.setState({ loadStatus: "loaded" });
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_OK, label: "g1", title: "ok" })
    );
    render(<ChartRenderer nodeId="n1" />);
    expect(screen.queryByTestId("chart-schema-warning")).toBeNull();
    expect(screen.getAllByTestId("chart-widget").length).toBe(1);
  });
});

describe("SPEC audit-c C2 ErrorBoundary（INV-6 单点崩溃不连累整 tab）", () => {
  test("ChartErrorBoundary：子组件 throw → 显示 chart-error-fallback", () => {
    const Throw: React.FC = () => {
      throw new Error("test throw");
    };
    render(
      <ChartErrorBoundary>
        <Throw />
      </ChartErrorBoundary>
    );
    expect(screen.getByTestId("chart-error-fallback")).toBeInTheDocument();
  });

  test("ChartErrorBoundary：包裹多个子组件，一个 throw 不影响其他（独立 boundary）", () => {
    // 验证 boundary 隔离：每个 boundary 实例独立兜底，互不连累。
    const CrashingWidget: React.FC = () => {
      throw new Error("crash");
    };
    render(
      <div>
        <ChartErrorBoundary>
          <CrashingWidget />
        </ChartErrorBoundary>
        <ChartErrorBoundary>
          <div data-testid="healthy-chart">healthy</div>
        </ChartErrorBoundary>
      </div>
    );
    expect(screen.getByTestId("chart-error-fallback")).toBeInTheDocument();
    expect(screen.getByTestId("healthy-chart")).toBeInTheDocument();
  });
  // 真集成测试（vi.mock 真实 LineChartWidget throw）见
  // test/chart-error-boundary-integration.test.tsx（独立文件避免 vi.mock 影响其他测试）。
});

describe("SPEC audit-c #27/E3/BLOCKER-1 ChartGroup key=identity", () => {
  test("ChartGroup 签名 {identity, payload}[] + key=identity grep 命中", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const src = fs.readFileSync(
      path.resolve(
        import.meta.dirname,
        "..",
        "src",
        "components",
        "chart",
        "ChartGroup.tsx"
      ),
      "utf8"
    );
    expect(src).toContain("key={c.identity}");
    expect(src).not.toContain("key={c.title");
  });

  test("titled chart：跨 re-render key 稳定（identity 不变 → 无 remount）", () => {
    useWorkflowStore.setState({ loadStatus: "loaded" });
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_OK, label: "g1", title: "stable-title" })
    );
    const { rerender } = render(<ChartRenderer nodeId="n1" />);
    const before = screen.getAllByTestId("chart-widget")[0];
    // 强制 rerender（state 未变 → React 复用）
    rerender(<ChartRenderer nodeId="n1" />);
    const after = screen.getAllByTestId("chart-widget")[0];
    // 同一 DOM 节点（key 稳定，无 remount）
    expect(after).toBe(before);
  });
});

describe("SPEC audit-c MINOR-5 无 title chart warn-once-per-identity", () => {
  test("同 untitled identity 跨多次 re-render → warn spy 调用次数 === 1", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    useWorkflowStore.setState({ loadStatus: "loaded" });
    // 无 title chart
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        chart_type: "scatter",
        data: [{ x: 1, y: 2 }],
        label: "g1",
        title: "", // 空 title
      })
    );
    const { rerender } = render(<ChartRenderer nodeId="n1" />);
    // 多次 rerender（partition 反复执行）
    rerender(<ChartRenderer nodeId="n1" />);
    rerender(<ChartRenderer nodeId="n1" />);
    rerender(<ChartRenderer nodeId="n1" />);
    const untitledWarns = warnSpy.mock.calls.filter((c) =>
      String(c[0] ?? "").includes("chart 缺 title")
    );
    expect(untitledWarns.length).toBe(1); // warn-once-per-identity
  });
});
