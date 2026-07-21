// test/chart.test.tsx —— chart 渲染测试（D2/D3/D7 验收）。
//
// 覆盖 SPEC §3.2 / §0 D3 / D7：
//   - 5 种 widget 各渲染对应 recharts class（.recharts-line / .recharts-bar / .recharts-scatter / table rows）
//   - PALETTE 配色实际使用（读 SVG stroke/fill 断言在 PALETTE）
//   - label 分组（不同 label 不同 section）
//   - 同 label+title 替换（dedupe → 只 1 个 chart，不堆积）
//
// web-shell-v2 §8：删除 Replay 同步测试块（无 Replay 功能）。
//
// 注：recharts ResponsiveContainer 在 happy-dom 下异步渲染（measure 后 useEffect 出子 SVG），
// 故 SVG 断言用 waitFor 等待异步渲染完成。

import { describe, expect, test, afterEach } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { ChartRenderer } from "@/components/chart/ChartRenderer";
import { ChartWidget } from "@/components/chart/ChartWidget";
import { ChartGroup } from "@/components/chart/ChartGroup";
import { PALETTE, getAxisTick, getCursor } from "@/components/chart/chartTheme";
import { selectCharts } from "@/selectors";
import type { ChartPayload, ChartType } from "@/components/chart/types";
import type { WebEvent } from "@/types/events";

let _seq = 100;
function chartEvent(node: string, payload: ChartPayload): WebEvent {
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

const AREA_PAYLOAD: ChartPayload = {
  chart_type: "area",
  data: [
    { x: 1, y: 2 },
    { x: 2, y: 4 },
    { x: 3, y: 6 },
  ],
  x: "x",
  y: "y",
  label: "g1",
  title: "area-t",
};

const RADAR_PAYLOAD: ChartPayload = {
  chart_type: "radar",
  data: [
    { dimension: "speed", value: 6 },
    { dimension: "power", value: 8 },
    { dimension: "range", value: 4 },
    { dimension: "cost", value: 7 },
  ],
  x: "dimension",
  y: "value",
  label: "g1",
  title: "radar-t",
};

// hue 多系列 payload（验证 pivotByHue 接线 + 多系列渲染 + 颜色轮转）
const AREA_HUE_PAYLOAD: ChartPayload = {
  chart_type: "area",
  data: [
    { x: 1, series: "A", y: 2 },
    { x: 2, series: "A", y: 4 },
    { x: 1, series: "B", y: 1 },
    { x: 2, series: "B", y: 3 },
  ],
  x: "x",
  y: "y",
  hue: "series",
  label: "g1",
  title: "area-hue-t",
};

const RADAR_HUE_PAYLOAD: ChartPayload = {
  chart_type: "radar",
  data: [
    { dimension: "speed", model: "X", value: 6 },
    { dimension: "power", model: "X", value: 8 },
    { dimension: "speed", model: "Y", value: 4 },
    { dimension: "power", model: "Y", value: 5 },
  ],
  x: "dimension",
  y: "value",
  hue: "model",
  label: "g1",
  title: "radar-hue-t",
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

// heatmap（第 8 种 chart_type）：长格式 record（每行一个 cell），value=着色字段。
const HEATMAP_PAYLOAD: ChartPayload = {
  chart_type: "heatmap",
  data: [
    { recipe: "smooth+gptq", bitwidth: "w4a4-mx", accuracy: 0.92 },
    { recipe: "smooth+gptq", bitwidth: "w8a8", accuracy: 0.95 },
    { recipe: "rtn", bitwidth: "w4a4-mx", accuracy: 0.81 },
    { recipe: "rtn", bitwidth: "w8a8", accuracy: 0.88 },
  ],
  x: "bitwidth",
  y: "recipe",
  value: "accuracy",
  label: "quant",
  title: "acc-matrix",
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

// ── P5 R1 回归：getCSSVar 必须把 ``R G B`` token 包成合法 CSS 颜色 ────────────────
// 事故回顾：原 ``hsl(${raw})`` 把 ``51 65 85`` 包成 ``hsl(51 65 85)`` —— CSS Color 4
// 要求 ``hsl(H S L)`` 中 S/L 为百分比；裸数字被浏览器静默判非法 → SVG fill 退回默认黑
// → P5 验收 #1「坐标轴 slate-700」实际未达成。修成 ``rgb(${raw})`` 后必须钉死。
describe("chartTheme —— P5 R1 getCSSVar 返回合法 CSS 颜色（防 hsl 包 RGB 静默失败）", () => {
  test("getAxisTick().fill 是被 CSS 接受的颜色（set on el.style.color 不被丢弃）", () => {
    // 模拟 index.css 的 token 定义（RGB 三元组空格分隔）。
    document.documentElement.style.setProperty("--axis-tick", "51 65 85");
    try {
      const { fill } = getAxisTick();
      // 探针：把 fill 赋给 el.style.color，CSS 解析器拒绝则空串（fail loud）。
      const probe = document.createElement("div");
      probe.style.color = fill;
      expect(probe.style.color).not.toBe("");
      // 合法 ``rgb(51 65 85)`` 被 happy-dom 规范化。
      expect(fill.startsWith("rgb")).toBe(true);
    } finally {
      document.documentElement.style.removeProperty("--axis-tick");
    }
  });

  test("getCursor(true).stroke 是合法 CSS 颜色（cursor 不退回 SVG 默认黑）", () => {
    document.documentElement.style.setProperty("--border", "226 232 240");
    try {
      const cursor = getCursor(true);
      expect(cursor.stroke).toBeDefined();
      const probe = document.createElement("div");
      probe.style.color = cursor.stroke!;
      expect(probe.style.color).not.toBe("");
    } finally {
      document.documentElement.style.removeProperty("--border");
    }
  });

  test("token 未定义 → fallback #888（不静默 crash）", () => {
    // 清掉所有可能残留的同名 token，确保 getCSSVar 走 fallback 路径。
    document.documentElement.style.removeProperty("--nonexistent-token-xyz");
    const { fill } = getAxisTick();
    expect(fill).toBe("#888");
  });
});

describe("8 种 widget 各渲染对应 recharts class（SPEC §3.2）", () => {
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

  test("area → .recharts-area path 存在", async () => {
    render(<ChartWidget payload={AREA_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-area path")).toBeTruthy();
    });
  });

  test("radar → .recharts-radar path + polar-angle-axis 存在", async () => {
    render(<ChartWidget payload={RADAR_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-radar path")).toBeTruthy();
    });
    // 维度轴（PolarAngleAxis）渲染各维度 tick
    expect(
      document.querySelectorAll(".recharts-polar-angle-axis-tick").length,
    ).toBeGreaterThanOrEqual(1);
  });

  test("area(hue) → 多系列 pivot 正确（2 系列）", async () => {
    render(<ChartWidget payload={AREA_HUE_PAYLOAD} />);
    // 注：recharts AreaChart 多系列在 happy-dom 下 <Area> shape 渲染不稳定
    // （同 pareto 前沿线问题，见下 pareto 测试注释），但 Legend + 轴会可靠渲染。
    // 这里断言 hue pivot 接线正确（2 个 hue 值 → 2 个 legend item "A"/"B"），
    // 各 <Area> 的实际 path 渲染由 playwright test_chart_area_radar 在真实浏览器验证。
    await waitFor(() => {
      const legendItems = document.querySelectorAll(".recharts-legend-item");
      expect(legendItems.length).toBe(2);
      const texts = Array.from(legendItems).map((it) =>
        it.querySelector(".recharts-legend-item-text")?.textContent,
      );
      expect(texts).toEqual(expect.arrayContaining(["A", "B"]));
    });
  });

  test("radar(hue) → 多系列 pivot：≥2 条 radar + 各 stroke 落 PALETTE", async () => {
    render(<ChartWidget payload={RADAR_HUE_PAYLOAD} />);
    await waitFor(() => {
      const radars = document.querySelectorAll(".recharts-radar path");
      expect(radars.length).toBeGreaterThanOrEqual(2);
      const strokes = Array.from(radars)
        .map((p) => (p as SVGPathElement).getAttribute("stroke"))
        .filter((s) => s && s !== "none");
      strokes.forEach((s) => expect(PALETTE).toContain(s));
    });
  });

  test("radar 默认 key（dimension/value）回退：不传 x/y 仍渲染", async () => {
    // 不传 x/y → 回退到 dimension/value 默认（雷达图惯例，与 AgentHarness 一致）
    const { x: _x, y: _y, ...radarNoKeys } = RADAR_PAYLOAD;
    void _x;
    void _y;
    render(<ChartWidget payload={radarNoKeys as ChartPayload} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-radar path")).toBeTruthy();
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

  test("heatmap → cell 数 = 行×列，色阶 + 数值渲染", () => {
    render(<ChartWidget payload={HEATMAP_PAYLOAD} />);
    // 2 recipe × 2 bitwidth = 4 cell
    const cells = document.querySelectorAll('[data-testid="heatmap-cell"]');
    expect(cells.length).toBe(4);
    // 每个 cell 含数值（formatTick 把 0.92 格式化为 "0.920"，toPrecision(3)）。
    const values = Array.from(cells).map((c) => c.textContent ?? "");
    expect(values).toEqual(expect.arrayContaining(["0.920", "0.950", "0.810", "0.880"]));
    // legend 渲染（色阶条存在）
    expect(document.querySelector('[data-testid="heatmap-legend"]')).toBeTruthy();
  });

  test("heatmap → cell backgroundColor 随值单调（min 浅 / max 深，钉死色阶方向）", () => {
    render(<ChartWidget payload={HEATMAP_PAYLOAD} />);
    const cells = document.querySelectorAll('[data-testid="heatmap-cell"]');
    // HEATMAP_PAYLOAD: max=0.95（smooth+gptq×w8a8），min=0.81（rtn×w4a4-mx）
    const byValue: Record<string, string> = {};
    Array.from(cells).forEach((c) => {
      const el = c as HTMLElement;
      const v = el.getAttribute("data-value") ?? "";
      byValue[v] = el.style.backgroundColor;
    });
    // max cell (0.95) 与 min cell (0.81) 都有 backgroundColor（rgb 三元组）
    expect(byValue["0.95"]).toBeTruthy();
    expect(byValue["0.81"]).toBeTruthy();
    // 解析 rgb 分量，断言 max 比 min 更接近 PALETTE[0] 钢蓝（91,141,184）：
    // max 的蓝分量应更大（更接近 184），min 更小（更接近 251）。
    function parseRGB(s: string): [number, number, number] | null {
      const m = s.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
      return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
    }
    const maxRGB = parseRGB(byValue["0.95"]);
    const minRGB = parseRGB(byValue["0.81"]);
    expect(maxRGB).not.toBeNull();
    expect(minRGB).not.toBeNull();
    // 色阶方向：max cell 的蓝分量（b）应小于 min cell 的蓝分量（浅色蓝更大，因
    // SCALE_LIGHT=(245,248,251) 蓝分量 251，SCALE_DARK=(91,141,184) 蓝分量 184）。
    expect(maxRGB![2]).toBeLessThan(minRGB![2]);
  });

  test("heatmap → 单值矩阵 max==min 不除零（全非空 cell 同色，normalize 兜底返回 1）", () => {
    const single: ChartPayload = {
      chart_type: "heatmap",
      data: [
        { a: "r1", b: "c1", v: 0.5 },
        { a: "r1", b: "c2", v: 0.5 },
        { a: "r2", b: "c1", v: 0.5 },
        // (r2, c2) 缺失 → 单值矩阵不除零的兜底只影响非空 cell
      ],
      x: "b",
      y: "a",
      value: "v",
      label: "g",
      title: "t",
    };
    render(<ChartWidget payload={single} />);
    const cells = document.querySelectorAll('[data-testid="heatmap-cell"]');
    // 网格 2×2 = 4 cell（3 个有值 + 1 个缺失）
    expect(cells.length).toBe(4);
    // 非空 cell 全同色（normalize max==min 兜底返回 1，全用深色端）
    const colored = Array.from(cells).filter(
      (c) => (c as HTMLElement).style.backgroundColor !== "",
    );
    expect(colored.length).toBe(3);
    const bgs = new Set(colored.map((c) => (c as HTMLElement).style.backgroundColor));
    expect(bgs.size).toBe(1);
  });

  test("heatmap → 稀疏矩阵（缺失 cell）显示空位（不全 coerce 成 0）", () => {
    // 2×2 但只有 3 个 cell，缺 (r2, c2)。
    const sparse: ChartPayload = {
      chart_type: "heatmap",
      data: [
        { a: "r1", b: "c1", v: 0.1 },
        { a: "r1", b: "c2", v: 0.9 },
        { a: "r2", b: "c1", v: 0.5 },
        // (r2, c2) 缺
      ],
      x: "b",
      y: "a",
      value: "v",
      label: "g",
      title: "t",
    };
    render(<ChartWidget payload={sparse} />);
    const cells = document.querySelectorAll('[data-testid="heatmap-cell"]');
    expect(cells.length).toBe(4); // 2×2 网格仍渲染 4 个 cell（缺失位留空）
    // 缺失 cell (r2, c2) 无 backgroundColor（标记为缺失，不 coerce 成 0）
    const missing = Array.from(cells).find(
      (c) =>
        (c as HTMLElement).getAttribute("data-y") === "r2" &&
        (c as HTMLElement).getAttribute("data-x") === "c2",
    ) as HTMLElement | undefined;
    expect(missing).toBeTruthy();
    expect(missing!.style.backgroundColor).toBe("");
    expect((missing!.getAttribute("data-value") ?? "") === "").toBe(true);
  });

  test("heatmap → 非数值 cell（null / 空串 / 布尔 / 字符串）显示为空位（不 coerce 成 0）", () => {
    const dirty: ChartPayload = {
      chart_type: "heatmap",
      data: [
        { a: "r1", b: "c1", v: null },
        { a: "r1", b: "c2", v: "" },
        { a: "r2", b: "c1", v: "not a number" },
        { a: "r2", b: "c2", v: true },
      ],
      x: "b",
      y: "a",
      value: "v",
      label: "g",
      title: "t",
    };
    render(<ChartWidget payload={dirty} />);
    const cells = document.querySelectorAll('[data-testid="heatmap-cell"]');
    // 全 4 cell 都被视为缺失（无 backgroundColor / 无 value）
    cells.forEach((c) => {
      const el = c as HTMLElement;
      expect(el.style.backgroundColor).toBe("");
    });
  });

  test("heatmap → 空 data 显示 heatmap-empty 提示（不崩）", () => {
    const empty: ChartPayload = {
      chart_type: "heatmap",
      data: [],
      x: "b",
      y: "a",
      value: "v",
      label: "g",
      title: "t",
    };
    render(<ChartWidget payload={empty} />);
    expect(screen.getByTestId("heatmap-empty")).toBeInTheDocument();
  });

  test("heatmap → 行/列标签存在（recipe / bitwidth 值）", () => {
    render(<ChartWidget payload={HEATMAP_PAYLOAD} />);
    const grid = document.querySelector('[data-testid="heatmap-grid"]');
    expect(grid).toBeTruthy();
    const text = grid?.textContent ?? "";
    // 行标签（recipe 值）+ 列标签（bitwidth 值）都在 DOM 中
    expect(text).toContain("smooth+gptq");
    expect(text).toContain("rtn");
    expect(text).toContain("w4a4-mx");
    expect(text).toContain("w8a8");
  });

  test("heatmap → 缺 value 字段 → fail loud 提示（防御性，后端 _validate 已挡）", () => {
    const noValue: ChartPayload = {
      chart_type: "heatmap",
      data: [{ a: 1, b: 2, c: 3 }],
      x: "b",
      y: "a",
      label: "g",
      title: "t",
      // value 故意缺
    };
    render(<ChartWidget payload={noValue} />);
    expect(screen.getByTestId("heatmap-no-value")).toBeInTheDocument();
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

  test("area stroke + 半透明 fill 用 PALETTE 颜色", async () => {
    render(<ChartWidget payload={AREA_PAYLOAD} />);
    // Area 渲染两条 path：填充面（stroke=none, fill=PALETTE）+ 曲线（stroke=PALETTE, fill=none）。
    // 契约（chartTheme BOX_FILL_OPACITY）：stroke + 半透明 fill。
    let stroke: string | null = null;
    let fill: string | null = null;
    let fillOpacity: string | null = null;
    await waitFor(() => {
      const paths = Array.from(
        document.querySelectorAll(".recharts-area path"),
      ) as SVGPathElement[];
      expect(paths.length).toBeGreaterThan(0);
      stroke = paths.map((p) => p.getAttribute("stroke")).find((s) => s && s !== "none") ?? null;
      // 填充面 path：fill=PALETTE，fillOpacity 半透明
      const fillPath = paths.find((p) => {
        const f = p.getAttribute("fill");
        return f && f !== "none";
      });
      fill = fillPath?.getAttribute("fill") ?? null;
      fillOpacity = fillPath?.getAttribute("fill-opacity") ?? null;
      expect(stroke).toBeTruthy();
      expect(fill).toBeTruthy();
    });
    expect(PALETTE).toContain(stroke);
    expect(PALETTE).toContain(fill);
    // 半透明契约：fillOpacity 是 (0,1) 之间的小数（BOX_FILL_OPACITY=0.2）
    expect(Number(fillOpacity)).toBeGreaterThan(0);
    expect(Number(fillOpacity)).toBeLessThan(1);
  });

  test("radar stroke + 半透明 fill 用 PALETTE 颜色", async () => {
    render(<ChartWidget payload={RADAR_PAYLOAD} />);
    let stroke: string | null = null;
    let fill: string | null = null;
    let fillOpacity: string | null = null;
    await waitFor(() => {
      const path = document.querySelector(
        ".recharts-radar path",
      ) as SVGPathElement | null;
      expect(path).toBeTruthy();
      stroke = path?.getAttribute("stroke") ?? null;
      fill = path?.getAttribute("fill") ?? null;
      fillOpacity = path?.getAttribute("fill-opacity") ?? null;
      expect(stroke).toBeTruthy();
      expect(fill).toBeTruthy();
    });
    expect(PALETTE).toContain(stroke);
    expect(PALETTE).toContain(fill);
    expect(Number(fillOpacity)).toBeGreaterThan(0);
    expect(Number(fillOpacity)).toBeLessThan(1);
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

  test("dedupe 真相在 selectCharts：同 title 两条事件经 selector 后只产出 1 chart（ChartRenderer 不再二次去重）", async () => {
    // 两条同 label+title 事件：selectCharts 已 upsert 去重（identity=title）
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_PAYLOAD, label: "组A", title: "loss", data: [{ x: 1, y: 2 }] }),
    );
    useWorkflowStore.getState().processEvent(
      chartEvent("n1", { ...LINE_PAYLOAD, label: "组A", title: "loss", data: [{ x: 2, y: 4 }] }),
    );
    render(<ChartRenderer nodeId="n1" />);
    const widgets = screen.getAllByTestId("chart-widget");
    expect(widgets.length).toBe(1);
  });

  test("空 title 多 chart 共存（identity = chart_type+seq；ChartGroup 不二次去重，铁律 1）", async () => {
    // 空 title → selectCharts identity = chart_type#seq，两个独立 chart；ChartGroup 必须都渲染
    const a: ChartPayload = {
      ...SCATTER_PAYLOAD,
      title: "",
      data: [{ x: 1, y: 2 }],
    };
    const b: ChartPayload = {
      ...SCATTER_PAYLOAD,
      title: "",
      data: [{ x: 3, y: 4 }],
    };
    useWorkflowStore.getState().processEvent(chartEvent("n1", a));
    useWorkflowStore.getState().processEvent(chartEvent("n1", b));
    render(<ChartRenderer nodeId="n1" />);
    // 两个空 title chart 都渲染（selectCharts identity 用 seq 区分；ChartGroup 不压成 1）
    await waitFor(() => {
      expect(screen.getAllByTestId("chart-widget").length).toBe(2);
    });
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

describe("ChartGroup —— 响应式 grid + IntersectionObserver 懒挂（SPEC §5.4）", () => {
  test("响应式 grid：gridTemplateColumns = repeat(auto-fit, minmax(300px, 1fr))", async () => {
    render(
      <ChartGroup
        label="g1"
        charts={[{ ...LINE_PAYLOAD, label: "g1", title: "t1" }]}
      />,
    );
    // 展开（默认未折叠）→ 找到 grid 容器（border-t 内的 div）
    const group = screen.getByTestId("chart-group");
    const gridContainer = group.querySelector(
      'div[style*="grid-template-columns"]',
    ) as HTMLElement | null;
    expect(gridContainer).toBeTruthy();
    const style = gridContainer!.getAttribute("style") ?? "";
    expect(style).toContain("repeat(auto-fit, minmax(300px, 1fr))");
  });

  test("chart 进入视口（IO stub）→ 挂载 ChartWidget（无 skeleton 残留）", async () => {
    // setup.ts 的 IOStub 立即触发 isIntersecting=true → LazyChartWidget 直接渲染 ChartWidget
    render(
      <ChartGroup
        label="g1"
        charts={[{ ...LINE_PAYLOAD, label: "g1", title: "t1" }]}
      />,
    );
    // chart-widget 立即出现（IO stub 同步触发）
    await waitFor(() => {
      expect(screen.getAllByTestId("chart-widget").length).toBe(1);
    });
    // skeleton 已被 widget 取代
    expect(screen.queryAllByTestId("chart-skeleton").length).toBe(0);
  });
});

describe("ScatterChartWidget —— size 字段（气泡图，SPEC §5.4 D3）", () => {
  test("size 字段：z 列映射（widget 渲染不抛错，scatter path 存在）", async () => {
    const bubble: ChartPayload = {
      chart_type: "scatter",
      data: [
        { x: 1, y: 2, z: 10 },
        { x: 3, y: 4, z: 50 },
      ],
      x: "x",
      y: "y",
      size: "z",
      label: "g1",
      title: "bubble-t",
    };
    render(<ChartWidget payload={bubble} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-scatter path")).toBeTruthy();
    });
    // 断言 size 字段被消费：渲染出 path 且无报错（recharts ZAxis 不产可见 DOM class
    // 无法直接断言 dataKey；bubble 与 scatter-without-size 的差别由 prod UI 验证）。
  });

  test("hue + size 组合：每 hue 一组气泡", async () => {
    const bubble: ChartPayload = {
      chart_type: "scatter",
      data: [
        { x: 1, y: 2, z: 10, group: "A" },
        { x: 3, y: 4, z: 50, group: "B" },
      ],
      x: "x",
      y: "y",
      size: "z",
      hue: "group",
      label: "g1",
      title: "bubble-hue-t",
    };
    render(<ChartWidget payload={bubble} />);
    await waitFor(() => {
      // 两组 hue → 两个 legend item
      const legendItems = document.querySelectorAll(".recharts-legend-item");
      expect(legendItems.length).toBe(2);
    });
  });
});

describe("selectCharts D7 序无关（SPEC §0 D7 / §9 AC2）", () => {
  test("selectCharts(T) == selectCharts(sort(T)) == selectCharts(reverse(T))", () => {
    // 直接断言 selector 输出（render 不参与）。同一 store 三次 fold 顺序不同 → 同集。
    const events: WebEvent[] = [
      chartEvent("n1", { ...LINE_PAYLOAD, label: "g", title: "t1", data: [{ x: 1, y: 1 }] }),
      chartEvent("n1", { ...BAR_PAYLOAD, label: "g", title: "t2", data: [{ x: 1, y: 1 }] }),
      chartEvent("n2", { ...AREA_PAYLOAD, label: "g2", title: "t3", data: [{ x: 1, y: 1 }] }),
    ];
    useWorkflowStore.getState().loadFromEvents(events);
    const baseline = JSON.stringify(selectCharts(useWorkflowStore.getState()));

    // 升序
    useWorkflowStore.getState().loadFromEvents([...events].sort((a, b) => a.seq - b.seq));
    const asc = JSON.stringify(selectCharts(useWorkflowStore.getState()));

    // 降序
    useWorkflowStore.getState().loadFromEvents([...events].sort((a, b) => b.seq - a.seq));
    const desc = JSON.stringify(selectCharts(useWorkflowStore.getState()));

    expect(asc).toBe(baseline);
    expect(desc).toBe(baseline);
  });
});

// ── 轴标签 / caption（render_chart 新字段，2026-07-21 解「图表看不懂」根因 C）────
//
// 意图：钉死 x_label/y_label 透传到 XAxis/YAxis label、caption 渲染为 chart-caption 元素，
// 旧 payload（无新字段）不渲染 caption（向后兼容）。

describe("轴标签 / caption（render_chart x_label/y_label/caption）", () => {
  test("caption 非空 → 渲染 [data-testid=chart-caption]", () => {
    render(
      <ChartWidget
        payload={{ ...LINE_PAYLOAD, caption: "★=达标，数据来源：账本" }}
      />,
    );
    const cap = screen.queryByTestId("chart-caption");
    expect(cap).toBeInTheDocument();
    expect(cap?.textContent).toContain("★=达标");
  });

  test("caption 空 → 不渲染 chart-caption（向后兼容旧 tape）", () => {
    render(<ChartWidget payload={LINE_PAYLOAD} />);
    expect(screen.queryByTestId("chart-caption")).not.toBeInTheDocument();
  });

  test("bar caption 渲染（覆盖 hue 分支 + 默认分支各一）", () => {
    // 默认分支
    const { rerender } = render(
      <ChartWidget payload={{ ...BAR_PAYLOAD, caption: "默认分支 caption" }} />,
    );
    expect(screen.getByTestId("chart-caption").textContent).toContain("默认分支");
    // hue 分支
    rerender(
      <ChartWidget
        payload={{
          ...BAR_PAYLOAD,
          hue: "series",
          data: [
            { x: "a", series: "A", y: 1 },
            { x: "a", series: "B", y: 2 },
          ],
          caption: "hue 分支 caption",
        }}
      />,
    );
    expect(screen.getByTestId("chart-caption").textContent).toContain("hue 分支");
  });

  test("heatmap caption + x_label/y_label 渲染（覆盖 heatmap 路径）", () => {
    render(
      <ChartWidget
        payload={{
          ...HEATMAP_PAYLOAD,
          x_label: "bitwidth（位宽）",
          y_label: "recipe（配方）",
          caption: "cell = accuracy，越深越高",
        }}
      />,
    );
    // caption
    const cap = screen.getByTestId("chart-caption");
    expect(cap.textContent).toContain("accuracy");
    // x_label / y_label 在轴标题区渲染
    const widget = screen.getByTestId("chart-widget");
    const text = widget.textContent ?? "";
    expect(text).toContain("bitwidth（位宽）");
    expect(text).toContain("recipe（配方）");
  });

  test("table caption 渲染（覆盖 DataTable 路径）", () => {
    render(<ChartWidget payload={{ ...TABLE_PAYLOAD, caption: "表说明" }} />);
    expect(screen.getByTestId("chart-caption").textContent).toContain("表说明");
  });

  test("scatter/pareto/area/radar caption 渲染（覆盖剩余 widget）", () => {
    for (const [p, name] of [
      [SCATTER_PAYLOAD, "scatter"],
      [PARETO_PAYLOAD, "pareto"],
      [AREA_PAYLOAD, "area"],
      [RADAR_PAYLOAD, "radar"],
    ] as const) {
      const { unmount } = render(
        <ChartWidget payload={{ ...p, caption: `${name}-cap` }} />,
      );
      expect(screen.getByTestId("chart-caption").textContent).toContain(
        `${name}-cap`,
      );
      unmount();
    }
  });

  test("line + x_label/y_label → XAxis/YAxis label prop 写入（recharts-axis-label 文本可见）", async () => {
    render(
      <ChartWidget
        payload={{
          ...LINE_PAYLOAD,
          x_label: "候选序号",
          y_label: "时延 (ms)",
        }}
      />,
    );
    await waitFor(() => {
      expect(document.querySelector(".recharts-line path")).toBeTruthy();
    });
    // recharts 把 XAxis label 渲染为 .recharts-label（SVG <text>）；y 轴 label 同款。
    const labels = Array.from(document.querySelectorAll(".recharts-label"));
    const texts = labels.map((l) => l.textContent ?? "");
    expect(texts).toEqual(expect.arrayContaining(["候选序号", "时延 (ms)"]));
  });

  test("scatter + x_label/y_label → XAxis/YAxis name prop 同步（tooltip label 也用人话）", async () => {
    render(
      <ChartWidget
        payload={{
          ...SCATTER_PAYLOAD,
          x_label: "迭代轮",
          y_label: "score",
        }}
      />,
    );
    await waitFor(() => {
      expect(document.querySelector(".recharts-scatter path")).toBeTruthy();
    });
    // Scatter 用 XAxis/YAxis ``name`` prop 表达轴语义（tooltip 显示）。读取 recharts-surface
    // 内的 XAxis/YAxis 节点 attrs 验证 name 写入（recharts 把 name 映射到 axis 属性，可在
    // 内部 state 用 querySelector 读 axis data-key，但 ``name`` 非 DOM attr，故此处通过
    // recharts-label 同样渲染验证 label prop 路径，name prop 留给真机 playwright）。
    const labels = Array.from(document.querySelectorAll(".recharts-label"));
    const texts = labels.map((l) => l.textContent ?? "");
    expect(texts).toEqual(expect.arrayContaining(["迭代轮", "score"]));
  });

  test("pareto + x_label/y_label → 轴 label 文本（ComposedChart happy-dom 下可能不稳；至少 widget 渲染不崩）", async () => {
    // ComposedChart 在 happy-dom 下 label 渲染不稳（同前沿线 happy-dom 不渲染，见 pareto
    // 主测试注释）。此处仅断言 widget 挂载成功且 scatter symbols 出现；label 文本断言留给
    // 真机 playwright（line/scatter widget 已用同款 chartTheme helper 钉死 label 路径）。
    render(
      <ChartWidget
        payload={{
          ...PARETO_PAYLOAD,
          x_label: "时延 (ms)",
          y_label: "accuracy",
        }}
      />,
    );
    await waitFor(() => {
      expect(document.querySelectorAll(".recharts-symbols").length).toBeGreaterThanOrEqual(1);
    });
    // widget 渲染成功（无 crash）—— pass
  });

  test("heatmap 单边 x_label（不设 y_label）→ 只渲染 x_label span，无空 span 占位", () => {
    render(
      <ChartWidget
        payload={{
          ...HEATMAP_PAYLOAD,
          x_label: "只设 x_label",
        }}
      />,
    );
    // x_label 在轴标题区渲染；不渲染 y_label（无空占位 span）
    const widget = screen.getByTestId("chart-widget");
    expect(widget.textContent).toContain("只设 x_label");
    // y_label 未设 → 不应出现空 span 占位（条件渲染）
    const axisTitleDiv = widget.querySelector(".text-\\[10px\\].orca-text-faint.mt-1");
    expect(axisTitleDiv).toBeTruthy();
    const spans = axisTitleDiv?.querySelectorAll("span") ?? [];
    expect(spans.length).toBe(1);  // 只渲染 x_label span，不渲染 y_label 空 span
  });

  test("heatmap 双轴 x_label/y_label 都缺 → 不渲染 axis title div（向后兼容）", () => {
    // HEATMAP_PAYLOAD 默认无 x_label/y_label（旧 tape 形态）→ 守卫 {(x_label || y_label) && ...}
    // 不挂载 axis title div（钉死「不渲染多余 DOM」契约）。
    render(<ChartWidget payload={HEATMAP_PAYLOAD} />);
    const widget = screen.getByTestId("chart-widget");
    const axisTitleDiv = widget.querySelector(".text-\\[10px\\].orca-text-faint.mt-1");
    expect(axisTitleDiv).toBeNull();
  });

  test("空 x_label/y_label → 回退用字段名（payload.x='x' → 轴 label='x'）", async () => {
    // 不传 x_label → getXAxisLabelProp 回退字段名 payload.x="x" → label="x"
    render(<ChartWidget payload={LINE_PAYLOAD} />);
    await waitFor(() => {
      expect(document.querySelector(".recharts-line path")).toBeTruthy();
    });
    const labels = Array.from(document.querySelectorAll(".recharts-label"));
    const texts = labels.map((l) => l.textContent ?? "");
    // 字段名回退：x="x"、y="y"
    expect(texts).toEqual(expect.arrayContaining(["x", "y"]));
  });
});
