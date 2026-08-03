// hooks/use-ws-runlist.ts —— 列表页 WS 控制帧 + 指数退避重连（SPEC §5.8/§3.2）。
//
// 契约：
//   - 连 ``wss?:<host>/ws``，仅处理控制帧 ``{kind:"control",type:"run_changed",run_id,action}``。
//   - 非 JSON 帧 ``console.warn`` 留诊断（D1 m1，**不**静默吞）。
//   - ``onclose``（非主动）/``onerror`` → 指数退避重连 1/2/4/8/16s 封顶 30s（SPEC §5.8）。
//   - 重连 >3 次仍失败 → 升级提示 + 暴露手动 ``reconnect()``（按钮调用）。
//   - unmount → 主动 close（标记 ``intentionalClose``，不触发重连）+ 清退避定时器。
//
// 暴露 state：``{ connected, reconnects, giveUp, reconnect }``。
//   - ``connected``：当前 WS 是否开着。
//   - ``reconnects``：累计重连次数（成功连接后清零）。
//   - ``giveUp``：>3 次失败标记，UI 据此显「手动重试连接」按钮。
//   - ``reconnect()``：手动重连（重置 giveUp + 立即连）。

import { useCallback, useEffect, useRef, useState } from "react";

export interface WsRunlistState {
  connected: boolean;
  reconnects: number;
  giveUp: boolean;
  reconnect: () => void;
}

const BACKOFF_STEPS = [1000, 2000, 4000, 8000, 16000]; // 封顶 30s（最后一档后稳定 30s）
const CAP_MS = 30000;
const GIVE_UP_THRESHOLD = 3;

export function useWsRunlist(
  url: string,
  onRunChanged: (frame: { run_id: string; action: string }) => void,
  onConnected?: () => void,
): WsRunlistState {
  const [connected, setConnected] = useState(false);
  const [reconnects, setReconnects] = useState(0);
  const [giveUp, setGiveUp] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const intentionalCloseRef = useRef(false);
  const backoffTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectsRef = useRef(0);
  // 把最新的 onRunChanged 存 ref，避免重连时 effect 依赖抖动。
  const cbRef = useRef(onRunChanged);
  cbRef.current = onRunChanged;
  // 把最新的 onConnected 存 ref（重连成功后回调，SPEC §5.8「重连成功淡出 + refresh」）。
  const onConnectedRef = useRef(onConnected);
  onConnectedRef.current = onConnected;
  // 首次 onopen 不触发 onConnected（mount effect 已 refresh）；仅重连时触发（review MINOR / §5.8）。
  const didInitialConnectRef = useRef(false);

  const clearBackoff = () => {
    if (backoffTimerRef.current !== null) {
      clearTimeout(backoffTimerRef.current);
      backoffTimerRef.current = null;
    }
  };

  const connect = useCallback(() => {
    if (typeof window === "undefined") return;
    clearBackoff();
    intentionalCloseRef.current = false;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      console.error("[orca/ws-runlist] WebSocket 构造失败", e);
      scheduleReconnect();
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      reconnectsRef.current = 0;
      setReconnects(0);
      setGiveUp(false);
      // 仅重连（非首次连接）触发 onConnected：调用方清 lastFetch 后 refresh（SPEC §5.8）。
      if (didInitialConnectRef.current) {
        onConnectedRef.current?.();
      } else {
        didInitialConnectRef.current = true;
      }
    };

    ws.onmessage = (ev) => {
      // 仅处理控制帧 run_changed；其它（含非 JSON）→ console.warn 留诊断（不静默）。
      try {
        const msg = JSON.parse(ev.data);
        if (msg?.kind === "control" && msg?.type === "run_changed") {
          cbRef.current({ run_id: msg.run_id, action: msg.action });
        }
      } catch (e) {
        console.warn("[orca/ws-runlist] 非 JSON 帧或解析失败", e, ev.data);
      }
    };

    ws.onerror = (e) => {
      // 不在这重连——交给 onclose 兜底（浏览器 onerror 后必跟 onclose）。
      console.warn("[orca/ws-runlist] WS error", e);
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      if (!intentionalCloseRef.current) {
        scheduleReconnect();
      }
    };
  }, [url]);

  const scheduleReconnect = useCallback(() => {
    clearBackoff();
    const n = ++reconnectsRef.current;
    setReconnects(n);
    const delay = n > BACKOFF_STEPS.length ? CAP_MS : BACKOFF_STEPS[n - 1] ?? CAP_MS;
    if (n > GIVE_UP_THRESHOLD) {
      // 升级提示：仍排程重连，但 UI 显手动重试按钮（用户可立即恢复）。
      setGiveUp(true);
    }
    backoffTimerRef.current = setTimeout(() => {
      // 重新读取当前 wsRef/intentional 状态（闭包不 capture 最新）。
      if (!intentionalCloseRef.current) connect();
    }, delay);
  }, [connect]);

  const reconnect = useCallback(() => {
    // 手动重连：重置计数 + giveUp + 立即连。
    reconnectsRef.current = 0;
    setReconnects(0);
    setGiveUp(false);
    // 先关旧的（标记 intentional，避免触发 scheduleReconnect）。
    if (wsRef.current) {
      intentionalCloseRef.current = true;
      try {
        wsRef.current.close();
      } catch {
        // ignore
      }
      wsRef.current = null;
    }
    connect();
  }, [connect]);

  // mount：初次连接；unmount：主动关 + 清定时器。
  useEffect(() => {
    connect();
    return () => {
      intentionalCloseRef.current = true;
      clearBackoff();
      if (wsRef.current) {
        try {
          wsRef.current.close();
        } catch {
          // ignore
        }
        wsRef.current = null;
      }
    };
  }, [connect]);

  return { connected, reconnects, giveUp, reconnect };
}
