// stores/run-list-store.ts —— 多 run 元数据列表 store（SPEC §13 §6.2 / R3）。
//
// **铁律 R3**：本 store **绝不** import / 写入 workflow-store。只持元数据数组
// （RunSummary[]），无 reducer / fold / 状态机——是目录列表，不是事件 fold。
//
// 生命周期（mockup + §6.2）：
//   - mount → ``refresh()`` + ~4s 轮询（client 节流 2s，§13 I-16）
//   - unmount → ``runs = []`` 清空 + 停轮询（reviewer I-14：无残留）
//   - WS ``run_changed``（控制帧，``kind==="control"``）→ action=deleted 乐观移除 / else refresh
//   - deleteRun：乐观移除 + DELETE + 失败回滚
//
// 单测守门（AC11）：grep `run-list-store` 不 import `workflow-store`。

import { create } from "zustand";

export interface RunSummary {
  run_id: string;
  workflow_name: string;
  project_id?: string | null;
  project_name?: string | null;
  status: string;
  progress?: string;
  cost?: number;
  elapsed?: number;
  started_at?: number | null;
  event_count?: number;
  source?: string;
}

interface RunListState {
  runs: RunSummary[];
  loading: boolean;
  error: string | null;
  lastFetch: number;

  refresh: () => Promise<void>;
  deleteRun: (runId: string) => Promise<void>;
  onRunChanged: (frame: { run_id: string; action: string }) => void;
  reset: () => void;
}

const REFRESH_THROTTLE_MS = 2000;
const POLL_INTERVAL_MS = 4000;

// 单例：mount/unmount 多次复用同一 store。轮询在组件 effect 里启停（避免 orphan task）。
let pollTimer: ReturnType<typeof setInterval> | null = null;

export const useRunListStore = create<RunListState>((set, get) => ({
  runs: [],
  loading: false,
  error: null,
  lastFetch: 0,

  refresh: async () => {
    // 节流（§13 I-16）：距上次 fetch < 2s → 跳过（防多 tab 风暴）。
    const now = Date.now();
    if (now - get().lastFetch < REFRESH_THROTTLE_MS) {
      return;
    }
    set({ loading: true, error: null });
    try {
      const r = await fetch("/api/runs?scope=all");
      if (!r.ok) {
        throw new Error(`HTTP ${r.status}`);
      }
      const data = (await r.json()) as RunSummary[];
      set({
        runs: data,
        loading: false,
        lastFetch: Date.now(),
      });
    } catch (e) {
      set({
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  },

  deleteRun: async (runId: string) => {
    // 乐观移除
    const before = get().runs;
    set({ runs: before.filter((r) => r.run_id !== runId) });
    try {
      const r = await fetch(`/api/runs/${runId}`, { method: "DELETE" });
      if (r.status !== 200 && r.status !== 404) {
        // 失败（409 live / 500）→ 回滚
        set({ runs: before });
        const body = await r.json().catch(() => ({}));
        throw new Error(
          `删除失败 HTTP ${r.status}：${body?.error ?? body?.detail ?? ""}`,
        );
      }
      // 成功或 404（已删）都视为 ok
    } catch (e) {
      // 网络异常 → 回滚 + rethrow
      set({ runs: before });
      throw e;
    }
  },

  onRunChanged: (frame) => {
    // §13 §6.2 + M-8：控制帧 ``run_changed`` → action=deleted 乐观移除 / else refresh。
    if (frame.action === "deleted") {
      set({ runs: get().runs.filter((r) => r.run_id !== frame.run_id) });
    } else {
      // changed/attached → 异步 refresh（不阻塞控制帧处理）。
      void get().refresh();
    }
  },

  reset: () => {
    // unmount 调用（reviewer I-14：清 runs=[]）。
    stopPolling();
    set({ runs: [], loading: false, error: null, lastFetch: 0 });
  },
}));

// ── 轮询管理（mount 启 / unmount 停，防 orphan task） ──────────────────────────

export function startPolling() {
  if (pollTimer !== null) return;
  pollTimer = setInterval(() => {
    void useRunListStore.getState().refresh();
  }, POLL_INTERVAL_MS);
}

export function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
