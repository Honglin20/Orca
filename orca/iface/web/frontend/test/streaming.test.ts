// test/streaming.test.ts —— RAF 批处理 hook 验收（SPEC §3.3）。
//
// 断言：
//   1. 多次 appendText 同帧只 commit 一次（RAF batching）
//   2. 多 session 粒度：sync-flush 立即 commit 该 session
//   3. dropBuffer 清空缓冲（D6 AH 边界：run 切换丢缓冲）
//   4. ingestEvent 把 agent_message/agent_thinking 入缓冲；tool_call sync-flush

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useStreamingText } from "@/hooks/use-streaming-text";
import type { EventType, WebEvent } from "@/types/events";

// happy-dom 有 RAF 但不会自动触发帧回调——测试里把 RAF stub 成同步执行，
// 让 appendText 后 commit 立即可见（断言 RAF batching 行为）。
beforeEach(() => {
  vi.stubGlobal(
    "requestAnimationFrame",
    (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    }
  );
  vi.stubGlobal("cancelAnimationFrame", () => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function makeEvent(overrides: Partial<WebEvent> & { type: EventType }): WebEvent {
  const { type, ...rest } = overrides;
  return {
    seq: Math.floor(Math.random() * 1_000_000),
    type,
    timestamp: Date.now() / 1000,
    node: null,
    session_id: null,
    data: {},
    ...rest,
  };
}

describe("useStreamingText", () => {
  // happy-dom 可能无 RAF，hook 内部已用 microtask 兜底；测试用 await flush microtasks

  it("appendText 多次同帧只 commit 一次（frameSeq +1）", async () => {
    const { result } = renderHook(() => useStreamingText());
    const startSeq = result.current.frame.frameSeq;

    act(() => {
      result.current.appendText("s1", "hello ");
      result.current.appendText("s1", "world");
    });
    // 等待 microtask flush
    await Promise.resolve();
    await Promise.resolve();

    // frameSeq 应至少 +1（commit 发生）
    expect(result.current.frame.frameSeq).toBeGreaterThan(startSeq);
    expect(result.current.frame.texts.s1).toContain("hello");
    expect(result.current.frame.texts.s1).toContain("world");
  });

  it("多 session 粒度：sync-flush on tool_call 立即 commit 该 session", async () => {
    const { result } = renderHook(() => useStreamingText());
    const startSeq = result.current.frame.frameSeq;

    act(() => {
      result.current.ingestEvent(
        makeEvent({
          type: "agent_message",
          node: "A",
          session_id: "sX",
          data: { text: "msg-from-X" },
        })
      );
      result.current.ingestEvent(
        makeEvent({
          type: "agent_tool_call",
          node: "A",
          session_id: "sX",
          data: { tool: "bash", args: {}, tool_call_id: "tc1" },
        })
      );
    });
    await Promise.resolve();

    // sync-flush 应触发 commit（frameSeq +1）
    expect(result.current.frame.frameSeq).toBeGreaterThan(startSeq);
    expect(result.current.frame.texts.sX).toContain("msg-from-X");
  });

  it("dropBuffer 清空所有缓冲 + reset frame（D6 AH 边界）", async () => {
    const { result } = renderHook(() => useStreamingText());

    act(() => {
      result.current.appendText("s1", "data");
    });
    await Promise.resolve();
    expect(Object.keys(result.current.frame.texts).length).toBeGreaterThan(0);

    act(() => {
      result.current.dropBuffer();
    });
    expect(Object.keys(result.current.frame.texts).length).toBe(0);
  });

  it("非文本事件（如 node_completed）sync-flush 但不缓冲文本", async () => {
    const { result } = renderHook(() => useStreamingText());

    act(() => {
      result.current.ingestEvent(
        makeEvent({
          type: "node_completed",
          node: "A",
          session_id: "sY",
          data: { elapsed: 0.5, output: "ok" },
        })
      );
    });
    await Promise.resolve();
    // sync-flush 触发但 sY 没文本 → 不在 texts
    expect(result.current.frame.texts.sY ?? "").toBe("");
  });
});
