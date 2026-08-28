// components/conversation/reuse-entries.ts —— entries 数组引用复用（SPEC 2026-08-28 C4.3）。
//
// 动机：buildEntries 每次 build 都新建全部 entry 对象 → EntryRenderer React.memo 全失效。
// 依赖 buildEntries 的 **append-only 语义**（已产出 entry 不回写；尾部 step-marker 被
// 后续 thinking/message 改写属末位变化，非中间回写），前缀逐位等价 → 复用 prev entry
// 引用，memo 对未变行跳过重渲染。
//
// 判等口径（C4.3）：
//   - 逐 entry 按 ``entry.kind`` 全字段比较，**含 ``stepMarker``**（undefined→有值是改写）；
//   - ToolPair 级 = ``tool_call_id`` + ``call``/``result`` **事件对象引用**（pairToolEvents
//     每次 build 重建 pair 对象，禁以 pair 对象引用判等）；
//   - **禁 JSON.stringify**（事件对象大、O(N) 序列化每 render 反而更慢）。
//
// 全复用返回 prev 数组引用（entries 层引用稳定）。

import type { ConvEntry, ToolPair } from "./entries";
import type { WebEvent } from "@/types/events";

/** ToolPair 判等：tool_call_id + call/result 事件对象引用（C4.3）。 */
function pairEquals(a: ToolPair, b: ToolPair): boolean {
  return (
    a.tool_call_id === b.tool_call_id &&
    a.call === b.call &&
    a.result === b.result
  );
}

/** 逐 entry 按 kind 全字段比较（含 stepMarker；事件用引用等）。 */
function entryEquals(a: ConvEntry, b: ConvEntry): boolean {
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case "thinking":
    case "message": {
      const o = b as typeof a;
      return a.event === o.event && a.stepMarker === o.stepMarker;
    }
    case "tool-single": {
      const o = b as typeof a;
      return pairEquals(a.pair, o.pair);
    }
    case "tool-group": {
      const o = b as typeof a;
      return (
        a.pairs.length === o.pairs.length &&
        a.pairs.every((p, i) => pairEquals(p, o.pairs[i]))
      );
    }
    default: {
      // 其余 kind 均为单 event 字段（discriminated union）
      const o = b as { event: WebEvent };
      return a.event === o.event;
    }
  }
}

/**
 * 复用 prev 中等价的 entry 引用构建 next 的渲染数组。
 *
 * @param prev 上一次渲染的 entries（调用方 ref 持有）
 * @param next 本次 buildEntries 产物
 * @param keyOf entry 稳定 key（与 ConversationView 渲染 key 同源——key 不变且字段等价才复用，
 *              防 append-only 语义被破坏时错位复用）
 * @returns 全复用 → prev 数组引用；否则新建数组（未变位持 prev 引用，变化位持 next 引用）
 */
export function reuseEntries(
  prev: ConvEntry[],
  next: ConvEntry[],
  keyOf: (entry: ConvEntry, index: number) => string
): ConvEntry[] {
  if (prev.length === 0) return next;
  const n = Math.min(prev.length, next.length);
  let allReused = next.length === prev.length;
  const out: ConvEntry[] = new Array(next.length);
  for (let i = 0; i < next.length; i++) {
    if (
      i < n &&
      keyOf(prev[i], i) === keyOf(next[i], i) &&
      entryEquals(prev[i], next[i])
    ) {
      out[i] = prev[i];
    } else {
      allReused = false;
      out[i] = next[i];
    }
  }
  return allReused ? prev : out;
}
