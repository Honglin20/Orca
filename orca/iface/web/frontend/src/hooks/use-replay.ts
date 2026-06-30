// hooks/use-replay.ts —— replay 定时器推进 + 速度控制（SPEC §2.4）。
//
// 播放：按真实时间推进 setReplayTarget。speed=1× 表示按事件真实时间戳间隔推进（墙钟 1s =
// replay 1s）；speed=5×/10×/20× 加速。到达末尾自动暂停。
//
// 清理（无 leak）：unmount/暂停/切 run → 清 timer。

import { useCallback, useEffect, useRef, useState } from "react";
import { useWorkflowStore } from "@/stores/workflow-store";

export type ReplaySpeed = 1 | 5 | 10 | 20;

export const REPLAY_SPEEDS: ReplaySpeed[] = [1, 5, 10, 20];

/** 每「墙钟毫秒」推进多少 replay 毫秒。tick 间隔固定，按 speed 放大推进量。 */
const TICK_MS = 100;

export interface ReplayController {
  playing: boolean;
  speed: ReplaySpeed;
  /** 当前位置（-1 = 未开始；events.length-1 = 末尾）。 */
  position: number;
  /** 事件总数（-1 表示无 events）。 */
  total: number;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  setSpeed: (s: ReplaySpeed) => void;
  /** 跳到指定位置（拖滑块）。 */
  seek: (pos: number) => void;
}

export function useReplay(): ReplayController {
  const events = useWorkflowStore((s) => s.events);
  const replayMode = useWorkflowStore((s) => s.replayMode);
  const replayPosition = useWorkflowStore((s) => s.replayPosition);
  const setReplayTarget = useWorkflowStore((s) => s.setReplayTarget);
  const setReplayPosition = useWorkflowStore((s) => s.setReplayPosition);

  const [playing, setPlaying] = useState(false);
  const [speed, setSpeedState] = useState<ReplaySpeed>(1);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const total = events.length;
  const position = replayMode ? replayPosition : total - 1;

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const play = useCallback(() => {
    if (total === 0) return;
    // 已到末尾 → 从头开始
    if (position >= total - 1) {
      setReplayTarget(0);
    }
    setPlaying(true);
  }, [total, position, setReplayTarget]);

  const pause = useCallback(() => {
    setPlaying(false);
  }, []);

  const toggle = useCallback(() => {
    setPlaying((p) => !p);
  }, []);

  const seek = useCallback(
    (pos: number) => {
      const clamped = Math.max(-1, Math.min(pos, total - 1));
      // 增量定位（前进 apply / 后退 checkpoint，SPEC §2.3）
      if (replayMode) {
        setReplayTarget(clamped);
      } else {
        setReplayPosition(clamped);
      }
    },
    [total, replayMode, setReplayTarget, setReplayPosition]
  );

  const setSpeed = useCallback((s: ReplaySpeed) => {
    setSpeedState(s);
  }, []);

  // 定时器：playing 时按 speed 推进 position（每 TICK_MS 一次，推进 (TICK_MS * speed)ms 的
  // 事件时间）。这里简化为「每 tick 推进一个事件」（speed 控制频率的反向）—— 更精确的按
  // 时间戳推进需对比 event.timestamp，但 9c 用「事件步进」足够（DAG 状态变化以事件为粒度）。
  useEffect(() => {
    if (!playing || !replayMode || total === 0) {
      clearTimer();
      return;
    }
    // tick 间隔 = TICK_MS / speed（speed 越大间隔越短）
    const interval = Math.max(16, TICK_MS / speed);
    timerRef.current = setInterval(() => {
      const cur = useWorkflowStore.getState().replayPosition;
      if (cur >= total - 1) {
        // 到末尾 → 自动暂停
        setPlaying(false);
        clearTimer();
        return;
      }
      setReplayTarget(cur + 1);
    }, interval);

    return clearTimer;
  }, [playing, replayMode, speed, total, setReplayTarget, clearTimer]);

  // unmount / 退出 replay → 清 timer（无 leak）
  useEffect(() => clearTimer, [clearTimer]);

  return {
    playing,
    speed,
    position,
    total,
    play,
    pause,
    toggle,
    setSpeed,
    seek,
  };
}
