// test/reuse-entries.test.tsx —— entries 引用复用单测（SPEC 2026-08-28 C4.3）。
//
// 断言意图（Rule 9——验证「memo 生效前提」而非仅行为）：
//   1. 前缀不变尾部追加 → 全部前缀 entry 复用 prev 引用 + 返回 prev 数组引用（memo 命中）；
//   2. pending tool → result 后到：pair 对象每次重建（禁以 pair 引用判等），但 call/result
//      事件引用等价 → entry 复用；
//   3. step→thinking 尾部改写（stepMarker undefined→有值）→ 该 entry 不复用（含 stepMarker 比较）；
//   4. tool-group pair 增长 → 未变 pair 复用；
//   5. orphan result 剔除导致中间错位 → key 守卫下不复用错位 entry。
//
// keyOf 用 ConversationView 同源 entryKey（渲染 key 语义一致）。

import { describe, expect, it } from "vitest";
import { buildEntries } from "@/components/conversation/entries";
import { reuseEntries } from "@/components/conversation/reuse-entries";
import { entryKey } from "@/components/views/ConversationView";
import type { EventType, WebEvent } from "@/types/events";

let _seq = 0;
function ev(type: EventType, overrides: Partial<WebEvent> = {}): WebEvent {
  _seq += 1;
  return {
    seq: _seq,
    type,
    timestamp: 1700000000 + _seq,
    node: null,
    session_id: null,
    data: {},
    ...overrides,
  };
}

describe("reuseEntries —— append-only 前缀引用复用（C4.3）", () => {
  it("前缀不变尾部追加：全复用位持 prev 引用；追加前全等时返回 prev 数组引用", () => {
    const prev = buildEntries([
      ev("node_started", { node: "n" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "a" } }),
    ]);
    const next = buildEntries([
      ...prevEventsOf(prev),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "b" } }),
    ]);
    const out = reuseEntries(prev, next, entryKey);
    // 前缀两位复用 prev 引用
    expect(out[0]).toBe(prev[0]);
    expect(out[1]).toBe(prev[1]);
    // 尾部新 entry 是 next 引用
    expect(out[2]).toBe(next[2]);
  });

  it("同输入（无变化）→ 返回 prev 数组引用（entries 层引用稳定）", () => {
    const prev = buildEntries([
      ev("node_started", { node: "n" }),
      ev("agent_message", { node: "n", session_id: "s", data: { text: "a" } }),
    ]);
    const next = buildEntries(prevEventsOf(prev));
    expect(reuseEntries(prev, next, entryKey)).toBe(prev);
  });

  it("pending tool → result 后到：result 事件引用变化 → entry **不复用**（合法重渲染：spinner→结果）", () => {
    const call = ev("agent_tool_call", {
      node: "n",
      session_id: "s",
      data: { tool: "bash", tool_call_id: "t1", args: {} },
    });
    const result = ev("agent_tool_result", {
      node: "n",
      session_id: "s",
      data: { tool_call_id: "t1", result: "ok" },
    });
    const prev = buildEntries([call]); // pending（tool-single，无 result）
    const next = buildEntries([call, result]); // done（pair 对象新建）
    expect(prev[0]).not.toBe(next[0]); // pairToolEvents 每次 build 重建 pair 对象（前提成立）
    const out = reuseEntries(prev, next, entryKey);
    // pair 判等含 result 事件引用：pending→done 是实质变化 → 用 next entry 重渲染
    expect(out[0]).toBe(next[0]);
  });

  it("tool-group pair 增长：pairs 数组变化 → entry 用 next（组渲染新增 pair）", () => {
    const c1 = ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "t1", args: {} } });
    const r1 = ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "t1", result: "1" } });
    const c2 = ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "read", tool_call_id: "t2", args: {} } });
    const r2 = ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "t2", result: "2" } });
    const prev = buildEntries([c1, r1, c2]);
    const next = buildEntries([c1, r1, c2, r2]);
    expect(prev[0].kind).toBe("tool-group");
    expect(next[0].kind).toBe("tool-group");
    const out = reuseEntries(prev, next, entryKey);
    // 组内 pairs 增长（1 done + 1 pending → 2 done）→ entry 整体用 next
    expect(out[0]).toBe(next[0]);
  });

  it("step→thinking 尾部改写（stepMarker undefined→有值）→ 不复用该 entry", () => {
    const step = ev("agent_step_started", { node: "n", session_id: "s", data: { step_reason: "x" } });
    const think = ev("agent_thinking", { node: "n", session_id: "s", data: { text: "hmm" } });
    const msg = ev("agent_message", { node: "n", session_id: "s", data: { text: "a" } });
    const prev = buildEntries([msg, step]); // 尾部孤立 step → step-marker entry（dim 分隔）
    const next = buildEntries([msg, step, think]); // thinking 后到 → step 改写为 thinking.stepMarker
    const out = reuseEntries(prev, next, entryKey);
    // index 1：prev 是 step-marker entry，next 是 thinking entry（stepMarker 有值）→ 不复用
    expect(out[1]).not.toBe(prev[1]);
    expect(out[1]).toBe(next[1]);
    // 前缀 message 不变 → 复用
    expect(out[0]).toBe(prev[0]);
  });

  it("中间剔除（orphan result 不进 entries）→ key 错位守卫：不复用错位 entry", () => {
    const c1 = ev("agent_tool_call", { node: "n", session_id: "s", data: { tool: "bash", tool_call_id: "t1", args: {} } });
    const r1 = ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "t1", result: "1" } });
    const orphan = ev("agent_tool_result", { node: "n", session_id: "s", data: { tool_call_id: "nope", result: "?" } });
    const msg = ev("agent_message", { node: "n", session_id: "s", data: { text: "a" } });
    const prev = buildEntries([c1, r1, orphan, msg]); // orphan 剔除 → [tool-single, message]
    const next = buildEntries([c1, r1, msg]); // 无 orphan → 同样 [tool-single, message]
    // 两数组 entry 数相同但语义对齐——key 同且事件引用等价 → 复用成立（append-only 无破坏）
    const out = reuseEntries(prev, next, entryKey);
    expect(out[0]).toBe(prev[0]);
    expect(out[1]).toBe(prev[1]);
  });
});

/** 从 entries 反推原始事件序列（简化：单 event entry 用 event；tool entry 取 call/result）。 */
function prevEventsOf(entries: ReturnType<typeof buildEntries>): WebEvent[] {
  const out: WebEvent[] = [];
  for (const e of entries) {
    if (e.kind === "tool-single" || e.kind === "tool-group") {
      const pairs = e.kind === "tool-single" ? [e.pair] : e.pairs;
      for (const p of pairs) {
        if (p.call) out.push(p.call);
        if (p.result) out.push(p.result);
      }
    } else if ("event" in e) {
      out.push(e.event);
    }
  }
  return out;
}
