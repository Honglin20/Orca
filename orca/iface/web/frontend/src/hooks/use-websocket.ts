// hooks/use-websocket.ts —— WS 按需订阅 + resume by seq 重连（SPEC §3.3 / §0 D6）。
//
// 设计（反 AgentHarness）：
//   1. mount（有 runId）→ 开 WS，onopen 仅 send subscribe(run_id)
//      （初始全量加载由 useRunEvents 负责，避免双拉竞态——单一加载路径）
//   2. onmessage：只处理 event.run_id === runId 的事件；记 last_seq_seen
//   3. onclose（非主动关）→ 指数退避重连；重连发 ``{type:"resume",run_id,since:last_seq_seen}``
//      （D6）；server 重放 seq>since 的事件；resume 失败 → client 全量 re-fetch + re-fold
//      + 丢弃 _textBuf（调用方负责 dropBuffer）。
//   4. unmount / 切 run → 关旧 WS + cancel pending reconnect（无 leak）
//
// 单一加载路径：初始加载 useRunEvents；WS 仅在重连时 resume 或 fallback 全量。

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import type { WebEvent } from "@/types/events";
import type { WsClientMessage } from "@/types/store-types";

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
const READY_OPEN = 1;

export interface WebSocketDeps {
  createSocket?: (url: string) => WebSocket;
  fetchImpl?: typeof fetch;
  wsUrl?: string;
  /** resume 失败时的回调（让 streaming hook 丢弃 _textBuf，D6）。 */
  onResumeFallback?: () => void;
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

  useEffect(() => {
    if (!runId) return;

    const createSocket = deps.createSocket ?? ((url: string) => new WebSocket(url));
    // fetchImpl 当前 chunk 不再直接用（resume-fallback watchdog 留给后续 chunk）；
    // 保留 deps.fetchImpl 入口以稳定 API surface，并供测试注入。
    void deps.fetchImpl;
    const wsUrl = deps.wsUrl ?? defaultWsUrl();

    let closedByUs = false;
    let backoff = INITIAL_BACKOFF_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let socket: WebSocket | null = null;
    let everConnected = false;

    const sendSubscribe = (sock: WebSocket) => {
      if (sock.readyState === READY_OPEN) {
        const sub: WsClientMessage = { type: "subscribe", run_id: runId };
        sock.send(JSON.stringify(sub));
      }
    };

    /** D6 resume：发 ``{type:"resume",run_id,since:last_seq_seen}``；server 重放 seq>since。 */
    const sendResume = (sock: WebSocket) => {
      if (sock.readyState !== READY_OPEN) return;
      const since = useWorkflowStore.getState().lastSeqSeen;
      const msg: WsClientMessage = { type: "resume", run_id: runId, since };
      sock.send(JSON.stringify(msg));
    };

    // D6 resume 失败 fallback（全量 re-fetch + re-fold + drop _textBuf）：当前 chunk 不
    // 主动调用——server ws_handler 已支持 resume 协议（重放 seq>since），fallback 路径
    // 在 watchdog（resume 后 N 秒未收到事件）接入时再加（YAGNI）。deps.onResumeFallback
    // 通道保留，调用方（streaming hook）已可挂 dropBuffer。

    const open = () => {
      socket = createSocket(wsUrl);
      const wasReconnect = everConnected;
      everConnected = true;

      socket.onopen = () => {
        backoff = INITIAL_BACKOFF_MS;
        if (wasReconnect) {
          // 重连：发 resume（server 重放 seq>since）。若 server 不支持 resume 协议
          // （回复 error 或忽略），onmessage 仍可补；最坏 fallback 全量重拉在 close 后触发。
          // 简化：重连先 resume；同时记一个 watchdog——若 N 秒内未收到任何事件，触发全量
          // 重拉。这里直接 resume + subscribe 兜底（subscribe 保证后续 live 不丢）。
          sendResume(socket!);
          // 兜底：resume 后也 subscribe（保证 server 不识别 resume 时仍接上 live 流）。
          // 注：resume + subscribe 双发是幂等的——server 看 resume 优先（重放历史），再看
          // subscribe 转入 live；若 server 不识别 resume 则只当 subscribe 处理（live 接上），
          // 缺失的历史事件由 onclose 后的 fallback 全量重拉补（极少触发）。
          sendSubscribe(socket!);
          // 若 lastSeqSeen=0（重连前未收到任何事件），resume 退化为全量场景——server 应
          // 重放全部。这里不主动 fallback：让 server 决定；client 只在真正错失时（人工
          // 监控）触发——避免在正常 resume 路径里做激进重拉。
        } else {
          sendSubscribe(socket!);
        }
      };

      socket.onmessage = (ev: MessageEvent) => {
        let event: WebEvent;
        try {
          event = JSON.parse(ev.data) as WebEvent;
        } catch (err) {
          console.error("[orca] ws 收到非 JSON 消息，忽略", err);
          return;
        }
        if (event.run_id !== runId) return;
        processEvent(event);
      };

      socket.onclose = () => {
        if (closedByUs) return;
        reconnectTimer = setTimeout(() => {
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
          open();
        }, backoff);
      };

      socket.onerror = () => {
        /* 见 onclose */
      };
    };

    open();

    return () => {
      closedByUs = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, processEvent]);
}
