// hooks/use-websocket.ts —— WS 按需订阅 + resume by seq 重连（SPEC §3.3 / §0 D6）。
//
// 设计（反 AgentHarness）：
//   1. mount（有 runId）→ 开 WS，onopen 仅 send subscribe(run_id)
//      （初始全量加载由 useRunEvents 负责，避免双拉竞态——单一加载路径）
//   2. onmessage：只处理 event.run_id === runId 的事件；记 last_seq_seen
//   3. onclose（非主动关）→ 指数退避重连；重连发 ``{type:"resume",run_id,since:last_seq_seen}``
//      （D6）；server 重放 seq>since 的事件；resume 失败 → client 全量 re-fetch + re-fold
//      + 丢弃 _textBuf（调用方负责 dropBuffer）。
//   4. unmount / 切 run → 关旧 WS + cancel pending reconnect + cancel watchdog（无 leak）
//
// **D4 resume-fallback watchdog**（SPEC §0 D6 失败路径）：重连发 resume 后启 watchdog 计时；
// 若 ``RESUME_WATCHDOG_MS`` 内未收到任何事件 → 判定 resume 失败（server 不识别 / 历史丢失），
// 触发全量 re-fetch（``GET /api/runs/<id>/events``）+ ``loadFromEvents`` re-fold +
// ``onResumeFallback()`` 让调用方 drop _textBuf。任一事件到达即清 watchdog（resume 成功）。
//
// 单一加载路径：初始加载 useRunEvents；WS 仅在重连时 resume 或 fallback 全量。

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useWsConnectionStore } from "./ws-connection-store";
import type { WebEvent } from "@/types/events";
import type { WsClientMessage } from "@/types/store-types";

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
const READY_OPEN = 1;
/**
 * resume 发出后等这么久还没收到任何事件 → 判定 resume 失败，触发全量 re-fetch。
 * 取 3s：太短易误判（server 重放延迟 / 网络抖动），太长用户感知卡顿。后端 ws_handler
 * 在 resume 后立即 emit backlog（同 tick），3s 足够覆盖 P99 网络 RTT。
 */
const RESUME_WATCHDOG_MS = 3_000;

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
    const fetchImpl = deps.fetchImpl ?? globalThis.fetch;
    const wsUrl = deps.wsUrl ?? defaultWsUrl();
    const onResumeFallback = deps.onResumeFallback;

    let closedByUs = false;
    let backoff = INITIAL_BACKOFF_MS;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    /** D4 resume watchdog：resume 发出后启；超时未收事件 → fallback 全量重拉。 */
    let resumeWatchdog: ReturnType<typeof setTimeout> | null = null;
    let socket: WebSocket | null = null;
    let everConnected = false;

    /** D4 全量 re-fetch + re-fold + drop _textBuf（resume 失败 / ws 不可用 fallback）。 */
    const triggerResumeFallback = async () => {
      try {
        const resp = await fetchImpl(
          `/api/runs/${encodeURIComponent(runId)}/events`
        );
        if (!resp.ok) {
          console.error(
            `[orca] resume-fallback 全量重拉失败 HTTP ${resp.status} (run=${runId})`
          );
          return;
        }
        const events = (await resp.json()) as WebEvent[];
        // 全量 re-fold（loadFromEvents 内部 sort by seq + refold）。
        useWorkflowStore.getState().loadFromEvents(events);
      } catch (err) {
        // fail loud：网络错误不静默吞（SPEC 铁律 12）。下次重连仍会再次尝试。
        console.error(
          `[orca] resume-fallback 全量重拉网络错误 (run=${runId})`,
          err
        );
        return;
      }
      // dropBuffer 必须在 loadFromEvents 之后：先 re-fold 真相，再让 streaming hook 清
      // 旧 buffer（顺序反之会在 re-fold 渲染的瞬间残留旧 buffer frame）。
      onResumeFallback?.();
    };

    /** 清 watchdog（任一事件到达即 resume 成功）。 */
    const clearResumeWatchdog = () => {
      if (resumeWatchdog !== null) {
        clearTimeout(resumeWatchdog);
        resumeWatchdog = null;
      }
    };

    const armResumeWatchdog = () => {
      clearResumeWatchdog();
      resumeWatchdog = setTimeout(() => {
        resumeWatchdog = null;
        console.warn(
          `[orca] resume 后 ${RESUME_WATCHDOG_MS}ms 未收到事件，触发全量重拉 fallback (run=${runId})`
        );
        void triggerResumeFallback();
      }, RESUME_WATCHDOG_MS);
    };

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

    const open = () => {
      socket = createSocket(wsUrl);
      const wasReconnect = everConnected;
      everConnected = true;
      // P3/Y4：transport-only 连接状态（sanctioned exception）。首次 connecting、重连 reconnecting。
      useWsConnectionStore.getState()[wasReconnect ? "setReconnecting" : "setConnecting"]();

      socket.onopen = () => {
        useWsConnectionStore.getState().setConnected();
        backoff = INITIAL_BACKOFF_MS;
        if (wasReconnect) {
          // 重连：发 resume（server 重放 seq>since）。同时启 watchdog——若 RESUME_WATCHDOG_MS
          // 内未收到任何事件，判定 resume 失败（server 不识别 / 历史丢失），触发全量重拉。
          sendResume(socket!);
          // 兜底：resume 后也 subscribe（保证 server 不识别 resume 时仍接上 live 流）。
          // resume + subscribe 双发幂等——server 看 resume 优先（重放历史），再看 subscribe
          // 转入 live；若 server 不识别 resume 则只当 subscribe 处理，缺失历史由 watchdog
          // 触发的全量重拉补。
          sendSubscribe(socket!);
          armResumeWatchdog();
        } else {
          sendSubscribe(socket!);
        }
      };

      socket.onmessage = (ev: MessageEvent) => {
        let parsed: Record<string, unknown>;
        try {
          parsed = JSON.parse(ev.data) as Record<string, unknown>;
        } catch (err) {
          console.error("[orca] ws 收到非 JSON 消息，忽略", err);
          return;
        }
        // D4 watchdog ack：server resume 重放完毕（含零事件重放即 client 已 caught-up 场景）
        // 后发 ``{type:"resume_ok"}`` 帧。本帧**不进 tape**（控制平面，非业务事件）→ 不调
        // processEvent，只清 watchdog。避免 idle 场景下「无事件 = resume 失败」的误判。
        if (parsed.type === "resume_ok") {
          if (parsed.run_id === runId) clearResumeWatchdog();
          return;
        }
        // 业务事件：run_id 匹配过滤 + 清 watchdog（任一事件 = resume 成功）。
        if (parsed.run_id !== runId) return;
        clearResumeWatchdog();
        processEvent(parsed as unknown as WebEvent);
      };

      socket.onclose = () => {
        // 重连前清 watchdog（避免重连间隙触发误 fallback）
        clearResumeWatchdog();
        if (closedByUs) {
          useWsConnectionStore.getState().setDisconnected();
          return;
        }
        useWsConnectionStore.getState().setReconnecting();
        reconnectTimer = setTimeout(() => {
          backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
          open();
        }, backoff);
      };

      socket.onerror = () => {
        // P3：transport-only 状态（出错 → 重连中，见 onclose）。
        useWsConnectionStore.getState().setReconnecting();
        /* 见 onclose */
      };
    };

    open();

    return () => {
      closedByUs = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      clearResumeWatchdog();
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
