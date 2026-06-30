// hooks/use-runs-list.ts —— 元数据列表轮询（SPEC §4.2，铁律 1）。
//
// 只 fetch `/api/runs`（**元数据，无事件**，懒加载红线），定时轮询反映 status 实时变化。
// 事件**不**在此拉 —— 点开 run 才经 useRunEvents 拉（铁律 1）。
//
// 返回 { metas, loading, error, refresh }，调用方按需渲染。组件卸载清 interval（无 leak）。

import { useCallback, useEffect, useRef, useState } from "react";
import type { RunMeta } from "@/types/events";

const POLL_INTERVAL_MS = 2000; // SPEC §4.2 示例值

export interface UseRunsListResult {
  metas: RunMeta[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
}

export function useRunsList(pollMs: number = POLL_INTERVAL_MS): UseRunsListResult {
  const [metas, setMetas] = useState<RunMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // refresh 用 useCallback 稳定引用（避免 stale closure + 满足 effect deps）。
  // 依赖只含 setter（React 保证稳定），所以引用在组件生命周期内恒定。
  const refresh = useCallback(async () => {
    try {
      const resp = await fetch("/api/runs");
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as RunMeta[];
      setMetas(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  // 用 ref 持有 refresh 让 interval 总调最新（防御性，配合上面的稳定引用双保险）
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    // 首次立即拉一次
    void refreshRef.current();
    const interval = setInterval(() => {
      void refreshRef.current();
    }, pollMs);
    return () => clearInterval(interval); // 卸载清 interval（无 leak）
  }, [pollMs]);

  return { metas, loading, error, refresh };
}
