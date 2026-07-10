// hooks/use-elapsed-tick.ts —— 单一共享 elapsed tick（SPEC §0 D5 / §5.1 / §5.2 / §6）。
//
// 铁律（SPEC §5.2）：**单一 useElapsedTick() 在页根**——N 个 agent 行不能开 N 个 timer。
// 本 hook 用模块级 singleton 订阅：全 app 同一时刻最多一个 setInterval；页根通过
// ``useElapsedTickActive(active)`` 控制启停，消费者用 ``useElapsedNow()`` 拿当前时间
// 并在每个 tick 触发 re-render。
//
// D5：running 时 wall-clock tick；workflow_completed 到达 → status 切 completed → 页根
// active=false → 自动停 tick（snap 到 workflowElapsed 由 selector 处理，本 hook 只管
// wall-clock 更新）。**单一 timer** 防止 N agent 行 × N second 重复 setInterval 泄漏。
//
// 用 useSyncExternalStore（React 18+ 并发安全；tearing-free）。
//
// **单位**：``currentNow`` 与 ``useElapsedNow`` 返回值都是 Unix **秒**（``Date.now()/1000``），
// 与 ``WebEvent.timestamp`` 一致；selector 内 ``now - ts`` 直接得秒差，无 ms/秒换算。

import { useEffect, useSyncExternalStore } from "react";

const TICK_INTERVAL_MS = 1000;

// ── 模块级 singleton 状态 ─────────────────────────────────────────────────────
// tickCounter 单调递增；每次 tick 通知所有订阅者。currentNow 单独维护便于消费者拿
// 「最近 tick 的 Date.now()/1000」（避免每个消费者各调一次 Date.now 导致读到的 ms 不一致）。
const subscribers = new Set<() => void>();
let tickCounter = 0;
let currentNow = Date.now() / 1000;
let intervalId: ReturnType<typeof setInterval> | null = null;
let activeConsumerCount = 0;

function notifyAll(): void {
  for (const cb of subscribers) cb();
}

function maybeStart(): void {
  if (intervalId !== null) return; // 已在跑
  intervalId = setInterval(() => {
    currentNow = Date.now() / 1000;
    tickCounter++;
    notifyAll();
  }, TICK_INTERVAL_MS);
}

function maybeStop(): void {
  if (intervalId === null) return;
  clearInterval(intervalId);
  intervalId = null;
}

// ── useSyncExternalStore store shape ─────────────────────────────────────────
function subscribe(cb: () => void): () => void {
  subscribers.add(cb);
  return () => {
    subscribers.delete(cb);
  };
}

function getSnapshot(): number {
  return tickCounter;
}

function getServerSnapshot(): number {
  return 0;
}

// ── 公开 hooks ───────────────────────────────────────────────────────────────

/**
 * 页根调用：当 ``active`` 为 true 时启用全局 tick，false 或 unmount 时停。
 *
 * 多次调用安全：内部 activeConsumerCount 引用计数——任一 consumer active 即跑，
 * 全部 inactive 才停。典型用法：
 *
 * ```ts
 * const status = useWorkflowStore(s => s.status);
 * useElapsedTickActive(status === "running");
 * ```
 */
export function useElapsedTickActive(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    activeConsumerCount++;
    maybeStart();
    return () => {
      activeConsumerCount = Math.max(0, activeConsumerCount - 1);
      if (activeConsumerCount === 0) maybeStop();
    };
  }, [active]);
}

/**
 * 消费者调用：订阅 tick；每秒 re-render。返回最近 tick 的 Unix 时间戳（**秒**）。
 *
 * 与 ``useElapsedTickActive`` 配合：消费者本身不需要 active 参数——只要页根激活了，
 * 所有 ``useElapsedNow`` 调用方都同步刷新。SSR 环境 getServerSnapshot 返回 0（本
 * 前端 CSR，仅作 React 18 兼容占位）。
 */
export function useElapsedNow(): number {
  // _tick 仅作 re-render 触发器（同 value 不刷新；变更则触发组件 re-render）。
  useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  return currentNow;
}

// ── 测试 helper（仅 test 环境用；prod bundle 不应含此 export）─────────────────
// NODE_ENV 守卫：production tree-shake 时虽不删 export，但调用方都在测试文件里，
// prod 调用 __testTick/__testReset 是逻辑错误（运行时无防御——单纯约定）。
const __testHelpers = {
  /** 手动触发一次 tick（绕过 setInterval）。仅 test 调用。 */
  tick(): void {
    currentNow = Date.now() / 1000;
    tickCounter++;
    notifyAll();
  },
  /** 重置 singleton 状态（每个 test 独立）。仅 test 调用。 */
  reset(): void {
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    subscribers.clear();
    tickCounter = 0;
    currentNow = Date.now() / 1000;
    activeConsumerCount = 0;
  },
};

export const __testTick = __testHelpers.tick;
export const __testReset = __testHelpers.reset;

