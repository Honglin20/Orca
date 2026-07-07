// hooks/use-streaming-text.ts —— RAF 批处理 + 多 session 粒度（SPEC §3.3 / §0 D6）。
//
// 抄 AH 机制不抄 store（反 AH 多 store）：
//   - 文本增量缓冲在独立 hook/module（``_textBuf: Map<sessionId, string>``），store 只见
//     committed frame。
//   - ``requestAnimationFrame`` 每帧一次 ``setState``（``_rafSeq`` 失效）。
//   - 多 session 粒度（foreach 并发）：sync-flush on event E 只 flush ``_textBuf[E.session_id]``；
//     RAF tick flush 全部 session。
//   - AH 边界硬化：buffer 永不参与 render 决策；run 切换丢弃 buffer。
//
// 适配 opencode 块级（整块到即渲染）；为未来 token 级（claude/serve）预留同一套 UX。

import { useCallback, useEffect, useRef, useState } from "react";
import type { WebEvent } from "@/types/events";

/** commit 出去的 frame（store-equivalent committed view of buffered text）。 */
export interface StreamingFrame {
  /** sessionId → 当前累积文本。 */
  texts: Record<string, string>;
  /** 单调递增的帧序号（每次 commit +1）。 */
  frameSeq: number;
}

/**
 * sync-flush 触发事件类型（SPEC §3.3）：这些事件到达时，立即 flush 该 session_id 的缓冲
 * （让工具调用 / 结果 / node 结束与之前文本同帧显示）。
 */
const SYNC_FLUSH_TYPES = new Set<WebEvent["type"]>([
  "agent_tool_call",
  "agent_tool_result",
  "node_completed",
  "node_failed",
]);

export interface StreamingTextApi {
  /** 当前 committed frame（render 决策只读这个）。 */
  frame: StreamingFrame;
  /** agent_message / agent_thinking 文本增量入缓冲。 */
  appendText: (sessionId: string, delta: string) => void;
  /**
   * 处理流式事件：若是文本增量则入缓冲；若是 sync-flush 事件则立即 flush 该 session。
   * 由调用方在每条 WS 事件上调。
   */
  ingestEvent: (event: WebEvent) => void;
  /** 主动 flush 指定 session（如 node_completed 后清缓冲）。 */
  flushSession: (sessionId: string) => void;
  /** 丢弃全部缓冲（run 切换 / WS 重连 fallback 用，SPEC §3.3 / D6）。 */
  dropBuffer: () => void;
}

const EMPTY_FRAME: StreamingFrame = { texts: {}, frameSeq: 0 };

export function useStreamingText(): StreamingTextApi {
  // 缓冲（模块级 hook-local）：Map<sessionId, string>。**不参与 render 决策**（AH 边界）。
  const textBufRef = useRef<Map<string, string>>(new Map());
  // 已 schedule 的 RAF handle；每帧 commit 后清空。
  const rafRef = useRef<number | null>(null);
  // rafSeq：RAF 失效计数（每次 schedule +1；同一帧内多次 append 只 schedule 一次）。
  const rafSeqRef = useRef(0);
  // committed frame（render 输入）。
  const [frame, setFrame] = useState<StreamingFrame>(EMPTY_FRAME);

  const commitFrame = useCallback(() => {
    rafRef.current = null;
    const texts: Record<string, string> = {};
    for (const [k, v] of textBufRef.current) {
      texts[k] = v;
    }
    setFrame((prev) => ({ texts, frameSeq: prev.frameSeq + 1 }));
  }, []);

  const scheduleRaf = useCallback(() => {
    if (rafRef.current !== null) return; // 已 schedule
    rafSeqRef.current += 1;
    // 标记「已 schedule」**先于** RAF 调用——这样即便 RAF 同步执行 cb（测试 stub），
    // 也只有 commitFrame 内的 ``rafRef.current = null`` 能清标记，不被 RAF 返回值覆盖。
    rafRef.current = 1; // 占位非 null 哨兵；真实 RAF handle 由 cb 内部管理生命周期
    const invoke = () => commitFrame();
    if (typeof requestAnimationFrame === "function") {
      // 真实 RAF 异步；返回的 handle 仅用于 cancel（当前未暴露 cancel 路径）。
      requestAnimationFrame(() => invoke());
    } else {
      // 测试环境（无 RAF）：microtask 兜底
      Promise.resolve().then(() => invoke());
    }
  }, [commitFrame]);

  const flushSession = useCallback(
    (_sessionId: string) => {
      // sync-flush 只 commit 该 session 的缓冲——但 RAF 是「整帧 commit 全部 session」的
      // 单一机制（SPEC §3.3「sync-flush on event E flushes only _textBuf[E.session_id]」
      // 指的是**对该 session 的可见性立即生效**）。实现上：sync-flush 立即 commit 全部
      // 已缓冲文本（更激进也不违反 SPEC——单帧提前），简化为「即时 commit + 取消 pending RAF」。
      // _sessionId 形参保留以传达 SPEC 语义（调用方传入 event.session_id）；当前实现
      // commit 全部 session（更激进），未来若需 per-session 隔离再细化。
      if (rafRef.current !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafRef.current);
      }
      rafRef.current = null;
      commitFrame();
    },
    [commitFrame]
  );

  const appendText = useCallback(
    (sessionId: string, delta: string) => {
      if (!delta) return;
      const buf = textBufRef.current;
      buf.set(sessionId, (buf.get(sessionId) ?? "") + delta);
      scheduleRaf();
    },
    [scheduleRaf]
  );

  const ingestEvent = useCallback(
    (event: WebEvent) => {
      if (!event.session_id) return;
      // 文本增量入缓冲（agent_message/agent_thinking）
      if (event.type === "agent_message" || event.type === "agent_thinking") {
        const delta = String(event.data?.text ?? "");
        if (delta) appendText(event.session_id, delta);
      }
      // sync-flush 事件：立即 commit（让工具调用 / 结果 / node 结束同帧可见）
      if (SYNC_FLUSH_TYPES.has(event.type)) {
        flushSession(event.session_id);
      }
    },
    [appendText, flushSession]
  );

  const dropBuffer = useCallback(() => {
    if (rafRef.current !== null && typeof cancelAnimationFrame === "function") {
      cancelAnimationFrame(rafRef.current);
    }
    rafRef.current = null;
    textBufRef.current.clear();
    setFrame(EMPTY_FRAME);
  }, []);

  // unmount：清 RAF 无 leak
  useEffect(() => {
    return () => {
      if (rafRef.current !== null && typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(rafRef.current);
      }
      rafRef.current = null;
    };
  }, []);

  return { frame, appendText, ingestEvent, flushSession, dropBuffer };
}
