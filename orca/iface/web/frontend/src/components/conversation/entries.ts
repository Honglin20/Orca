// components/conversation/entries.ts —— 纯函数：WebEvent[] → 渲染 entries。
//
// SPEC §5.3 折叠规则 + 工具配对 + 工具成组 + step marker 附着 + orphan 过滤。
//
// **纯函数**（无 React 依赖），便于单测断言折叠 oracle（SPEC §9 AC1 / §10 fixture）。
//
// 设计：
//   - orphan tool_result（无对应 call）→ 不进 conversation（SPEC §5.3），调用方应已
//     通过 buildEntries 内 console.warn 报告（fail loud；SPEC 让 LogStream 渲染该事件
//     ——selectLog 不经此函数，故 LogStream 仍可见）。
//   - tool_call/result 配对：在同 node 范围内按 tool_call_id 索引（无论到达顺序）。
//   - 成组：连续 tool 事件（中间无非 tool 事件）→ 单一 tool-single 或 tool-group。
//   - agent_step_started：附下一个 thinking/message；若直到末尾无 → 末尾 dim step-marker。
//   - 单 tool 事件 → kind=tool-single（不强制成组，1 个就是单行）。
//
// 渲染层（ConversationView）消费 Entry[] 决定折叠默认状态（§5.3 折叠规则）。

import type { WebEvent } from "@/types/events";

/** 一对工具（call + 可能匹配的 result）。tool_call_id 缺则用占位 id。 */
export interface ToolPair {
  tool_call_id: string;
  call?: WebEvent;
  result?: WebEvent;
}

// ── Entry 联合（discriminated union；OCP：新 kind 新增分支，不改既有）──
export type ConvEntry =
  | { kind: "prompt"; event: WebEvent }
  | { kind: "thinking"; event: WebEvent; stepMarker?: WebEvent }
  | { kind: "message"; event: WebEvent; stepMarker?: WebEvent }
  | { kind: "tool-single"; pair: ToolPair }
  | { kind: "tool-group"; pairs: ToolPair[] }
  | { kind: "dialog-message"; event: WebEvent }
  | { kind: "dialog-divider"; event: WebEvent }
  | { kind: "chart-ref"; event: WebEvent }
  | { kind: "custom-generic"; event: WebEvent }
  | { kind: "node-divider"; event: WebEvent }
  | { kind: "node-output"; event: WebEvent }
  | { kind: "node-error"; event: WebEvent }
  | { kind: "status-line"; event: WebEvent }
  | { kind: "step-marker"; event: WebEvent }
  | { kind: "unknown"; event: WebEvent };

/** 状态行事件类型（dim 渲染，默认折叠，SPEC §5.3）。 */
const STATUS_LINE_TYPES = new Set<WebEvent["type"]>([
  "retry_started",
  "retry_succeeded",
  "retry_exhausted",
  "interrupt_requested",
  "interrupt_resolved",
  "validator_started",
  "validator_passed",
  "validator_failed",
  "wait_started",
  "wait_completed",
  "foreach_started",
  "foreach_item_started",
  "foreach_item_completed",
  "foreach_completed",
]);

/** 节点生命周期分隔（细，dim）。
 *  注：``node_completed`` 已升格为 ``node-output`` block（B1：显示 output 文字），
 *  不再作 divider——完成信号由 output 本身承担；边界感留给 node_started / node_skipped。 */
const NODE_DIVIDER_TYPES = new Set<WebEvent["type"]>([
  "node_started",
  "node_skipped",
]);

/** 红色 error block 类型（SPEC §5.3 闭 review #29）。 */
const NODE_ERROR_TYPES = new Set<WebEvent["type"]>([
  "node_failed",
  "workflow_failed",
]);

/** 把 tool_call / tool_result 序列配对成 ToolPair[]。orphan（无 call）被过滤。 */
function pairToolEvents(toolEvents: WebEvent[]): {
  pairs: ToolPair[];
  orphans: WebEvent[];
} {
  const byId = new Map<string, ToolPair>();
  const order: string[] = [];
  const orphans: WebEvent[] = [];
  let anonCounter = 0;
  for (const e of toolEvents) {
    if (e.type !== "agent_tool_call" && e.type !== "agent_tool_result") {
      continue;
    }
    let id = String(e.data?.tool_call_id ?? "");
    if (!id) {
      if (e.type === "agent_tool_call") {
        // 无 id 的 call：用占位 id（视为 pending call，仍渲染）
        id = `__anon_${anonCounter++}__`;
      } else {
        // 无 id 的 result：无法配对，记为 orphan（SPEC §5.3「orphan 不进 conversation」）。
        // **不**早 return——否则会丢弃此前已配对的全部 pair（review B1）。
        orphans.push(e);
        continue;
      }
    }
    let pair = byId.get(id);
    if (!pair) {
      pair = { tool_call_id: id };
      byId.set(id, pair);
      order.push(id);
    }
    if (e.type === "agent_tool_call") pair.call = e;
    else pair.result = e;
  }
  const allPairs = order.map((id) => byId.get(id)!);
  // 另一类 orphan：有 id 但只有 result（无 call 匹配）
  for (const p of allPairs) {
    if (!p.call && p.result) orphans.push(p.result);
  }
  // 渲染集 = 有 call 的 pair（pending call 也算）
  const renderable = allPairs.filter((p) => p.call);
  return { pairs: renderable, orphans };
}

/**
 * 把 selectConversation 输出（WebEvent[]）折叠为渲染 entries。
 *
 * @param events 该 node 的 conversation 事件（seq 升序）
 */
export function buildEntries(events: WebEvent[]): ConvEntry[] {
  const entries: ConvEntry[] = [];
  let pendingStep: WebEvent | null = null;
  let toolRun: WebEvent[] = [];
  const orphanResults: WebEvent[] = [];

  const flushToolRun = () => {
    if (toolRun.length === 0) return;
    const { pairs, orphans } = pairToolEvents(toolRun);
    orphanResults.push(...orphans);
    if (pairs.length === 1) {
      entries.push({ kind: "tool-single", pair: pairs[0] });
    } else if (pairs.length > 1) {
      entries.push({ kind: "tool-group", pairs });
    }
    toolRun = [];
  };

  for (const e of events) {
    if (e.type === "agent_tool_call" || e.type === "agent_tool_result") {
      toolRun.push(e);
      continue;
    }
    flushToolRun();

    if (e.type === "agent_step_started") {
      // 若有未消费 step marker，先落 dim 分隔
      if (pendingStep) {
        entries.push({ kind: "step-marker", event: pendingStep });
      }
      pendingStep = e;
      continue;
    }
    if (e.type === "agent_thinking") {
      entries.push({
        kind: "thinking",
        event: e,
        stepMarker: pendingStep ?? undefined,
      });
      pendingStep = null;
      continue;
    }
    if (e.type === "agent_message") {
      entries.push({
        kind: "message",
        event: e,
        stepMarker: pendingStep ?? undefined,
      });
      pendingStep = null;
      continue;
    }
    if (e.type === "prompt_rendered") {
      entries.push({ kind: "prompt", event: e });
      continue;
    }
    if (e.type === "dialog_message") {
      entries.push({ kind: "dialog-message", event: e });
      continue;
    }
    if (e.type === "dialog_started" || e.type === "dialog_ended") {
      entries.push({ kind: "dialog-divider", event: e });
      continue;
    }
    if (e.type === "custom") {
      if (e.data?.kind === "chart") {
        entries.push({ kind: "chart-ref", event: e });
      } else {
        entries.push({ kind: "custom-generic", event: e });
      }
      continue;
    }
    if (e.type === "node_completed") {
      // B1：升格为 output block，不再作 divider（否则 data.output 文字被丢弃）。
      entries.push({ kind: "node-output", event: e });
      continue;
    }
    if (NODE_DIVIDER_TYPES.has(e.type)) {
      entries.push({ kind: "node-divider", event: e });
      continue;
    }
    if (NODE_ERROR_TYPES.has(e.type)) {
      entries.push({ kind: "node-error", event: e });
      continue;
    }
    if (STATUS_LINE_TYPES.has(e.type)) {
      entries.push({ kind: "status-line", event: e });
      continue;
    }
    if (e.type === "unknown_event") {
      entries.push({ kind: "unknown", event: e });
      continue;
    }
    // 不可达：selectConversation 已过滤非 conversation 类型。fail loud 但不 crash。
    console.warn(
      `[orca] buildEntries: 未预期事件 type=${e.type} seq=${e.seq}，跳过`
    );
  }
  // 收尾
  flushToolRun();
  if (pendingStep) {
    entries.push({ kind: "step-marker", event: pendingStep });
  }

  // orphan tool_result：SPEC §5.3「不进 conversation」→ fail loud 报告（不抛，保渲染稳定）。
  for (const orphan of orphanResults) {
    console.warn(
      `[orca] orphan agent_tool_result (tool_call_id=${
        String(orphan.data?.tool_call_id ?? "?")
      }, seq=${orphan.seq}) 无对应 call，已在 conversation 内剔除（LogStream 仍可见）`
    );
  }

  return entries;
}

/** 工具状态：pending（无 result）/ done（有 result）。 */
export type ToolStatus = "pending" | "done";

export function toolStatus(pair: ToolPair): ToolStatus {
  return pair.result ? "done" : "pending";
}
