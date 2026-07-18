// test/topbar.test.tsx —— TopBar（SPEC §5.1 / §0 D5）。
//
// 覆盖：
//   - status icon 5 档 + cancelled/blocked
//   - D5 elapsed：running 时 live tick（now - workflowStartedAt）；
//     completed 时 snap workflowElapsed（不再依赖 now）
//
// P5a：cost UI 已移除（``top-cost`` testid 删除，store.cost fold 保留不破坏 agent_usage
// 累加测试）。原 cost 测试块同步删除——见 store.test.ts 对 cost fold 的覆盖。

import { describe, expect, test, afterEach, beforeEach } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { TopBar } from "@/components/layout/TopBar";
import { useWorkflowStore } from "@/stores/workflow-store";
import {
  useElapsedTickActive,
  __testReset,
  __testTick,
} from "@/hooks/use-elapsed-tick";
import type { WebEvent } from "@/types/events";

let _seq = 1;
function ev(type: WebEvent["type"], data: Record<string, unknown>): void {
  useWorkflowStore.getState().processEvent({
    seq: _seq++,
    type,
    timestamp: 100 + _seq,
    node: null,
    session_id: null,
    data,
  });
}

beforeEach(() => {
  __testReset();
  useWorkflowStore.getState().unloadRun();
  _seq = 1;
});

afterEach(() => {
  cleanup();
  __testReset();
  useWorkflowStore.getState().unloadRun();
});

// 把 TopBar 包在 useElapsedTickActive 容器里（模拟 RunDetailPage 行为）。
function Root({ active }: { active: boolean }) {
  useElapsedTickActive(active);
  return <TopBar runId="abc12345" />;
}

describe("TopBar —— status icon 5 档（SPEC §5.1）", () => {
  test("idle → ○", () => {
    render(<Root active={false} />);
    expect(screen.getByTestId("top-status").textContent).toContain("idle");
  });

  test("running → ●", () => {
    ev("workflow_started", { workflow_name: "wf" });
    render(<Root active={true} />);
    const status = screen.getByTestId("top-status");
    expect(status.textContent).toContain("running");
    // P1：emoji ● → lucide <StatusIcon/>（svg），配色 text-orca-running
    expect(status.querySelector("svg")).toBeInTheDocument();
    expect(status.className).toContain("text-orca-running");
  });

  test("completed → ✓", () => {
    ev("workflow_started", { workflow_name: "wf" });
    ev("workflow_completed", { elapsed: 30 });
    render(<Root active={false} />);
    const status = screen.getByTestId("top-status");
    expect(status.textContent).toContain("completed");
    expect(status.querySelector("svg")).toBeInTheDocument();
    expect(status.className).toContain("text-orca-done");
  });

  test("failed → ✗", () => {
    ev("workflow_started", { workflow_name: "wf" });
    ev("workflow_failed", { message: "boom" });
    render(<Root active={false} />);
    const status = screen.getByTestId("top-status");
    expect(status.textContent).toContain("failed");
    expect(status.querySelector("svg")).toBeInTheDocument();
    expect(status.className).toContain("text-orca-failed");
  });

  test("cancelled → ⊘", () => {
    ev("workflow_started", { workflow_name: "wf" });
    ev("workflow_cancelled", { reason: "user" });
    render(<Root active={false} />);
    const status = screen.getByTestId("top-status");
    expect(status.textContent).toContain("cancelled");
    expect(status.querySelector("svg")).toBeInTheDocument();
    expect(status.className).toContain("text-orca-pending");
  });
});

describe("TopBar —— D5 elapsed snap 语义（SPEC §0 D5）", () => {
  test("running 时 elapsed 随 tick 增长（live wall-clock）", () => {
    // workflow_started.ts = 100s（timestamp 字段）
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    render(<Root active={true} />);
    // 初次渲染：now = Date.now()/1000（远大于 100）→ elapsed 显示大数。
    const beforeEl = screen.getByTestId("top-elapsed");
    const beforeText = beforeEl.textContent ?? "";
    // P1：⏱ emoji → lucide <Timer/>（svg）。断言图标存在 + 有数值（非 "—"）。
    expect(beforeEl.querySelector("svg")).toBeInTheDocument();
    expect(beforeText).not.toContain("—");
    // 提取数值（如 "12345.6s"）
    const beforeSec = parseFloat(beforeText.replace(/[\s]/g, ""));
    expect(beforeSec).toBeGreaterThan(0);
    // 触发 tick（绕过 setInterval）→ now 增长 → elapsed 数值更大
    act(() => {
      __testTick();
    });
    const afterText = screen.getByTestId("top-elapsed").textContent ?? "";
    const afterSec = parseFloat(afterText.replace(/[\s]/g, ""));
    expect(afterSec).toBeGreaterThanOrEqual(beforeSec);
  });

  test("completed 时 elapsed snap = workflow_completed.data.elapsed（不再随 now 变）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "workflow_completed",
      timestamp: 130,
      node: null,
      session_id: null,
      data: { elapsed: 30 },
    });
    render(<Root active={false} />);
    const text = screen.getByTestId("top-elapsed").textContent ?? "";
    expect(text).toContain("30.0s");
    // 手动触发 tick 也不变（snap）
    act(() => {
      __testTick();
    });
    expect(screen.getByTestId("top-elapsed").textContent ?? "").toContain("30.0s");
  });

  test("未启动 → —", () => {
    render(<Root active={false} />);
    expect(screen.getByTestId("top-elapsed").textContent).toContain("—");
  });

  test("failed → snap（不丢 elapsed）", () => {
    useWorkflowStore.getState().processEvent({
      seq: 1,
      type: "workflow_started",
      timestamp: 100,
      node: null,
      session_id: null,
      data: { workflow_name: "wf" },
    });
    useWorkflowStore.getState().processEvent({
      seq: 2,
      type: "workflow_failed",
      timestamp: 130,
      node: null,
      session_id: null,
      data: { message: "boom" },
    });
    render(<Root active={false} />);
    // 130 - 100 = 30s
    expect(screen.getByTestId("top-elapsed").textContent).toContain("30.0s");
  });
});

describe("TopBar —— cost UI 已移除（P5a）", () => {
  test("agent_usage 仍累计 cost 到 store，但 TopBar 不再渲染 top-cost testid", () => {
    ev("workflow_started", { workflow_name: "wf" });
    ev("agent_usage", {
      cost_usd: 0.001,
      input_tokens: 10,
      output_tokens: 5,
      node: null,
    });
    ev("agent_usage", {
      cost_usd: 0.002,
      input_tokens: 20,
      output_tokens: 8,
      node: null,
    });
    render(<Root active={true} />);
    // P5a：top-cost testid 不存在（UI 已删），store.cost 仍累加（fold 不变）。
    expect(screen.queryByTestId("top-cost")).not.toBeInTheDocument();
    // store fold 不受 UI 移除影响（agent_usage 累加 = 0.003，幂等铁律）。
    expect(useWorkflowStore.getState().cost).toBeCloseTo(0.003, 5);
  });
});

describe("TopBar —— workflow 名 + runId 展示", () => {
  test("workflow 名显示", () => {
    ev("workflow_started", { workflow_name: "my-wf" });
    render(<Root active={true} />);
    expect(screen.getByText("my-wf")).toBeInTheDocument();
  });

  test("runId 截断 8 字符", () => {
    ev("workflow_started", { workflow_name: "wf" });
    render(<Root active={true} />);
    expect(screen.getByText("abc12345")).toBeInTheDocument();
  });
});
