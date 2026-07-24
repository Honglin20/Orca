// hooks/ws-connection-store.ts —— WS 连接状态（transport-only，P3 D1=A）。
//
// **SPEC sanctioned exception**（web-shell-v2-spec.md §1.1）：WebSocket 连接状态是
// transport-only（前端网络层派生），**非 tape 真相**。它独立于 workflow-store（铁律 1
// 单一真相源 = tape 事件 fold），不参与 reducer / 不进 tape / 不影响幂等。
//
// use-websocket 在 onopen/onclose/onerror 回调里写本 store；TopBar ``useWsStatus()``
// 订阅它显示连接指示点（绿/琥珀/红）。纯前端，后端 ws_handler.py 零改。

import { create } from "zustand";

// 四态（code-reviewer Y4）：disconnected（未连/已卸载）/ connecting（首次连接中）/
// connected（已连）/ reconnecting（断后重连中）。区分首次与重连，避免首帧紫点「重连」误读。
export type WsConnStatus =
  | "disconnected"
  | "connecting"
  | "connected"
  | "reconnecting";

interface WsConnState {
  status: WsConnStatus;
  setConnected: () => void;
  setConnecting: () => void;
  setReconnecting: () => void;
  setDisconnected: () => void;
}

export const useWsConnectionStore = create<WsConnState>((set) => ({
  status: "disconnected",
  setConnected: () => set({ status: "connected" }),
  setConnecting: () => set({ status: "connecting" }),
  setReconnecting: () => set({ status: "reconnecting" }),
  setDisconnected: () => set({ status: "disconnected" }),
}));
