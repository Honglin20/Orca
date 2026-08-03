// components/runlist/format-helpers.ts —— 列表页格式化 DRY（SPEC §6 隐含）。
//
// 集中 elapsed/cost/ago 的格式化逻辑，供 RunRow/ProjectGroup/SortMenu 共用。

export function fmtElapsed(sec: number | undefined | null): string {
  if (!sec || sec < 0) return "—";
  const s = Math.floor(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  if (m < 60) return rem ? `${m}m${rem}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h < 24) return mm ? `${h}h${mm}m` : `${h}h`;
  const d = Math.floor(h / 24);
  const hh = h % 24;
  return hh ? `${d}d${hh}h` : `${d}d`;
}

export function fmtCost(c: number | undefined | null): string {
  if (!c) return "$0.00";
  return `$${c.toFixed(2)}`;
}

export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return "—";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

/** 高亮匹配子串（SPEC §6.2）：仅在 q 非空时调用，否则零开销。 */
export function highlightMatch(
  text: string,
  qLower: string,
): React.ReactNode {
  if (!qLower) return text;
  const lower = text.toLowerCase();
  const idx = lower.indexOf(qLower);
  if (idx < 0) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded bg-[rgb(var(--accent)/0.3)] px-0.5">
        {text.slice(idx, idx + qLower.length)}
      </mark>
      {text.slice(idx + qLower.length)}
    </>
  );
}
