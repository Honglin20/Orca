// test/ws-connection-store.test.ts —— WS 连接状态机（transport-only，P3 D1=A）。
//
// 验证四态迁移（code-reviewer R1）：disconnected / connecting（首次）/ connected /
// reconnecting（断后重连）。store 是 module-level zustand，独立于 workflow-store（铁律 1
// sanctioned exception），测试直接驱动 action 无需走 useWebSocket。

import { describe, expect, test, beforeEach } from "vitest";
import { useWsConnectionStore } from "@/hooks/ws-connection-store";

describe("ws-connection-store —— transport-only 连接状态机", () => {
  beforeEach(() => {
    useWsConnectionStore.setState({ status: "disconnected" });
  });

  test("初始 disconnected", () => {
    expect(useWsConnectionStore.getState().status).toBe("disconnected");
  });

  test("首次连接：disconnected → connecting → connected", () => {
    const s = useWsConnectionStore.getState();
    s.setConnecting();
    expect(useWsConnectionStore.getState().status).toBe("connecting");
    s.setConnected();
    expect(useWsConnectionStore.getState().status).toBe("connected");
  });

  test("断线重连：connected → reconnecting → connected（重连不回 connecting）", () => {
    const s = useWsConnectionStore.getState();
    s.setConnected();
    s.setReconnecting();
    expect(useWsConnectionStore.getState().status).toBe("reconnecting");
    s.setConnected();
    expect(useWsConnectionStore.getState().status).toBe("connected");
  });

  test("卸载：任意态 → disconnected", () => {
    const s = useWsConnectionStore.getState();
    s.setConnected();
    s.setDisconnected();
    expect(useWsConnectionStore.getState().status).toBe("disconnected");
  });
});
