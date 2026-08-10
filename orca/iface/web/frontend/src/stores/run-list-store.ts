// stores/run-list-store.ts —— 多 run 元数据列表 store（SPEC §13 §6.2 / R3 /
// docs/specs/web-runlist-redesign.md §3.1）。
//
// **铁律 R3**：本 store **绝不** import 或写入 workflow 的状态 store（详情页那套）。
// 只持元数据数组（RunSummary[]），无 reducer / fold / 状态机——是目录列表，不是事件 fold。
//
// 生命周期（mockup + §6.2 + redesign §3.1）：
//   - mount → ``refresh()`` + ~8s 轮询（SPEC 2026-08-10 §3.5，client 节流 2s，§13 I-16）
//   - unmount → ``runs = []`` 清空 + 停轮询（reviewer I-14：无残留）
//   - WS ``run_changed``（控制帧，``kind==="control"``）→ action=deleted 乐观移除 / else refresh
//   - deleteRun：乐观移除 + DELETE + 失败回滚
//   - deleteRuns（批量，§3.1）：乐观全移 + 并行 DELETE + 任一失败 refresh 对账
//
// 关键不变量（redesign §3.1/§3.3）：
//   - **inflightSeq**（D1 M2）：模块级递增 seq，refresh 入口 ++seq，响应回来若 seq 过期则丢弃
//     （防 stale 覆盖 fresh）。与 ``lastFetch`` 节流**正交叠加**：节流 gate 入口、seq gate 出口。
//   - **pendingDeletes**（D1 M4，防幽灵 run）：删除期间 WS refresh 不会复活 run——refresh 成功后
//     用它过滤（``runs.filter(r => !pendingDeletes.has(r.run_id))``）。
//   - **NM4 deferred cleanup**：deleteRun/deleteRuns 成功 id **不立即**从 pendingDeletes 移除——
//     保留到下一次 refresh 确认后端已无该 run 再移除（防「成功删除→refresh 拿到尚未落盘的后端
//     数据→幽灵复活」）。失败 id 回滚时立即移除。WS ``action=deleted``（nm2）——服务端已确认，
//     同步移除（无需再防幽灵）。
//   - **epoch guard**（NM3）：``reset()`` 自增 epoch；deleteRun/deleteRuns 写回前校验 epoch 未变，
//     防 reset 后到达的 stale ``before`` 写回 store。

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

// SPEC §13.3 P3：注册表里 path 失效的项目（只读折叠区显示）。
export interface StaleProject {
  project_id: string;
  path: string;
  name: string;
  first_seen?: number;
  last_seen?: number;
}

// SPEC §3.1：批量删除返回结构（部分失败对账用）。
export interface DeleteRunsResult {
  deleted: string[];
  failed: { id: string; reason: string }[];
}

interface RunListState {
  runs: RunSummary[];
  staleProjects: StaleProject[];
  loading: boolean;
  error: string | null;
  lastFetch: number;

  refresh: () => Promise<void>;
  deleteRun: (runId: string) => Promise<void>;
  deleteRuns: (ids: string[]) => Promise<DeleteRunsResult>;
  onRunChanged: (frame: { run_id: string; action: string }) => void;
  reset: () => void;
}

const REFRESH_THROTTLE_MS = 2000;
// SPEC 2026-08-10-home-list-lazy-index §3.5：4s→8s（保守拉长，不依赖 WS 重连可靠性前置确认；
// WS run_changed 仍是主要增量源，8s 作断连兜底）。后端索引化后单次 refresh <300ms，轮询本身已轻。
const POLL_INTERVAL_MS = 8000;

// 单例：mount/unmount 多次复用同一 store。轮询在组件 effect 里启停（避免 orphan task）。
let pollTimer: ReturnType<typeof setInterval> | null = null;

// ── inflight guard（D1 M2）：模块级递增 seq ─────────────────────────────────
// refresh 入口 ``++inflightSeq``；响应回来若 ``seq !== inflightSeq`` → 丢弃（防 stale 覆盖 fresh）。
// 模块级（非 store state）因为它只是 refresh 内部竞态守卫，不参与渲染。
let inflightSeq = 0;

// ── epoch guard（NM3）：reset 自增，deleteRun/deleteRuns 写回前校验 ──────────
// 防 reset 后到达的 stale DELETE 响应把旧 ``before`` 写回已清空的 store。
let epoch = 0;

// ── pendingDeletes（D1 M4 + NM4）：防幽灵 run ────────────────────────────────
// deleteRun/deleteRuns 乐观移除时把 id 入此 Set；refresh 成功后用它过滤（防 WS refresh 复活）。
// 成功 id **不立即**移除（NM4）——保留到下一次 refresh 确认后端已无该 run。
// 失败 id 回滚时立即移除；WS action=deleted（服务端已确认）同步移除。
// 模块级（非 store state）因为它跨多个 action 共享，且只对 refresh 有意义。
const pendingDeletes = new Set<string>();

export const useRunListStore = create<RunListState>((set, get) => ({
  runs: [],
  staleProjects: [],
  loading: false,
  error: null,
  lastFetch: 0,

  refresh: async () => {
    // 节流（§13 I-16）：距上次 fetch < 2s → 跳过（防多 tab 风暴）。
    // 注：节流 gate 入口、inflightSeq gate 出口，二者正交不冲突。
    const now = Date.now();
    if (now - get().lastFetch < REFRESH_THROTTLE_MS) {
      return;
    }
    // inflightSeq gate 入口：本次 refresh 拿一个递增序号。
    const seq = ++inflightSeq;
    set({ loading: true, error: null });
    try {
      // 并发拉 runs 与 stale projects（stale 失败不阻断列表，SPEC §13.3 P3 fail-soft）。
      const [runsR, staleR] = await Promise.allSettled([
        fetch("/api/runs?scope=all"),
        fetch("/api/projects/stale"),
      ]);
      if (runsR.status !== "fulfilled" || !runsR.value.ok) {
        throw new Error(
          runsR.status === "fulfilled"
            ? `HTTP ${runsR.value.status}`
            : "runs fetch rejected",
        );
      }
      const data = (await runsR.value.json()) as RunSummary[];
      const stale =
        staleR.status === "fulfilled" && staleR.value.ok
          ? ((await staleR.value.json()) as StaleProject[])
          : [];
      // inflightSeq gate 出口：若期间有更新 refresh 发起，本次响应丢弃（防 stale 覆盖 fresh）。
      if (seq !== inflightSeq) return;
      // NM4：refresh 确认后端已无该 run → 从 pendingDeletes 移除。
      // 服务端 data 是真相源——id 不在 data 内说明后端已无该 run，不再需要过滤防复活。
      const liveIds = new Set(data.map((r) => r.run_id));
      for (const id of [...pendingDeletes]) {
        if (!liveIds.has(id)) pendingDeletes.delete(id);
      }
      set({
        // pendingDeletes 守卫：删除期间不让 WS refresh 复活 run（D1 M4）。
        runs: data.filter((r) => !pendingDeletes.has(r.run_id)),
        staleProjects: stale,
        loading: false,
        lastFetch: Date.now(),
      });
    } catch (e) {
      // inflightSeq gate 出口（错误路径也要守）：过期响应的错误不覆盖更新鲜的状态。
      if (seq !== inflightSeq) return;
      set({
        loading: false,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  },

  deleteRun: async (runId: string) => {
    // 乐观移除 + pendingDeletes 入队（防 refresh 复活）。
    const myEpoch = epoch;
    const before = get().runs;
    pendingDeletes.add(runId);
    set({ runs: before.filter((r) => r.run_id !== runId) });
    try {
      const r = await fetch(`/api/runs/${runId}`, { method: "DELETE" });
      if (r.status !== 200 && r.status !== 404) {
        const body = await r.json().catch(() => ({}));
        const err = new Error(
          `删除失败 HTTP ${r.status}：${body?.error ?? body?.detail ?? ""}`,
        );
        // NM3 epoch guard：若期间 reset 已发生，不写回 stale ``before``、不动 pendingDeletes，
        // 但仍抛错（fail-loud：UI 已 unmount 不再 await，但 store 行为显式失败）。
        if (myEpoch === epoch) {
          pendingDeletes.delete(runId);
          set({ runs: before });
        }
        throw err;
      }
      // 成功或 404（已删）→ **不**立即从 pendingDeletes 移除（NM4）。
      // 保留到下次 refresh 确认后端已无该 run 再移除，防「DELETE 200 → refresh 拿到尚未
      // 落盘的数据 → 幽灵复活」。WS action=deleted 也会同步移除（服务端已确认）。
    } catch (e) {
      // 网络异常 → 回滚 + 移除 pendingDeletes + rethrow
      // NM3 epoch guard：reset 后到达的 stale 回滚跳过 state mutation，但仍 rethrow（fail-loud）。
      if (myEpoch === epoch) {
        pendingDeletes.delete(runId);
        set({ runs: before });
      }
      throw e;
    }
  },

  deleteRuns: async (ids: string[]) => {
    // SPEC §3.1：批量删除——乐观全移 → 逐个 DELETE（Promise.allSettled）→ 任一失败 refresh 对账。
    if (ids.length === 0) return { deleted: [], failed: [] };

    const myEpoch = epoch;
    const before = get().runs;
    const byId = new Map(before.map((r) => [r.run_id, r])); // 失败回滚用
    // 乐观：全部 ids 入 pendingDeletes + 从 runs 移除。
    for (const id of ids) pendingDeletes.add(id);
    set({ runs: before.filter((r) => !ids.includes(r.run_id)) });

    const results = await Promise.allSettled(
      ids.map(async (id) => {
        const r = await fetch(`/api/runs/${id}`, { method: "DELETE" });
        if (r.status !== 200 && r.status !== 404) {
          const body = await r.json().catch(() => ({}));
          throw new Error(
            `HTTP ${r.status}：${body?.error ?? body?.detail ?? ""}`,
          );
        }
        return id;
      }),
    );

    // NM3 epoch guard：reset 后到达的整批 stale 结果丢弃（不写回 before，不部分回滚）。
    // 仍按 fulfilled/failed 分桶返回（UI 已等待结果）。
    if (myEpoch !== epoch) {
      const deleted: string[] = [];
      const failed: { id: string; reason: string }[] = [];
      results.forEach((res, i) => {
        const id = ids[i];
        if (res.status === "fulfilled") deleted.push(id);
        else
          failed.push({
            id,
            reason:
              res.reason instanceof Error
                ? res.reason.message
                : String(res.reason),
          });
      });
      return { deleted, failed };
    }

    const deleted: string[] = [];
    const failed: { id: string; reason: string }[] = [];
    results.forEach((res, i) => {
      const id = ids[i];
      if (res.status === "fulfilled") {
        // NM4：成功 id **不**立即从 pendingDeletes 移除——留到下次 refresh 确认。
        deleted.push(id);
      } else {
        // 失败：从 pendingDeletes 移除（让后续 refresh 能拉回该 run）。
        pendingDeletes.delete(id);
        const reason =
          res.reason instanceof Error ? res.reason.message : String(res.reason);
        failed.push({ id, reason });
        // 恢复该 run 到 runs（若原本存在）：原顺序追加，不重排——
        // 排序是组件侧职责（useListSort + sortRuns），store 不持 sort state。
        const orig = byId.get(id);
        if (orig) {
          set({ runs: [...get().runs, orig] });
        }
      }
    });

    // 任一失败 → 与后端对账（pendingDeletes 守卫过滤掉仍 inflight 的删除）。
    if (failed.length > 0) {
      // 强制 refresh（绕过节流）：清 lastFetch 再调。
      set({ lastFetch: 0 });
      await get().refresh();
    }

    return { deleted, failed };
  },

  onRunChanged: (frame) => {
    // §13 §6.2 + M-8：控制帧 ``run_changed`` → action=deleted 乐观移除 / else refresh。
    if (frame.action === "deleted") {
      // nm2：服务端已确认删除 → 同步从 pendingDeletes 移除（无需再防幽灵）。
      pendingDeletes.delete(frame.run_id);
      set({ runs: get().runs.filter((r) => r.run_id !== frame.run_id) });
    } else {
      // changed/attached → 异步 refresh（不阻塞控制帧处理）。
      void get().refresh();
    }
  },

  reset: () => {
    // unmount 调用（reviewer I-14：清 runs=[]）。
    // SPEC §3.1：reset 清理 pendingDeletes（防下次 mount 残留）。
    // NM3：++epoch 让所有 inflight deleteRun/deleteRuns 的写回作废（stale ``before`` 不复活）。
    stopPolling();
    pendingDeletes.clear();
    inflightSeq = 0;
    epoch += 1;
    set({
      runs: [],
      staleProjects: [],
      loading: false,
      error: null,
      lastFetch: 0,
    });
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
