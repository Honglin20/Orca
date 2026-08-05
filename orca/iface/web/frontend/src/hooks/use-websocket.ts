// hooks/use-websocket.ts —— WS 按需订阅 + resume by seq 重连（SPEC §3.3 / §0 D6）。
//
// 设计（反 AgentHarness）：
//   1. mount（有 runId）→ 开 WS，onopen 读 ``useWorkflowStore.getState().loadStatus``：
//      - **loaded 或 error → 立即 sendResume**（两态 sendResume 均安全：loaded 有 committed
//        lastSeqSeen；error 时 lastSeqSeen 或为同 run 上个 loaded 残留值，或为切 run 时
//        unloadRun 清的 0，since=0 即全量重放无 dup，since=stale 即部分补全无 dup）。
//      - **loading/idle → 等回调**（listener 仍 active）。
//   2. **defer-RESUME（SPEC audit-c §3 INV-7 E1 BLOCKER）**：listener 在 useEffect 顶层注册
//      一次（**非 onopen 内**，BLOCKER-2），订阅 store loadStatus 翻转——首次到 loaded 时
//      fire sendResume(since=lastSeqSeen) + one-shot 自清 ``unsub()``。server ``_handle_resume``
//      从 tape 重放 seq>since 后再 subscribe（``_handle_subscribe``），补全 ``(fetch_commit,
//      sendResume_send]`` 窗口事件（subscribe forward-only 不管用，bus.py:218）。
//   3. onmessage：只处理 event.run_id === runId 的事件；记 last_seq_seen
//   4. onclose（非主动关）→ 指数退避重连；reconnect onopen 读 loadStatus 分支：
//      - **loadStatus 优先于 wasReconnect（F1）**：loaded/error → sendResume 首帧 + sendSubscribe
//        次帧（MAJOR-3 reverse A5：保 sendSubscribe 作 server-restart lazy-mount fallback）；
//        loading/idle → 不双发，仍走 listener 路径（loading 期 server 一般未重启）。
//      resume 失败 → client 全量 re-fetch + re-fold + 丢弃 _textBuf（调用方负责 dropBuffer）。
//   5. unmount / 切 run → 关旧 WS + cancel pending reconnect + cancel watchdog（无 leak）。
//
// **per-socket resumeSent dedup（MAJOR-2）**：onopen 立即 sendResume 与 listener fire sendResume
// 同 socket 可能 race（onopen 触发后 listener 也观察到 loaded 翻转）→ 两次 ``_handle_subscribe``
// → server cancel-then-resubscribe await yield 期间事件丢。每个 socket 实例维护 ``resumeSent:
// boolean`` closure（reconnect 随新 socket 重置）；onopen 与 listener 共用此标志。
//
// **D4 resume-fallback watchdog**（SPEC §0 D6 失败路径）：重连发 resume 后启 watchdog 计时；
// 若 ``RESUME_WATCHDOG_MS`` 内未收到任何事件 → 判定 resume 失败（server 不识别 / 历史丢失），
// 触发全量 re-fetch（``GET /api/runs/<id>/events``）+ ``loadFromEvents`` re-fold +
// ``onResumeFallback()`` 让调用方 drop _textBuf。任一事件到达即清 watchdog（resume 成功）。
//
// 单一加载路径：初始加载 useRunEvents；WS 在重连时 resume 或 fallback 全量。

import { useEffect } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";
import { useApprovalStore } from "@/stores/approval-store";
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
  const ingestApprovalFrame = useApprovalStore((s) => s.ingestFrame);

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
    /**
     * SPEC audit-c MAJOR-2：per-socket resumeSent dedup。onopen 立即 sendResume 与 listener
     * fire sendResume 同 socket 可能 race（onopen 触发后 listener 也观察到 loaded 翻转）→
     * 两次 _handle_subscribe → server cancel-race。reconnect 时随新 socket 重置为 false。
     */
    let resumeSent = false;

    /**
     * SPEC audit-c E1 BLOCKER defer-RESUME：发 sendResume(since=lastSeqSeen)。
     * server 经 ``_handle_resume`` 从 tape 重放 seq>since 后再 subscribe，补全
     * ``(fetch_commit, sendResume_send]`` 窗口事件（subscribe forward-only 不管用）。
     *
     * per-socket resumeSent dedup（MAJOR-2）：已发则 no-op，防 onopen+listener 双 fire。
     *
     * @returns true 若真发出帧（caller 可据此决定是否 arm watchdog）；false 若被 dedup/sock 不可用跳过。
     */
    const trySendResume = (sock: WebSocket | null): boolean => {
      if (!sock || sock.readyState !== READY_OPEN || resumeSent) return false;
      const since = useWorkflowStore.getState().lastSeqSeen;
      const msg: WsClientMessage = { type: "resume", run_id: runId, since };
      sock.send(JSON.stringify(msg));
      resumeSent = true;
      return true;
    };

    /**
     * SPEC audit-c BLOCKER-2：listener 在 useEffect 顶层注册一次（**非 onopen 内**），
     * 防 reconnect 累积。zustand 默认全 state listener（A1：不引 subscribeWithSelector
     * middleware），listener 内部 diff ``prev.loadStatus !== curr.loadStatus`` 判翻转。
     * one-shot 自清（A4）：首次 loaded 后 sendResume + ``unsub()``。
     *
     * **M3 韧性**：listener fire sendResume 后 arm resume watchdog——若 server 在 loading
     * 期重启（in-memory handle 丢失，_handle_resume short-circuit），client 收不到任何
     * 事件 → 3s 后 watchdog 触发 triggerResumeFallback 全量重拉（兜底，不依赖 server 正常应答）。
     */
    const unsubListener = useWorkflowStore.subscribe((curr, prev) => {
      if (
        prev &&
        prev.loadStatus !== curr.loadStatus &&
        curr.loadStatus === "loaded"
      ) {
        trySendResume(socket);
        armResumeWatchdog();
        unsubListener();
      }
    });

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

    /** SPEC in-session-permission-hook §4.3 P2：connect / 重连后请求权威 approval snapshot。 */
    const sendRequestApprovalSnapshot = (sock: WebSocket) => {
      if (sock.readyState === READY_OPEN) {
        const msg: WsClientMessage = { type: "request_approval_snapshot" };
        sock.send(JSON.stringify(msg));
      }
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
        // SPEC audit-c §3 INV-7 契约：onopen 读 loadStatus 优先于 wasReconnect（F1）。
        const st = useWorkflowStore.getState().loadStatus;
        if (st === "loaded" || st === "error") {
          // BLOCKER-1：loaded/error → 立即 sendResume（两态均安全，详见模块头注释）。
          // MAJOR-2 per-socket resumeSent dedup：与 listener fire 共用此标志。
          // M3 韧性：只要 sendResume 真发出（trySendResume 内 dedup 通过），就 arm watchdog——
          // 若 server 静默丢 resume（in-memory handle 丢失），3s 后 watchdog 触发 fallback。
          const sent = trySendResume(socket);
          if (sent) {
            armResumeWatchdog();
          }
          // MAJOR-3 reverse A5：reconnect 路径保留 sendSubscribe 作 server-restart fallback
          // （_handle_resume handle=None short-circuit → _handle_subscribe lazy-mount from discovery）。
          // 顺序：sendResume 首帧（重放历史 + handle 存在时 server 内部 subscribe）+
          // sendSubscribe 次帧（server-restart 后无 handle 时 lazy-mount）。
          if (wasReconnect) {
            sendSubscribe(socket!);
          }
          // SPEC in-session-permission-hook §4.3 P2/B-5：connect / 重连后请求权威 approval
          // snapshot，清掉 broker 重启后 stale 本地卡（N13）。无论 initial / reconnect 都发：
          // subscribe 时 server 也会自动推 snapshot，但显式 request 兜底 lazy-mount 失败场景。
          sendRequestApprovalSnapshot(socket!);
        } else if (wasReconnect) {
          // F1：loading/idle + reconnect → 不双发（loadStatus 优先），仍走 listener 路径
          // （loading 期 server 一般未重启，lazy-mount 不需要；listener 后续 fire 仅 sendResume）。
          // 注：不 armResumeWatchdog——listener fire 后才进入 resume 流程，watchdog 由 listener
          // 路径隐式覆盖（loaded 翻转 → sendResume → server 重放 → onmessage 清 watchdog）。
        }
        // initial mount + loading/idle：onopen 不发任何帧，等 listener 回调（BLOCKER-2）。
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
        // SPEC in-session-permission-hook §4.3：approval 帧（``kind === "approval"``）是
        // broker 直推的非 tape 事件（权限决策 ≠ workflow 事件）。单独路由到 approval store，
        // **不**进 processEvent（避免污染 workflow reducer / 触发 watchdog 误清逻辑）。
        if (parsed.kind === "approval") {
          ingestApprovalFrame(parsed);
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

      // SPEC audit-c MAJOR-2：reconnect 随新 socket 重置 per-socket resumeSent 标志
      resumeSent = false;
    };

    open();

    return () => {
      closedByUs = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      clearResumeWatchdog();
      // SPEC audit-c BLOCKER-2：cleanup 时也 unsub listener（幂等——zustand unsub 多次安全）
      unsubListener();
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, processEvent, ingestApprovalFrame]);
}
