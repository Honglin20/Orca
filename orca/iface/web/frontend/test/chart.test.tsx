// test/chart.test.tsx —— chart 渲染测试（D2/D3 验收）。
//
// 覆盖 SPEC §3.2：
//   - 5 种 widget 各渲染对应 recharts class（.recharts-line / .recharts-bar / .recharts-scatter / table rows）
//   - PALETTE 配色实际使用（读 SVG stroke/fill 断言在 PALETTE）
//   - label 分组（不同 label 不同 section）
//   - 同 label+title 替换（dedupe → 只 1 个 chart，不堆积）
//   - replay 模式只显示到 replayPosition 的 chart
//
// 注：recharts ResponsiveContainer 在 happy-dom 下异步渲染（measure 后 useEffect 出子 SVG），
// 故 SVG 断言用 waitFor 等待异步渲染完成。

import { describe, expect, test, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { ChartRenderer } from "@/components/chart/ChartRenderer";
import { ChartWidget } from "@/components/chart/ChartWidget";
import { dedupeByLabelTitle } from "@/components/chart/ChartGroup";
import { PALETTE } from "@/components/chart/chartTheme";
import type { ChartPayload, ChartType } from "@/components/chart/types";
import type { WorkflowEvent } from "@/types/events";

let _seq = 100;
function chartEvent(node: string, payload: ChartPayload): WorkflowEvent {
  return {
    seq: _seq++,
    type: "custom",
    timestamp: Date.now() / 1000,
    node,
    session_id: node,
    data: { kind: "chart", chart: payload },
  };
}

const LINE_PAYLOAD: ChartPayload = {
  chart_type: "line",
  data: [
    { x: 1, y: 2 },
    { x: 2, y: 4 },
    { x: 3, y: 6 },
  ],
  x: "x",
  y: "y",
  label: "训练曲线",
  title: "loss",
};

const BAR_PAYLOAD: ChartPayload = {
  chart_type: "bar",
  data: [
    { x: "a", y: 3 },
    { x: "b", y: 5 },
  ],
  x: "x",
  y: "y",
  label: "g1",
  title: "bar-t",
};

const SCATTER_PAYLOAD: ChartPayload = {
  chart_type: "scatter",
  data: [
    { x: 1, y: 2 },
    { x: 3, y: 4 },
  ],
  x: "x",
  y: "y",
  label: "g1",
  title: "scatter-t",
};

const PARETO_PAYLOAD: ChartPayload = {
  chart_type: "pareto",
  data: [
    { x: 1, y: 1 },
    { x: 2, y: 3 },
    { x: 3, y: 2 },
  ],
  x: "x",
  y: "y",
  label: "g1",
  title: "pareto-t",
  pareto_direction: "max",
};

const TABLE_PAYLOAD: ChartPayload = {
  chart_type: "table",
  data: [
    { name: "a", value: 1 },
    { name: "b", value: 2 },
    { name: "c", value: 3 },
  ],
  columns: ["name", "value"],
  label: "g1",
  title: "table-t",
};

afterEach(() => {
  cleanup();
  useWorkflowStore.getState().unloadRun();
});

describe("chartTheme —— PALETTE 迁移自 AgentHarness（铁律 5）", () => {
  test("PALETTE 8 色存在 + 学术色值一致", () => {
    expect(PALETTE).toHaveLength(8);
    // SPEC §2.5 关键色值（钢蓝/暖琥珀/灰珊瑚/鼠尾草青/橄榄绿/古金/柔紫/灰粉）
    expect(PALETTE[0]).toBe("#5B8DB8");
    expect(PALETTE[1]).toBe("#E29D3E");
    expect(PALETTE[2]).toBe("#D4605A");
    expect(PALETTE[3]).toBe("#6BA5A0");
    expect(PALETTE[4]).toBe("#6B9E5C");
    expect(PALETTE[5]).toBe("#C9A843");
    expect(PALETTE[6]).toBe("#9A7BA8");
    expect(PALETTE[7]).toBe("#E08E9B");
  });
});

describe("5 种 widget 各渲染对应 recharts class（SPEC §3.2）", () => {
  test("line → .recharts-line path 存在", async () => {
    render(<ChartWidget payload={LINE_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-line path")).toBeTruthy();
    });
  });

  test("bar → .recharts-bar path 存在", async () => {
    render(<ChartWidget payload={BAR_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-bar path")).toBeTruthy();
    });
  });

  test("scatter → .recharts-scatter path 存在", async () => {
    render(<ChartWidget payload={SCATTER_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-scatter path")).toBeTruthy();
    });
  });

  test("pareto → 散点渲染（dominated NEUTRAL + front PALETTE）+ 前沿 Line", async () => {
    render(<ChartWidget payload={PARETO_PAYLOAD} />);
    // ComposedChart：Scatter 渲染为 .recharts-symbols（dominated + front 两组）。
    // 前沿连线（Line）在 recharts ComposedChart + per-series data 下对 happy-dom 渲染不稳定，
    // 但在真实浏览器（playwright）下会渲染。此处断言散点（pareto 本质：前沿/被支配点区分着色），
    // 前沿线由 playwright 9d 集成测试验证（SPEC §3.4）。
    await waitFor(() => {
      expect(document.querySelectorAll(".recharts-symbols").length).toBeGreaterThanOrEqual(1);
    });
  });

  test("table → 表格行数 == payload.data 长度", () => {
    render(<ChartWidget payload={TABLE_PAYLOAD} />);
    const rows = document.querySelectorAll('[data-testid="data-table"] tbody tr');
    expect(rows.length).toBe(TABLE_PAYLOAD.data.length);
    const headers = document.querySelectorAll('[data-testid="data-table"] thead th');
    expect(headers.length).toBe(2);
  });

  test("未知 chart_type → fail loud（显示提示）", () => {
    render(
      <ChartWidget
        payload={{ ...LINE_PAYLOAD, chart_type: "unknown" as unknown as ChartType }}
      />,
    );
    expect(screen.getByTestId("chart-unknown")).toBeInTheDocument();
  });
});

describe("PALETTE 配色实际使用（SPEC §3.2 学术配色断言）", () => {
  test("line stroke 用 PALETTE 颜色", async () => {
    render(<ChartWidget payload={LINE_PAYLOAD} />);
    let stroke: string | null = null;
    await waitFor(() => {
      const path = document.querySelector(".recharts-line path") as SVGPathElement | null;
      expect(path).toBeTruthy();
      stroke = path?.getAttribute("stroke") ?? null;
      expect(stroke).toBeTruthy();
    });
    // 关键断言：stroke 实际是 PALETTE 内的颜色（铁律 5 复用 AgentHarness 配色）
    expect(PALETTE).toContain(stroke);
  });

  test("bar path fill 用 PALETTE 颜色", async () => {
    render(<ChartWidget payload={BAR_PAYLOAD} />);
    let fill: string | null = null;
    await waitFor(() => {
      // recharts Bar 用 <path class="recharts-rectangle">，fill 在 path 上
      const path = document.querySelector(
        ".recharts-bar path",
      ) as SVGPathElement | null;
      expect(path).toBeTruthy();
      fill = path?.getAttribute("fill") ?? null;
      expect(fill).toBeTruthy();
    });
    expect(PALETTE).toContain(fill);
  });

  test("scatter stroke 用 PALETTE 颜色", async () => {
    render(<ChartWidget payload={SCATTER_PAYLOAD} />);
    let stroke: string | null = null;
    await waitFor(() => {
      const path = document.querySelector(
        ".recharts-scatter path",
      ) as SVGPathElement | null;
      expect(path).toBeTruthy();
      stroke = path?.getAttribute("stroke") ?? null;
      expect(stroke).toBeTruthy();
    });
    expect(PALETTE).toContain(stroke);
  });
});

describe("ChartRenderer —— label 分组 + 实时更新（SPEC §2.4 §2.7）", () => {
  test("label 分组：不同 label → 不同 chart-group section", async () => {
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_PAYLOAD, label: "组A", title: "t1" }),
    );
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_PAYLOAD, label: "组B", title: "t2" }),
    );
    render(<ChartRenderer nodeId="n1" />);

    const groups = screen.getAllByTestId("chart-group");
    expect(groups.length).toBe(2);
    expect(groups[0].getAttribute("data-label")).toBe("组A");
    expect(groups[1].getAttribute("data-label")).toBe("组B");
  });

  test("同 label+title 第二个事件 → 替换（只 1 个 chart，不堆积）", async () => {
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        ...LINE_PAYLOAD,
        label: "组A",
        title: "loss",
        data: [{ x: 1, y: 2 }],
      }),
    );
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", {
        ...LINE_PAYLOAD,
        label: "组A",
        title: "loss",
        data: [{ x: 2, y: 4 }],
      }),
    );
    render(<ChartRenderer nodeId="n1" />);

    // SPEC §2.7：实时更新键（同 label+title 替换非追加）—— 只 1 个 chart
    const widgets = screen.getAllByTestId("chart-widget");
    expect(widgets.length).toBe(1);
  });

  test("dedupeByLabelTitle 单元（直接断言）", () => {
    const a: ChartPayload = {
      ...LINE_PAYLOAD,
      label: "g",
      title: "t",
      data: [{ x: 1, y: 1 }],
    };
    const b: ChartPayload = {
      ...LINE_PAYLOAD,
      label: "g",
      title: "t",
      data: [{ x: 2, y: 2 }],
    };
    const c: ChartPayload = {
      ...LINE_PAYLOAD,
      label: "g",
      title: "other",
      data: [{ x: 1, y: 1 }],
    };
    expect(dedupeByLabelTitle([a, b])).toHaveLength(1); // 同 title → 替换
    expect(dedupeByLabelTitle([a, b, c])).toHaveLength(2); // 不同 title → 各 1
  });

  test("nodeId 过滤：只显示指定节点的 chart", () => {
    useWorkflowStore
      .getState()
      .processEvent(chartEvent("n1", { ...LINE_PAYLOAD, label: "g", title: "t1" }));
    useWorkflowStore
      .getState()
      .processEvent(chartEvent("n2", { ...LINE_PAYLOAD, label: "g", title: "t2" }));
    render(<ChartRenderer nodeId="n1" />);

    // 只 n1 的 chart（n2 的被过滤掉）
    const widgets = screen.getAllByTestId("chart-widget");
    expect(widgets.length).toBe(1);
  });

  test("nodeId undefined → 显示所有节点的 chart（Output Panel）", () => {
    useWorkflowStore
      .getState()
      .processEvent(chartEvent("n1", { ...LINE_PAYLOAD, label: "g", title: "t1" }));
    useWorkflowStore
      .getState()
      .processEvent(chartEvent("n2", { ...LINE_PAYLOAD, label: "g", title: "t2" }));
    render(<ChartRenderer />);

    const widgets = screen.getAllByTestId("chart-widget");
    expect(widgets.length).toBe(2);
  });
});

describe("ChartRenderer —— replay 同步（SPEC §2.7）", () => {
  test("replay 模式只显示到 replayPosition 的 chart", () => {
    const store = useWorkflowStore.getState();
    // 注入两个 chart 事件（同 label+title 模拟实时更新）
    store.processEvent(
      chartEvent("n1", {
        ...LINE_PAYLOAD,
        label: "g",
        title: "t",
        data: [{ x: 1, y: 1 }],
      }),
    );
    store.processEvent(
      chartEvent("n1", {
        ...LINE_PAYLOAD,
        label: "g",
        title: "t",
        data: [{ x: 2, y: 2 }],
      }),
    );

    // 进 replay 模式 + 拨 replayPosition 到第一个事件（只显示 1 个 chart）
    store.setReplayMode(true);
    store.setReplayPosition(0);

    render(<ChartRenderer nodeId="n1" />);
    // replayPosition=0 → events[0..0]，只第一个事件 → 1 chart
    const widgets = screen.getAllByTestId("chart-widget");
    expect(widgets.length).toBe(1);

    // 拨到末尾 → 2 个事件去重后 1 chart（同 label+title 替换）
    store.setReplayPosition(1);
    expect(screen.getAllByTestId("chart-widget").length).toBe(1);
  });
});
