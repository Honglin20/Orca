// test/conversation-virtualized.test.tsx —— 虚拟化分支回归网（SPEC 2026-08-28 C5.3）。
//
// 计划步 0：测试**先于**阈值切换落地——现阈值 500 下 >500 条事件即入虚拟化分支；
// 步 9 阈值降 100 后本用例仍绿（回归网对步 1-8 恒有效）。
//
// 断言口径：
//   - `conv-vrow-0` testid 存在（虚拟化分支独有；三分支共享 `conversation-view`
//     testid，只断后者对阈值回归失明——SPEC C5.3 明文）；
//   - 首行内容正确（entry 顺序 = seq 升序，首行 = 第一条 prompt）；
//   - 未全量渲染（虚拟化裁剪生效）。
//
// happy-dom 无布局：ResizeObserver 不触发 → 行高恒 defaultRowHeight（**禁断言测量行高**，
// SPEC C5.3）。行数断言只做「远小于全量」的方向性裁剪。

import { afterEach, beforeEach, describe, expect, test } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import * as React from "react";
import { ConversationView } from "@/components/views/ConversationView";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { WebEvent } from "@/types/events";
import { resetStore } from "./_helpers";

/** 造 count 条 prompt 事件（每条一个 entry；PromptRow 折叠态渲染便宜，适合大批量）。 */
function promptEvents(count: number): WebEvent[] {
  return Array.from({ length: count }, (_, i) => ({
    seq: i + 1,
    type: "prompt_rendered" as const,
    timestamp: 1700000000 + i,
    node: "n",
    session_id: null,
    data: { preview: `prompt-${i}` },
  }));
}

beforeEach(() => resetStore());
afterEach(() => cleanup());

describe("ConversationView 虚拟化分支（SPEC C5）", () => {
  test(">500 条事件 → 虚拟化挂载：conv-vrow-0 存在 + 首行内容正确 + 非全量渲染", () => {
    useWorkflowStore.getState().loadFromEvents(promptEvents(520));
    render(React.createElement(ConversationView, { nodeId: "n" }));
    // 虚拟化分支独有 testid（非虚拟化分支渲染 prompt-row 但无 conv-vrow-*）
    const row0 = screen.getByTestId("conv-vrow-0");
    // 首行内容 = 第一条 prompt（entry 顺序 = seq 升序）
    expect(row0.textContent).toContain("user prompt");
    // 虚拟化裁剪：渲染行数远小于总量（非全量渲染）
    const rendered = screen.queryAllByTestId(/^conv-vrow-/);
    expect(rendered.length).toBeLessThan(520);
    expect(rendered.length).toBeGreaterThan(0);
  });

  // 步 9 阈值切换（500→100）补的终态口径断言（SPEC edge case：120+ 条 → 虚拟化分支挂载）。
  test("阈值 100 下 120 条事件 → 虚拟化分支（conv-vrow-0 存在）", () => {
    useWorkflowStore.getState().loadFromEvents(promptEvents(120));
    render(React.createElement(ConversationView, { nodeId: "n" }));
    expect(screen.getByTestId("conv-vrow-0")).toBeInTheDocument();
    expect(screen.queryAllByTestId(/^conv-vrow-/).length).toBeLessThan(120);
  });
});
