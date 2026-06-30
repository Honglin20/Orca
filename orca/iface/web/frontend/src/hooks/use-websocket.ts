// hooks/use-websocket.ts —— WS 按需订阅 + 重连全量重拉（SPEC §5，铁律 5）。
//
// 设计（反 AgentHarness）：
//   1. mount（有 runId）→ 开 WS，onopen 仅 send subscribe(run_id)
//      （**初始全量加载由 useRunEvents 负责**，避免双拉竞态 —— 单一加载路径，SPEC §4.1）
//   2. onmessage：只处理 event.run_id === runId 的事件（按需订阅，过滤他 run 噪声）
//   3. onclose（非主动关）→ 指数退避重连，**重连流程 = 全量重拉 + 重新 subscribe**
//      （SPEC §5.2.2：断了先 GET /events 全量 replay 再 subscribe，避免断连期间丢事件）
//   4. unmount / 切 run → 关旧 WS + cancel pending reconnect（无 leak）
//
// 单一加载路径：初始加载 useRunEvents（一次 GET /events），WS 只在重连时全量重拉。
// 这样进入详情页只发一次 /events（无竞态），重连断连才走全量重拉（保证不丢）。

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { WorkflowEvent, WsClientMessage } from "@/types/events";

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
// WebSocket readyState 常量（spec 值：0 CONNECTING / 1 OPEN / 2 CLOSING / 3 CLOSED）。
// 用字面量而非 WebSocket.OPEN 全局 —— 后者在 happy-dom/jsdom 测试 env 可能 undefined。
const READY_OPEN = 1;

// 暴露给测试的工厂：可注入 WebSocket 构造器 + 网络层（happy-dom 无原生 WS，测试 mock）
export interface WebSocketDeps {
  /** 构造 WebSocket。默认 `new WebSocket(url)`。测试注入 mock。 */
  createSocket?: (url: string) => WebSocket;
  /** fetch override（测试注入）。默认全局 fetch。 */
  fetchImpl?: typeof fetch;
  /** ws url。默认基于 location.host 派生（dev 直连 vite；prod 同源）。 */
  wsUrl?: string;
}

function defaultWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws`;
}

export function useWebSocket(
  runId: string | undefined,
  deps: WebSocketDeps = {}
): void {
  const processEvent = useWorkflowStore((s) => s.processEvent);
  const replayState = useWorkflowStore((s) => s.replayState);

  useEffect(() => {
    if (!runId) return; // 无 runId 不开 WS（列表页不订阅）

    const createSocket = deps.createSocket ?? ((url: string) => new WebSocket(url));
    const fetchImpl = deps.fetchImpl ?? fetch.bind(globalThis);
    const wsUrl = deps.wsUrl ?? defaultWsUrl();

    let closedByUs = false; // 区分主动关 vs 异常断（仅异常断重连）
    let backoff = INITIAL_BACKOFF_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let socket: WebSocket | null = null;
    let everConnected = false; // 首次连接只 subscribe；重连才全量重拉（避免与 useRunEvents 双拉）

    const sendSubscribe = (sock: WebSocket) => {
      if (sock.readyState === READY_OPEN) {
        const sub: WsClientMessage = { type: "subscribe", run_id: runId };
        sock.send(JSON.stringify(sub));
      }
    };

    const fullReplayThenSubscribe = async (sock: WebSocket) => {
      // 重连全量重拉（SPEC §5.2.2）：先 GET /events → replayState（保证一致），
      // 再 subscribe（WS 只补之后的新事件）。仅在重连走，初始连接不走（useRunEvents 已拉）。
      try {
        const resp = await fetchImpl(
          `/api/runs/${encodeURIComponent(runId)}/events`
        );
        if (resp.ok) {
          const events = (await resp.json()) as WorkflowEvent[];
          if (events.length > 0) replayState(events);
        }
      } catch (err) {
        // 全量重拉失败不阻断 WS 订阅（live 仍可补）；记 console（fail loud）
        console.error(`[orca] ws 重连全量重拉 ${runId} 失败`, err);
      }
      sendSubscribe(sock);
    };

    const open = () => {
      socket = createSocket(wsUrl);
      const wasReconnect = everConnected; // 本次 open 是否为重连
      everConnected = true;

      socket.onopen = () => {
        backoff = INITIAL_BACKOFF_MS; // 连上重置退避
        if (wasReconnect) {
          // 重连：先全量重拉补断连期间事件，再 subscribe
          void fullReplayThenSubscribe(socket!);
        } else {
          // 初始连接：仅 subscribe（useRunEvents 已负责初始全量加载）
          sendSubscribe(socket!);
        }
      };

      socket.onmessage = (ev: MessageEvent) => {
        // 按需订阅铁律：只处理 event.run_id === runId 的事件（过滤他 run 噪声）
        let event: WorkflowEvent;
        try {
          event = JSON.parse(ev.data) as WorkflowEvent;
        } catch (err) {
          console.error("[orca] ws 收到非 JSON 消息，忽略", err);
          return;
        }
        if (event.run_id !== runId) return; // 他 run 事件，丢弃（按需订阅）
        processEvent(event);
      };

      socket.onclose = () => {
        if (closedByUs) return; // 主动关（unmount/切 run）不重连
        // 异常断 → 指数退避重连（重连流程会再全量重拉）
        reconnectTimer = setTimeout(() => {
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
          open();
        }, backoff);
      };

      // onerror 不额外动作 —— 浏览器会在 error 后触发 onclose，重连逻辑在 onclose
      socket.onerror = () => {
        /* 见 onclose */
      };
    };

    open();

    return () => {
      // unmount / 切 run：主动关 + cancel pending reconnect + 切断所有回调（无 leak）
      closedByUs = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null; // 阻止主动关触发重连
        socket.close();
      }
    };
    // deps.* 是可选注入（测试用），runId 是触发重订阅的真正依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, processEvent, replayState]);
}
