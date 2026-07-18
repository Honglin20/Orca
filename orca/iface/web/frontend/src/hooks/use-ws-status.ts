// hooks/use-ws-status.ts —— 订阅 WS 连接状态（P3 D1=A，transport-only）。
//
// TopBar 用此 hook 显示连接指示点。详见 ws-connection-store.ts 的 SPEC sanctioned exception。

import { useWsConnectionStore } from "./ws-connection-store";
import type { WsConnStatus } from "./ws-connection-store";

export type { WsConnStatus };

export function useWsStatus(): WsConnStatus {
  return useWsConnectionStore((s) => s.status);
}
