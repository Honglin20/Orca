// components/layout/ReplayBar.tsx —— replay 时间轴 + 播放/速度（SPEC §2.4）。
//
// 拖滑块 → seek（增量 apply）；播放/暂停 → useReplay 定时器；速度下拉 1×/5×/10×/20×。
// 仅在 replayMode 时渲染（RunDetailPage 控制）。

import { REPLAY_SPEEDS, useReplay, type ReplaySpeed } from "@/hooks/use-replay";

export function ReplayBar() {
  const r = useReplay();
  const max = Math.max(0, r.total - 1);
  const value = r.position < 0 ? 0 : r.position;

  return (
    <div
      className="flex items-center gap-3 border-t border-slate-200 bg-slate-50 px-4 py-2"
      data-testid="replay-bar"
    >
      <button
        type="button"
        onClick={r.toggle}
        className="rounded border border-slate-300 bg-white px-3 py-1 text-sm hover:bg-slate-100"
        data-testid="replay-play-btn"
        aria-label={r.playing ? "暂停" : "播放"}
      >
        {r.playing ? "⏸" : "▶"}
      </button>

      <input
        type="range"
        min={0}
        max={max}
        value={value}
        onChange={(e) => r.seek(Number(e.target.value))}
        className="flex-1"
        data-testid="replay-slider"
        aria-label="replay 时间轴"
      />

      <span className="font-mono text-xs text-slate-600" data-testid="replay-position">
        Event {value}/{max}
      </span>

      <select
        value={r.speed}
        onChange={(e) => r.setSpeed(Number(e.target.value) as ReplaySpeed)}
        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
        data-testid="replay-speed"
        aria-label="播放速度"
      >
        {REPLAY_SPEEDS.map((s) => (
          <option key={s} value={s}>
            {s}×
          </option>
        ))}
      </select>
    </div>
  );
}
