// conversation-types.ts —— 共享常量：进 ConversationView 的事件集合。
//
// 提取自 selectors.ts（P2 前 CONVERSATION_TYPES 是 selectors.ts 内 module-private）。
// P2（web-presentation-refinement §P2）需在 workflow-store.ts 维护 nodesIndex 倒排索引，
// 该索引只统计 conversation 类事件 → 需在同一处定义此集合。
//
// 放独立文件（而非 selectors.ts 或 store-types.ts）：
//   - 不放 selectors.ts：会让 workflow-store.ts 反向 runtime 依赖 selectors.ts（架构上
//     store 是底层，selectors 在其上读）。
//   - 不放 store-types.ts：该文件注释明示「类型描述派生结果形状」，混入 runtime const 不符。
//   - 放独立常量文件 = 双向只单向依赖（双方都 import 此文件），无 cycle。
//
// 与 entries.ts 的 STATUS_LINE_TYPES / NODE_DIVIDER_TYPES 等是子集关系（那些是渲染层
// 进一步细分，本集合是「能否进 ConversationView」的总入口）。

import type { WebEvent } from "@/types/events";

/**
 * 进 conversation 的事件集合（DRY：selectConversation / selectStreamingCursor /
 * workflow-store nodesIndex 三处共用）。
 *
 * SPEC §5.3：foreach_* / retry_* / interrupt_* / validator_* / wait_* 在 conversation
 * 内 dim 渲染 —— 故纳入 conversation 事件集。过程事件（agent_message/thinking/tool_call/
 * tool_result/step_started）+ prompt_rendered + custom + dialog_message + unknown_event
 * 也都进 conversation。
 *
 * **workflow_failed** 特例：make_workflow_failed 把责任 node 写入 ``data.node``（top-level
 * ``e.node`` 仍为 null）。SPEC §5.3 要求它进 conversation 红 block —— 故同时按 top-level
 * ``e.node`` 或 ``data.node`` 匹配（在 selectConversation / nodesIndex 维护处体现）。
 */
export const CONVERSATION_TYPES: Set<WebEvent["type"]> = new Set([
  "prompt_rendered",
  "agent_thinking",
  "agent_message",
  "agent_tool_call",
  "agent_tool_result",
  "agent_step_started",
  "dialog_started",
  "dialog_message",
  "dialog_ended",
  "node_started",
  "node_completed",
  "node_failed",
  "node_skipped",
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
  "custom",
  "workflow_failed",
  "unknown_event",
]);

/**
 * 判断事件是否归属某 node（DRY：selectConversation / selectStreamingCursor /
 * workflow-store.nodesIndex 三处共用）。
 *
 * 规则：
 *   - 一般事件：``e.node === nodeId``
 *   - **workflow_failed 特例**：top-level ``e.node`` 为 null（对齐 schema/event.py 注释），
 *     但 ``data.node`` 是责任 node → 按字符串严格匹配 ``data.node === nodeId``
 *     （类型守门防 number / 其他类型）。
 */
export function eventMatchesNode(e: WebEvent, nodeId: string): boolean {
  if (e.node === nodeId) return true;
  if (e.type === "workflow_failed") {
    const dn = e.data?.node;
    return typeof dn === "string" && dn === nodeId;
  }
  return false;
}
